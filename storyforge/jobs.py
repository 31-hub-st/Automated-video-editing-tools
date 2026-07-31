from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .cancellation import (
    CancellationToken,
    JobCancelledError,
    cancellation_scope,
)
from .failure_diagnostics import capture_failure_diagnostics
from .models import BatchSpec, JobStatus, PlatformProfile, RenderJob, utc_now


ProgressCallback = Callable[[JobStatus, float, str], None]
JobProcessor = Callable[[RenderJob, PlatformProfile, ProgressCallback], str]
JobLoader = Callable[[int], list[RenderJob]]
TerminalCallback = Callable[[RenderJob], None]


class _ScheduledBatch:
    """One FIFO scheduling unit created by a single enqueue operation."""

    def __init__(
        self,
        job_ids: list[str],
        platform_id: str,
        *,
        loader: JobLoader | None = None,
    ) -> None:
        self.job_ids = list(job_ids)
        self.platform_id = platform_id
        self.loader = loader
        self.loader_failures = 0
        self.loader_retry_at = 0.0
        self.loader_error = ""


ARCHIVABLE_JOB_STATUSES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    }
)


def _persist_job_failure(
    job: RenderJob,
    error: BaseException,
    traceback_text: str,
) -> str:
    """Best-effort persistent diagnostics for failures before render logs exist."""

    try:
        from .pipeline import job_workspace_directory

        job_dir = job_workspace_directory(job)
        job_dir.mkdir(parents=True, exist_ok=True)
        error_path = job_dir / "job-error.log"
        error_path.write_text(
            "\n".join(
                (
                    "StoryForge job failure",
                    f"time: {utc_now()}",
                    f"stage: {job.stage_label}",
                    f"progress: {job.progress:.4f}",
                    f"source: {job.source_file}",
                    f"error: {type(error).__name__}: {error}",
                    "",
                    traceback_text,
                )
            ),
            encoding="utf-8",
            newline="\n",
        )
        return str(error_path)
    except OSError:
        return ""


class JobQueue:
    """A deliberately sequential queue so GPU-heavy stages do not overlap."""

    def __init__(self, processor: JobProcessor | None = None) -> None:
        self._processor = processor
        self._jobs: list[RenderJob] = []
        self._platforms: dict[str, PlatformProfile] = {}
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._retiring_worker: threading.Thread | None = None
        # Once application shutdown starts this queue is permanently closed.
        # Without this gate a late web request could clear ``_cancel`` in
        # ``start`` and launch more FFmpeg/TTS work while leases are released.
        self._shutdown_requested = threading.Event()
        self._work_revision = 0
        self._cancel = threading.Event()
        self._cancelled_job_ids: set[str] = set()
        # Cancellation can arrive while a durable loader owns a record but
        # before its RenderJob reaches memory.  Keep those ids separately so
        # they can be promoted on load, then discard unmatched tombstones once
        # every known lazy loader has definitively exhausted its batch.
        self._lazy_cancelled_job_ids: set[str] = set()
        self._job_tokens: dict[str, CancellationToken] = {}
        # A cancelled job becomes terminal immediately in the UI, but its
        # processor may still be unwinding.  Keep that execution lifetime
        # separate from the public status so retry/archive/clear cannot race
        # the old attempt and let its late result overwrite the new state.
        self._active_job_ids: set[str] = set()
        self._scheduled_batches: list[_ScheduledBatch] = []
        self._streamed_job_ids: set[str] = set()
        # Terminal streamed jobs whose durable callback failed must remain in
        # memory until that callback succeeds.  Otherwise bounded history can
        # evict the only copy of the final state while the ledger is offline.
        self._pending_terminal_callback_ids: set[str] = set()
        self._stream_history_limit = 50
        self._stream_paused = False
        self._stream_retry_failures = 0
        self._stream_retry_at = 0.0
        self._stream_last_error = ""
        self._stream_last_reconnected_at = ""
        self._terminal_callback: TerminalCallback | None = None
        self._terminal_callback_lock = threading.RLock()

    def set_processor(self, processor: JobProcessor) -> None:
        """Attach the render pipeline before the queue is started.

        The API is created before the desktop window, while the pipeline needs
        access to the API's live settings.  Keeping this as an explicit setter
        avoids a circular constructor dependency.
        """

        if not callable(processor):
            raise TypeError("processor must be callable")
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise RuntimeError("渲染队列运行时不能更换处理器。")
            self._processor = processor
            has_recovered_work = any(
                job.status == JobStatus.QUEUED for job in self._jobs
            )
        # Startup recovery runs before PipelineRunner is constructed.  As soon
        # as the processor becomes available, continue the durable queue
        # without requiring an employee to notice and press Start again.
        if has_recovered_work:
            self.start()

    def set_terminal_callback(self, callback: TerminalCallback | None) -> None:
        """Observe every terminal job before it can leave the live queue.

        The callback is owned by the long-running production worker, not by a
        browser poll. Streamed jobs still use it as an eviction barrier, while
        ordinary jobs publish their final state as soon as processing unwinds.
        """

        if callback is not None and not callable(callback):
            raise TypeError("terminal callback must be callable")
        with self._lock:
            if callback is None and self._pending_terminal_callback_ids:
                raise RuntimeError(
                    "cannot remove the terminal callback while streamed results are unsaved"
                )
            self._terminal_callback = callback

    def resume_streams(self) -> None:
        """Retry unsaved terminal states before durable loaders continue."""

        with self._lock:
            pending = [
                job
                for job in self._jobs
                if job.id in self._pending_terminal_callback_ids
            ]
        for job in pending:
            self._finish_streamed_job(job)
        with self._lock:
            if self._pending_terminal_callback_ids:
                raise RuntimeError(
                    "生产记录服务仍不可用，流式任务会继续保持暂停。"
                )
            self._stream_paused = False
            self._stream_retry_failures = 0
            self._stream_retry_at = 0.0
            self._stream_last_error = ""
            self._stream_last_reconnected_at = utc_now()
            self._work_revision += 1

    @staticmethod
    def _retry_delay(failures: int) -> float:
        """Bound automatic Hub retries without making a brief outage fatal."""

        return min(30.0, float(2 ** max(0, min(5, int(failures) - 1))))

    def stream_status(self) -> dict[str, Any]:
        """Expose stream connectivity without changing the legacy job-list shape."""

        now = time.monotonic()
        with self._lock:
            batches = [
                batch
                for batch in self._scheduled_batches
                if batch.loader is not None and batch.loader_failures
            ]
            retry_at = max(
                [self._stream_retry_at, *(batch.loader_retry_at for batch in batches)],
                default=0.0,
            )
            failures = max(
                [self._stream_retry_failures, *(batch.loader_failures for batch in batches)],
                default=0,
            )
            error = self._stream_last_error or next(
                (batch.loader_error for batch in batches if batch.loader_error), ""
            )
            reconnecting = bool(self._stream_paused or batches)
            return {
                "state": "reconnecting" if reconnecting else "connected",
                "reconnecting": reconnecting,
                "retry_in_seconds": (
                    max(0.0, round(retry_at - now, 1)) if reconnecting else 0.0
                ),
                "failures": failures,
                "message": (
                    "主机连接暂时中断，队列正在自动重连。" if reconnecting else "队列连接正常。"
                ),
                "last_error": error,
                "last_reconnected_at": self._stream_last_reconnected_at,
            }

    def _append_scheduled_batch_locked(
        self,
        jobs: list[RenderJob],
        platform_id: str,
        *,
        loader: JobLoader | None = None,
    ) -> None:
        if not jobs and loader is None:
            return
        self._scheduled_batches.append(
            _ScheduledBatch(
                [job.id for job in jobs],
                platform_id,
                loader=loader,
            )
        )
        self._work_revision += 1

    def _promote_lazy_cancellations_locked(self, jobs: list[RenderJob]) -> None:
        loaded_ids = {job.id for job in jobs}
        promoted = loaded_ids.intersection(self._lazy_cancelled_job_ids)
        if not promoted:
            return
        self._lazy_cancelled_job_ids.difference_update(promoted)
        self._cancelled_job_ids.update(promoted)

    def _discard_exhausted_lazy_cancellations_locked(self) -> None:
        if not any(batch.loader is not None for batch in self._scheduled_batches):
            self._lazy_cancelled_job_ids.clear()

    def _detach_scheduled_jobs_locked(self, job_ids: set[str]) -> None:
        if not job_ids:
            return
        for batch in self._scheduled_batches:
            batch.job_ids = [job_id for job_id in batch.job_ids if job_id not in job_ids]

    def _schedule_existing_jobs_locked(self, jobs: list[RenderJob]) -> None:
        """Put newly-runnable existing jobs at the tail exactly once."""

        runnable = [job for job in jobs if job.status == JobStatus.QUEUED]
        if not runnable:
            return
        job_ids = {job.id for job in runnable}
        self._detach_scheduled_jobs_locked(job_ids)
        self._append_scheduled_batch_locked(
            runnable,
            runnable[0].platform_id,
        )

    def _remove_scheduled_jobs_locked(self, job_ids: set[str]) -> None:
        self._detach_scheduled_jobs_locked(job_ids)

    @staticmethod
    def scan_batch(batch: BatchSpec) -> dict[str, Any]:
        from .services.text_processing import parse_story_filename

        folder = Path(batch.text_folder)
        if not folder.is_dir():
            raise ValueError("小说文件夹不存在。")
        stories: list[dict[str, Any]] = []
        errors: list[str] = []
        for path in sorted(folder.glob("*.txt"), key=lambda item: item.name.casefold()):
            if path.name.casefold().endswith(".meta.txt"):
                continue
            try:
                identity = parse_story_filename(path)
                if isinstance(identity, tuple):
                    code, title = identity
                else:
                    code, title = identity.code, identity.title
                stories.append(
                    {
                        "source_file": str(path.resolve()),
                        "code": str(code),
                        "title": str(title),
                    }
                )
            except (TypeError, ValueError) as error:
                errors.append(f"{path.name}: {error}")
        return {"stories": stories, "errors": errors}

    def enqueue_batch(
        self, batch: BatchSpec, platform: PlatformProfile
    ) -> tuple[list[RenderJob], list[str]]:
        if self._shutdown_requested.is_set():
            raise RuntimeError("render queue is shutting down")
        scan = self.scan_batch(batch)
        jobs = [
            RenderJob(
                batch_id=batch.id,
                platform_id=batch.platform_id,
                source_file=story["source_file"],
                title=story["title"],
                code=story["code"],
                video_folder=batch.video_folder,
                music_folder=batch.music_folder,
                output_folder=batch.output_folder,
                settings_snapshot={
                    "output_mode": batch.output_mode,
                    "export_narration_audio": batch.output_mode == "video_and_mp3",
                    "source_narration_audio": batch.source_narration_audio,
                },
            )
            for story in scan["stories"]
        ]
        with self._lock:
            if self._shutdown_requested.is_set():
                raise RuntimeError("render queue is shutting down")
            self._promote_lazy_cancellations_locked(jobs)
            self._jobs.extend(jobs)
            self._platforms[platform.id] = platform
            self._append_scheduled_batch_locked(jobs, platform.id)
        return jobs, scan["errors"]

    def enqueue_jobs(
        self,
        jobs: list[RenderJob],
        platform: PlatformProfile,
    ) -> list[RenderJob]:
        """Add already-structured library jobs to the local render queue."""

        if not jobs:
            return []
        if self._shutdown_requested.is_set():
            raise RuntimeError("render queue is shutting down")
        if any(job.platform_id != platform.id for job in jobs):
            raise ValueError("All jobs must reference the supplied platform.")
        with self._lock:
            if self._shutdown_requested.is_set():
                raise RuntimeError("render queue is shutting down")
            self._promote_lazy_cancellations_locked(jobs)
            self._jobs.extend(jobs)
            self._platforms[platform.id] = platform
            self._append_scheduled_batch_locked(jobs, platform.id)
        return jobs

    def enqueue_stream(
        self,
        initial_jobs: list[RenderJob],
        loader: JobLoader,
        platform: PlatformProfile,
        *,
        history_limit: int = 50,
    ) -> list[RenderJob]:
        """Queue a bounded first window and lazily request subsequent windows.

        ``loader`` is expected to read already-persisted job snapshots.  Thus
        the queue can keep memory bounded without making the remaining batch
        dependent on browser state.
        """

        if not callable(loader):
            raise TypeError("loader must be callable")
        if self._shutdown_requested.is_set():
            raise RuntimeError("render queue is shutting down")
        if any(job.platform_id != platform.id for job in initial_jobs):
            raise ValueError("All jobs must reference the supplied platform.")
        with self._lock:
            if self._shutdown_requested.is_set():
                raise RuntimeError("render queue is shutting down")
            self._promote_lazy_cancellations_locked(initial_jobs)
            self._jobs.extend(initial_jobs)
            self._streamed_job_ids.update(job.id for job in initial_jobs)
            self._append_scheduled_batch_locked(
                initial_jobs,
                platform.id,
                loader=loader,
            )
            self._platforms[platform.id] = platform
            self._stream_history_limit = max(1, int(history_limit))
        return initial_jobs

    def _load_stream_window(
        self,
        batch: _ScheduledBatch,
        size: int = 4,
    ) -> bool:
        """Load the head batch's next persisted window outside the lock."""

        with self._lock:
            if (
                self._stream_paused
                or not self._scheduled_batches
                or self._scheduled_batches[0] is not batch
                or batch.loader is None
            ):
                return False
            loader = batch.loader
            platform_id = batch.platform_id
            if batch.loader_retry_at > time.monotonic():
                return False
        try:
            jobs = list(loader(max(1, int(size))))
        except BaseException as error:
            print(traceback.format_exc(), end="")
            # Keep this FIFO entry at the head and retry it automatically.
            # Younger batches must not overtake an unconsumed durable tail.
            with self._lock:
                if (
                    self._scheduled_batches
                    and self._scheduled_batches[0] is batch
                    and batch.loader is loader
                ):
                    batch.loader_failures += 1
                    batch.loader_retry_at = time.monotonic() + self._retry_delay(
                        batch.loader_failures
                    )
                    batch.loader_error = f"{type(error).__name__}: {error}"
                    self._work_revision += 1
            return False
        invalid_jobs = {
            id(job) for job in jobs if job.platform_id != platform_id
        }
        with self._lock:
            # Whole-queue cancellation can discard the entry while the loader
            # performs I/O. Never resurrect those durable records afterwards.
            if (
                not self._scheduled_batches
                or self._scheduled_batches[0] is not batch
                or batch.loader is not loader
            ):
                return False
            if not jobs:
                batch.loader = None
                if batch.loader_failures:
                    self._stream_last_reconnected_at = utc_now()
                batch.loader_failures = 0
                batch.loader_retry_at = 0.0
                batch.loader_error = ""
                self._discard_exhausted_lazy_cancellations_locked()
                self._work_revision += 1
                return False
            self._promote_lazy_cancellations_locked(jobs)
            if batch.loader_failures:
                self._stream_last_reconnected_at = utc_now()
            batch.loader_failures = 0
            batch.loader_retry_at = 0.0
            batch.loader_error = ""
            now = utc_now()
            for job in jobs:
                if job.id in self._cancelled_job_ids:
                    job.status = JobStatus.CANCELLED
                    job.stage_label = "已取消"
                    job.updated_at = now
                elif id(job) in invalid_jobs:
                    job.status = JobStatus.FAILED
                    job.progress = 0.0
                    job.stage_label = "平台配置不匹配"
                    job.message = "流式任务的平台与所属批次不一致，已安全跳过。"
                    job.updated_at = now
            self._jobs.extend(jobs)
            batch.job_ids.extend(job.id for job in jobs)
            self._streamed_job_ids.update(job.id for job in jobs)
            self._work_revision += 1
            terminal_jobs = [
                job for job in jobs if job.status in ARCHIVABLE_JOB_STATUSES
            ]
        for job in terminal_jobs:
            self._finish_streamed_job(job)
        return True

    def _finish_streamed_job(self, job: RenderJob) -> None:
        """Publish one terminal result; retain streamed history only after ack.

        The historical method name is kept as an internal compatibility
        surface. Terminal publication now applies to ordinary jobs as well.
        """

        with self._lock:
            streamed = job.id in self._streamed_job_ids
            callback = self._terminal_callback
        if job.status not in ARCHIVABLE_JOB_STATUSES:
            return
        if callback is not None:
            # Mark publication before invoking external storage. Readers must
            # not observe a completed/failed card while its durable record and
            # lease release are still in flight.
            with self._lock:
                self._pending_terminal_callback_ids.add(job.id)
            try:
                with self._terminal_callback_lock:
                    callback(job)
            except BaseException:
                # Keep the terminal snapshot in memory and let the Worker
                # retry it. A browser GET must never be required to make a
                # finished render durable.
                print(traceback.format_exc(), end="")
                with self._lock:
                    self._stream_paused = True
                    self._stream_retry_failures += 1
                    self._stream_retry_at = time.monotonic() + self._retry_delay(
                        self._stream_retry_failures
                    )
                    self._stream_last_error = traceback.format_exc().strip().splitlines()[-1]
                    self._pending_terminal_callback_ids.add(job.id)
                return
        with self._lock:
            self._pending_terminal_callback_ids.discard(job.id)
            if not self._pending_terminal_callback_ids and self._stream_paused:
                self._stream_paused = False
                self._stream_retry_failures = 0
                self._stream_retry_at = 0.0
                self._stream_last_error = ""
                self._stream_last_reconnected_at = utc_now()
            terminal = [
                item
                for item in self._jobs
                if item.id in self._streamed_job_ids
                and item.status in ARCHIVABLE_JOB_STATUSES
                and item.id not in self._pending_terminal_callback_ids
            ]
            overflow = max(0, len(terminal) - self._stream_history_limit)
            evicted = {item.id for item in terminal[:overflow]}
            if evicted:
                self._jobs = [item for item in self._jobs if item.id not in evicted]
                self._remove_scheduled_jobs_locked(evicted)
                self._streamed_job_ids.difference_update(evicted)
                self._cancelled_job_ids.difference_update(evicted)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            snapshots: list[dict[str, Any]] = []
            for job in self._jobs:
                snapshot = job.to_dict()
                publication_pending = (
                    job.id in self._pending_terminal_callback_ids
                    or (
                        job.id in self._active_job_ids
                        and job.status
                        in {
                            JobStatus.COMPLETED,
                            JobStatus.FAILED,
                            JobStatus.INTERRUPTED,
                        }
                    )
                )
                if publication_pending and job.status in ARCHIVABLE_JOB_STATUSES:
                    snapshot["status"] = JobStatus.RENDERING.value
                    snapshot["progress"] = min(float(job.progress), 0.99)
                    snapshot["stage_label"] = "正在保存制作记录"
                snapshots.append(snapshot)
            return snapshots

    def archive_snapshot(self, job_id: str) -> dict[str, Any]:
        """Return a stable snapshot only when a job can no longer execute."""

        with self._lock:
            job = next((item for item in self._jobs if item.id == job_id), None)
            if job is None:
                raise KeyError("production job not found")
            if job.id in self._active_job_ids:
                raise ValueError("任务仍在停止中，请稍后重试。")
            if job.id in self._pending_terminal_callback_ids:
                raise ValueError("任务结果尚未写入生产记录，请恢复主机连接后再试。")
            if job.status not in ARCHIVABLE_JOB_STATUSES:
                raise ValueError("only finished jobs can be archived")
            return {**job.to_dict(), "archived": True}

    def archive_batch_snapshots(self, batch_id: str) -> list[dict[str, Any]]:
        """Validate and snapshot every live card in one production batch.

        Validation happens while holding the queue lock.  Callers can therefore
        persist the complete set before removing any card, without a partial
        batch becoming visible between individual job operations.
        """

        normalized_batch_id = str(batch_id or "").strip()
        if not normalized_batch_id:
            raise ValueError("batch_id is required")
        with self._lock:
            jobs = [item for item in self._jobs if item.batch_id == normalized_batch_id]
            if not jobs:
                return []
            blocked = [
                item.id
                for item in jobs
                if item.id in self._active_job_ids
                or item.id in self._pending_terminal_callback_ids
                or item.status not in ARCHIVABLE_JOB_STATUSES
            ]
            if blocked:
                raise ValueError(
                    "only a batch whose every task is finished can be archived"
                )
            if any(not str(item.production_record_id or "") for item in jobs):
                raise ValueError(
                    "every task in the batch must have a production record before archiving"
                )
            return [{**item.to_dict(), "archived": True} for item in jobs]

    def remove_archived_batch(
        self, batch_id: str, job_ids: Sequence[str]
    ) -> list[str]:
        """Atomically remove a validated set of terminal cards from the queue."""

        normalized_batch_id = str(batch_id or "").strip()
        requested = {str(item or "").strip() for item in job_ids if str(item or "").strip()}
        if not normalized_batch_id:
            raise ValueError("batch_id is required")
        if not requested:
            return []
        with self._lock:
            jobs = [item for item in self._jobs if item.id in requested]
            if {item.id for item in jobs} != requested:
                raise KeyError("one or more production jobs were not found")
            if any(item.batch_id != normalized_batch_id for item in jobs):
                raise ValueError("production job batch does not match")
            if any(
                item.id in self._active_job_ids
                or item.id in self._pending_terminal_callback_ids
                or item.status not in ARCHIVABLE_JOB_STATUSES
                for item in jobs
            ):
                raise ValueError(
                    "only a batch whose every task is finished can be archived"
                )
            self._jobs = [item for item in self._jobs if item.id not in requested]
            self._remove_scheduled_jobs_locked(requested)
            self._streamed_job_ids.difference_update(requested)
            self._cancelled_job_ids.difference_update(requested)
            self._lazy_cancelled_job_ids.difference_update(requested)
            self._pending_terminal_callback_ids.difference_update(requested)
            self._work_revision += 1
            return sorted(requested)

    def remove_archived(self, job_id: str) -> None:
        """Remove an already-persisted terminal job from the live film strip."""

        with self._lock:
            job = next((item for item in self._jobs if item.id == job_id), None)
            if job is None:
                raise KeyError("production job not found")
            if job.id in self._active_job_ids:
                raise ValueError("任务仍在停止中，请稍后重试。")
            if job.id in self._pending_terminal_callback_ids:
                raise ValueError("任务结果尚未写入生产记录，请恢复主机连接后再试。")
            if job.status not in ARCHIVABLE_JOB_STATUSES:
                raise ValueError("only finished jobs can be archived")
            self._jobs = [item for item in self._jobs if item.id != job_id]
            self._remove_scheduled_jobs_locked({job_id})
            self._streamed_job_ids.discard(job_id)
            self._cancelled_job_ids.discard(job_id)
            self._lazy_cancelled_job_ids.discard(job_id)
            self._pending_terminal_callback_ids.discard(job_id)
            self._work_revision += 1

    def restore_archived(
        self,
        snapshot: dict[str, Any],
        platform: PlatformProfile,
    ) -> RenderJob:
        """Reinsert a persisted terminal job for viewing or an explicit retry."""

        job = RenderJob.from_dict(snapshot)
        if job.status not in ARCHIVABLE_JOB_STATUSES:
            raise ValueError("archived job snapshot is not finished")
        if job.platform_id != platform.id:
            raise ValueError("archived job platform does not match")
        job.archived = False
        job.archived_at = ""
        job.archived_by_user_id = ""
        with self._lock:
            if any(item.id == job.id for item in self._jobs):
                raise ValueError("production job is already in the active film strip")
            self._jobs.append(job)
            self._platforms[platform.id] = platform
        return job

    def restore_archived_batch(
        self,
        snapshots: Sequence[Mapping[str, Any]],
        platforms: Mapping[str, PlatformProfile],
    ) -> list[RenderJob]:
        """Validate then restore a complete archived batch under one lock."""

        restored: list[RenderJob] = []
        for raw in snapshots:
            job = RenderJob.from_dict(dict(raw))
            if job.status not in ARCHIVABLE_JOB_STATUSES:
                raise ValueError("archived job snapshot is not finished")
            platform = platforms.get(job.platform_id)
            if platform is None:
                raise ValueError("archived job platform does not exist")
            job.archived = False
            job.archived_at = ""
            job.archived_by_user_id = ""
            restored.append(job)
        if not restored:
            return []
        batch_ids = {item.batch_id for item in restored}
        if len(batch_ids) != 1:
            raise ValueError("archived jobs do not belong to one batch")
        job_ids = [item.id for item in restored]
        if len(set(job_ids)) != len(job_ids):
            raise ValueError("archived batch contains duplicate job ids")
        with self._lock:
            existing = {item.id for item in self._jobs}
            if existing.intersection(job_ids):
                raise ValueError("production batch is already in the active film strip")
            self._jobs.extend(restored)
            for platform_id in {item.platform_id for item in restored}:
                self._platforms[platform_id] = platforms[platform_id]
            self._work_revision += 1
        return restored

    def is_rendering_busy(self) -> bool:
        """Whether a worker is inside a stage that must not be interrupted."""

        active = {
            JobStatus.PREFLIGHT,
            JobStatus.PREPARING,
            JobStatus.POLISHING,
            JobStatus.NARRATING,
            JobStatus.COMPOSING,
            JobStatus.PREVIEWING,
            JobStatus.RENDERING,
        }
        with self._lock:
            return bool(self._active_job_ids) or any(
                job.status in active for job in self._jobs
            )

    def has_unfinished_work(self) -> bool:
        """Whether production should take priority over background work.

        This includes queued work.  A large update must not reserve disk and
        CPU in the short gap between enqueueing a batch and FFmpeg starting.
        """

        terminal = {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }
        with self._lock:
            return bool(self._active_job_ids) or any(
                job.status not in terminal for job in self._jobs
            )

    def active_job_ids(self) -> set[str]:
        """Return a stable snapshot of processors which currently own work."""

        with self._lock:
            return set(self._active_job_ids)

    def get_job(self, job_id: str) -> RenderJob | None:
        with self._lock:
            return next((item for item in self._jobs if item.id == job_id), None)

    def jobs_for_draft(self, draft_id: str) -> list[RenderJob]:
        with self._lock:
            return [
                item for item in self._jobs if item.production_draft_id == draft_id
            ]

    def clear_finished(self) -> list[dict[str, Any]]:
        """Remove terminal jobs while leaving queued and active work untouched."""

        terminal_statuses = {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }
        with self._lock:
            removed = {
                job.id
                for job in self._jobs
                if job.status in terminal_statuses
                and job.id not in self._active_job_ids
                and job.id not in self._pending_terminal_callback_ids
            }
            self._jobs = [job for job in self._jobs if job.id not in removed]
            self._remove_scheduled_jobs_locked(removed)
            self._streamed_job_ids.difference_update(removed)
            self._cancelled_job_ids.difference_update(removed)
            self._lazy_cancelled_job_ids.difference_update(removed)
            self._pending_terminal_callback_ids.difference_update(removed)
            if removed:
                self._work_revision += 1
            return [job.to_dict() for job in self._jobs]

    def approve_preview(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = next((item for item in self._jobs if item.id == job_id), None)
            if job is None:
                raise KeyError("找不到该样片任务。")
            if job.status != JobStatus.AWAITING_APPROVAL:
                raise ValueError("该任务当前不在等待样片确认状态。")
            related = (
                [
                    item
                    for item in self._jobs
                    if item.production_draft_id == job.production_draft_id
                    and item.status
                    in {JobStatus.AWAITING_APPROVAL, JobStatus.WAITING_PREVIEW}
                ]
                if job.production_draft_id
                else [job]
            )
            for item in related:
                item.preview_approved = True
                item.job_kind = "full"
                item.status = JobStatus.QUEUED
                item.progress = 0.0
                item.stage_label = "样片已通过，等待成片"
                item.updated_at = utc_now()
            self._schedule_existing_jobs_locked(related)
            return job.to_dict()

    def regenerate_preview(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = next((item for item in self._jobs if item.id == job_id), None)
            if job is None:
                raise KeyError("找不到该样片任务。")
            if job.status not in {
                JobStatus.AWAITING_APPROVAL,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.INTERRUPTED,
            }:
                raise ValueError("当前任务仍在运行，不能重新生成样片。")
            job.preview_approved = False
            job.preview_file = ""
            job.preview_uri = ""
            job.output_file = ""
            job.narration_audio_file = ""
            job.error_log = ""
            job.failure_diagnostics = {}
            job.message = ""
            job.job_kind = "preview"
            job.status = JobStatus.QUEUED
            job.progress = 0.0
            job.stage_label = "等待生成样片"
            job.updated_at = utc_now()
            if job.production_draft_id:
                for related in self._jobs:
                    if (
                        related.id != job.id
                        and related.production_draft_id == job.production_draft_id
                        and related.status == JobStatus.QUEUED
                        and related.preview_approved
                    ):
                        related.preview_approved = False
                        related.job_kind = "full"
                        related.status = JobStatus.WAITING_PREVIEW
                        related.progress = 0.0
                        related.stage_label = "等待主样片确认"
                        related.updated_at = utc_now()
            self._schedule_existing_jobs_locked([job])
            return job.to_dict()

    def invalidate_awaiting_previews(
        self,
        settings_snapshot: dict[str, Any],
    ) -> list[str]:
        """Invalidate samples approved against obsolete render settings.

        Only samples which are already waiting for a human decision are
        restarted here.  A sample that is currently rendering keeps its
        immutable snapshot; the API performs the same stale-snapshot check
        when the user attempts to approve it.  This avoids racing FFmpeg or
        the TTS provider while still making it impossible to approve a sample
        produced with settings that are no longer current.
        """

        invalidated: list[str] = []
        with self._lock:
            for job in self._jobs:
                if (
                    job.job_kind != "preview"
                    or job.status != JobStatus.AWAITING_APPROVAL
                    or job.settings_snapshot == settings_snapshot
                ):
                    continue
                job.preview_approved = False
                job.preview_file = ""
                job.preview_uri = ""
                job.output_file = ""
                job.narration_audio_file = ""
                job.error_log = ""
                job.failure_diagnostics = {}
                job.message = "全局制作设置已改变，样片需要重新生成并确认。"
                job.status = JobStatus.QUEUED
                job.progress = 0.0
                job.stage_label = "设置已改变，等待重新生成样片"
                job.settings_snapshot = dict(settings_snapshot)
                job.updated_at = utc_now()
                invalidated.append(job.id)
                if job.production_draft_id:
                    for related in self._jobs:
                        if (
                            related.id != job.id
                            and related.production_draft_id
                            == job.production_draft_id
                            and related.status
                            in {
                                JobStatus.QUEUED,
                                JobStatus.WAITING_PREVIEW,
                                JobStatus.APPROVED,
                            }
                        ):
                            related.preview_approved = False
                            related.job_kind = "full"
                            related.status = JobStatus.WAITING_PREVIEW
                            related.progress = 0.0
                            related.stage_label = "等待新的主样片确认"
                            related.settings_snapshot = dict(settings_snapshot)
                            related.updated_at = utc_now()
            invalidated_ids = set(invalidated)
            self._schedule_existing_jobs_locked(
                [job for job in self._jobs if job.id in invalidated_ids]
            )
        return invalidated

    def _require_retryable_locked(self, job_id: str) -> RenderJob:
        job = next((item for item in self._jobs if item.id == job_id), None)
        if job is None:
            raise KeyError("找不到该失败任务。")
        if job.status not in {
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }:
            raise ValueError("只有失败、取消或中断任务可以重试。")
        if job.id in self._active_job_ids:
            raise ValueError("任务仍在停止中，请稍后重试。")
        if job.id in self._pending_terminal_callback_ids:
            raise ValueError("任务结果尚未写入生产记录，请恢复主机连接后再试。")
        return job

    def assert_retryable(self, job_id: str) -> None:
        """Validate retry state before an external ledger begins its update."""

        with self._lock:
            self._require_retryable_locked(job_id)

    def retry_failed(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._require_retryable_locked(job_id)
            job.status = JobStatus.QUEUED
            job.progress = 0.0
            job.stage_label = "等待重试"
            job.output_file = ""
            job.narration_audio_file = ""
            job.error_log = ""
            job.failure_diagnostics = {}
            job.message = ""
            job.updated_at = utc_now()
            self._cancelled_job_ids.discard(job.id)
            self._lazy_cancelled_job_ids.discard(job.id)
            self._schedule_existing_jobs_locked([job])
            return job.to_dict()

    def start(self) -> None:
        if self._processor is None:
            raise RuntimeError("渲染管线尚未初始化。")
        with self._lock:
            if self._shutdown_requested.is_set():
                raise RuntimeError("render queue is shutting down")
            if (
                self._worker
                and self._worker.is_alive()
                and self._worker is not self._retiring_worker
            ):
                return
            self._cancel.clear()
            self._worker = threading.Thread(
                target=self._run, name="storyforge-render-queue", daemon=True
            )
            self._retiring_worker = None
            self._worker.start()

    def _next_scheduled_work(
        self,
    ) -> tuple[RenderJob | None, _ScheduledBatch | None, int]:
        """Return work only from the oldest unfinished enqueue operation."""

        with self._lock:
            if self._shutdown_requested.is_set():
                return None, None, self._work_revision
            while self._scheduled_batches:
                batch = self._scheduled_batches[0]
                jobs_by_id = {job.id: job for job in self._jobs}
                for job_id in batch.job_ids:
                    job = jobs_by_id.get(job_id)
                    if job is not None and job.status == JobStatus.QUEUED:
                        self._active_job_ids.add(job.id)
                        return job, batch, self._work_revision
                if batch.loader is not None:
                    return None, batch, self._work_revision
                self._scheduled_batches.pop(0)
            return None, None, self._work_revision

    def _head_has_queued_job_locked(self) -> bool:
        if not self._scheduled_batches:
            return False
        head_ids = set(self._scheduled_batches[0].job_ids)
        return any(
            job.id in head_ids and job.status == JobStatus.QUEUED
            for job in self._jobs
        )

    def _retire_if_idle(self, observed_revision: int) -> bool:
        """Atomically commit an idle worker to exit.

        Appending jobs and starting the queue are separate calls. A caller may
        therefore reach ``start`` after this worker's final work check but just
        before the thread actually exits. The retiring marker lets ``start``
        hand the queue to a replacement worker instead of losing that wakeup.
        """

        current = threading.current_thread()
        with self._lock:
            if (
                self._work_revision != observed_revision
                or self._head_has_queued_job_locked()
            ):
                return False
            if self._worker is current:
                self._retiring_worker = current
            return True

    def cancel_jobs(
        self,
        job_ids: list[str] | tuple[str, ...] | set[str],
        *,
        reason: str = "",
    ) -> list[dict[str, Any]]:
        """Make selected queue items terminal and stop their local processes.

        A running FFmpeg or local TTS CLI is terminated through the per-job
        cancellation token. All later progress callbacks are ignored and
        queued siblings not selected remain available to the worker.
        """

        requested = {str(item) for item in job_ids if str(item)}
        if not requested:
            return []
        changed: list[dict[str, Any]] = []
        callback_jobs: list[RenderJob] = []
        tokens: list[CancellationToken] = []
        with self._lock:
            # Keep tombstones even for lazy stream-tail jobs which the loader
            # has claimed but has not appended to ``_jobs`` yet.  The load
            # completion path applies these before a job can become runnable.
            known_ids = {job.id for job in self._jobs}
            self._cancelled_job_ids.update(requested.intersection(known_ids))
            self._lazy_cancelled_job_ids.update(requested.difference(known_ids))
            for job in self._jobs:
                if job.id not in requested or job.status in ARCHIVABLE_JOB_STATUSES:
                    continue
                job.status = JobStatus.CANCELLED
                job.stage_label = "已取消"
                if reason:
                    job.message = str(reason)
                job.updated_at = utc_now()
                changed.append(job.to_dict())
                token = self._job_tokens.get(job.id)
                if token is not None:
                    tokens.append(token)
                elif job.id not in self._active_job_ids:
                    # Queued work has no processor to unwind, so its terminal
                    # state is safe to publish now. Active work is published by
                    # ``_run`` only after child processes have stopped.
                    callback_jobs.append(job)
            if changed:
                self._work_revision += 1
        for token in tokens:
            token.cancel()
        for job in callback_jobs:
            self._finish_streamed_job(job)
        return changed

    def cancel(self) -> None:
        with self._lock:
            active_ids = {
                job.id
                for job in self._jobs
                if job.status not in ARCHIVABLE_JOB_STATUSES
            }
            # A whole-queue stop also discards lazy loaders. Their durable
            # records are cancelled by the API, so restart cannot silently
            # resurrect the unconsumed tail of a large batch.
            self._scheduled_batches.clear()
            self._stream_paused = False
            self._lazy_cancelled_job_ids.clear()
            self._work_revision += 1
        self.cancel_jobs(active_ids)
        self._cancel.set()

    def begin_shutdown(self) -> None:
        """Reject any new queue work before the shutdown snapshot is taken."""

        self._shutdown_requested.set()

    def stop_and_wait(self, timeout_seconds: float = 15.0) -> bool:
        """Permanently stop this queue and confirm its worker has unwound.

        ``cancel`` terminates registered FFmpeg/TTS process trees through each
        active job token. Shutdown must additionally join the Python worker:
        a provider can still be unwinding after its child exits, and releasing
        the durable lease before that point would let another workstation run
        the same job concurrently.

        ``False`` is deliberately a safe failure. Callers must keep durable
        leases untouched and let them expire when the worker cannot be
        confirmed stopped within the deadline.
        """

        self.begin_shutdown()
        # Closing the UI or applying an update must not destroy work which has
        # never started. Cancel only the processor currently owning resources;
        # durable queued snapshots remain queued for the next Worker process.
        with self._lock:
            active_ids = set(self._active_job_ids)
        self.cancel_jobs(active_ids, reason="application shutdown")
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        current = threading.current_thread()

        while True:
            with self._lock:
                threads = tuple(
                    dict.fromkeys(
                        thread
                        for thread in (self._worker, self._retiring_worker)
                        if thread is not None
                    )
                )
            live_threads = [thread for thread in threads if thread.is_alive()]
            if not live_threads:
                break
            if current in live_threads:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # Join every known worker against one shared deadline. This also
            # covers the narrow retiring-worker hand-off window.
            live_threads[0].join(timeout=remaining)

        with self._lock:
            return not self._active_job_ids and not self._job_tokens

    def mark_shutdown_interrupted(
        self,
        job_ids: set[str] | tuple[str, ...] | list[str],
        *,
        reason: str,
    ) -> list[RenderJob]:
        """Convert shutdown-cancelled jobs only after the worker is stopped."""

        requested = {str(item) for item in job_ids if str(item)}
        with self._lock:
            live_worker = any(
                thread is not None and thread.is_alive()
                for thread in (self._worker, self._retiring_worker)
            )
            if live_worker or self._active_job_ids or self._job_tokens:
                raise RuntimeError("render worker has not stopped")
            changed: list[RenderJob] = []
            for job in self._jobs:
                if job.id not in requested or job.status != JobStatus.CANCELLED:
                    continue
                job.status = JobStatus.INTERRUPTED
                job.stage_label = "软件关闭，任务已中断"
                job.message = str(reason)
                job.updated_at = utc_now()
                changed.append(job)
            if changed:
                self._work_revision += 1
            return changed

    def _update(
        self, job: RenderJob, status: JobStatus, progress: float, label: str
    ) -> None:
        with self._lock:
            if job.id in self._cancelled_job_ids and status != JobStatus.CANCELLED:
                return
            job.status = status
            job.progress = max(0.0, min(1.0, progress))
            job.stage_label = label
            job.updated_at = utc_now()

    def _run(self) -> None:
        # Pull one queued item at a time. Drafts may be appended while a long
        # render is running; a one-time snapshot would leave those jobs parked
        # forever because ``start`` correctly refuses to launch a second worker.
        while True:
            if self._shutdown_requested.is_set():
                return
            if self._cancel.is_set():
                with self._lock:
                    queued = [item for item in self._jobs if item.status == JobStatus.QUEUED]
                for item in queued:
                    self._update(item, JobStatus.CANCELLED, item.progress, "已取消")
                return
            with self._lock:
                stream_paused = self._stream_paused
                retry_at = self._stream_retry_at
            if stream_paused:
                wait_seconds = max(0.0, retry_at - time.monotonic())
                if wait_seconds and self._cancel.wait(min(wait_seconds, 1.0)):
                    continue
                if wait_seconds > 1.0:
                    continue
                try:
                    self.resume_streams()
                except RuntimeError:
                    continue
            job, scheduled_batch, observed_revision = self._next_scheduled_work()
            if job is None:
                if scheduled_batch is not None and self._load_stream_window(
                    scheduled_batch
                ):
                    continue
                if scheduled_batch is not None:
                    with self._lock:
                        loader_exhausted = (
                            scheduled_batch not in self._scheduled_batches
                            or scheduled_batch.loader is None
                        )
                        loader_retry_at = scheduled_batch.loader_retry_at
                    if loader_exhausted:
                        continue
                    wait_seconds = max(0.0, loader_retry_at - time.monotonic())
                    if wait_seconds:
                        self._cancel.wait(min(wait_seconds, 1.0))
                    continue
                if self._retire_if_idle(observed_revision):
                    return
                continue
            platform = self._platforms.get(job.platform_id)
            if platform is None:
                try:
                    self._update(job, JobStatus.FAILED, 0.0, "平台配置缺失")
                    job.message = "找不到此任务对应的平台配置。"
                    self._finish_streamed_job(job)
                finally:
                    with self._lock:
                        self._active_job_ids.discard(job.id)
                continue
            try:
                self._update(job, JobStatus.PREFLIGHT, 0.02, "检查输入")
                token = CancellationToken()
                with self._lock:
                    self._job_tokens[job.id] = token
                    already_cancelled = job.id in self._cancelled_job_ids
                if already_cancelled:
                    token.cancel()
                with cancellation_scope(token):
                    output = self._processor(
                        job,
                        platform,
                        lambda status, progress, label: self._update(
                            job, status, progress, label
                        ),
                    )
                with self._lock:
                    cancelled_after_processor = job.id in self._cancelled_job_ids
                if cancelled_after_processor:
                    continue
                if job.job_kind == "preview":
                    job.preview_file = output
                    try:
                        job.preview_uri = Path(output).resolve().as_uri()
                    except (OSError, ValueError):
                        job.preview_uri = ""
                    self._update(
                        job,
                        JobStatus.AWAITING_APPROVAL,
                        1.0,
                        "样片待确认",
                    )
                else:
                    job.output_file = output
                    self._update(job, JobStatus.COMPLETED, 1.0, "已完成")
            except JobCancelledError:
                # ``cancel_jobs`` already made cancellation terminal.  Do not
                # create failure logs or allow the killed child to revive it.
                self._update(job, JobStatus.CANCELLED, job.progress, "已取消")
            except BaseException as error:  # keep the remaining queue alive
                job.message = f"{type(error).__name__}: {error}"
                traceback_text = traceback.format_exc()
                traceback_log = _persist_job_failure(job, error, traceback_text)
                primary_error_log = str(getattr(error, "error_log", "") or "")
                job.error_log = primary_error_log or traceback_log
                supplied_diagnostics = getattr(error, "failure_diagnostics", None)
                if (
                    isinstance(supplied_diagnostics, Mapping)
                    and supplied_diagnostics
                    and str(supplied_diagnostics.get("code") or "").strip()
                    and str(supplied_diagnostics.get("summary") or "").strip()
                ):
                    job.failure_diagnostics = dict(supplied_diagnostics)
                elif job.error_log:
                    job.failure_diagnostics = capture_failure_diagnostics(
                        job.error_log,
                        stage=job.stage_label or "job",
                    )
                print(traceback_text, end="")
                self._update(job, JobStatus.FAILED, job.progress, "生成失败")
            finally:
                with self._lock:
                    self._job_tokens.pop(job.id, None)
                try:
                    self._finish_streamed_job(job)
                finally:
                    with self._lock:
                        self._active_job_ids.discard(job.id)
