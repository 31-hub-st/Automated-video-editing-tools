"""Cooperative job cancellation with hard subprocess termination.

The render queue is deliberately single threaded, but FFmpeg and optional
local TTS CLIs run outside the Python process.  Merely marking a job cancelled
would otherwise leave those children consuming CPU/GPU until they finish.

This module keeps cancellation state scoped to the worker thread and owns the
small process registry needed to terminate the current job's process tree.  It
does not expose process ids through the web/API boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any, TypeVar


class JobCancelledError(RuntimeError):
    """Raised inside the worker when an operator has cancelled the job."""


_T = TypeVar("_T", bound=subprocess.CompletedProcess[Any])
_STDLIB_RUN = subprocess.run
_CURRENT_TOKEN: ContextVar[CancellationToken | None] = ContextVar(
    "storyforge_cancellation_token", default=None
)


class CancellationToken:
    """Thread-safe cancellation flag and registry of owned child processes."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._processes: set[subprocess.Popen[Any]] = set()
        self._reason = "job cancelled by operator"

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelledError(self._reason)

    def register(self, process: subprocess.Popen[Any]) -> None:
        """Register a process, closing the spawn/cancel race safely."""

        with self._lock:
            self._processes.add(process)
            cancelled = self._event.is_set()
        if cancelled:
            terminate_process_tree(process)

    def unregister(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._processes.discard(process)

    def cancel(self, reason: str = "job cancelled by operator") -> None:
        with self._lock:
            if reason.strip():
                self._reason = reason.strip()
            self._event.set()
            processes = tuple(self._processes)
        # Do not hold the registry lock while Windows waits for taskkill.  A
        # process that exits concurrently can still unregister safely.
        for process in processes:
            terminate_process_tree(process)


def current_cancellation_token() -> CancellationToken | None:
    return _CURRENT_TOKEN.get()


def raise_if_cancelled() -> None:
    token = current_cancellation_token()
    if token is not None:
        token.raise_if_cancelled()


@contextmanager
def cancellation_scope(token: CancellationToken) -> Iterator[CancellationToken]:
    marker = _CURRENT_TOKEN.set(token)
    try:
        token.raise_if_cancelled()
        yield token
    finally:
        _CURRENT_TOKEN.reset(marker)


def _wait_briefly(process: subprocess.Popen[Any], seconds: float) -> bool:
    try:
        process.wait(timeout=max(0.01, seconds))
        return True
    except (subprocess.TimeoutExpired, OSError):
        return process.poll() is not None


def terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    """Best-effort hard stop for a process and descendants on Windows/POSIX."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        # FFmpeg and configurable TTS launchers may create helper children.
        # taskkill /T is the only universally available Windows mechanism that
        # also terminates those descendants.  No shell is involved.
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            killer = subprocess.Popen(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                shell=False,
            )
            killer.communicate(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        _wait_briefly(process, 2.0)
        return

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            return
    if _wait_briefly(process, 0.5):
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass
    _wait_briefly(process, 2.0)


def run_cancellable_process(
    command: Sequence[str | os.PathLike[str]],
    *,
    runner: Callable[..., _T] = _STDLIB_RUN,
    token: CancellationToken | None = None,
    **kwargs: Any,
) -> _T:
    """Run a command and hard-stop it when the current job is cancelled.

    Injected test/application runners retain their original behaviour.  The
    production ``subprocess.run`` path is implemented with ``Popen`` so the
    cancellation token can own and terminate the live child process.
    """

    active = token or current_cancellation_token()
    if active is not None:
        active.raise_if_cancelled()

    # Custom runners are intentionally opaque; preserving them keeps unit
    # tests and embedders compatible.  StoryForge production paths use the
    # original stdlib runner and therefore take the cancellable Popen branch.
    if runner is not _STDLIB_RUN:
        completed = runner(list(command), **kwargs)
        if active is not None:
            active.raise_if_cancelled()
        return completed

    input_value = kwargs.pop("input", None)
    capture_output = bool(kwargs.pop("capture_output", False))
    timeout = kwargs.pop("timeout", None)
    check = bool(kwargs.pop("check", False))
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    kwargs.setdefault("shell", False)
    if os.name == "nt":
        flags = int(kwargs.pop("creationflags", 0))
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        kwargs["creationflags"] = flags
    else:
        # A separate group lets cancellation terminate descendants as well as
        # the direct FFmpeg/TTS launcher.
        kwargs["start_new_session"] = True

    process = subprocess.Popen(list(command), **kwargs)
    if active is not None:
        active.register(process)
    try:
        try:
            stdout, stderr = process.communicate(input=input_value, timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_process_tree(process)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                process.args,
                timeout,
                output=stdout,
                stderr=stderr,
            )
        if active is not None:
            active.raise_if_cancelled()
        completed = subprocess.CompletedProcess(
            process.args, process.returncode, stdout, stderr
        )
        if check:
            completed.check_returncode()
        return completed  # type: ignore[return-value]
    finally:
        if active is not None:
            active.unregister(process)


__all__ = [
    "CancellationToken",
    "JobCancelledError",
    "cancellation_scope",
    "current_cancellation_token",
    "raise_if_cancelled",
    "run_cancellable_process",
    "terminate_process_tree",
]
