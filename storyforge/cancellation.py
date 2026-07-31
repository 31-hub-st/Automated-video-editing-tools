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
from pathlib import Path
from typing import Any, TypeVar


class JobCancelledError(RuntimeError):
    """Raised inside the worker when an operator has cancelled the job."""


_T = TypeVar("_T", bound=subprocess.CompletedProcess[Any])
_STDLIB_RUN = subprocess.run
DEFAULT_CAPTURE_LIMIT_BYTES = 1024 * 1024
_STREAM_READ_LIMIT = 64 * 1024
_POST_EXIT_PIPE_DRAIN_SECONDS = 0.5
_CURRENT_TOKEN: ContextVar[CancellationToken | None] = ContextVar(
    "storyforge_cancellation_token", default=None
)


class _TailCapture:
    """Keep only the newest bounded portion of a child-process stream."""

    def __init__(self, limit_bytes: int, *, encoding: str) -> None:
        self.limit_bytes = max(0, int(limit_bytes))
        self.encoding = encoding
        self._lock = threading.Lock()
        self._value = bytearray()

    def append(self, chunk: str | bytes) -> None:
        if self.limit_bytes <= 0:
            return
        raw = (
            bytes(chunk)
            if isinstance(chunk, bytes)
            else str(chunk).encode(self.encoding, errors="replace")
        )
        with self._lock:
            if len(raw) >= self.limit_bytes:
                self._value[:] = raw[-self.limit_bytes :]
                return
            overflow = len(self._value) + len(raw) - self.limit_bytes
            if overflow > 0:
                del self._value[:overflow]
            self._value.extend(raw)

    def value(self, *, text_mode: bool) -> str | bytes:
        with self._lock:
            raw = bytes(self._value)
        if text_mode:
            return raw.decode(self.encoding, errors="replace")
        return raw


def _bounded_completed_output(
    value: str | bytes | None,
    *,
    limit_bytes: int,
    encoding: str,
) -> str | bytes | None:
    if value is None:
        return None
    capture = _TailCapture(limit_bytes, encoding=encoding)
    capture.append(value)
    return capture.value(text_mode=isinstance(value, str))


def _write_completed_output(
    path: str | os.PathLike[str] | None,
    value: str | bytes | None,
    *,
    encoding: str,
) -> None:
    if path is None or value is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = value if isinstance(value, bytes) else value.encode(encoding, errors="replace")
    target.write_bytes(raw)


def _is_ffmpeg_command(command: Sequence[str | os.PathLike[str]]) -> bool:
    if not command:
        return False
    executable = os.path.basename(os.fspath(command[0])).casefold()
    stem, suffix = os.path.splitext(executable)
    if suffix not in {"", ".exe"}:
        return False
    return (
        stem in {"ffmpeg", "ffprobe"}
        or stem.startswith("ffmpeg-")
        or stem.startswith("ffprobe-")
    )


def windows_process_creation_flags(
    command: Sequence[str | os.PathLike[str]],
    base_flags: int = 0,
) -> int:
    """Return bounded Windows priority flags for a StoryForge subprocess."""

    flags = int(base_flags)
    flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    if _is_ffmpeg_command(command):
        flags |= int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
    return flags


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


def _cancel_windows_thread_io(thread: threading.Thread) -> bool:
    """Cancel a pipe read without closing its buffered stream cross-thread."""

    if os.name != "nt" or not thread.is_alive() or thread.native_id is None:
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenThread.argtypes = [
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        kernel32.OpenThread.restype = ctypes.c_void_p
        kernel32.CancelSynchronousIo.argtypes = [ctypes.c_void_p]
        kernel32.CancelSynchronousIo.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        # CancelSynchronousIo requires THREAD_TERMINATE access even though it
        # does not terminate the thread itself.
        handle = kernel32.OpenThread(0x0001, False, int(thread.native_id))
        if not handle:
            return False
        try:
            return bool(kernel32.CancelSynchronousIo(handle))
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return False


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
    stdout_line_callback: Callable[[str], None] | None = None,
    capture_limit_bytes: int = DEFAULT_CAPTURE_LIMIT_BYTES,
    stdout_log_path: str | os.PathLike[str] | None = None,
    stderr_log_path: str | os.PathLike[str] | None = None,
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
    try:
        capture_limit = int(capture_limit_bytes)
    except (TypeError, ValueError) as error:
        raise ValueError("capture_limit_bytes must be an integer") from error
    if capture_limit < 0:
        raise ValueError("capture_limit_bytes must be non-negative")
    encoding = str(kwargs.get("encoding") or "utf-8")

    # Custom runners are intentionally opaque; preserving them keeps unit
    # tests and embedders compatible.  StoryForge production paths use the
    # original stdlib runner and therefore take the cancellable Popen branch.
    if runner is not _STDLIB_RUN:
        completed = runner(list(command), **kwargs)
        _write_completed_output(
            stdout_log_path, completed.stdout, encoding=encoding
        )
        _write_completed_output(
            stderr_log_path, completed.stderr, encoding=encoding
        )
        completed.stdout = _bounded_completed_output(
            completed.stdout,
            limit_bytes=capture_limit,
            encoding=encoding,
        )
        completed.stderr = _bounded_completed_output(
            completed.stderr,
            limit_bytes=capture_limit,
            encoding=encoding,
        )
        if stdout_line_callback is not None:
            output = completed.stdout
            if isinstance(output, bytes):
                output = output.decode(
                    str(kwargs.get("encoding") or "utf-8"), errors="replace"
                )
            if isinstance(output, str):
                for line in output.splitlines():
                    try:
                        stdout_line_callback(line)
                    except Exception:
                        # Progress reporting is best-effort and must never turn
                        # a successful render into a failed one.
                        pass
        if active is not None:
            active.raise_if_cancelled()
        return completed

    input_value = kwargs.pop("input", None)
    capture_output = bool(kwargs.pop("capture_output", False))
    timeout = kwargs.pop("timeout", None)
    check = bool(kwargs.pop("check", False))
    if input_value is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if stdout_line_callback is not None and kwargs.get("stdout") is not subprocess.PIPE:
        raise ValueError("stdout_line_callback requires stdout=PIPE or capture_output=True")
    kwargs.setdefault("shell", False)
    if os.name == "nt":
        kwargs["creationflags"] = windows_process_creation_flags(
            command,
            int(kwargs.pop("creationflags", 0)),
        )
    else:
        # A separate group lets cancellation terminate descendants as well as
        # the direct FFmpeg/TTS launcher.
        kwargs["start_new_session"] = True

    process = subprocess.Popen(list(command), **kwargs)
    if active is not None:
        active.register(process)
    try:
        if process.stdout is not None or process.stderr is not None:
            text_mode = bool(
                kwargs.get("text")
                or kwargs.get("universal_newlines")
                or kwargs.get("encoding") is not None
                or kwargs.get("errors") is not None
            )
            stdout_capture = _TailCapture(capture_limit, encoding=encoding)
            stderr_capture = _TailCapture(capture_limit, encoding=encoding)
            stop_stream_readers = threading.Event()

            stdout_log = None
            stderr_log = None
            try:
                if stdout_log_path is not None:
                    target = Path(stdout_log_path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    stdout_log = target.open("wb", buffering=0)
                if stderr_log_path is not None:
                    target = Path(stderr_log_path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    stderr_log = target.open("wb", buffering=0)
            except BaseException:
                if stdout_log is not None:
                    stdout_log.close()
                if stderr_log is not None:
                    stderr_log.close()
                terminate_process_tree(process)
                raise

            def captured_output(
                capture: _TailCapture,
                *,
                captured: bool,
            ) -> str | bytes | None:
                if not captured:
                    return None
                return capture.value(text_mode=text_mode)

            def close_pipe_streams() -> None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is None:
                        continue
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

            def drain_stream(
                stream: Any,
                capture: _TailCapture,
                *,
                report_lines: bool,
                log_file: Any,
            ) -> None:
                if stream is None:
                    return
                while not stop_stream_readers.is_set():
                    try:
                        # A size bound prevents a malformed child from making
                        # one newline-free diagnostic allocate arbitrarily.
                        chunk = stream.readline(_STREAM_READ_LIMIT)
                    except (OSError, ValueError):
                        return
                    if not chunk:
                        return
                    # The direct child can exit after launching a helper which
                    # inherited its stdout/stderr handles.  Once the bounded
                    # post-exit drain window expires, do not let that helper
                    # append diagnostics or invoke UI callbacks for a job that
                    # has already completed.
                    if stop_stream_readers.is_set():
                        return
                    capture.append(chunk)
                    if log_file is not None:
                        raw = (
                            chunk
                            if isinstance(chunk, bytes)
                            else str(chunk).encode(encoding, errors="replace")
                        )
                        try:
                            log_file.write(raw)
                        except (OSError, ValueError):
                            # A diagnostic volume becoming unavailable must
                            # not abort an otherwise healthy render.
                            log_file = None
                    if not report_lines:
                        continue
                    if isinstance(chunk, bytes):
                        line = chunk.decode(
                            encoding,
                            errors="replace",
                        )
                    else:
                        line = str(chunk)
                    try:
                        stdout_line_callback(line.rstrip("\r\n"))
                    except Exception:
                        # A UI callback is diagnostic only.  FFmpeg must keep
                        # running even if the browser closes mid-render.
                        pass

            stdout_reader = threading.Thread(
                target=drain_stream,
                args=(process.stdout, stdout_capture),
                kwargs={
                    "report_lines": stdout_line_callback is not None,
                    "log_file": stdout_log,
                },
                name="storyforge-stdout-reader",
                daemon=True,
            )
            stderr_reader = threading.Thread(
                target=drain_stream,
                args=(process.stderr, stderr_capture),
                kwargs={"report_lines": False, "log_file": stderr_log},
                name="storyforge-stderr-reader",
                daemon=True,
            )
            stdout_reader.start()
            stderr_reader.start()
            if input_value is not None and process.stdin is not None:
                try:
                    process.stdin.write(input_value)
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                terminate_process_tree(process)
                process.wait()
                stdout_reader.join(timeout=2.0)
                stderr_reader.join(timeout=2.0)
                close_pipe_streams()
                if stdout_log is not None:
                    stdout_log.close()
                if stderr_log is not None:
                    stderr_log.close()
                raise subprocess.TimeoutExpired(
                    process.args,
                    timeout,
                    output=captured_output(
                        stdout_capture, captured=process.stdout is not None
                    ),
                    stderr=captured_output(
                        stderr_capture, captured=process.stderr is not None
                    ),
                )

            # Usually both readers observe EOF immediately after the direct
            # child exits.  A launcher may, however, leave a descendant alive
            # with inherited pipe handles.  Waiting indefinitely for that
            # unrelated process would wedge the single production queue.  Give
            # already-written diagnostics a short window to drain, then detach
            # any still-blocked daemon reader.  Its stream is deliberately not
            # closed here: closing a buffered pipe from another thread can
            # itself wait on the reader's internal lock.  The reader exits and
            # releases the handle when the descendant writes again or exits.
            drain_deadline = time.monotonic() + _POST_EXIT_PIPE_DRAIN_SECONDS
            for reader in (stdout_reader, stderr_reader):
                remaining = drain_deadline - time.monotonic()
                if remaining <= 0:
                    break
                reader.join(timeout=remaining)
            if stdout_reader.is_alive() or stderr_reader.is_alive():
                stop_stream_readers.set()
                for reader in (stdout_reader, stderr_reader):
                    _cancel_windows_thread_io(reader)
                for reader in (stdout_reader, stderr_reader):
                    reader.join(timeout=0.25)
            if not stdout_reader.is_alive() and not stderr_reader.is_alive():
                close_pipe_streams()
            else:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except (OSError, ValueError):
                        pass
            if stdout_log is not None:
                stdout_log.close()
            if stderr_log is not None:
                stderr_log.close()

            if active is not None:
                active.raise_if_cancelled()
            completed = subprocess.CompletedProcess(
                process.args,
                process.returncode,
                captured_output(stdout_capture, captured=process.stdout is not None),
                captured_output(stderr_capture, captured=process.stderr is not None),
            )
            if check:
                completed.check_returncode()
            return completed  # type: ignore[return-value]

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
    "windows_process_creation_flags",
]
