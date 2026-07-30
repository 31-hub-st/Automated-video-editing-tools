from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from storyforge.cancellation import (
    CancellationToken,
    JobCancelledError,
    cancellation_scope,
    run_cancellable_process,
    windows_process_creation_flags,
)
from storyforge.jobs import JobQueue
from storyforge.models import JobStatus, PlatformProfile, RenderJob
from storyforge.providers.base import ProviderConfig
from storyforge.providers.tts import KokoroProvider


class WindowsProcessFlagTests(unittest.TestCase):
    def test_ffmpeg_gets_below_normal_priority_without_affecting_other_tools(self) -> None:
        with (
            mock.patch.object(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True
            ),
            mock.patch.object(
                subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, create=True
            ),
        ):
            ffmpeg = windows_process_creation_flags(("FFmpeg.exe",), 0x10)
            imageio_ffmpeg = windows_process_creation_flags(
                (r"C:\cache\ffmpeg-win-x86_64-v7.1.exe",), 0x10
            )
            python = windows_process_creation_flags(("python.exe",), 0x10)

        self.assertEqual(ffmpeg, 0x10 | 0x200 | 0x4000)
        self.assertEqual(imageio_ffmpeg, 0x10 | 0x200 | 0x4000)
        self.assertEqual(python, 0x10 | 0x200)


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.025)
    return bool(predicate())


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, int(pid)
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_file_has_fields(path: Path, expected: int) -> bool:
    """Wait for the child to finish its tiny PID-file write, not just create it."""

    try:
        return len(path.read_text(encoding="ascii").split()) == expected
    except OSError:
        return False


def _job(job_id: str, platform: PlatformProfile, output: Path) -> RenderJob:
    return RenderJob(
        id=job_id,
        batch_id="hard-cancel-batch",
        platform_id=platform.id,
        source_file=__file__,
        title="Hard Cancel Story",
        code="B73165",
        video_folder=str(output),
        music_folder=str(output),
        output_folder=str(output),
    )


class CancellableProcessTests(unittest.TestCase):
    def test_cancel_terminates_process_tree_and_raises_terminal_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pid_file = Path(temp) / "pids.txt"
            token = CancellationToken()
            errors: list[BaseException] = []
            command = [
                sys.executable,
                "-c",
                (
                    "import os,pathlib,subprocess,sys,time;"
                    "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                    "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}',encoding='ascii');"
                    "time.sleep(60)"
                ),
                str(pid_file),
            ]

            def run() -> None:
                try:
                    with cancellation_scope(token):
                        run_cancellable_process(command, capture_output=True, check=False)
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            self.assertTrue(
                _wait_until(lambda: _pid_file_has_fields(pid_file, 2)),
                "child process did not finish publishing its PIDs",
            )
            parent_pid, child_pid = [
                int(item) for item in pid_file.read_text(encoding="ascii").split()
            ]
            self.assertTrue(_pid_is_alive(parent_pid))
            self.assertTrue(_pid_is_alive(child_pid))

            token.cancel("test requested cancellation")
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive(), "cancelled process call did not unwind")
            self.assertTrue(errors)
            self.assertIsInstance(errors[0], JobCancelledError)
            self.assertTrue(
                _wait_until(lambda: not _pid_is_alive(parent_pid)),
                "direct child survived cancellation",
            )
            self.assertTrue(
                _wait_until(lambda: not _pid_is_alive(child_pid)),
                "descendant process survived cancellation",
            )

    def test_kokoro_cli_uses_current_job_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pid_file = root / "tts.pid"
            token = CancellationToken()
            errors: list[BaseException] = []
            provider = KokoroProvider(
                ProviderConfig(
                    name="local_kokoro",
                    options={
                        "cache_enabled": False,
                        "command": [
                            sys.executable,
                            "-c",
                            (
                                "import os,pathlib,sys,time;"
                                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()),encoding='ascii');"
                                "time.sleep(60)"
                            ),
                            str(pid_file),
                            "{output}",
                        ],
                    },
                )
            )

            def synthesize() -> None:
                try:
                    with cancellation_scope(token):
                        provider.synthesize(
                            ["A sentence that should be cancelled."],
                            root / "voice",
                            voice="af_heart",
                        )
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=synthesize, daemon=True)
            worker.start()
            self.assertTrue(
                _wait_until(
                    lambda: pid_file.is_file()
                    and bool(pid_file.read_text(encoding="ascii").strip())
                ),
                "Kokoro CLI did not start",
            )
            pid = int(pid_file.read_text(encoding="ascii"))
            token.cancel("stop TTS")
            worker.join(timeout=8)

            self.assertFalse(worker.is_alive())
            self.assertTrue(errors)
            self.assertIsInstance(errors[0], JobCancelledError)
            self.assertFalse(_pid_is_alive(pid))


class JobQueueHardCancellationTests(unittest.TestCase):
    def test_shutdown_waits_for_worker_and_rejects_late_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pid_file = root / "shutdown-worker.pid"
            entered = threading.Event()
            platform = PlatformProfile(id="platform-1", name="GoodNovel")
            job = _job("shutdown-active", platform, root)
            queued = _job("shutdown-queued", platform, root)
            calls: list[str] = []

            def processor(current_job, _platform, _progress):
                calls.append(current_job.id)
                entered.set()
                run_cancellable_process(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os,pathlib,sys,time;"
                            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()),encoding='ascii');"
                            "time.sleep(60)"
                        ),
                        str(pid_file),
                    ],
                    capture_output=True,
                    check=False,
                )
                return "late-output.mp4"

            queue = JobQueue(processor)
            queue.enqueue_jobs([job, queued], platform)
            queue.start()
            self.assertTrue(entered.wait(4))
            self.assertTrue(
                _wait_until(lambda: _pid_file_has_fields(pid_file, 1)),
                "render child did not finish publishing its PID",
            )
            pid = int(pid_file.read_text(encoding="ascii"))

            self.assertTrue(queue.stop_and_wait(timeout_seconds=8))
            self.assertFalse(_pid_is_alive(pid))
            worker = queue._worker
            self.assertFalse(worker and worker.is_alive())
            changed = queue.mark_shutdown_interrupted(
                {job.id}, reason="application shutdown"
            )
            self.assertEqual([item.id for item in changed], [job.id])
            self.assertEqual(job.status, JobStatus.INTERRUPTED)
            self.assertEqual(queued.status, JobStatus.QUEUED)
            self.assertEqual(calls, [job.id])

            with self.assertRaisesRegex(RuntimeError, "shutting down"):
                queue.enqueue_jobs([_job("late-job", platform, root)], platform)
            with self.assertRaisesRegex(RuntimeError, "shutting down"):
                queue.start()

    def test_selected_cancel_kills_process_keeps_sibling_and_ignores_late_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pid_file = root / "worker.pid"
            entered = threading.Event()
            platform = PlatformProfile(id="platform-1", name="GoodNovel")
            first = _job("hard-cancel-first", platform, root)
            second = _job("hard-cancel-second", platform, root)
            calls: list[str] = []

            def processor(job, _platform, progress):
                calls.append(job.id)
                if job.id == first.id:
                    entered.set()
                    try:
                        run_cancellable_process(
                            [
                                sys.executable,
                                "-c",
                                (
                                    "import os,pathlib,sys,time;"
                                    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()),encoding='ascii');"
                                    "time.sleep(60)"
                                ),
                                str(pid_file),
                            ],
                            capture_output=True,
                            check=False,
                        )
                    finally:
                        # Simulate a provider/renderer reporting progress while
                        # its stack unwinds after the process has been killed.
                        progress(JobStatus.RENDERING, 0.99, "late callback")
                    return "late-output.mp4"
                output = root / "second.mp4"
                output.write_bytes(b"second")
                return str(output)

            queue = JobQueue(processor)
            queue.enqueue_jobs([first, second], platform)
            queue.start()
            self.assertTrue(entered.wait(4))
            self.assertTrue(
                _wait_until(lambda: _pid_file_has_fields(pid_file, 1)),
                "render child did not finish publishing its PID",
            )
            pid = int(pid_file.read_text(encoding="ascii"))

            changed = queue.cancel_jobs({first.id})
            self.assertEqual([item["id"] for item in changed], [first.id])
            self.assertTrue(
                _wait_until(
                    lambda: queue.get_job(second.id).status == JobStatus.COMPLETED
                ),
                "uncancelled sibling did not continue",
            )
            worker = queue._worker
            if worker is not None:
                worker.join(timeout=8)
            self.assertFalse(worker and worker.is_alive())
            self.assertFalse(_pid_is_alive(pid))

            first_state, second_state = queue.list_jobs()
            self.assertEqual(first_state["status"], JobStatus.CANCELLED.value)
            self.assertEqual(first_state["stage_label"], "已取消")
            self.assertEqual(first_state["output_file"], "")
            self.assertEqual(first_state["error_log"], "")
            self.assertEqual(first_state["message"], "")
            self.assertEqual(second_state["status"], JobStatus.COMPLETED.value)
            self.assertEqual(calls, [first.id, second.id])


if __name__ == "__main__":
    unittest.main()
