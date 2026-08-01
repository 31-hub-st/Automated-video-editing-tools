from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import secrets
import shutil
import socket
import sqlite3
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path
from typing import Any, TypeVar

from . import __version__
from .backup import HubBackupManager
from .catalog import (
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogPermissionError,
    CatalogRepository,
    installation_id_sha256,
    normalize_portable_device_config,
)
from .config import ApplicationState, MASKED_SECRET, SettingsRepository
from .component_updater import (
    ComponentPackageError,
    ComponentRepository,
    ComponentUpdater,
    validate_component_publication,
)
from .credentials import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    DEFAULT_EMPLOYEE_PASSWORD,
    DUMMY_PASSWORD_VERIFIER,
    hash_password,
    password_matches,
    validate_new_password,
)
from .failure_diagnostics import sanitize_failure_log
from .hub import (
    HubAuthenticationError,
    HubCatalogProxy,
    HubClient,
    HubConnectionError,
    HubError,
    HubRemoteError,
    HubServer,
    HubTextProvider,
)
from .jobs import JobQueue
from .library_service import LibraryService
from .maintenance import run_startup_cache_maintenance
from .models import AppSettings, BatchSpec, JobStatus, PlatformProfile, RenderJob
from .providers.text import create_text_provider
from .providers.tts import edge_tts_runtime_available
from .style_options import resolved_visual_style_presets
from .system import embedded_kokoro_available, system_snapshot
from .updater import UpdateManager, UpdateRepository


T = TypeVar("T")

# This is a memory/page window, never a production quantity cap.  Larger
# batches are fully recorded and consumed from the durable ledger in windows.
PRODUCTION_QUEUE_WINDOW = 4
PRODUCTION_STREAM_THRESHOLD = 128
QUEUE_SHUTDOWN_TIMEOUT_SECONDS = 15.0
RECORD_LEASE_SECONDS = 180
RECORD_LEASE_HEARTBEAT_SECONDS = 45.0


def _sanitize_hub_diagnostic_value(value: Any) -> Any:
    """Remove local paths and secrets from diagnostic data before Hub sync.

    Completed artifact paths remain explicit catalog fields so the originating
    employee computer can open its own output folder.  Free-form errors and
    media-selection diagnostics have no such operational need and must cross
    the client/Hub boundary only after sanitization.
    """

    if isinstance(value, str):
        return sanitize_failure_log(value)
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_hub_diagnostic_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_hub_diagnostic_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_hub_diagnostic_value(item) for item in value]
    return value


class StoryForgeApi:
    """Small, JSON-serializable bridge exposed to the embedded web interface."""

    def __init__(
        self,
        repository: SettingsRepository | None = None,
        queue: JobQueue | None = None,
        catalog: CatalogRepository | None = None,
        *,
        hub_listen_host: str | None = None,
        hub_listen_port: int | None = None,
    ) -> None:
        # pywebview recursively exposes every public attribute on ``js_api``.
        # Keeping the object graph private prevents it from walking through the
        # queue, repository and bound pipeline forever during bridge startup.
        self._repository = repository or SettingsRepository()
        self._backup_manager = HubBackupManager(self._repository.data_dir)
        self._hub_listen_host_override = (
            str(hub_listen_host).strip() if hub_listen_host is not None else None
        )
        self._hub_listen_port_override = hub_listen_port
        self._state = ApplicationState(self._repository)
        self._queue = queue or JobQueue()
        self._update_repository = UpdateRepository(
            self._repository.data_dir / "updates" / "published"
        )
        self._component_repository = ComponentRepository(
            self._repository.data_dir / "updates" / "components" / "published",
            app_version=__version__,
        )
        self._component_updater = ComponentUpdater(
            self._repository.data_dir / "components",
            app_version=__version__,
        )
        self._component_runtime_error = ""
        try:
            self._component_updater.activate_runtime()
        except (ComponentPackageError, OSError) as error:
            # Optional packs must never make the core desktop fail to start.
            self._component_runtime_error = str(error) or type(error).__name__
        self._hub_server: HubServer | None = None
        self._hub_client: HubClient | None = None
        self._hub_client_lock = threading.RLock()
        self._client_web_server: Any = None
        self._hub_error = ""
        self._device_sync_lock = threading.RLock()
        self._device_sync_stop = threading.Event()
        self._device_sync_wake = threading.Event()
        self._device_sync_thread: threading.Thread | None = None
        self._device_sync_status: dict[str, Any] = {
            "state": "idle",
            "last_success_at": "",
            "last_error": "",
            "device_id": self._state.settings.hub.device_id,
            "applied_revision_id": (
                self._state.settings.hub.applied_config_revision_id
            ),
        }
        if self._state.settings.hub.app_version != __version__:
            self._state.update_settings(
                {"hub": {"app_version": __version__}}
            )
        self._runtime_hub_mode = "embedded" if catalog is not None else "local"
        self._catalog = catalog or self._initialize_catalog_runtime()
        self._desktop_session_lock = threading.RLock()
        self._folder_dialog_lock = threading.Lock()
        # Candidate synthesis can load the embedded Kokoro model.  Local web
        # requests are handled by a ThreadingHTTPServer, so serialize previews
        # and keep them out of the FFmpeg render window on memory-constrained
        # employee PCs.
        self._heavy_resource_lock = threading.Lock()
        # Compatibility alias for the candidate-preview path and older tests.
        # The exact same lock is also held by PipelineRunner for each job, so
        # a queued render cannot start in the tiny window after the busy check.
        self._voice_preview_lock = self._heavy_resource_lock
        # No render can be active before the local pipeline is attached.  Use
        # this idle startup window to remove only reproducible crash leftovers
        # and bound local speech caches before another batch can fill C:.
        self._startup_cache_maintenance = run_startup_cache_maintenance(
            self._repository.data_dir
        )
        self._local_draft_files_lock = threading.RLock()
        self._local_draft_files: dict[str, dict[str, str]] = {}
        self._desktop_session_path = (
            self._repository.data_dir / "desktop-session.json"
        )
        self._desktop_session: dict[str, Any] = {}
        self._desktop_session_loaded = False
        self._library = LibraryService(
            self._catalog,
            lambda: self._state.settings,
            self._repository.data_dir,
            text_provider_factory=self._runtime_text_provider_factory,
            remote_text_provider=self._runtime_hub_mode == "client",
        )
        if self._runtime_hub_mode == "client" and self._hub_client is not None:
            remote_platforms = self._catalog.list_platforms().get("items", [])
            self._state.platforms = [
                PlatformProfile.from_dict(dict(item))
                for item in remote_platforms
                if isinstance(item, dict)
            ]
        elif self._runtime_hub_mode == "host":
            # Once a Hub catalog exists it is authoritative. Replaying a stale
            # settings.json profile here could erase branding uploaded by a
            # different computer before the host last restarted.
            remote_platforms = self._catalog.list_platforms().get("items", [])
            if remote_platforms:
                self._state.platforms = [
                    PlatformProfile.from_dict(dict(item))
                    for item in remote_platforms
                    if isinstance(item, dict)
                ]
            else:
                self._library.sync_platforms(self._state.platforms)
        else:
            self._library.sync_platforms(self._state.platforms)
        self._window: Any = None
        # The scheduled Local Worker has no pywebview window.  Its process
        # lifetime is owned by ``main._run_local_worker_service`` and can be
        # stopped safely through this callback after the HTTP response has
        # been returned to either the browser or desktop viewer.
        self._process_exit_callback: Callable[[], None] | None = None
        self._request_context = threading.local()
        self._recorded_artifacts: set[tuple[str, str, str]] = set()
        self._recorded_media_jobs: set[str] = set()
        self._job_materials: dict[str, list[dict[str, Any]]] = {}
        self._job_media_selection: dict[str, dict[str, Any]] = {}
        self._lease_lock = threading.RLock()
        self._leased_records: set[str] = set()
        # Record ids whose ownership was authoritatively lost while their
        # local worker was still unwinding.  One final terminal callback can
        # legitimately receive a lease conflict for these records; that
        # conflict acknowledges that another device/Hub is authoritative and
        # must not pause every younger batch forever.
        self._superseded_lease_records: set[str] = set()
        # record id -> next monotonic heartbeat time / consecutive failures.
        # Transport faults are retried; only an authoritative owner change
        # stops local rendering.
        self._lease_health: dict[str, dict[str, Any]] = {}
        # draft id -> (gate record id, durable production batch id).  The
        # durable id is filled only after planning has committed every task;
        # release decisions must never use the bounded in-memory queue window.
        self._draft_gate_leases: dict[str, tuple[str, str]] = {}
        self._lease_stop = threading.Event()
        self._lease_thread: threading.Thread | None = None
        self._shutdown_lock = threading.RLock()
        self._shutdown_in_progress = threading.Event()
        self._shutdown_job_ids: set[str] = set()
        self._queue.set_terminal_callback(self._sync_terminal_job_record)
        self._update_publish_lock = threading.RLock()
        self._update_publish_status: dict[str, Any] = {
            "state": "publisher_idle",
            "progress": 0.0,
            "message": "尚未发布软件更新。",
            "error": "",
        }
        self._update_manager = UpdateManager(
            current_version=__version__,
            data_dir=self._repository.data_dir,
            client_getter=self._update_hub_client,
            mode_getter=lambda: self._runtime_hub_mode,
            enabled_getter=lambda: bool(
                self._state.settings.hub.auto_update_enabled
            ),
            auto_download_getter=lambda: bool(
                self._state.settings.hub.auto_download_updates
            ),
            interval_minutes_getter=lambda: int(
                self._state.settings.hub.update_check_minutes
            ),
            rendering_busy_getter=self._queue.has_unfinished_work,
            heavy_resource_lock=self._heavy_resource_lock,
        )
        self._reconcile_interrupted_records()
        self._update_manager.start()
        self._ensure_device_sync()
        if self._runtime_hub_mode == "host":
            self._backup_manager.start_daily()

    def _update_hub_client(self) -> HubClient | None:
        """Return a live or short-lived client so update checks self-heal."""

        if self._runtime_hub_mode != "client":
            return None
        if self._hub_client is not None:
            return self._hub_client
        configured = self._state.settings.hub
        if not configured.access_token:
            return None
        # A temporary transport is deliberately not installed as the catalog
        # proxy. It lets software updates recover after a brief LAN outage
        # without making shared-data writes appear reconnected prematurely.
        return HubClient(
            configured.endpoint,
            configured.access_token,
            timeout_seconds=8,
        )

    def _runtime_text_provider_factory(self, config: Any) -> Any:
        """Keep cloud text credentials on Hub while media remains local.

        Client mode intentionally ignores every text endpoint, model and key in
        the employee computer's settings. HubTextProvider calls the
        authenticated Hub text service and fails visibly after bounded retries;
        it never silently replaces AI output with deterministic local rules.
        Host/local mode keeps the existing provider selection so the main
        computer can still be used for local validation and production.
        """

        if self._runtime_hub_mode == "client":
            return HubTextProvider(self._hub_client)
        return create_text_provider(config)

    def _device_runtime_capabilities(self) -> dict[str, Any]:
        """Return the small, non-secret capability projection sent to Hub."""

        configured = dict(self._state.settings.hub.capabilities)
        configured.update(
            {
                "device_config_sync": 1,
                "local_render": True,
                "local_tts": True,
                "local_subtitles": True,
            }
        )
        return configured

    def _ensure_host_local_worker_device(self, actor_user_id: str) -> dict[str, Any]:
        """Bind the Hub computer's localhost worker to the current browser actor.

        A Hub browser opened on the main computer still renders locally.  The
        public page receives only a synthetic discovery id; this method turns
        it into the installation's normal managed-device row immediately before
        the one-use worker ticket is issued.
        """

        if self._runtime_hub_mode != "host" or self._hub_server is None:
            raise RuntimeError("the Hub computer local worker is unavailable")
        actor = str(actor_user_id or "").strip()
        if not actor:
            raise PermissionError("an authenticated account is required")
        actor_user = self._catalog._web_user_by_id(actor)
        if not actor_user or str(actor_user.get("role") or "") != "admin":
            raise PermissionError(
                "只有管理员可让 Hub 主电脑承担本机生成任务；员工任务仍在各自电脑生成。"
            )
        registration = self._catalog.register_hub_device(
            {
                "installation_id_hash": installation_id_sha256(
                    self._state.settings.hub.installation_id
                ),
                "name": self._state.settings.hub.device_name
                or f"{socket.gethostname()} (Hub 本机制作)",
                "hostname": socket.gethostname(),
                "app_version": __version__,
                "os_name": platform.system(),
                "architecture": platform.machine(),
                "capabilities": self._device_runtime_capabilities(),
                "last_user_id": actor,
            },
            actor_user_id=actor,
        )
        device = dict(registration.get("device") or {})
        device_id = str(device.get("id") or "")
        if not device_id:
            raise RuntimeError("the Hub computer device registration failed")
        if (
            self._state.settings.hub.device_id != device_id
            or self._state.settings.hub.device_name != str(device.get("name") or "")
        ):
            self._state.update_settings(
                {
                    "hub": {
                        "device_id": device_id,
                        "device_name": str(device.get("name") or ""),
                    }
                }
            )
        return device

    def _current_device_id(self) -> str:
        hub = self._state.settings.hub
        return str(hub.device_id or hub.device_name or "local")

    def _hub_client_snapshot(self) -> HubClient | None:
        """Return the installed Hub transport as one synchronized value."""

        with self._hub_client_lock:
            return self._hub_client

    def _observe_hub_client(self, client: HubClient) -> HubClient:
        """Make authoritative credential rejection update Worker health."""

        client.authentication_failure_callback = (
            lambda error, source=client: self._mark_hub_authentication_failed(
                source, error
            )
        )
        return client

    def _mark_hub_authentication_failed(
        self,
        client: HubClient,
        error: BaseException,
        *,
        force: bool = False,
    ) -> bool:
        """Disconnect only the rejected installed credential.

        The persisted token is intentionally retained so diagnostics can
        explain what failed and a password login can rotate it in place. LAN
        timeouts never call this path and therefore do not turn a temporary
        outage into a forced re-enrolment.
        """

        message = str(error) or type(error).__name__
        with self._hub_client_lock:
            if not force and self._hub_client is not client:
                return False
            self._hub_client = None
            self._hub_error = message
        self._set_device_sync_status(
            state="authentication_required",
            last_error=message,
        )
        self._device_sync_wake.set()
        return True

    def _activate_hub_client(self, client: HubClient) -> dict[str, Any]:
        """Install one verified transport as the shared-data runtime."""

        client = self._observe_hub_client(client)
        try:
            identity = client.verify_identity()
            catalog = HubCatalogProxy(client)
            remote_platforms = catalog.list_platforms().get("items", [])
        except HubAuthenticationError as error:
            self._mark_hub_authentication_failed(client, error, force=True)
            raise
        library = LibraryService(
            catalog,
            lambda: self._state.settings,
            self._repository.data_dir,
            text_provider_factory=self._runtime_text_provider_factory,
            remote_text_provider=True,
        )
        self._catalog = catalog
        self._library = library
        self._runtime_hub_mode = "client"
        self._hub_error = ""
        self._state.platforms = [
            PlatformProfile.from_dict(dict(item))
            for item in remote_platforms
            if isinstance(item, Mapping)
        ]
        # Publish readiness only after every shared-data dependency has moved
        # to the newly verified transport.
        with self._hub_client_lock:
            self._hub_client = client
        return identity

    def _device_sync_status_value(self) -> dict[str, Any]:
        with self._device_sync_lock:
            status = dict(self._device_sync_status)
        status.update(
            {
                "enabled": bool(
                    self._runtime_hub_mode == "client"
                    and self._state.settings.hub.device_id
                ),
                "poll_seconds": (
                    10 if self._queue.is_rendering_busy() else 20
                ),
            }
        )
        return status

    def _set_device_sync_status(self, **patch: Any) -> None:
        with self._device_sync_lock:
            self._device_sync_status.update(patch)

    def _device_sync_once(self) -> dict[str, Any]:
        if self._runtime_hub_mode != "client":
            raise RuntimeError("device configuration sync requires client mode")
        hub = self._state.settings.hub
        if not hub.device_id:
            self._set_device_sync_status(
                state="legacy_token",
                last_error="当前连接方式已过期，请使用账号和密码重新登录。",
                device_id="",
            )
            return self._device_sync_status_value()
        client = self._hub_client
        if client is None:
            if not hub.access_token:
                raise RuntimeError("当前电脑尚未完成账号登录和设备登记。")
            client = HubClient(hub.endpoint, hub.access_token, timeout_seconds=8)
            self._activate_hub_client(client)

        self._set_device_sync_status(state="syncing", last_error="")
        heartbeat = client.heartbeat_device(
            app_version=__version__,
            capabilities=self._device_runtime_capabilities(),
        )
        heartbeat_device = heartbeat.get("device")
        if isinstance(heartbeat_device, Mapping):
            authoritative_name = str(heartbeat_device.get("name") or "").strip()
            if (
                authoritative_name
                and authoritative_name != self._state.settings.hub.device_name
            ):
                self._state.update_settings(
                    {"hub": {"device_name": authoritative_name}}
                )
        current_revision_id = str(
            self._state.settings.hub.applied_config_revision_id or ""
        )
        desired_response = client.get_desired_device_config(
            current_revision_id=current_revision_id
        )
        raw_desired = desired_response.get("desired")
        applied_revision_id = current_revision_id
        if isinstance(raw_desired, Mapping):
            desired = dict(raw_desired)
            revision_id = str(desired.get("id") or "")
            should_apply = bool(
                revision_id
                and (
                    bool(desired_response.get("needs_apply"))
                    or revision_id != current_revision_id
                )
            )
            if should_apply:
                config_hash = str(desired.get("config_hash") or "").casefold()
                try:
                    portable = normalize_portable_device_config(
                        desired.get("config")
                    )
                    patch = dict(portable)
                    patch["hub"] = {
                        "applied_config_revision_id": revision_id,
                        "applied_config_hash": config_hash,
                        "app_version": __version__,
                        "capabilities": self._device_runtime_capabilities(),
                    }
                    self._state.update_settings(patch)
                    client.acknowledge_device_config(
                        revision_id,
                        status="applied",
                        reported_config_hash=config_hash,
                    )
                    applied_revision_id = revision_id
                except (HubError, OSError, RuntimeError, TypeError, ValueError) as error:
                    try:
                        client.acknowledge_device_config(
                            revision_id,
                            status="failed",
                            message=(str(error) or type(error).__name__)[:500],
                        )
                    except (HubError, OSError, RuntimeError, TypeError, ValueError):
                        pass
                    raise
        now = datetime.now(timezone.utc).isoformat()
        self._set_device_sync_status(
            state="ready",
            last_success_at=now,
            last_error="",
            device_id=str(
                (
                    heartbeat_device.get("id")
                    if isinstance(heartbeat_device, Mapping)
                    else ""
                )
                or hub.device_id
            ),
            applied_revision_id=applied_revision_id,
        )
        return self._device_sync_status_value()

    def _ensure_device_sync(self) -> None:
        if self._runtime_hub_mode != "client":
            return
        with self._device_sync_lock:
            if self._device_sync_thread is not None and self._device_sync_thread.is_alive():
                self._device_sync_wake.set()
                return
            self._device_sync_stop.clear()
            self._device_sync_wake.set()
            self._device_sync_thread = threading.Thread(
                target=self._device_sync_loop,
                name="storyforge-device-sync",
                daemon=True,
            )
            self._device_sync_thread.start()

    def _device_sync_loop(self) -> None:
        while not self._device_sync_stop.is_set():
            self._device_sync_wake.clear()
            try:
                self._device_sync_once()
            except HubAuthenticationError as error:
                client = self._hub_client_snapshot()
                if client is not None:
                    self._mark_hub_authentication_failed(client, error)
            except (HubError, OSError, RuntimeError, TypeError, ValueError) as error:
                self._hub_error = str(error) or type(error).__name__
                self._set_device_sync_status(
                    state="offline",
                    last_error=self._hub_error,
                )
            interval = 10.0 if self._queue.is_rendering_busy() else 20.0
            self._device_sync_wake.wait(interval)

    def get_device_sync_status(self) -> dict[str, Any]:
        return self._ok(self._device_sync_status_value())

    def sync_device_config_now(self) -> dict[str, Any]:
        return self._guard(self._device_sync_once)

    @staticmethod
    def _ensure_default_owner(catalog: CatalogRepository) -> dict[str, Any]:
        users = catalog.list_users(include_inactive=False).get("items", [])
        active_admins = [
            item
            for item in users
            if item.get("active") and item.get("role") == "admin"
        ]
        owner = (
            active_admins[0]
            if active_admins
            else catalog.save_user(
                {
                    "username": DEFAULT_ADMIN_USERNAME,
                    "display_name": "StoryForge Owner",
                    "role": "admin",
                    "active": True,
                    "password_hash": hash_password(
                        validate_new_password(DEFAULT_ADMIN_PASSWORD)
                    ),
                }
            )
        )
        if not bool(owner.get("has_password")):
            owner = catalog.save_user(
                {
                    "id": owner["id"],
                    "username": owner["username"],
                    "display_name": owner.get("display_name", ""),
                    "role": "admin",
                    "active": True,
                    "expected_version": owner.get("row_version"),
                    "password_hash": hash_password(
                        validate_new_password(DEFAULT_ADMIN_PASSWORD)
                    ),
                }
            )
        return owner

    def _initialize_catalog_runtime(self) -> Any:
        """Select a local catalog, Hub host, or permission-aware Hub proxy."""

        hub = self._state.settings.hub
        mode = str(hub.mode or "local").strip().casefold()
        self._runtime_hub_mode = mode
        if mode == "client":
            try:
                if not hub.access_token:
                    raise ValueError("当前电脑尚未使用账号密码完成登记。")
                client = self._observe_hub_client(
                    HubClient(hub.endpoint, hub.access_token, timeout_seconds=8)
                )
                client.verify_identity()
                with self._hub_client_lock:
                    self._hub_client = client
                return HubCatalogProxy(client)
            except (HubError, OSError, ValueError, RuntimeError) as error:
                # Keep the desktop shell usable so the operator can repair its
                # settings, but never silently write shared work to this cache.
                self._hub_error = str(error) or type(error).__name__
                return CatalogRepository(
                    self._repository.data_dir / "storyforge-hub-offline-cache.sqlite3",
                    site_id="hub-offline-cache",
                    site_name="Hub 离线只读缓存",
                )

        catalog = CatalogRepository(
            self._repository.data_dir / "storyforge-catalog.sqlite3"
        )
        if mode != "host":
            self._runtime_hub_mode = "local"
            self._ensure_default_owner(catalog)
            return catalog

        try:
            token = str(hub.access_token or "").strip()
            if not token:
                token = secrets.token_urlsafe(36)
                self._state.update_settings({"hub": {"access_token": token}})
                hub = self._state.settings.hub
            owner = self._ensure_default_owner(catalog)
            attachment_root = self._repository.data_dir / "hub-attachments"
            attachment_root.mkdir(parents=True, exist_ok=True)
            self._hub_server = HubServer(
                catalog,
                {token: str(owner["id"])},
                host=self._hub_listen_host_override or hub.listen_host,
                port=(
                    int(self._hub_listen_port_override)
                    if self._hub_listen_port_override is not None
                    else int(hub.listen_port)
                ),
                data_root=self._repository.data_dir,
                attachment_root=attachment_root,
                update_repository=self._update_repository,
                component_repository=self._component_repository,
                text_provider_config_getter=lambda: self._state.settings.providers,
            ).start()
        except (HubError, OSError, ValueError, RuntimeError) as error:
            # The Hub computer can still work against its authoritative local
            # catalog while the UI clearly reports that clients cannot connect.
            self._hub_error = str(error) or type(error).__name__
        return catalog

    def _require_shared_catalog_online(self) -> None:
        if self._runtime_hub_mode == "client" and self._hub_client is None:
            detail = self._hub_error or "Hub 当前不可连接"
            raise RuntimeError(f"Hub 已断开，不能新建或修改共享数据：{detail}")

    @staticmethod
    def _hub_reference(value: str) -> tuple[str, str] | None:
        prefix = "hub://"
        raw = str(value or "")
        if not raw.startswith(prefix):
            return None
        root, separator, relative = raw[len(prefix) :].partition("/")
        relative = relative.replace("\\", "/").strip("/")
        if (
            not separator
            or root not in {"data", "attachments"}
            or not relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise ValueError("Hub 文件引用无效。")
        return root, relative

    def _resolve_shared_file(self, value: str, *, group: str = "shared") -> str:
        reference = self._hub_reference(value)
        if reference is None:
            return value
        root_alias, relative = reference
        if self._runtime_hub_mode == "host":
            root = (
                self._repository.data_dir
                if root_alias == "data"
                else self._repository.data_dir / "hub-attachments"
            ).resolve()
            candidate = (root / Path(relative)).resolve()
            if root not in candidate.parents or not candidate.is_file():
                return ""
            return str(candidate)
        if self._runtime_hub_mode != "client" or self._hub_client is None:
            return ""
        cache_root = (self._repository.data_dir / "hub-cache" / group).resolve()
        cache_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        destination = (cache_root / f"{digest}{Path(relative).suffix}").resolve()
        if cache_root not in destination.parents:
            raise ValueError("Hub 缓存路径超出安全目录。")
        metadata_path = destination.with_name(destination.name + ".hub-cache.json")
        remote_metadata: Mapping[str, Any] | None = None
        metadata_getter = getattr(self._hub_client, "file_metadata", None)
        if callable(metadata_getter):
            try:
                candidate_metadata = metadata_getter(root_alias, relative)
                if isinstance(candidate_metadata, Mapping):
                    remote_metadata = candidate_metadata
            except HubConnectionError:
                # An already verified cache remains usable during a temporary
                # Hub outage; an invalid cache will still fall through to GET.
                remote_metadata = None
        if not self._shared_cache_file_is_valid(
            destination,
            metadata_path,
            source=value,
            remote_metadata=remote_metadata,
        ):
            downloaded = self._hub_client.download_file(
                root_alias,
                relative,
                destination=destination,
            )
            if not isinstance(downloaded, Mapping):
                raise RuntimeError("Hub file download did not return cache metadata")
            self._write_shared_cache_metadata(
                metadata_path,
                source=value,
                size_bytes=int(downloaded.get("size_bytes") or 0),
                sha256=str(downloaded.get("sha256") or ""),
            )
        return str(destination)

    @classmethod
    def _shared_cache_file_is_valid(
        cls,
        destination: Path,
        metadata_path: Path,
        *,
        source: str,
        remote_metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        """Validate a cached Hub asset without downloading it again."""

        try:
            if not destination.is_file() or not metadata_path.is_file():
                return False
            size_bytes = destination.stat().st_size
            if size_bytes <= 0:
                return False
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, Mapping):
                return False
            expected_size = int(metadata.get("size_bytes") or 0)
            expected_sha256 = str(metadata.get("sha256") or "").casefold()
            if (
                int(metadata.get("schema_version") or 0) != 1
                or str(metadata.get("source") or "") != source
                or expected_size != size_bytes
                or len(expected_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_sha256
                )
            ):
                return False
            if remote_metadata is not None:
                remote_size = int(remote_metadata.get("size_bytes") or 0)
                remote_sha256 = str(
                    remote_metadata.get("sha256") or ""
                ).strip().casefold()
                if remote_size != expected_size:
                    return False
                if remote_sha256 and remote_sha256 != expected_sha256:
                    return False
            return cls._file_sha256(destination) == expected_sha256
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
            return False

    @staticmethod
    def _write_shared_cache_metadata(
        metadata_path: Path,
        *,
        source: str,
        size_bytes: int,
        sha256: str,
    ) -> None:
        clean_sha256 = str(sha256 or "").strip().casefold()
        if (
            size_bytes <= 0
            or len(clean_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in clean_sha256
            )
        ):
            raise RuntimeError("Hub file download returned invalid cache metadata")
        payload = {
            "schema_version": 1,
            "source": source,
            "size_bytes": int(size_bytes),
            "sha256": clean_sha256,
        }
        temporary = metadata_path.with_name(
            f".{metadata_path.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, metadata_path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _publish_shared_file(self, path: Path, relative: str) -> str:
        if self._runtime_hub_mode not in {"host", "client"}:
            return str(path.resolve())
        relative = relative.replace("\\", "/").strip("/")
        sha256 = self._file_sha256(path)
        if self._runtime_hub_mode == "client":
            if self._hub_client is None:
                raise RuntimeError("Hub 已断开，无法上传共享文件。")
            self._hub_client.upload_file(
                "attachments", relative, path, sha256=sha256
            )
        else:
            root = (self._repository.data_dir / "hub-attachments").resolve()
            destination = (root / Path(relative)).resolve()
            if root not in destination.parents:
                raise ValueError("Hub 文件目标超出安全目录。")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination != path.resolve():
                temporary = destination.with_suffix(destination.suffix + ".part")
                shutil.copy2(path, temporary)
                os.replace(temporary, destination)
        return f"hub://attachments/{relative}"

    def _hydrate_novel(self, novel: dict[str, Any]) -> dict[str, Any]:
        hydrated = dict(novel)
        shared_cover = str(hydrated.get("cover_path") or "")
        if self._hub_reference(shared_cover) is not None:
            local_cover = self._resolve_shared_file(shared_cover, group="covers")
            hydrated["shared_cover_path"] = shared_cover
            hydrated["cover_path"] = local_cover
            hydrated["cover_uri"] = Path(local_cover).as_uri() if local_cover else ""
        candidates: list[dict[str, Any]] = []
        for raw in hydrated.get("voice_candidates") or []:
            candidate = dict(raw)
            audio_path = str(candidate.get("audio_path") or "")
            if self._hub_reference(audio_path) is not None:
                local_audio = self._resolve_shared_file(audio_path, group="voice-previews")
                candidate["shared_audio_path"] = audio_path
                candidate["audio_path"] = local_audio
                candidate["audio_uri"] = Path(local_audio).as_uri() if local_audio else ""
            candidates.append(candidate)
        hydrated["voice_candidates"] = candidates
        return hydrated

    def _hydrate_platform(self, platform: dict[str, Any]) -> dict[str, Any]:
        """Resolve a shared platform logo into a safe, local display payload."""

        hydrated = dict(platform)
        shared_logo = str(hydrated.get("logo_path") or "").strip()
        hydrated["shared_logo_path"] = ""
        hydrated["logo_path"] = ""
        hydrated["logo_uri"] = ""
        if not shared_logo:
            return hydrated
        try:
            reference = self._hub_reference(shared_logo)
            if reference is not None:
                hydrated["shared_logo_path"] = shared_logo
                local_logo = self._resolve_shared_file(
                    shared_logo, group="platform-assets"
                )
            else:
                candidate = Path(shared_logo).expanduser()
                local_logo = str(candidate.resolve()) if candidate.is_file() else ""
            if local_logo and Path(local_logo).is_file():
                hydrated["logo_path"] = local_logo
                hydrated["logo_uri"] = Path(local_logo).resolve().as_uri()
        except (HubError, OSError, RuntimeError, ValueError):
            # A removed attachment or an offline cache must not break startup.
            # The persisted shared reference remains available for a later retry.
            pass
        return hydrated

    def _refresh_shared_platforms(self) -> None:
        """Refresh the in-memory profiles from the authoritative Hub catalog."""

        if self._runtime_hub_mode not in {"host", "client"}:
            return
        if self._runtime_hub_mode == "client" and self._hub_client is None:
            return
        try:
            values = self._catalog.list_platforms().get("items", [])
        except (HubError, OSError, RuntimeError, ValueError):
            return
        self._state.platforms = [
            PlatformProfile.from_dict(dict(item))
            for item in values
            if isinstance(item, Mapping)
        ]

    def _platform_for_local_render(
        self, platform: PlatformProfile
    ) -> PlatformProfile:
        """Resolve Hub branding while keeping the shared profile authoritative."""

        hydrated = self._hydrate_platform(platform.to_dict())
        render_profile = PlatformProfile.from_dict(platform.to_dict())
        render_profile.logo_path = str(hydrated.get("logo_path") or "")
        return render_profile

    def _hydrate_library_payload(self, value: dict[str, Any]) -> dict[str, Any]:
        payload = dict(value)
        payload["novels"] = [
            self._hydrate_novel(dict(item)) for item in payload.get("novels") or []
        ]
        payload["platforms"] = [
            self._hydrate_platform(dict(item))
            for item in payload.get("platforms") or []
            if isinstance(item, Mapping)
        ]
        return payload

    def _hydrate_service_result(self, value: dict[str, Any]) -> dict[str, Any]:
        result = dict(value)
        if isinstance(result.get("novel"), dict):
            result["novel"] = self._hydrate_novel(dict(result["novel"]))
        return result

    def _share_saved_cover(self, novel_id: str, local_path: str) -> dict[str, Any]:
        path = Path(local_path)
        if not path.is_file():
            return self._hydrate_novel(self._library.novel_for_ui(novel_id))
        safe_id = hashlib.sha256(novel_id.encode("utf-8")).hexdigest()[:32]
        relative = f"covers/{safe_id}{path.suffix.casefold()}"
        shared = self._publish_shared_file(path, relative)
        if shared != str(path.resolve()):
            self._catalog.save_novel({"id": novel_id, "cover_path": shared})
        return self._hydrate_novel(self._library.novel_for_ui(novel_id))

    def _prepare_platform_profile(
        self, value: dict[str, Any]
    ) -> PlatformProfile:
        """Merge legacy edits and publish a selected logo when Hub is active."""

        incoming = dict(value)
        existing = self._state.platform_by_id(str(incoming.get("id") or ""))
        if existing is not None:
            merged = existing.to_dict()
            merged.update(incoming)
            incoming = merged
        profile = PlatformProfile.from_dict(incoming)
        logo_value = str(profile.logo_path or "").strip()
        if not logo_value:
            profile.logo_path = ""
            return profile
        reference = self._hub_reference(logo_value)
        if reference is not None:
            return profile
        logo = Path(logo_value).expanduser()
        if not logo.is_file():
            profile.logo_path = ""
            return profile
        safe_id = hashlib.sha256(profile.id.encode("utf-8")).hexdigest()[:32]
        profile.logo_path = self._publish_shared_file(
            logo.resolve(),
            f"platform-assets/{safe_id}{logo.suffix.casefold()}",
        )
        return profile

    def _share_voice_candidates(
        self, novel_id: str, value: dict[str, Any]
    ) -> dict[str, Any]:
        shared_candidates: list[dict[str, Any]] = []
        novel_key = hashlib.sha256(novel_id.encode("utf-8")).hexdigest()[:24]
        for raw in value.get("candidates") or []:
            candidate = dict(raw)
            audio = Path(str(candidate.get("audio_path") or ""))
            if audio.is_file():
                voice_key = hashlib.sha256(
                    (
                        str(candidate.get("provider") or "")
                        + ":"
                        + str(candidate.get("voice_id") or "")
                        + ":"
                        + str(candidate.get("narration_wpm") or "")
                        + ":"
                        + str(candidate.get("cache_key") or "")
                    ).encode("utf-8")
                ).hexdigest()[:20]
                candidate["audio_path"] = self._publish_shared_file(
                    audio,
                    f"voice-previews/{novel_key}/{voice_key}{audio.suffix.casefold()}",
                )
                candidate["audio_uri"] = ""
            shared_candidates.append(candidate)
        self._catalog.save_novel_voice_state(
            novel_id,
            {"voice_candidates": shared_candidates},
        )
        hydrated_novel = self._hydrate_novel(self._library.novel_for_ui(novel_id))
        return {
            **dict(value),
            "candidates": hydrated_novel.get("voice_candidates", []),
            "novel": hydrated_novel,
        }

    @staticmethod
    def _local_ipv4() -> str:
        """Return a LAN-reachable IPv4 address for the Hub status card.

        ``gethostbyname`` can prefer a VPN, benchmark, or desktop-sandbox
        adapter.  Enumerate every hostname address and prefer RFC1918 LAN
        ranges so another production computer receives the usable address.
        """

        private_networks = (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
        benchmark_network = ipaddress.ip_network("198.18.0.0/15")
        candidates: list[ipaddress.IPv4Address] = []
        try:
            values = socket.getaddrinfo(
                socket.gethostname(),
                None,
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
            )
        except OSError:
            values = []
        for value in values:
            try:
                address = ipaddress.ip_address(value[4][0])
            except (IndexError, TypeError, ValueError):
                continue
            if not isinstance(address, ipaddress.IPv4Address):
                continue
            if (
                address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or address in benchmark_network
                or address in candidates
            ):
                continue
            candidates.append(address)
        for address in candidates:
            if any(address in network for network in private_networks):
                return str(address)
        if candidates:
            return str(candidates[0])
        return "127.0.0.1"

    def _hub_status_value(self) -> dict[str, Any]:
        configured = self._state.settings.hub
        online = bool(
            self._runtime_hub_mode in {"local", "embedded"}
            or self._hub_client is not None
            or (self._hub_server is not None and self._hub_server.is_running)
        )
        endpoint = configured.endpoint
        if self._hub_server is not None and self._hub_server.is_running:
            endpoint = f"http://{self._local_ipv4()}:{self._hub_server.address[1]}"
        public_mode = (
            "local" if self._runtime_hub_mode == "embedded" else self._runtime_hub_mode
        )
        running = bool(self._hub_server is not None and self._hub_server.is_running)
        client_web_running = bool(
            self._client_web_server is not None
            and self._client_web_server.is_running
        )
        connected = bool(public_mode != "client" or self._hub_client is not None)
        if online:
            if public_mode == "host":
                message = f"本机 Hub 正在运行，局域网地址：{endpoint}"
            elif public_mode == "client":
                message = f"已连接 StoryForge Hub：{endpoint}"
            else:
                message = "本机独立运行，不同步其他电脑。"
        else:
            message = self._hub_error or "Hub 尚未连接，请重新登录或检查主电脑网络。"
        return {
            "configured_mode": configured.mode,
            "runtime_mode": self._runtime_hub_mode,
            # Stable aliases consumed by the desktop UI contract.
            "mode": public_mode,
            "online": online,
            "connected": connected,
            "running": running,
            "client_web_running": client_web_running,
            "client_web_url": (
                self._client_web_server.base_url if client_web_running else ""
            ),
            "status": "ready" if online else "offline",
            "message": message,
            "endpoint": endpoint,
            "account_username": configured.account_username,
            "device_id": configured.device_id,
            "device_name": configured.device_name,
            "has_access_token": bool(configured.access_token),
            # Retained as false-only compatibility fields for older clients.
            "share_previews": False,
            "share_narration": False,
            "error": self._hub_error,
            "restart_required": configured.mode != self._runtime_hub_mode,
            "device_sync": self._device_sync_status_value(),
            "backup": self._backup_status_value(),
        }

    def _backup_status_value(self, *, include_error: bool = False) -> dict[str, Any]:
        status = self._backup_manager.status(include_error=include_error)
        status["available"] = self._runtime_hub_mode == "host"
        if self._runtime_hub_mode != "host":
            status["enabled"] = False
            status["running"] = False
        return status

    @staticmethod
    def _backup_snapshot_value(value: Mapping[str, Any]) -> dict[str, Any]:
        """Remove host filesystem paths before a backup result enters RPC."""

        allowed = (
            "id",
            "reason",
            "created_at",
            "catalog_schema_version",
            "settings_schema_version",
            "file_count",
            "total_size_bytes",
            "archive_size_bytes",
            "manifest_sha256",
            "content_sha256",
            "created",
            "deduplicated",
            "duplicate_of",
            "stored_reason",
            "metadata",
            "valid",
            "error",
        )
        result = {key: deepcopy(value[key]) for key in allowed if key in value}
        raw_path = str(value.get("path") or "")
        result["filename"] = Path(raw_path).name if raw_path else ""
        return result

    def _attach_window(self, window: Any) -> None:
        self._window = window

    def _load_desktop_session(self) -> None:
        with self._desktop_session_lock:
            if self._desktop_session_loaded:
                return
            self._desktop_session_loaded = True
            try:
                value = json.loads(
                    self._desktop_session_path.read_text(encoding="utf-8")
                )
                self._desktop_session = dict(value) if isinstance(value, Mapping) else {}
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                self._desktop_session = {}

    def _persist_desktop_session(self) -> None:
        with self._desktop_session_lock:
            if not self._desktop_session:
                try:
                    self._desktop_session_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return
            self._desktop_session_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._desktop_session_path.with_suffix(".json.partial")
            temporary.write_text(
                json.dumps(
                    self._desktop_session,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, self._desktop_session_path)

    def _clear_desktop_session(self) -> None:
        with self._desktop_session_lock:
            self._desktop_session = {}
            self._desktop_session_loaded = True
        self._persist_desktop_session()

    def _desktop_session_payload(self) -> dict[str, Any] | None:
        self._load_desktop_session()
        with self._desktop_session_lock:
            saved = dict(self._desktop_session)
        try:
            expires_at = float(saved.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if not saved.get("user_id") or expires_at <= datetime.now(timezone.utc).timestamp():
            self._clear_desktop_session()
            return None
        try:
            if self._runtime_hub_mode == "client":
                if self._hub_client is None:
                    raise RuntimeError("Hub is offline")
                identity = self._hub_client.get_device_session()
                user = dict(identity.get("user") or {})
                permissions = [str(item) for item in identity.get("permissions") or []]
                device = dict(identity.get("device") or {})
            else:
                user = self._catalog._web_user_by_id(str(saved["user_id"]))
                if not user:
                    raise RuntimeError("account no longer exists")
                permissions_result = self._catalog.get_effective_permissions(
                    str(user["id"])
                )
                permissions = [
                    name
                    for name, allowed in dict(
                        permissions_result.get("effective") or {}
                    ).items()
                    if allowed
                ]
                device = {}
            if (
                not bool(user.get("active"))
                or str(user.get("id") or "") != str(saved["user_id"])
                or int(user.get("row_version") or 1)
                != int(saved.get("row_version") or 1)
            ):
                raise RuntimeError("account session changed")
        except (HubError, OSError, RuntimeError, TypeError, ValueError):
            self._clear_desktop_session()
            return None
        return {
            "authenticated": True,
            "user": {
                "id": str(user["id"]),
                "username": str(user.get("username") or ""),
                "display_name": str(user.get("display_name") or ""),
                "role": str(user.get("role") or "producer"),
                "active": True,
            },
            "permissions": sorted(set(permissions)),
            "expires_at": datetime.fromtimestamp(
                expires_at, timezone.utc
            ).isoformat(),
            "password_configured": True,
            "must_set_password": False,
            "host_name": str(
                device.get("name")
                or self._state.settings.hub.device_name
                or socket.gethostname()
            ),
            "capabilities": {
                "desktop": True,
                "client_local": self._runtime_hub_mode == "client",
                "media_rpc": True,
                "password_change": False,
                "logout": True,
            },
        }

    def _connection_profile_endpoint(self) -> str:
        """Return the packaged Hub endpoint only for an unbound installation."""

        hub = self._state.settings.hub
        # A registered device keeps its saved endpoint.  Legacy installations
        # may still have an old account token but no device id; those must read
        # the packaged profile so the next password login can replace the old
        # credential with a proper device registration.
        if str(hub.device_id or "").strip():
            return ""
        try:
            from .connection_profile import load_connection_profile

            profile = load_connection_profile()
        except ImportError:
            profile = {}
        if isinstance(profile, Mapping):
            return str(profile.get("endpoint") or "").strip()
        return str(getattr(profile, "endpoint", "") or "").strip()

    def _enroll_client_with_password(
        self,
        endpoint: str,
        username: str,
        password: str,
        device_name: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any], AppSettings]:
        """Enroll once and persist only the DPAPI-protected device token."""

        clean_endpoint = str(endpoint or "").strip()
        clean_username = str(username or "").strip()
        clean_password = str(password or "")
        clean_device_name = str(device_name or "").strip() or socket.gethostname()
        if not clean_endpoint or not clean_username or not clean_password:
            raise ValueError("请填写主电脑地址、员工账号和密码。")
        enrolled = HubClient.enroll_device(
            clean_endpoint,
            clean_username,
            clean_password,
            clean_device_name,
            installation_id=self._state.settings.hub.installation_id,
            app_version=__version__,
            capabilities=self._device_runtime_capabilities(),
            hostname=socket.gethostname(),
            os_name=platform.system(),
            architecture=platform.machine(),
            timeout_seconds=15,
        )
        token = str(enrolled.get("token") or "")
        client = HubClient(clean_endpoint, token, timeout_seconds=8)
        updated = self._state.update_settings(
            {
                "hub": {
                    "mode": "client",
                    "endpoint": client.base_url,
                    "access_token": token,
                    "account_username": clean_username,
                    "device_id": str(enrolled.get("device_id") or ""),
                    "device_name": str(
                        enrolled.get("device_name") or clean_device_name
                    ),
                    "app_version": __version__,
                    "capabilities": self._device_runtime_capabilities(),
                    "applied_config_revision_id": "",
                    "applied_config_hash": "",
                }
            }
        )
        try:
            identity = self._activate_hub_client(client)
        except (HubError, OSError, RuntimeError, TypeError, ValueError) as error:
            # Enrollment rotates every token for this stable installation.  If
            # the follow-up identity/bootstrap call then fails, the previously
            # installed client necessarily holds a revoked token.  Keeping it
            # would make the next password login take the verification path
            # and repeat a misleading HTTP 401 until the application restarts.
            # Leave the freshly issued token persisted, but force the next
            # attempt through enrollment so the desktop can recover in-place.
            self._hub_client = None
            self._hub_error = str(error) or type(error).__name__
            raise
        self._update_manager.start()
        self._update_manager.wake()
        self._ensure_device_sync()
        return dict(enrolled), dict(identity), updated

    def _worker_autostart_after_login(self) -> dict[str, Any]:
        """Best-effort setup: a Task Scheduler failure must not reject login."""

        if self._runtime_hub_mode != "client":
            return {
                "state": "not_required",
                "automatic": False,
                "message": "主电脑或独立模式不需要员工后台制作服务。",
            }
        if bool(getattr(sys, "frozen", False)) and self._client_web_server is None:
            ui_root = getattr(self, "_desktop_ui_root", None)
            if ui_root is None:
                return {
                    "state": "warning",
                    "automatic": True,
                    "message": "账号已登录，但当前桌面未能接管本机制作服务。",
                    "fix": "请关闭后重新打开 StoryForge；当前不会启动第二套制作队列。",
                }
            try:
                self._ensure_local_worker_server(
                    Path(ui_root).resolve(strict=True),
                    serve_ui=False,
                    use_port_override=False,
                )
            except (OSError, RuntimeError, ValueError) as error:
                return {
                    "state": "warning",
                    "automatic": True,
                    "message": "账号已登录，但当前桌面未能接管本机制作服务。",
                    "fix": "请关闭后重新打开 StoryForge；当前不会启动第二套制作队列。",
                    "technical": (str(error) or type(error).__name__)[:500],
                }
        try:
            from .worker import ensure_local_worker_autostart

            return dict(ensure_local_worker_autostart())
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            return {
                "state": "warning",
                "automatic": True,
                "message": "账号已登录，当前桌面可以继续使用；但后台制作服务未能自动启用。",
                "fix": "请重启 StoryForge 再试；若仍失败，把诊断报告交给管理员。",
                "technical": (str(error) or type(error).__name__)[:500],
            }

    def desktop_session_status(self) -> dict[str, Any]:
        return self._ok(self._desktop_session_payload() or {"authenticated": False})

    def desktop_login(self, username: str, password: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            clean_username = str(username or "").strip()
            clean_password = str(password or "")
            profile_endpoint = self._connection_profile_endpoint()
            configured_hub = self._state.settings.hub
            saved_username = str(configured_hub.account_username or "").strip()
            account_changed = bool(
                self._runtime_hub_mode == "client"
                and (
                    not saved_username
                    or saved_username.casefold() != clean_username.casefold()
                )
            )
            should_enroll = bool(
                self._runtime_hub_mode != "host"
                and (
                    not str(configured_hub.device_id or "").strip()
                    or account_changed
                    or (
                        self._runtime_hub_mode == "client"
                        and self._hub_client is None
                    )
                )
                and (self._runtime_hub_mode == "client" or profile_endpoint)
            )
            if not clean_username or not clean_password or len(clean_username) > 200:
                raise ValueError("请输入账号和密码。")
            if len(clean_password) > 512:
                raise ValueError("账号或密码不正确。")
            if should_enroll:
                endpoint = profile_endpoint or str(
                    self._state.settings.hub.endpoint or ""
                ).strip()
                if not endpoint:
                    raise RuntimeError(
                        "这台电脑尚未配置主电脑地址，请让管理员重新发送完整安装包。"
                    )
                enrolled, _identity, _updated = self._enroll_client_with_password(
                    endpoint,
                    clean_username,
                    clean_password,
                    socket.gethostname(),
                )
                user = dict(enrolled.get("user") or {})
            elif self._runtime_hub_mode == "client":
                if self._hub_client is None:
                    raise RuntimeError("无法连接 StoryForge Hub，请检查主电脑网络。")
                try:
                    identity = self._hub_client.verify_account_password(
                        clean_username, clean_password
                    )
                    user = dict(identity.get("user") or {})
                except HubAuthenticationError as error:
                    if error.code != "login_failed":
                        raise
                    # Older clients could persist an account label that no
                    # longer matched the user bound to the bearer token.  The
                    # Hub deliberately reports both a bad password and that
                    # identity mismatch as login_failed.  A single password
                    # enrollment is safe for both cases: invalid credentials
                    # fail before device/token mutation, while valid
                    # credentials atomically rebind this installation.
                    endpoint = str(
                        self._state.settings.hub.endpoint or ""
                    ).strip()
                    if not endpoint:
                        raise RuntimeError(
                            "这台电脑尚未配置主电脑地址，请让管理员重新发送完整安装包。"
                        )
                    enrolled, _identity, _updated = (
                        self._enroll_client_with_password(
                            endpoint,
                            clean_username,
                            clean_password,
                            socket.gethostname(),
                        )
                    )
                    user = dict(enrolled.get("user") or {})
            else:
                candidate = self._catalog._web_user_by_username(clean_username)
                verifier = str((candidate or {}).get("password_hash") or "")
                matched = password_matches(
                    clean_password, verifier or DUMMY_PASSWORD_VERIFIER
                )
                if not candidate or not bool(candidate.get("active")) or not matched:
                    raise ValueError("账号或密码不正确。")
                user = dict(candidate)
                self._catalog.register_hub_device(
                    {
                        "installation_id_hash": installation_id_sha256(
                            self._state.settings.hub.installation_id
                        ),
                        "name": self._state.settings.hub.device_name
                        or socket.gethostname(),
                        "hostname": socket.gethostname(),
                        "app_version": __version__,
                        "os_name": platform.system(),
                        "architecture": platform.machine(),
                        "capabilities": self._device_runtime_capabilities(),
                        "last_user_id": str(user["id"]),
                    },
                    actor_user_id=str(user["id"]),
                )
            expires_at = datetime.now(timezone.utc).timestamp() + 30 * 24 * 3600
            with self._desktop_session_lock:
                self._desktop_session_loaded = True
                self._desktop_session = {
                    "user_id": str(user["id"]),
                    "username": str(user.get("username") or ""),
                    "role": str(user.get("role") or "producer"),
                    "row_version": int(user.get("row_version") or 1),
                    "expires_at": expires_at,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            self._persist_desktop_session()
            payload = self._desktop_session_payload()
            if payload is None:
                raise RuntimeError("登录会话建立失败。")
            worker_autostart = self._worker_autostart_after_login()
            payload["worker_autostart"] = worker_autostart
            if worker_autostart.get("state") == "warning":
                payload["notices"] = [
                    {
                        "kind": "warning",
                        "code": "worker_autostart_failed",
                        "message": str(worker_autostart.get("message") or ""),
                        "fix": str(worker_autostart.get("fix") or ""),
                    }
                ]
            return payload

        return self._guard(operation)

    def desktop_logout(self) -> dict[str, Any]:
        self._clear_desktop_session()
        return self._ok({"authenticated": False})

    @contextmanager
    def _web_actor_scope(self, actor_user_id: str):
        """Bind a browser actor to only the current request thread."""

        previous = getattr(self._request_context, "actor_user_id", "")
        self._request_context.actor_user_id = str(actor_user_id or "")
        try:
            yield
        finally:
            self._request_context.actor_user_id = previous

    def _current_web_actor(self) -> str:
        scoped = str(getattr(self._request_context, "actor_user_id", "") or "")
        if scoped:
            return scoped
        self._load_desktop_session()
        with self._desktop_session_lock:
            try:
                expires_at = float(self._desktop_session.get("expires_at") or 0)
            except (TypeError, ValueError):
                expires_at = 0
            if expires_at > datetime.now(timezone.utc).timestamp():
                return str(self._desktop_session.get("user_id") or "")
        return ""

    def _can_manage_all_jobs(self, actor_user_id: str) -> bool:
        if not actor_user_id:
            return True
        permissions = self._catalog.get_effective_permissions(actor_user_id)
        effective = dict(permissions.get("effective") or {})
        return any(
            bool(effective.get(permission))
            for permission in ("jobs.retry_all", "drafts.manage_all", "hub.manage")
        )

    def _require_job_record_access(self, record_id: str) -> dict[str, Any]:
        record = self._catalog.get_record(str(record_id))
        actor_user_id = self._current_web_actor()
        if (
            actor_user_id
            and not self._can_manage_all_jobs(actor_user_id)
            and str(record.get("created_by_user_id") or "") != actor_user_id
        ):
            raise PermissionError("只能归档或恢复自己创建的制作任务。")
        return record

    def _batch_records(self, batch_id: str) -> list[dict[str, Any]]:
        """Return and authorize every durable task record in one batch."""

        normalized = str(batch_id or "").strip()
        if not normalized:
            raise ValueError("batch_id is required")
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._catalog.list_records(
                batch_id=normalized,
                trashed=None,
                limit=500,
                offset=offset,
            )
            items = [
                dict(item)
                for item in page.get("items") or []
                if str(item.get("job_id") or "")
            ]
            records.extend(items)
            offset += len(page.get("items") or [])
            if offset >= int(page.get("total") or 0) or not page.get("items"):
                break
        if not records:
            raise LookupError("production batch not found")
        for record in records:
            self._require_job_record_access(str(record.get("id") or ""))
        return records

    def _archived_jobs_page(
        self, options: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        value = dict(options or {})
        limit = min(200, max(1, int(value.get("limit") or 50)))
        offset = max(0, int(value.get("offset") or 0))
        actor_user_id = self._current_web_actor()
        created_by = (
            actor_user_id
            if actor_user_id and not self._can_manage_all_jobs(actor_user_id)
            else None
        )
        page = self._catalog.list_archived_jobs(
            created_by_user_id=created_by,
            limit=limit,
            offset=offset,
        )
        page["items"] = self._jobs_with_batch_summaries(page.get("items") or [])
        return page

    def _jobs_with_batch_summaries(
        self, jobs: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Decorate queue windows with complete durable-batch aggregates."""

        items = [dict(item) for item in jobs]
        batch_ids = list(
            dict.fromkeys(
                str(item.get("batch_id") or "").strip()
                for item in items
                if str(item.get("batch_id") or "").strip()
            )
        )
        summaries: dict[str, dict[str, Any]] = {}
        if batch_ids:
            try:
                response = self._catalog.get_production_batch_summaries(batch_ids)
                raw = response.get("items") or {}
                if isinstance(raw, Mapping):
                    summaries = {
                        str(key): dict(value)
                        for key, value in raw.items()
                        if isinstance(value, Mapping)
                    }
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                # Rolling upgrades can briefly pair a new client with an old
                # Hub.  Keep the list contract usable and let the UI fall back
                # to the model's persisted total fields.
                summaries = {}
        for item in items:
            batch_id = str(item.get("batch_id") or "")
            summary = summaries.get(batch_id)
            if summary is not None:
                item["batch_summary"] = summary
                item["batch_total_count"] = max(
                    int(item.get("batch_total_count") or 0),
                    int(summary.get("total") or 0),
                )
        return items

    def _queue_jobs(self) -> list[dict[str, Any]]:
        return self._jobs_with_batch_summaries(self._queue.list_jobs())

    def _archived_jobs(self) -> list[dict[str, Any]]:
        """Compatibility list for older desktop shells.

        Current clients call :meth:`get_archived_jobs` with pagination options,
        so a large production history is never loaded into the film strip at
        once.
        """

        page = self._archived_jobs_page({"limit": 200, "offset": 0})
        return [dict(item) for item in page.get("items") or []]

    def _ensure_local_worker_server(
        self,
        ui_root: Path,
        *,
        serve_ui: bool,
        use_port_override: bool,
    ) -> Any:
        """Start the fixed-port localhost media worker for this installation."""

        from .web import ClientLocalWebServer
        from .worker import LOCAL_WORKER_DEFAULT_PORT, LOCAL_WORKER_DISCOVERY_PORTS

        if self._client_web_server is not None:
            return self._client_web_server
        requested_port = (
            int(self._hub_listen_port_override)
            if use_port_override and self._hub_listen_port_override is not None
            else LOCAL_WORKER_DEFAULT_PORT
        )
        candidate_ports = (
            (requested_port,)
            if use_port_override and self._hub_listen_port_override is not None
            else tuple(dict.fromkeys((requested_port, *LOCAL_WORKER_DISCOVERY_PORTS)))
        )
        server = None
        last_error: OSError | None = None
        for candidate_port in candidate_ports:
            candidate = ClientLocalWebServer(
                self,
                ui_root=ui_root,
                upload_root=self._repository.data_dir / "client-web-uploads",
                port=candidate_port,
                serve_ui=serve_ui,
            )
            try:
                server = candidate.start()
                break
            except OSError as error:
                last_error = error
        if server is None:
            assert last_error is not None
            raise last_error
        self._client_web_server = server
        return server

    def _enable_web_access(self, ui_root: str | Path) -> dict[str, Any]:
        """Start the authenticated browser surface for this runtime.

        Hub hosts reuse their LAN listener and keep media RPC disabled. An
        enrolled rendering client gets a separate loopback-only listener whose
        API owns this workstation's local queue and media pipeline.
        """

        root = Path(ui_root).resolve(strict=True)
        if self._runtime_hub_mode == "client":
            if self._hub_client is None or not self._state.settings.hub.device_id:
                raise RuntimeError(
                    "The rendering computer must be enrolled with a Hub account first."
                )
            self._ensure_local_worker_server(
                root, serve_ui=True, use_port_override=True
            )
            return {
                "url": self._client_web_server.base_url,
                "local_url": self._client_web_server.base_url,
                "mode": "client_local",
                "loopback_only": True,
            }

        if self._runtime_hub_mode != "host" or self._hub_server is None:
            raise RuntimeError(
                "网页端需要本机设为 StoryForge Hub 主机，并创建可登录的管理员或员工账号。"
            )
        self._hub_server.attach_web_application(
            self,
            ui_root=root,
            upload_root=self._repository.data_dir / "web-uploads",
        )
        # The Hub computer is also allowed to produce.  Its browser discovers
        # this loopback-only worker exactly like every employee workstation,
        # so media never travels through the shared Hub HTTP surface.
        self._ensure_local_worker_server(
            root, serve_ui=False, use_port_override=False
        )
        return {
            "url": f"http://{self._local_ipv4()}:{self._hub_server.address[1]}",
            "local_url": self._hub_server.base_url,
            "mode": "host",
        }

    @staticmethod
    def _ok(data: Any = None, **extra: Any) -> dict[str, Any]:
        return {"ok": True, "data": data, **extra}

    @staticmethod
    def _error(error: BaseException) -> dict[str, Any]:
        return {"ok": False, "error": str(error) or type(error).__name__}

    def _guard(self, callback: Callable[[], T]) -> dict[str, Any]:
        try:
            return self._ok(callback())
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            return self._error(error)

    def get_bootstrap(self) -> dict[str, Any]:
        self._refresh_shared_platforms()
        archived_page = self._archived_jobs_page()
        settings = self._state.settings.to_dict(redact_secrets=True)
        settings["providers"]["has_text_api_key"] = bool(
            self._state.settings.providers.text_api_key
        )
        settings["providers"]["has_tts_api_key"] = bool(
            self._state.settings.providers.tts_api_key
        )
        settings["hub"]["has_access_token"] = bool(
            self._state.settings.hub.access_token
        )
        return self._ok(
            {
                "settings": settings,
                "platforms": [
                    self._hydrate_platform(item.to_dict())
                    for item in self._state.platforms
                ],
                "batches": [item.to_dict() for item in self._state.batches],
                "jobs": self._queue_jobs(),
                "queue_connection": self._queue.stream_status(),
                "archived_jobs": [
                    dict(item) for item in archived_page.get("items") or []
                ],
                "archived_jobs_total": int(archived_page.get("total") or 0),
                "system": system_snapshot(),
                "catalog": self._catalog.bootstrap_summary(),
                "hub_status": self._hub_status_value(),
                "backup_status": self._backup_status_value(),
                "update_status": self._update_status_value(),
                "visual_style_presets": resolved_visual_style_presets(),
                "production_presets": self._catalog.list_production_presets(
                    actor_user_id=self._current_web_actor() or None
                ),
            }
        )

    def get_local_runtime_snapshot(self) -> dict[str, Any]:
        """Return fresh, non-secret capabilities for the current workstation.

        The browser keeps running when the local service is restarted.  This
        lightweight endpoint lets it replace an old FFmpeg/Kokoro snapshot
        without reloading the entire library and production ledger.
        """

        providers = self._state.settings.to_dict(redact_secrets=True)["providers"]
        providers["has_text_api_key"] = bool(
            self._state.settings.providers.text_api_key
        )
        providers["has_tts_api_key"] = bool(
            self._state.settings.providers.tts_api_key
        )
        return self._ok(
            {
                "system": system_snapshot(),
                "providers": providers,
            }
        )

    def get_local_self_check(self) -> dict[str, Any]:
        """Run the same non-secret readiness check used by the local worker.

        This is intentionally separate from ``save_settings``.  Employee
        accounts may inspect this workstation's FFmpeg, H.264 encoder, free
        TTS engines and local media folders without receiving access to Hub,
        cloud credentials or any shared setting.
        """

        def operation() -> dict[str, Any]:
            # Import lazily: ``worker`` imports ``StoryForgeApi`` only through
            # the object it receives, and loading it at module import time
            # would otherwise create an unnecessary dependency cycle.
            from .worker import LocalWorkerGateway

            return LocalWorkerGateway(self).self_check()

        return self._guard(operation)

    def get_visual_style_presets(self) -> dict[str, Any]:
        """Return complete editable values for every bundled visual preset."""

        return self._ok(resolved_visual_style_presets())

    def get_production_presets(self) -> dict[str, Any]:
        """Return recipes owned by the member, or all recipes for an admin."""

        return self._guard(
            lambda: self._catalog.list_production_presets(
                actor_user_id=self._current_web_actor() or None
            )
        )

    def save_production_preset(self, value: dict[str, Any]) -> dict[str, Any]:
        """Save a complete recipe without content ids, secrets or local paths."""

        return self._guard(
            lambda: self._catalog.save_production_preset(
                dict(value),
                actor_user_id=self._current_web_actor() or None,
            )
        )

    def delete_production_preset(self, preset_id: str) -> dict[str, Any]:
        """Delete a personal recipe within the authenticated account scope."""

        return self._guard(
            lambda: self._catalog.delete_production_preset(
                str(preset_id),
                actor_user_id=self._current_web_actor() or None,
            )
        )

    def get_hub_status(self) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            client = self._hub_client_snapshot()
            if client is not None:
                try:
                    client.verify_identity()
                    self._hub_error = ""
                except HubAuthenticationError as error:
                    self._mark_hub_authentication_failed(client, error)
                except (HubError, OSError, ValueError, RuntimeError) as error:
                    # Keep a verified device transport installed through a
                    # temporary LAN/Hub outage. A later status/sync request can
                    # recover without forcing the employee to enter a password.
                    self._hub_error = str(error) or type(error).__name__
            return self._hub_status_value()

        return self._guard(operation)

    def get_hub_backup_status(self) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if self._runtime_hub_mode != "host":
                raise RuntimeError(
                    "Hub backups are available only on the StoryForge host computer."
                )
            return self._backup_status_value(include_error=True)

        return self._guard(operation)

    def list_hub_backups(self) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if self._runtime_hub_mode != "host":
                raise RuntimeError(
                    "Hub backups are available only on the StoryForge host computer."
                )
            items = [
                self._backup_snapshot_value(item)
                for item in self._backup_manager.list_snapshots(validate=True)
            ]
            return {
                "items": items,
                "total": len(items),
                "status": self._backup_status_value(include_error=True),
            }

        return self._guard(operation)

    def create_hub_backup(self) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if self._runtime_hub_mode != "host":
                raise RuntimeError(
                    "Hub backups are available only on the StoryForge host computer."
                )
            actor_user_id = self._current_web_actor()
            snapshot = self._backup_manager.create_snapshot(
                "manual",
                metadata={
                    "created_by_user_id": actor_user_id or "desktop-owner",
                    "app_version": __version__,
                },
            )
            return {
                "snapshot": self._backup_snapshot_value(snapshot),
                "status": self._backup_status_value(include_error=True),
            }

        return self._guard(operation)

    def _update_status_value(self) -> dict[str, Any]:
        status = self._update_manager.status()
        if self._runtime_hub_mode == "host":
            published = self._update_repository.get_manifest()
            with self._update_publish_lock:
                publisher_status = dict(self._update_publish_status)
            if publisher_status["state"] not in {"publishing", "publish_error"}:
                publisher_status = {
                    "state": "published" if published else "publisher_idle",
                    "progress": 1.0 if published else 0.0,
                    "message": (
                        f"已向制作电脑发布 {published['version']}。"
                        if published
                        else "尚未发布软件更新。"
                    ),
                    "error": "",
                }
            status.update(publisher_status)
            status.update(
                {
                    "published_update": published,
                    "available_version": (
                        str(published["version"]) if published else ""
                    ),
                    "release_notes": (
                        str(published["release_notes"]) if published else ""
                    ),
                    "published_at": (
                        str(published["published_at"]) if published else ""
                    ),
                }
            )
        return status

    def get_update_status(self) -> dict[str, Any]:
        return self._guard(self._update_status_value)

    def check_for_updates(self) -> dict[str, Any]:
        return self._guard(
            lambda: self._update_manager.check_now(
                auto_download=bool(
                    self._state.settings.hub.auto_download_updates
                )
            )
        )

    def download_update(self) -> dict[str, Any]:
        return self._guard(self._update_manager.download)

    def schedule_update_on_restart(self) -> dict[str, Any]:
        return self._guard(self._update_manager.schedule_on_restart)

    def restart_to_apply_update(self) -> dict[str, Any]:
        """Schedule a verified update and close the installed desktop safely.

        The window closes only after ``schedule_on_restart`` has verified the
        package and confirmed that rendering is idle.  Normal application
        shutdown then hands the package to the external updater, which installs
        it after this process exits and reopens StoryForge.
        """

        def operation() -> dict[str, Any]:
            window = self._window
            process_exit = self._process_exit_callback
            if window is None and process_exit is None:
                raise RuntimeError("当前 StoryForge 进程无法安全重启，请关闭后重新打开。")
            status = self._update_manager.schedule_on_restart()
            if bool(status.get("rendering_busy")):
                result = dict(status)
                result["exit_queued"] = False
                return result

            def request_process_exit() -> None:
                time.sleep(0.35)
                if process_exit is not None:
                    try:
                        process_exit()
                    except (OSError, RuntimeError):
                        pass
                    return
                if window is not None:
                    try:
                        window.destroy()
                    except (AttributeError, OSError, RuntimeError):
                        # The verified update remains scheduled, so a normal
                        # close still applies it without losing the package.
                        pass

            threading.Thread(
                target=request_process_exit,
                name="storyforge-update-restart",
                daemon=True,
            ).start()
            result = dict(status)
            result["exit_queued"] = True
            result["message"] = "StoryForge 正在安全退出，更新完成后会自动重新打开。"
            return result

        return self._guard(operation)

    def _attach_process_exit_callback(
        self, callback: Callable[[], None] | None
    ) -> None:
        """Attach the process owner used by a headless Local Worker."""

        self._process_exit_callback = callback

    def cancel_scheduled_update(self) -> dict[str, Any]:
        return self._guard(self._update_manager.cancel_schedule)

    def save_local_update_preferences(self, value: dict[str, Any]) -> dict[str, Any]:
        """Persist only this workstation's safe updater preferences.

        This deliberately does not reuse ``save_settings``: employee accounts
        must be able to control automatic checks on their own installation
        without gaining a general settings or Hub-management write surface.
        """

        def operation() -> dict[str, Any]:
            patch = dict(value or {})
            allowed = {
                "auto_update_enabled",
                "auto_download_updates",
                "update_check_minutes",
            }
            unexpected = sorted(set(patch) - allowed)
            if unexpected:
                raise ValueError("本机更新设置包含不允许修改的字段。")
            if "auto_update_enabled" in patch and not isinstance(
                patch["auto_update_enabled"], bool
            ):
                raise ValueError("自动检查更新必须是开启或关闭。")
            if "auto_download_updates" in patch and not isinstance(
                patch["auto_download_updates"], bool
            ):
                raise ValueError("自动下载更新必须是开启或关闭。")
            if "update_check_minutes" in patch:
                if isinstance(patch["update_check_minutes"], bool):
                    raise ValueError("更新检查间隔无效。")
                try:
                    interval = int(patch["update_check_minutes"])
                except (TypeError, ValueError) as error:
                    raise ValueError("更新检查间隔无效。") from error
                if interval not in {1, 2, 5, 10, 30}:
                    raise ValueError("更新检查间隔只能选择 1、2、5、10 或 30 分钟。")
                patch["update_check_minutes"] = interval
            updated = self._state.update_settings({"hub": patch})
            if updated.hub.auto_update_enabled:
                self._update_manager.start()
                self._update_manager.wake()
            else:
                self._update_manager.stop(timeout=0.5)
            settings = updated.to_dict(redact_secrets=True)
            settings["providers"]["has_text_api_key"] = bool(
                updated.providers.text_api_key
            )
            settings["providers"]["has_tts_api_key"] = bool(
                updated.providers.tts_api_key
            )
            settings["hub"]["has_access_token"] = bool(updated.hub.access_token)
            return {
                "settings": settings,
                "update_status": self._update_status_value(),
            }

        return self._guard(operation)

    def publish_update(
        self,
        package_path: str,
        version: str,
        release_notes: str = "",
    ) -> dict[str, Any]:
        """Publish one prebuilt, self-describing ZIP from the Hub computer."""

        def operation() -> dict[str, Any]:
            if self._runtime_hub_mode != "host" or self._hub_server is None:
                raise RuntimeError("只有正在运行的 StoryForge Hub 主机可以发布更新。")
            def progress(value: float, message: str) -> None:
                with self._update_publish_lock:
                    self._update_publish_status = {
                        "state": "publishing",
                        "progress": max(0.0, min(1.0, float(value))),
                        "message": str(message),
                        "error": "",
                    }

            progress(0.01, "准备发布软件更新…")
            try:
                manifest = self._update_repository.publish(
                    package_path,
                    version,
                    release_notes,
                    progress=progress,
                )
            except (OSError, RuntimeError, ValueError) as error:
                with self._update_publish_lock:
                    self._update_publish_status = {
                        "state": "publish_error",
                        "progress": 0.0,
                        "message": "软件更新发布失败。",
                        "error": str(error) or type(error).__name__,
                    }
                raise
            with self._update_publish_lock:
                self._update_publish_status = {
                    "state": "published",
                    "progress": 1.0,
                    "message": f"已向制作电脑发布 {manifest['version']}。",
                    "error": "",
                }
            return {
                "manifest": manifest,
                "update_status": self._update_status_value(),
            }

        return self._guard(operation)

    def clear_published_update(self) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if self._runtime_hub_mode != "host":
                raise RuntimeError("只有 StoryForge Hub 主机可以停止发布更新。")
            self._update_repository.clear()
            with self._update_publish_lock:
                self._update_publish_status = {
                    "state": "publisher_idle",
                    "progress": 0.0,
                    "message": "尚未发布软件更新。",
                    "error": "",
                }
            return self._update_status_value()

        return self._guard(operation)

    @staticmethod
    def _installed_component_value(value: Any) -> dict[str, Any]:
        return {
            "component_id": str(value.component_id),
            "version": str(value.version),
            "sha256": str(value.package_sha256),
            "app_compatibility": value.manifest.app_compatibility,
        }

    def _component_update_status_value(self) -> dict[str, Any]:
        installed = [
            self._installed_component_value(item)
            for item in self._component_updater.list_installed()
        ]
        published = (
            [dict(item) for item in self._component_repository.list_manifests()]
            if self._runtime_hub_mode == "host"
            else []
        )
        return {
            "installed": installed,
            "published": published,
            "runtime_error": self._component_runtime_error,
        }

    def get_component_update_status(self) -> dict[str, Any]:
        return self._guard(self._component_update_status_value)

    def check_component_updates(self) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if self._runtime_hub_mode == "host":
                available = [
                    dict(item) for item in self._component_repository.list_manifests()
                ]
            elif self._runtime_hub_mode == "client":
                client = self._update_hub_client()
                if client is None:
                    raise RuntimeError("StoryForge Hub is not connected.")
                available = client.get_component_manifests()
            else:
                available = []
            installed_by_id = {
                item.component_id: item
                for item in self._component_updater.list_installed()
            }
            releases: list[dict[str, Any]] = []
            for raw in available:
                publication = validate_component_publication(raw)
                current = installed_by_id.get(publication["component_id"])
                entry = dict(publication)
                entry["installed_version"] = current.version if current else ""
                entry["installed_sha256"] = current.package_sha256 if current else ""
                entry["install_available"] = bool(
                    current is None
                    or current.package_sha256 != publication["sha256"]
                )
                releases.append(entry)
            return {
                **self._component_update_status_value(),
                "available": releases,
            }

        return self._guard(operation)

    def install_component_update(
        self,
        component_id: str,
        version: str = "",
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if self._queue.is_rendering_busy():
                raise RuntimeError(
                    "Finish the current local render before installing a voice component."
                )
            normalized_id = str(component_id or "").strip().casefold()
            requested_version = str(version or "").strip()
            package_path: Path | None = None
            downloaded = False
            if self._runtime_hub_mode == "host":
                publication = self._component_repository.get_manifest(normalized_id)
                if publication is None:
                    raise RuntimeError("The requested component is not published.")
                if requested_version and publication["version"] != requested_version:
                    raise RuntimeError("The requested component version is not published.")
                package_path = self._component_repository.resolve_package(publication)
            elif self._runtime_hub_mode == "client":
                client = self._update_hub_client()
                if client is None:
                    raise RuntimeError("StoryForge Hub is not connected.")
                publication = next(
                    (
                        item
                        for item in client.get_component_manifests()
                        if item["component_id"] == normalized_id
                        and (not requested_version or item["version"] == requested_version)
                    ),
                    None,
                )
                if publication is None:
                    raise RuntimeError("The requested component is not published.")
                cache_root = (
                    self._repository.data_dir
                    / "updates"
                    / "components"
                    / "downloads"
                )
                package_path = cache_root / str(publication["filename"])
                client.download_component_package(
                    publication,
                    destination=package_path,
                )
                downloaded = True
            else:
                raise RuntimeError("Component updates require a StoryForge Hub.")
            checked = validate_component_publication(publication)
            try:
                with self._heavy_resource_lock:
                    installed = self._component_updater.install(
                        package_path,
                        expected_package_sha256=checked["sha256"],
                    )
                    self._component_updater.activate_runtime()
                    self._component_runtime_error = ""
            finally:
                if downloaded and package_path is not None:
                    try:
                        package_path.unlink()
                    except OSError:
                        pass
            return {
                "component": self._installed_component_value(installed),
                "status": self._component_update_status_value(),
            }

        return self._guard(operation)

    def rollback_component_update(self, component_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if self._queue.is_rendering_busy():
                raise RuntimeError(
                    "Finish the current local render before rolling back a voice component."
                )
            with self._heavy_resource_lock:
                restored = self._component_updater.rollback(
                    component_id,
                    activate=self._component_updater.activate_runtime,
                )
                self._component_runtime_error = ""
            return {
                "component": self._installed_component_value(restored),
                "status": self._component_update_status_value(),
            }

        return self._guard(operation)

    def publish_component_update(
        self,
        package_path: str,
        release_notes: str = "",
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if self._runtime_hub_mode != "host" or self._hub_server is None:
                raise RuntimeError(
                    "Only the running StoryForge Hub can publish components."
                )
            publication = self._component_repository.publish(
                package_path,
                release_notes,
            )
            return {
                "publication": publication,
                "status": self._component_update_status_value(),
            }

        return self._guard(operation)

    def clear_published_component(self, component_id: str = "") -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if self._runtime_hub_mode != "host":
                raise RuntimeError(
                    "Only the StoryForge Hub can stop publishing components."
                )
            catalog = self._component_repository.clear(component_id or None)
            return {
                "catalog": catalog,
                "status": self._component_update_status_value(),
            }

        return self._guard(operation)

    def reconnect_hub(self) -> dict[str, Any]:
        """Reconnect a client after settings or LAN connectivity are repaired."""

        def operation() -> dict[str, Any]:
            configured = self._state.settings.hub
            if configured.mode != "client":
                return self._hub_status_value()
            if not configured.access_token:
                raise ValueError("请先使用账号和密码登录这台电脑。")
            client = HubClient(
                configured.endpoint,
                configured.access_token,
                timeout_seconds=8,
            )
            self._activate_hub_client(client)
            self._update_manager.start()
            self._update_manager.wake()
            self._ensure_device_sync()
            return self._hub_status_value()

        return self._guard(operation)

    def connect_hub_with_password(
        self,
        endpoint: str,
        account_username: str,
        password: str,
        device_name: str,
    ) -> dict[str, Any]:
        """Enroll this desktop with a member password and connect immediately."""

        def operation() -> dict[str, Any]:
            if self._runtime_hub_mode == "host" and (
                self._hub_server is not None and self._hub_server.is_running
            ):
                raise RuntimeError("主电脑不能同时作为制作电脑接入自己。")
            clean_endpoint = str(endpoint or "").strip()
            clean_username = str(account_username or "").strip()
            clean_password = str(password or "")
            clean_device_name = str(device_name or "").strip() or socket.gethostname()
            if not all(
                (clean_endpoint, clean_username, clean_password, clean_device_name)
            ):
                raise ValueError("请填写主电脑地址、员工账号、密码和电脑名称。")
            enrolled = HubClient.enroll_device(
                clean_endpoint,
                clean_username,
                clean_password,
                clean_device_name,
                installation_id=self._state.settings.hub.installation_id,
                app_version=__version__,
                capabilities=self._device_runtime_capabilities(),
                hostname=socket.gethostname(),
                os_name=platform.system(),
                architecture=platform.machine(),
                timeout_seconds=15,
            )
            token = str(enrolled.get("token") or "")
            client = HubClient(clean_endpoint, token, timeout_seconds=8)

            updated = self._state.update_settings(
                {
                    "hub": {
                        "mode": "client",
                        "endpoint": client.base_url,
                        "access_token": token,
                        "account_username": clean_username,
                        "device_id": str(enrolled.get("device_id") or ""),
                        "device_name": str(
                            enrolled.get("device_name") or clean_device_name
                        ),
                        "app_version": __version__,
                        "capabilities": self._device_runtime_capabilities(),
                        "applied_config_revision_id": "",
                        "applied_config_hash": "",
                    }
                }
            )
            identity = self._activate_hub_client(client)
            self._update_manager.start()
            self._update_manager.wake()
            self._ensure_device_sync()

            settings = updated.to_dict(redact_secrets=True)
            settings["providers"]["has_text_api_key"] = bool(
                updated.providers.text_api_key
            )
            settings["providers"]["has_tts_api_key"] = bool(
                updated.providers.tts_api_key
            )
            settings["hub"]["has_access_token"] = True
            worker_autostart = self._worker_autostart_after_login()
            return {
                **self._hub_status_value(),
                "settings": settings,
                "member": dict(enrolled.get("user") or {}),
                "site": dict(identity.get("site") or {}),
                "worker_autostart": worker_autostart,
            }

        return self._guard(operation)

    def _shutdown(self) -> None:
        with self._shutdown_lock:
            # Block terminal callbacks before cancellation starts.  Otherwise
            # a fast cancellation callback can publish ``cancelled`` and
            # release the lease while FFmpeg/TTS is still unwinding.
            self._shutdown_in_progress.set()
            self._backup_manager.stop_daily()
            self._device_sync_stop.set()
            self._device_sync_wake.set()
            device_sync_thread = self._device_sync_thread
            if (
                device_sync_thread is not None
                and device_sync_thread.is_alive()
                and device_sync_thread is not threading.current_thread()
            ):
                device_sync_thread.join(timeout=2.0)
            self._update_manager.stop()

            # Stop extending leases while the local render worker is being
            # cancelled.  If it cannot be confirmed stopped, every lease is
            # intentionally left in place and expires naturally on Hub.
            self._lease_stop.set()
            lease_thread = self._lease_thread
            if (
                lease_thread is not None
                and lease_thread.is_alive()
                and lease_thread is not threading.current_thread()
            ):
                lease_thread.join(timeout=2.0)

            self._queue.begin_shutdown()
            queue_items = list(self._queue.list_jobs())
            # Only a processor which actually started is interrupted. Work
            # still waiting in the durable queue remains queued and resumes on
            # the next Worker start instead of becoming a manual retry.
            interrupted_job_ids = self._queue.active_job_ids()
            self._shutdown_job_ids.update(interrupted_job_ids)
            queue_record_ids = {
                str(item.get("production_record_id") or "")
                for item in queue_items
                if str(item.get("production_record_id") or "")
            }
            with self._lease_lock:
                leased_before_stop = set(self._leased_records)

            queue_stopped = self._queue.stop_and_wait(
                QUEUE_SHUTDOWN_TIMEOUT_SECONDS
            )
            if queue_stopped:
                interruption_message = (
                    "软件关闭时任务已安全停止，可从生产记录中重试。"
                )
                self._queue.mark_shutdown_interrupted(
                    self._shutdown_job_ids,
                    reason=interruption_message,
                )

                # Persist each stopped active job before releasing its lease.
                # Queued jobs were never executed, so keep their durable status
                # unchanged and release only the workstation lease for restart.
                for item in queue_items:
                    job = self._queue.get_job(str(item.get("id") or ""))
                    if job is None or not job.production_record_id:
                        continue
                    with self._lease_lock:
                        still_leased = (
                            job.production_record_id in self._leased_records
                        )
                    if not still_leased:
                        continue
                    if (
                        job.id not in interrupted_job_ids
                        and job.status == JobStatus.QUEUED
                    ):
                        self._release_record_lease(job.production_record_id)
                        continue
                    try:
                        self._sync_one_job_record(
                            job,
                            shutdown_confirmed=True,
                        )
                        if job.id not in interrupted_job_ids:
                            self._release_record_lease(job.production_record_id)
                    except (
                        OSError,
                        sqlite3.Error,
                        ValueError,
                        RuntimeError,
                        KeyError,
                        TypeError,
                    ):
                        continue

                # Draft-gate and other non-job leases are safe to release only
                # after the worker confirmation above.  Job-record leases that
                # failed to synchronize remain held until their normal expiry.
                with self._lease_lock:
                    remaining = set(self._leased_records)
                for record_id in sorted(
                    (leased_before_stop | remaining) - queue_record_ids
                ):
                    self._release_record_lease(record_id)

            if self._hub_server is not None:
                self._hub_server.stop()
            if self._client_web_server is not None:
                self._client_web_server.stop()
                self._client_web_server = None
            # A verified package is handed to an external updater only after
            # the queue is confirmed idle. The live process never overwrites
            # its own files.
            if queue_stopped:
                try:
                    self._update_manager.launch_scheduled_update()
                except (OSError, RuntimeError, ValueError):
                    pass

    def _ensure_lease_heartbeat(self) -> None:
        with self._lease_lock:
            if self._lease_thread is not None and self._lease_thread.is_alive():
                return
            self._lease_stop.clear()
            self._lease_thread = threading.Thread(
                target=self._lease_heartbeat_loop,
                name="storyforge-lease-heartbeat",
                daemon=True,
            )
            self._lease_thread.start()

    @staticmethod
    def _lease_error_is_authoritative(error: BaseException) -> bool:
        """Whether retrying the same work would violate Hub ownership/auth."""

        if isinstance(
            error,
            (HubAuthenticationError, CatalogConflictError, CatalogPermissionError),
        ):
            return True
        if isinstance(error, HubRemoteError):
            return int(error.status) in {401, 403, 409, 410, 423, 426}
        return isinstance(error, (PermissionError, ValueError, KeyError, TypeError))

    @staticmethod
    def _lease_deadline(result: Mapping[str, Any] | None) -> float:
        """Convert a server lease expiry into a conservative monotonic deadline."""

        record = dict((result or {}).get("record") or {})
        raw_expiry = str(record.get("lease_expires_at") or "").strip()
        remaining = float(RECORD_LEASE_SECONDS)
        if raw_expiry:
            try:
                expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                remaining = max(
                    0.0,
                    (expiry.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds(),
                )
            except ValueError:
                pass
        return time.monotonic() + min(float(RECORD_LEASE_SECONDS), remaining)

    def _healthy_lease_state(
        self, result: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "failures": 0,
            "next_attempt": time.monotonic() + RECORD_LEASE_HEARTBEAT_SECONDS,
            "deadline": self._lease_deadline(result),
            "last_error": "",
            "state": "healthy",
        }

    def _lease_heartbeat_loop(self) -> None:
        while not self._lease_stop.is_set():
            with self._lease_lock:
                record_ids = tuple(self._leased_records)
            if not record_ids:
                return
            device_id = self._current_device_id()
            now = time.monotonic()
            next_wait = RECORD_LEASE_HEARTBEAT_SECONDS
            for record_id in record_ids:
                with self._lease_lock:
                    health = dict(self._lease_health.get(record_id) or {})
                deadline = float(health.get("deadline") or 0.0)
                if deadline > 0.0 and now >= deadline:
                    self._stop_jobs_for_lost_lease(
                        record_id, "lease renewal deadline expired"
                    )
                    continue
                next_attempt = float(health.get("next_attempt") or 0.0)
                if next_attempt > now:
                    wait_until = next_attempt
                    if deadline > 0.0:
                        wait_until = min(wait_until, deadline)
                    next_wait = min(next_wait, max(0.25, wait_until - now))
                    continue
                try:
                    heartbeat = self._catalog.heartbeat_record_lease(
                        record_id,
                        device_id,
                        lease_seconds=RECORD_LEASE_SECONDS,
                    )
                except (
                    OSError,
                    sqlite3.Error,
                    ValueError,
                    RuntimeError,
                    KeyError,
                    TypeError,
                ) as error:
                    if self._lease_stop.is_set():
                        return
                    if self._lease_error_is_authoritative(error) or (
                        deadline > 0.0 and time.monotonic() >= deadline
                    ):
                        detail = str(error)
                        if deadline > 0.0 and time.monotonic() >= deadline:
                            detail = f"lease renewal deadline expired; {detail}"
                        self._stop_jobs_for_lost_lease(record_id, detail)
                        continue
                    recovery = self._recover_or_confirm_record_lease_loss(
                        record_id, device_id
                    )
                    if recovery is True:
                        self._stop_jobs_for_lost_lease(record_id, str(error))
                        continue
                    if recovery is None:
                        # Re-claim succeeded and already installed a fresh
                        # server-derived deadline.  Do not overwrite it below
                        # with the stale deadline captured before recovery.
                        with self._lease_lock:
                            recovered_health = dict(
                                self._lease_health.get(record_id) or {}
                            )
                        recovered_attempt = float(
                            recovered_health.get("next_attempt") or 0.0
                        )
                        if recovered_attempt > 0.0:
                            next_wait = min(
                                next_wait,
                                max(0.25, recovered_attempt - time.monotonic()),
                            )
                        continue
                    failures = int(health.get("failures") or 0) + 1
                    delay = min(30.0, float(2 ** max(0, min(5, failures - 1))))
                    with self._lease_lock:
                        if record_id in self._leased_records:
                            self._lease_health[record_id] = {
                                "failures": failures,
                                "next_attempt": time.monotonic() + delay,
                                "deadline": deadline,
                                "last_error": f"{type(error).__name__}: {error}",
                                "state": "reconnecting",
                            }
                    next_wait = min(next_wait, delay)
                    continue
                with self._lease_lock:
                    if record_id in self._leased_records:
                        self._lease_health[record_id] = self._healthy_lease_state(
                            heartbeat
                        )
            self._lease_stop.wait(
                max(0.25, min(RECORD_LEASE_HEARTBEAT_SECONDS, next_wait))
            )

    def _recover_or_confirm_record_lease_loss(
        self, record_id: str, device_id: str
    ) -> bool | None:
        """Return True for loss, None for renewal, False while unconfirmed.

        A missed heartbeat can be a brief LAN outage.  Re-claiming is safe for
        the same owner and also repairs a lease which expired before another
        workstation took it.  If Hub itself is unreachable, the result remains
        unconfirmed and the local task keeps running while heartbeats back off.
        """

        try:
            claimed = self._catalog.claim_record_lease(
                record_id,
                device_id,
                lease_seconds=RECORD_LEASE_SECONDS,
            )
            with self._lease_lock:
                if record_id in self._leased_records:
                    self._lease_health[record_id] = self._healthy_lease_state(
                        claimed
                    )
            return None
        except (OSError, sqlite3.Error, ValueError, RuntimeError, KeyError, TypeError) as error:
            if self._lease_error_is_authoritative(error):
                return True
        try:
            record = self._catalog.get_record(record_id)
        except (OSError, sqlite3.Error, ValueError, RuntimeError, KeyError, TypeError):
            return False
        owner = str(record.get("lease_owner_device") or "")
        status = str(record.get("status") or "")
        return bool(
            status in {"completed", "failed", "skipped", "cancelled"}
            or (owner and owner != device_id)
        )

    def _stop_jobs_for_lost_lease(self, record_id: str, detail: str = "") -> None:
        lost_batch_id = ""
        with self._lease_lock:
            self._leased_records.discard(record_id)
            self._lease_health.pop(record_id, None)
            # Install the marker before cancelling.  The active processor can
            # finish on another thread at any point after ownership is known
            # to be lost, including while this method is enumerating jobs.
            self._superseded_lease_records.add(record_id)
            for draft_id, (gate_id, batch_id) in tuple(
                self._draft_gate_leases.items()
            ):
                if gate_id == record_id:
                    lost_batch_id = batch_id
                    self._draft_gate_leases.pop(draft_id, None)
        queue_items = list(self._queue.list_jobs())
        job_ids = [
            str(item.get("id") or "")
            for item in queue_items
            if (
                str(item.get("production_record_id") or "") == record_id
                or (
                    lost_batch_id
                    and str(item.get("batch_id") or "") == lost_batch_id
                )
            )
            and str(item.get("id") or "")
        ]
        if not any(
            str(item.get("production_record_id") or "") == record_id
            for item in queue_items
        ):
            # Draft-gate lease loss can cancel a whole batch, but no render job
            # publishes a terminal state into the gate record itself.
            with self._lease_lock:
                self._superseded_lease_records.discard(record_id)
        if job_ids:
            message = "任务租约已由其他电脑接管，本机已安全停止，避免重复生成。"
            if detail:
                message = f"{message}（{detail[:200]}）"
            self._queue.cancel_jobs(job_ids, reason=message)

    def _claim_record_lease(self, record_id: str) -> None:
        device_id = self._current_device_id()
        claimed = self._catalog.claim_record_lease(
            record_id,
            device_id,
            lease_seconds=RECORD_LEASE_SECONDS,
        )
        with self._lease_lock:
            self._superseded_lease_records.discard(record_id)
            self._leased_records.add(record_id)
            self._lease_health[record_id] = self._healthy_lease_state(claimed)
        self._ensure_lease_heartbeat()

    def _release_record_lease(self, record_id: str) -> None:
        device_id = self._current_device_id()
        try:
            self._catalog.release_record_lease(record_id, device_id)
        except (OSError, sqlite3.Error, ValueError, RuntimeError, KeyError, TypeError):
            pass
        with self._lease_lock:
            self._superseded_lease_records.discard(record_id)
            self._leased_records.discard(record_id)
            self._lease_health.pop(record_id, None)

    def _claim_draft_gate(self, draft_id: str) -> str:
        device_id = self._current_device_id()
        now = datetime.now(timezone.utc)
        # The optimistic draft claim closes the first-run race where no record
        # exists yet.  Thereafter the heartbeat-backed gate record is the
        # authoritative lock, including after the metadata timestamp ages out.
        active_gate = self._catalog.find_active_draft_gate(
            draft_id,
            active_at=now.isoformat(),
        ).get("item")
        if isinstance(active_gate, Mapping):
            owner = str(active_gate.get("lease_owner_device") or "另一台电脑")
            raise RuntimeError(f"该批次正在由 {owner} 制作，请勿重复开始。")

        draft = self._catalog.get_draft(draft_id)
        metadata = dict(draft.get("metadata") or {})
        queue_claim = dict(metadata.get("queue_claim") or {})
        if queue_claim and self._future_iso(queue_claim.get("expires_at"), now):
            owner = str(queue_claim.get("device_id") or "另一台电脑")
            raise RuntimeError(f"该批次正在由 {owner} 创建队列，请稍后查看记录。")
        claim_id = secrets.token_hex(16)
        metadata["queue_claim"] = {
            "claim_id": claim_id,
            "device_id": device_id,
            "expires_at": (now + timedelta(seconds=180)).isoformat(),
        }
        claim_saved = False
        gate_id = ""
        try:
            self._catalog.save_draft(
                {
                    "id": draft_id,
                    "expected_version": int(draft["row_version"]),
                    "metadata": metadata,
                }
            )
            claim_saved = True
            gate = self._catalog.save_production_record(
                {
                    "draft_id": draft_id,
                    "job_id": "",
                    "device_id": device_id,
                    "status": "queued",
                    "progress": 0,
                    "metadata": {
                        "lease_gate": True,
                        "draft_id": draft_id,
                        "claim_id": claim_id,
                    },
                }
            )
            gate_id = str(gate["id"])
            self._claim_record_lease(gate_id)
        except BaseException:
            if gate_id:
                self._release_record_lease(gate_id)
                try:
                    self._catalog.save_production_record(
                        {
                            "id": gate_id,
                            "status": "interrupted",
                            "error_message": "批次锁创建失败，已自动清理。",
                            "metadata": {
                                "lease_gate": True,
                                "draft_id": draft_id,
                                "claim_id": claim_id,
                            },
                        }
                    )
                except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                    pass
            if claim_saved:
                self._clear_draft_claim(draft_id, claim_id=claim_id)
            raise
        with self._lease_lock:
            self._draft_gate_leases[draft_id] = (gate_id, "")
        return gate_id

    def _bind_draft_gate(
        self, draft_id: str, gate_id: str, durable_batch_id: str
    ) -> None:
        """Attach a planning gate to the exact durable batch it protects."""

        durable_batch_id = str(durable_batch_id or "").strip()
        if not durable_batch_id:
            raise RuntimeError("durable production batch was not created")
        self._catalog.bind_lease_gate_batch(
            gate_id,
            durable_batch_id,
            actor_user_id=self._current_web_actor() or None,
        )
        with self._lease_lock:
            self._draft_gate_leases[draft_id] = (gate_id, durable_batch_id)

    @staticmethod
    def _future_iso(value: Any, now: datetime | None = None) -> bool:
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed > (now or datetime.now(timezone.utc))
        except (TypeError, ValueError):
            return False

    def _clear_draft_claim(self, draft_id: str, *, claim_id: str = "") -> None:
        for _attempt in range(2):
            try:
                draft = self._catalog.get_draft(draft_id)
                metadata = dict(draft.get("metadata") or {})
                if "queue_claim" not in metadata:
                    return
                if claim_id and str(
                    (metadata.get("queue_claim") or {}).get("claim_id") or ""
                ) != claim_id:
                    # Never erase a newer claim made by another device while
                    # cleaning up a partially-created gate.
                    return
                metadata.pop("queue_claim", None)
                self._catalog.save_draft(
                    {
                        "id": draft_id,
                        "expected_version": int(draft["row_version"]),
                        "metadata": metadata,
                    }
                )
                return
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                continue

    def _release_finished_draft_gates(self) -> None:
        with self._lease_lock:
            gates = dict(self._draft_gate_leases)
        durable_ids = [batch_id for _gate_id, batch_id in gates.values() if batch_id]
        try:
            summaries = dict(
                (
                    self._catalog.get_production_batch_summaries(durable_ids).get("items")
                    or {}
                )
                if durable_ids
                else {}
            )
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            # A transient Hub/catalog fault must hold the gate, never release
            # it based on a partial in-memory stream window.
            return
        for draft_id, (gate_id, durable_batch_id) in gates.items():
            summary = summaries.get(durable_batch_id)
            if not durable_batch_id or not isinstance(summary, Mapping):
                continue
            if int(summary.get("total") or 0) <= 0:
                continue
            if int(summary.get("unfinished") or summary.get("active") or 0) > 0:
                continue
            self._release_record_lease(gate_id)
            self._clear_draft_claim(draft_id)
            try:
                self._catalog.save_production_record(
                    {
                        "id": gate_id,
                        "status": "skipped",
                        "progress": 1,
                        "metadata": {
                            "lease_gate": True,
                            "draft_id": draft_id,
                            "durable_batch_id": durable_batch_id,
                        },
                    }
                )
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                pass
            with self._lease_lock:
                self._draft_gate_leases.pop(draft_id, None)

    def _abandon_draft_gate(self, draft_id: str, gate_id: str) -> None:
        self._release_record_lease(gate_id)
        self._clear_draft_claim(draft_id)
        with self._lease_lock:
            _stored_gate, durable_batch_id = self._draft_gate_leases.pop(
                draft_id, (gate_id, "")
            )
        try:
            self._catalog.save_production_record(
                {
                    "id": gate_id,
                    "status": "interrupted",
                    "error_message": "批次创建未完成，租约已释放。",
                    "metadata": {
                        "lease_gate": True,
                        "draft_id": draft_id,
                        "durable_batch_id": durable_batch_id,
                    },
                }
            )
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            pass

    def choose_folder(self, _kind: str = "folder") -> dict[str, Any]:
        try:
            clean_kind = str(_kind or "folder").strip()
            if self._window is not None:
                import webview

                result = self._window.create_file_dialog(webview.FileDialog.FOLDER)
                value = str(result[0]) if result else ""
            else:
                # A workstation's loopback web page has no pywebview window,
                # but it still runs in that employee's interactive Windows
                # session. Use a native dialog so web and EXE configure the
                # same private media folders.
                if os.name != "nt":
                    raise RuntimeError(
                        "Folder selection without a desktop window is supported only on Windows."
                    )
                with self._folder_dialog_lock:
                    import tkinter as tk
                    from tkinter import filedialog

                    root = tk.Tk()
                    try:
                        root.withdraw()
                        root.attributes("-topmost", True)
                        value = str(
                            filedialog.askdirectory(
                                parent=root,
                                mustexist=True,
                                title="选择 StoryForge 本机文件夹",
                            )
                            or ""
                        )
                    finally:
                        root.destroy()
            if value and clean_kind in {
                "video_folder",
                "music_folder",
                "output_folder",
            }:
                from .worker import LocalWorkerProfileStore

                profile = LocalWorkerProfileStore(self._repository.data_dir)
                folders = profile.load()
                folders[clean_kind] = value
                profile.save(folders)
            return self._ok(value)
        except (
            ImportError,
            OSError,
            RuntimeError,
            IndexError,
            TypeError,
            ValueError,
        ) as error:
            return self._error(error)

    def choose_file(self, kind: str = "novel") -> dict[str, Any]:
        """Choose one supported input file without exposing filesystem objects."""

        if self._window is None:
            return self._error(RuntimeError("桌面窗口尚未准备好。"))
        try:
            import webview

            normalized = str(kind or "novel").strip().casefold()
            filters = {
                "novel": (
                    "小说正文 (*.txt;*.docx)",
                    "TXT 文件 (*.txt)",
                    "Word 文件 (*.docx)",
                ),
                "summary": (
                    "故事简介 (*.txt;*.docx)",
                    "TXT 文件 (*.txt)",
                    "Word 文件 (*.docx)",
                ),
                "cover": (
                    "封面图片 (*.jpg;*.jpeg;*.png;*.webp)",
                    "所有图片 (*.jpg;*.jpeg;*.png;*.webp;*.bmp)",
                ),
                "audio": (
                    "音频文件 (*.wav;*.mp3;*.m4a;*.aac;*.flac;*.ogg)",
                ),
                "narration_source": (
                    "StoryForge 配音或成品视频 (*.mp3;*.mp4;*.mov;*.mkv;*.webm)",
                    "StoryForge 配音 (*.mp3)",
                    "StoryForge 成品视频 (*.mp4;*.mov;*.mkv;*.webm)",
                ),
                "update": (
                    "StoryForge 更新包 (*.zip)",
                ),
            }.get(normalized, ("所有文件 (*.*)",))
            result = self._window.create_file_dialog(
                webview.FileDialog.OPEN,
                allow_multiple=False,
                file_types=filters,
            )
            value = str(result[0]) if result else ""
            return self._ok(value)
        except (ImportError, OSError, RuntimeError, IndexError, TypeError) as error:
            return self._error(error)

    def save_platform(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            self._refresh_shared_platforms()
            prepared = self._prepare_platform_profile(value)
            profile = self._state.upsert_platform(prepared.to_dict())
            self._catalog.save_platform(profile.to_dict())
            return self._hydrate_platform(profile.to_dict())

        return self._guard(operation)

    def delete_platform(self, platform_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            result = self._catalog.delete_platform(
                str(platform_id),
                actor_user_id=self._current_web_actor() or None,
            )
            self._refresh_shared_platforms()
            return result

        return self._guard(operation)

    def set_local_tts_provider(self, provider: str) -> dict[str, Any]:
        """Switch only this workstation between free, non-secret TTS engines.

        The operation deliberately cannot edit endpoints, commands or API keys
        and never writes to Hub shared settings.  It is safe for an employee to
        use from either the installed shell or the Hub page's localhost worker.
        """

        def operation() -> dict[str, Any]:
            normalized = str(provider or "").strip().casefold().replace("-", "_")
            aliases = {
                "edge": "edge_tts",
                "edge_tts": "edge_tts",
                "microsoft_edge": "edge_tts",
                "microsoft_edge_tts": "edge_tts",
                "local": "local_kokoro",
                "kokoro": "local_kokoro",
                "local_kokoro": "local_kokoro",
                "kokoro_local": "local_kokoro",
            }
            selected = aliases.get(normalized)
            if selected is None:
                raise ValueError("员工只能切换 Kokoro 本地配音或 Edge TTS 免费多语种配音。")
            updated = self._state.update_settings(
                {"providers": {"tts_provider": selected}}
            )
            return {
                "tts_provider": str(updated.providers.tts_provider),
                "edge_tts_runtime_ready": bool(edge_tts_runtime_available()),
                "embedded_kokoro_ready": bool(embedded_kokoro_available()),
            }

        return self._guard(operation)

    def save_settings(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            provider_patch = dict(value.get("providers") or {})
            current = self._state.settings.providers
            if provider_patch.get("text_api_key") == MASKED_SECRET:
                provider_patch["text_api_key"] = current.text_api_key
            if provider_patch.get("tts_api_key") == MASKED_SECRET:
                provider_patch["tts_api_key"] = current.tts_api_key
            if provider_patch:
                value["providers"] = provider_patch
            hub_patch = dict(value.get("hub") or {})
            current_hub = self._state.settings.hub
            desktop_session = self._desktop_session_payload()
            if (
                desktop_session
                and desktop_session.get("user", {}).get("role") == "producer"
                and (
                    str(hub_patch.get("mode") or "").strip().casefold() == "host"
                    or any(
                        key in hub_patch
                        for key in ("listen_host", "listen_port", "access_token")
                    )
                )
            ):
                raise PermissionError(
                    "员工账号不能将这台电脑设为 StoryForge Hub 主机。"
                )
            if hub_patch.get("access_token") == MASKED_SECRET:
                hub_patch["access_token"] = current_hub.access_token
            # Production media is workstation-local.  Ignore legacy or forged
            # attempts to re-enable Hub uploads through the settings API.
            hub_patch["share_previews"] = False
            hub_patch["share_narration"] = False
            # Device identity and config acknowledgements are runtime-owned.
            # A browser/settings payload must never be able to impersonate a
            # different installation or skip a pending Hub revision.
            for protected_key in (
                "installation_id",
                "device_id",
                "app_version",
                "capabilities",
                "applied_config_revision_id",
                "applied_config_hash",
            ):
                hub_patch.pop(protected_key, None)
            if "hub" in value:
                value["hub"] = hub_patch
            updated = self._state.update_settings(value)
            result = updated.to_dict(redact_secrets=True)
            result["providers"]["has_text_api_key"] = bool(
                updated.providers.text_api_key
            )
            result["providers"]["has_tts_api_key"] = bool(
                updated.providers.tts_api_key
            )
            result["hub"]["has_access_token"] = bool(updated.hub.access_token)
            if (
                self._runtime_hub_mode == "client"
                and updated.hub.auto_update_enabled
            ):
                self._update_manager.start()
                self._update_manager.wake()
            elif not updated.hub.auto_update_enabled:
                self._update_manager.stop()
            # Production drafts freeze their own recipe.  Changing defaults only
            # affects future drafts, so existing previews remain valid.
            result["preview_invalidation"] = {"count": 0, "job_ids": []}
            return result

        return self._guard(operation)

    def get_library_bootstrap(self) -> dict[str, Any]:
        return self._guard(
            lambda: self._hydrate_library_payload(self._library.library_bootstrap())
        )

    def import_novel_text(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            result = self._library.import_text(value)
            if str(value.get("cover_path") or "").strip():
                result["novel"] = self._share_saved_cover(
                    str(result["novel"]["id"]),
                    str(result["novel"].get("cover_path") or value["cover_path"]),
                )
            return self._hydrate_service_result(result)

        return self._guard(operation)

    def import_novel_file(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            result = self._library.import_file(value)
            if str(value.get("cover_path") or "").strip():
                result["novel"] = self._share_saved_cover(
                    str(result["novel"]["id"]),
                    str(result["novel"].get("cover_path") or value["cover_path"]),
                )
            return self._hydrate_service_result(result)

        return self._guard(operation)

    def read_text_document(self, file_path: str) -> dict[str, Any]:
        """Read a small TXT/DOCX synopsis through the same safe importer."""

        def operation() -> dict[str, Any]:
            from .services.manuscript_import import read_manuscript_file
            from .services.text_processing import normalize_manuscript_text

            text = normalize_manuscript_text(read_manuscript_file(str(file_path)))
            if len(text) > 100_000:
                raise ValueError("故事简介文件过长，请保持在 10 万字符以内。")
            return {"text": text.strip(), "file_path": str(Path(file_path).resolve())}

        return self._guard(operation)

    def get_novel(self, novel_id: str) -> dict[str, Any]:
        return self._guard(
            lambda: self._hydrate_novel(self._library.novel_for_ui(str(novel_id)))
        )

    def save_novel(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            result = self._library.save_novel(value)
            if "cover_path" in value and str(value.get("cover_path") or "").strip():
                return self._share_saved_cover(
                    str(result["id"]), str(result.get("cover_path") or "")
                )
            return self._hydrate_novel(result)

        return self._guard(operation)

    def delete_novel(self, novel_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            return self._catalog.delete_novel(
                str(novel_id),
                actor_user_id=self._current_web_actor() or None,
            )

        return self._guard(operation)

    def save_novel_binding(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            return self._hydrate_novel(self._library.save_binding(value))

        return self._guard(operation)

    def add_promo_code(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            return self._hydrate_service_result(self._library.add_promo_code(value))

        return self._guard(operation)

    def update_promo_code(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            return self._hydrate_service_result(self._library.update_promo_code(value))

        return self._guard(operation)

    def delete_promo_code(self, promo_code_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            return self._catalog.delete_promo_code(
                str(promo_code_id),
                actor_user_id=self._current_web_actor() or None,
            )

        return self._guard(operation)

    def save_publishing_account(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            return self._library.save_publishing_account(value)

        return self._guard(operation)

    def delete_publishing_account(self, account_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            return self._catalog.delete_publishing_account(
                str(account_id),
                actor_user_id=self._current_web_actor() or None,
            )

        return self._guard(operation)

    def save_production_draft(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            payload = dict(value)
            local_folders: dict[str, str] = {}
            local_files: dict[str, str] = {}
            if self._runtime_hub_mode == "client":
                from .worker import LocalWorkerProfileStore

                supplied = {
                    key: payload.get(key)
                    for key in ("video_folder", "music_folder", "output_folder")
                    if str(payload.get(key) or "").strip()
                    and not str(payload.get(key) or "").startswith("worker://")
                }
                store = LocalWorkerProfileStore(self._repository.data_dir)
                local_folders = store.save(supplied) if supplied else store.load()
                payload.update(
                    {
                        "video_folder": "worker://local/videos",
                        "music_folder": "worker://local/music",
                        "output_folder": "worker://local/output",
                    }
                )
                production_settings = (
                    dict(payload.get("production_settings") or {})
                    if isinstance(payload.get("production_settings"), Mapping)
                    else {}
                )
                source_audio = str(
                    payload.get("source_narration_audio")
                    or production_settings.get("source_narration_audio")
                    or ""
                ).strip()
                if source_audio and not source_audio.startswith("worker://"):
                    local_files["source_narration_audio"] = source_audio
                    payload["source_narration_audio"] = (
                        "worker://local/source-narration-audio"
                    )
                    production_settings["source_narration_audio"] = (
                        "worker://local/source-narration-audio"
                    )
                bgm_file = str(production_settings.get("bgm_file") or "").strip()
                if bgm_file and not bgm_file.startswith("worker://"):
                    local_files["bgm_file"] = bgm_file
                    production_settings["bgm_file"] = "worker://local/bgm-file"
                payload["production_settings"] = production_settings
            result = self._library.save_draft(payload)
            if local_folders:
                if isinstance(result.get("draft"), dict):
                    result["draft"].update(local_folders)
                novel = result.get("novel")
                if isinstance(novel, dict) and isinstance(novel.get("draft"), dict):
                    novel["draft"].update(local_folders)
            if local_files:
                for target in (
                    result.get("draft"),
                    (
                        result.get("novel", {}).get("draft")
                        if isinstance(result.get("novel"), dict)
                        else None
                    ),
                ):
                    if not isinstance(target, dict):
                        continue
                    if "source_narration_audio" in local_files:
                        target["source_narration_audio"] = local_files[
                            "source_narration_audio"
                        ]
                    settings = target.get("production_settings")
                    if isinstance(settings, dict):
                        settings.update(local_files)
                draft_id = str((result.get("draft") or {}).get("id") or "")
                if draft_id:
                    with self._local_draft_files_lock:
                        self._local_draft_files[draft_id] = dict(local_files)
            return result

        return self._guard(operation)

    def _planned_job_record_payload(
        self,
        job: RenderJob,
        *,
        draft_id: str,
        platform_id: str,
        device_id: str,
        stream_ordinal: int,
        durable_snapshot: bool,
    ) -> dict[str, Any]:
        """Build one bounded, JSON-safe durable task payload."""

        if self._hub_reference(job.cover_path) is not None:
            job.cover_path = self._resolve_shared_file(job.cover_path, group="covers")
        episode_label = job.episode_label or f"E{max(1, job.episode_number):03d}"
        episode_ids = list(
            job.episode_ids or ((job.episode_id,) if job.episode_id else ())
        )
        metadata: dict[str, Any] = {
            "platform_id": platform_id,
            "episode_label": episode_label,
            "episode_ids": episode_ids,
            "stage_label": job.stage_label,
            "output_folder": job.output_folder,
            "job_kind": job.job_kind,
            "variant_count": job.variant_count,
            "production_run_id": job.production_run_id,
            "preview_required": False,
            "stream_ordinal": max(1, int(stream_ordinal)),
            "batch_ordinal": max(1, int(job.batch_ordinal or stream_ordinal)),
            "batch_total_count": max(1, int(job.batch_total_count or 1)),
            "production_preset": {
                "id": job.production_preset_id,
                "revision": int(job.production_preset_revision or 0),
                "content_hash": job.production_preset_hash,
                "adjusted_after_apply": bool(job.production_preset_dirty),
            },
            "resolved_production_settings": deepcopy(job.settings_snapshot),
        }
        if durable_snapshot:
            metadata["job_snapshot"] = job.to_dict()
        return {
            "draft_id": draft_id,
            "job_id": job.id,
            "episode_id": job.episode_id,
            "variant_index": job.variant_index,
            "device_id": device_id,
            "status": "queued",
            "progress": 0,
            "created_by_user_id": self._current_web_actor() or None,
            "metadata": metadata,
        }

    def _persist_planned_job(
        self,
        job: RenderJob,
        *,
        draft_id: str,
        platform_id: str,
        device_id: str,
        stream_ordinal: int,
        durable_snapshot: bool,
    ) -> dict[str, Any]:
        """Create one durable task without retaining the whole batch."""

        record = self._catalog.save_production_record(
            self._planned_job_record_payload(
                job,
                draft_id=draft_id,
                platform_id=platform_id,
                device_id=device_id,
                stream_ordinal=stream_ordinal,
                durable_snapshot=durable_snapshot,
            )
        )
        job.production_record_id = str(record["id"])
        job.batch_id = str(record.get("batch_id") or job.batch_id)
        return record

    def _persist_planned_job_chunk(
        self,
        entries: list[tuple[int, RenderJob]],
        *,
        draft_id: str,
        platform_id: str,
        device_id: str,
    ) -> list[dict[str, Any]]:
        """Persist a bounded large-batch page with one catalog commit/RPC."""

        payloads = [
            self._planned_job_record_payload(
                job,
                draft_id=draft_id,
                platform_id=platform_id,
                device_id=device_id,
                stream_ordinal=ordinal,
                durable_snapshot=True,
            )
            for ordinal, job in entries
        ]
        result = self._catalog.save_production_records_bulk(payloads)
        records = list(result.get("items") or [])
        if len(records) != len(entries):
            raise RuntimeError("catalog returned an incomplete production-record page")
        for (_ordinal, job), record in zip(entries, records, strict=True):
            job.production_record_id = str(record["id"])
            job.batch_id = str(record.get("batch_id") or job.batch_id)
        return records

    def _durable_batch_job_loader(
        self,
        *,
        batch_id: str,
        total_count: int,
    ) -> Callable[[int], list[RenderJob]]:
        """Read oldest unvisited persisted snapshots in bounded pages."""

        remaining = max(0, int(total_count))
        loader_lock = threading.Lock()

        def load(size: int) -> list[RenderJob]:
            nonlocal remaining
            requested = max(1, int(size))
            loaded: list[RenderJob] = []
            with loader_lock:
                while remaining > 0 and len(loaded) < requested:
                    take = min(500, requested - len(loaded), remaining)
                    offset = remaining - take
                    page = self._catalog.list_records(
                        batch_id=batch_id,
                        trashed=None,
                        limit=take,
                        offset=offset,
                    )
                    items = list(page.get("items") or [])
                    # The ledger is immutable in size for this batch. If a
                    # permission/catalog fault returns a short page, consume
                    # what is visible and stop instead of spinning forever.
                    for record in reversed(items):
                        if str(record.get("status") or "") != "queued":
                            continue
                        if record.get("trashed_at"):
                            continue
                        snapshot = (record.get("metadata") or {}).get("job_snapshot")
                        if not isinstance(snapshot, dict):
                            continue
                        job = RenderJob.from_dict(snapshot)
                        job.production_record_id = str(record["id"])
                        job.batch_id = str(record.get("batch_id") or job.batch_id)
                        metadata = dict(record.get("metadata") or {})
                        job.batch_total_count = max(
                            int(job.batch_total_count or 0),
                            int(metadata.get("batch_total_count") or total_count),
                        )
                        job.batch_ordinal = max(
                            int(job.batch_ordinal or 0),
                            int(
                                metadata.get("batch_ordinal")
                                or metadata.get("stream_ordinal")
                                or 0
                            ),
                        )
                        try:
                            self._claim_record_lease(job.production_record_id)
                        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                            latest = self._catalog.get_record(job.production_record_id)
                            if str(latest.get("status") or "") != "queued":
                                continue
                            raise
                        loaded.append(job)
                    remaining = offset
                    if len(items) < take:
                        remaining = 0
            return loaded

        return load

    def queue_production_draft(self, value: dict[str, Any]) -> dict[str, Any]:
        """Create a full-render queue from one novel/platform draft."""

        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            payload_value = dict(value)
            if self._runtime_hub_mode == "client":
                from .worker import LocalWorkerProfileStore

                supplied = {
                    key: payload_value.get(key)
                    for key in ("video_folder", "music_folder", "output_folder")
                    if str(payload_value.get(key) or "").strip()
                    and not str(payload_value.get(key) or "").startswith("worker://")
                }
                local_folders = (
                    LocalWorkerProfileStore(self._repository.data_dir).save(supplied)
                    if supplied
                    else LocalWorkerProfileStore(self._repository.data_dir).load()
                )
                payload_value.update(local_folders)
            draft_id = str(payload_value.get("draft_id") or "").strip()
            if not draft_id:
                raise ValueError("请先保存生产草稿。")
            if self._runtime_hub_mode == "client":
                with self._local_draft_files_lock:
                    local_files = dict(self._local_draft_files.get(draft_id) or {})
                for key in ("source_narration_audio", "bgm_file"):
                    current = str(payload_value.get(key) or "").strip()
                    if (not current or current.startswith("worker://")) and local_files.get(
                        key
                    ):
                        payload_value[key] = local_files[key]
            active_statuses = {
                JobStatus.QUEUED,
                JobStatus.PREFLIGHT,
                JobStatus.PREPARING,
                JobStatus.POLISHING,
                JobStatus.NARRATING,
                JobStatus.COMPOSING,
                JobStatus.PREVIEWING,
                JobStatus.RENDERING,
                JobStatus.AWAITING_APPROVAL,
                JobStatus.WAITING_PREVIEW,
                JobStatus.APPROVED,
            }
            existing = [
                item
                for item in self._queue.jobs_for_draft(draft_id)
                if item.status in active_statuses
            ]
            if existing:
                raise ValueError("该制作批次已经在队列中，请等待现有任务完成，或先取消现有任务。")

            gate_id = self._claim_draft_gate(draft_id)
            try:
                (
                    draft,
                    platform_id,
                    total_videos,
                    job_iterator,
                ) = self._library.build_render_job_plan(payload_value)
                first_job = next(job_iterator)
                self._validate_provider_readiness(first_job.settings_snapshot)
                job_iterator = chain((first_job,), job_iterator)
            except BaseException:
                self._abandon_draft_gate(draft_id, gate_id)
                raise
            platform = self._state.platform_by_id(platform_id)
            if platform is None:
                self._abandon_draft_gate(draft_id, gate_id)
                raise ValueError("当前电脑缺少该推广平台的口令模板，请在设置中重新保存平台。")
            device_id = self._current_device_id()
            claimed_job_records: list[str] = []
            response_jobs: list[RenderJob] = []
            durable_batch_id = ""
            streamed = total_videos > PRODUCTION_STREAM_THRESHOLD
            try:
                if streamed:
                    chunk: list[tuple[int, RenderJob]] = []
                    for stream_ordinal, job in enumerate(job_iterator, start=1):
                        chunk.append((stream_ordinal, job))
                        if len(chunk) < 100:
                            continue
                        records = self._persist_planned_job_chunk(
                            chunk,
                            draft_id=str(draft["id"]),
                            platform_id=platform_id,
                            device_id=device_id,
                        )
                        durable_batch_id = durable_batch_id or str(
                            records[0].get("batch_id") or ""
                        )
                        chunk.clear()
                    if chunk:
                        records = self._persist_planned_job_chunk(
                            chunk,
                            draft_id=str(draft["id"]),
                            platform_id=platform_id,
                            device_id=device_id,
                        )
                        durable_batch_id = durable_batch_id or str(
                            records[0].get("batch_id") or ""
                        )
                else:
                    for stream_ordinal, job in enumerate(job_iterator, start=1):
                        record = self._persist_planned_job(
                            job,
                            draft_id=str(draft["id"]),
                            platform_id=platform_id,
                            device_id=device_id,
                            stream_ordinal=stream_ordinal,
                            # Every queued task must survive a workstation
                            # restart, not only batches above the stream limit.
                            durable_snapshot=True,
                        )
                        durable_batch_id = durable_batch_id or str(
                            record.get("batch_id") or ""
                        )
                        self._claim_record_lease(job.production_record_id)
                        claimed_job_records.append(job.production_record_id)
                        response_jobs.append(job)
                # From this point on the gate protects one immutable durable
                # run.  Its lifecycle is evaluated from the complete ledger,
                # never from the stream window retained by JobQueue.
                self._bind_draft_gate(draft_id, gate_id, durable_batch_id)
            except BaseException:
                for record_id in claimed_job_records:
                    self._release_record_lease(record_id)
                if durable_batch_id:
                    try:
                        while True:
                            page = self._catalog.list_records(
                                status="queued",
                                batch_id=durable_batch_id,
                                limit=500,
                                offset=0,
                            )
                            stale_ids = [
                                str(item.get("id") or "")
                                for item in page.get("items") or []
                                if str(item.get("id") or "")
                            ]
                            if not stale_ids:
                                break
                            self._catalog.request_record_cancellation(
                                stale_ids,
                                reason="large batch planning was interrupted",
                                actor_user_id=self._current_web_actor() or None,
                            )
                    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                        pass
                self._abandon_draft_gate(draft_id, gate_id)
                raise
            local_platform = self._platform_for_local_render(platform)
            if streamed:
                loader = self._durable_batch_job_loader(
                    batch_id=durable_batch_id,
                    total_count=total_videos,
                )
                response_jobs = loader(PRODUCTION_QUEUE_WINDOW)
                self._queue.enqueue_stream(
                    response_jobs,
                    loader,
                    local_platform,
                    history_limit=max(50, PRODUCTION_QUEUE_WINDOW),
                )
            else:
                self._queue.enqueue_jobs(response_jobs, local_platform)
            self._queue.start()
            if not streamed:
                self._sync_job_records()
            return {
                "draft": self._library._ui_draft(draft),
                "jobs": self._jobs_with_batch_summaries(
                    [job.to_dict() for job in response_jobs]
                ),
                # Keep the legacy response key so older clients can decode the
                # payload, while an empty value explicitly means no approval
                # sample is blocking this batch.
                "preview_job_id": "",
                "preview_required": False,
                "total_videos": total_videos,
                "returned_jobs": len(response_jobs),
                "jobs_truncated": streamed,
            }

        return self._guard(operation)

    def generate_voice_candidates(
        self,
        novel_id: str,
        mood: str = "suspense",
        narration_wpm: int | None = None,
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            if self._queue.is_rendering_busy():
                raise RuntimeError(
                    "当前电脑正在制作视频，为避免配音模型与 FFmpeg 同时占用内存，"
                    "请在当前视频完成后再生成候选配音。已加入队列的后续批次不受影响。"
                )
            if not self._voice_preview_lock.acquire(blocking=False):
                raise RuntimeError(
                    "候选配音正在生成，请等待当前试听完成，不要重复提交。"
                )
            try:
                # Close the check/acquire race between two browser requests.
                # A render already active here is authoritative; do not load
                # Kokoro into the same 16 GB workstation process.
                if self._queue.is_rendering_busy():
                    raise RuntimeError(
                        "当前电脑正在制作视频，请在当前视频完成后再生成候选配音。"
                    )
                result = self._library.generate_voice_candidates(
                    str(novel_id),
                    str(mood),
                    narration_wpm=narration_wpm,
                    persist=False,
                )
                return self._share_voice_candidates(str(novel_id), result)
            finally:
                self._voice_preview_lock.release()

        return self._guard(operation)

    def generate_intro_card_copy(
        self,
        novel_id: str,
        episode_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            return self._library.generate_intro_card_copy(
                str(novel_id or ""),
                list(episode_ids or []),
            )

        return self._guard(operation)

    def classify_novel(self, novel_id: str, force: bool = False) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            result = self._library.classify_novel(
                str(novel_id),
                force=bool(force),
            )
            return self._hydrate_service_result(result)

        return self._guard(operation)

    def lock_novel_voice(
        self, novel_id: str, value: dict[str, Any]
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            return self._hydrate_novel(
                self._library.lock_voice(str(novel_id), value)
            )

        return self._guard(operation)

    def save_software_user(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            prepared = dict(value)
            # UI/RPC callers may submit plaintext only through the explicit
            # password field. Never accept a caller-supplied opaque verifier.
            prepared.pop("password_hash", None)
            raw_password = prepared.pop("initial_password", None)
            if raw_password is None:
                raw_password = prepared.pop("password", None)
            else:
                prepared.pop("password", None)
            is_new = not str(prepared.get("id") or "").strip()
            role = str(prepared.get("role") or "producer").strip().casefold()
            if is_new and raw_password in (None, ""):
                if role == "producer":
                    raw_password = DEFAULT_EMPLOYEE_PASSWORD
                else:
                    raise ValueError("新建管理员时必须设置 8 位登录密码。")
            if raw_password not in (None, ""):
                clean_password = validate_new_password(str(raw_password))
                prepared["password_hash"] = hash_password(clean_password)
            return self._catalog.save_user(
                prepared,
                actor_user_id=self._current_web_actor() or None,
            )

        return self._guard(operation)

    def list_software_users(self) -> dict[str, Any]:
        return self._guard(
            lambda: self._catalog.list_users(include_inactive=True)
        )

    def delete_software_user(self, user_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            return self._catalog.delete_user(
                str(user_id),
                actor_user_id=self._current_web_actor() or None,
            )

        return self._guard(operation)

    def list_hub_user_tokens(self, user_id: str = "") -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if self._runtime_hub_mode != "host":
                raise RuntimeError("只有长期运行的 StoryForge Hub 主机可以管理设备令牌。")
            return self._catalog.list_hub_access_tokens(
                str(user_id or "") or None,
                include_revoked=True,
            )

        return self._guard(operation)

    def issue_hub_user_token(
        self, user_id: str, label: str = ""
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if self._runtime_hub_mode != "host":
                raise RuntimeError("只有 StoryForge Hub 主机可以生成账号设备令牌。")
            return self._catalog.issue_hub_access_token(
                str(user_id), label=str(label or "")
            )

        return self._guard(operation)

    def revoke_hub_user_token(self, token_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            if self._runtime_hub_mode != "host":
                raise RuntimeError("只有 StoryForge Hub 主机可以撤销账号设备令牌。")
            return self._catalog.revoke_hub_access_token(str(token_id))

        return self._guard(operation)

    def _managed_device_rpc(
        self,
        method: str,
        params: Mapping[str, Any],
        host_callback: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        if self._runtime_hub_mode == "host":
            return host_callback()
        if self._runtime_hub_mode == "client" and self._hub_client is not None:
            return self._hub_client.call(method, params)
        raise RuntimeError("Device management requires a connected StoryForge Hub.")

    def list_managed_devices(
        self, value: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            filters = dict(value or {})
            return self._managed_device_rpc(
                "devices_list",
                filters,
                lambda: self._catalog.list_hub_devices(**filters),
            )

        return self._guard(operation)

    def get_managed_device(self, device_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            params = {"device_id": str(device_id)}
            return self._managed_device_rpc(
                "device_get",
                params,
                lambda: self._catalog.get_hub_device(str(device_id)),
            )

        return self._guard(operation)

    def acknowledge_managed_device(self, device_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            actor = self._current_web_actor() or None
            params = {"device_id": str(device_id)}
            return self._managed_device_rpc(
                "device_acknowledge",
                params,
                lambda: self._catalog.acknowledge_hub_device(
                    str(device_id), actor_user_id=actor
                ),
            )

        return self._guard(operation)

    def rename_managed_device(
        self, device_id: str, name: str
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            params = {"device_id": str(device_id), "name": str(name)}
            actor = self._current_web_actor() or None
            return self._managed_device_rpc(
                "device_rename",
                params,
                lambda: self._catalog.rename_hub_device(
                    str(device_id), str(name), actor_user_id=actor
                ),
            )

        return self._guard(operation)

    def set_managed_device_active(
        self,
        device_id: str,
        active: bool,
        revoke_tokens: bool = True,
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            params = {
                "device_id": str(device_id),
                "active": active,
                "revoke_tokens": revoke_tokens,
            }
            actor = self._current_web_actor() or None
            return self._managed_device_rpc(
                "device_set_active",
                params,
                lambda: self._catalog.set_hub_device_active(
                    str(device_id),
                    active,
                    revoke_tokens=revoke_tokens,
                    actor_user_id=actor,
                ),
            )

        return self._guard(operation)

    def delete_managed_device(self, device_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            params = {"device_id": str(device_id)}
            actor = self._current_web_actor() or None
            return self._managed_device_rpc(
                "device_delete",
                params,
                lambda: self._catalog.delete_hub_device(
                    str(device_id), actor_user_id=actor
                ),
            )

        return self._guard(operation)

    def create_managed_device_config(
        self, value: dict[str, Any]
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            prepared = dict(value or {})
            actor = self._current_web_actor() or None
            return self._managed_device_rpc(
                "device_config_create",
                {"value": prepared},
                lambda: self._catalog.create_device_config_revision(
                    prepared, actor_user_id=actor
                ),
            )

        return self._guard(operation)

    def list_managed_device_configs(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            params = {"limit": limit, "offset": offset}
            return self._managed_device_rpc(
                "device_config_list",
                params,
                lambda: self._catalog.list_device_config_revisions(
                    limit=limit, offset=offset
                ),
            )

        return self._guard(operation)

    def get_managed_device_config(self, revision_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            params = {"revision_id": str(revision_id)}
            return self._managed_device_rpc(
                "device_config_get",
                params,
                lambda: self._catalog.get_device_config_revision(
                    str(revision_id)
                ),
            )

        return self._guard(operation)

    def set_user_permission(
        self, user_id: str, permission: str, allowed: bool | None
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            self._require_shared_catalog_online()
            return self._catalog.set_user_permission(
                str(user_id), str(permission), allowed
            )

        return self._guard(operation)

    def get_effective_permissions(self, user_id: str) -> dict[str, Any]:
        return self._guard(lambda: self._catalog.get_effective_permissions(str(user_id)))

    def get_record_artifacts(self, record_id: str) -> dict[str, Any]:
        """Return playable local/cache paths for one production record's files."""

        def operation() -> dict[str, Any]:
            record = self._catalog.get_record(str(record_id))
            resolved_items: list[dict[str, Any]] = []
            attachment_root = (self._repository.data_dir / "hub-attachments").resolve()
            cache_root = (self._repository.data_dir / "hub-cache" / "artifacts").resolve()
            cache_root.mkdir(parents=True, exist_ok=True)
            for raw in record.get("artifacts") or []:
                item = dict(raw)
                metadata = dict(item.get("metadata") or {})
                raw_path = str(item.get("local_path") or "")
                local_path: Path | None = None
                if raw_path and not raw_path.startswith("hub://"):
                    candidate = Path(raw_path)
                    if candidate.is_file():
                        local_path = candidate.resolve()
                relative = str(metadata.get("hub_relative_path") or "").replace(
                    "\\", "/"
                )
                root_alias = str(metadata.get("hub_root") or "attachments")
                if local_path is None and relative:
                    if self._runtime_hub_mode == "host":
                        candidate = (attachment_root / Path(relative)).resolve()
                        if attachment_root in candidate.parents and candidate.is_file():
                            local_path = candidate
                    elif self._runtime_hub_mode == "client" and self._hub_client is not None:
                        suffix = Path(relative).suffix
                        cache_name = f"{item.get('sha256') or item.get('id')}{suffix}"
                        candidate = (cache_root / cache_name).resolve()
                        if cache_root not in candidate.parents:
                            raise ValueError("附件缓存目标超出安全目录。")
                        if not candidate.is_file():
                            self._hub_client.download_file(
                                root_alias,
                                relative,
                                destination=candidate,
                            )
                        local_path = candidate
                item["available"] = bool(local_path and local_path.is_file())
                item["cached_path"] = str(local_path) if local_path else ""
                item["uri"] = local_path.as_uri() if local_path else ""
                resolved_items.append(item)
            return {"record": record, "artifacts": resolved_items}

        return self._guard(operation)

    def scan_batch(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            batch = self._batch_from_payload(value)
            scan = self._queue.scan_batch(batch)
            return {
                **scan,
                "batch": batch.to_dict(),
                "story_count": len(scan["stories"]),
            }

        return self._guard(operation)

    def queue_batch(self, value: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            batch = self._batch_from_payload(value)
            platform = self._state.platform_by_id(batch.platform_id)
            if platform is None:
                raise ValueError("请先选择平台。")
            required_folders = [("小说", batch.text_folder)]
            if batch.output_mode != "audio_only":
                required_folders.extend(
                    (("视频素材", batch.video_folder), ("背景音乐", batch.music_folder))
                )
            if batch.output_mode == "reuse_audio" and not Path(
                batch.source_narration_audio
            ).is_file():
                raise ValueError("请选择员工本机存在的已有配音。")
            for folder_name, folder_value in required_folders:
                if not Path(folder_value).is_dir():
                    raise ValueError(f"{folder_name}文件夹不存在。")
            Path(batch.output_folder).mkdir(parents=True, exist_ok=True)
            jobs, errors = self._queue.enqueue_batch(
                batch,
                self._platform_for_local_render(platform),
            )
            if not jobs:
                detail = errors[0] if errors else "文件夹中没有 TXT 小说。"
                raise ValueError(f"没有可加入队列的小说：{detail}")
            self._state.batches.append(batch)
            self._state.persist()
            return {
                "batch": batch.to_dict(),
                "jobs": [job.to_dict() for job in jobs],
                "errors": errors,
            }

        return self._guard(operation)

    def start_queue(self) -> dict[str, Any]:
        def operation() -> list[dict[str, Any]]:
            self._queue.resume_streams()
            pending = [
                item
                for item in self._queue.list_jobs()
                if str(item.get("status") or "")
                in {JobStatus.QUEUED.value, JobStatus.APPROVED.value}
            ]
            if pending:
                checked_recipes: set[str] = set()
                for item in pending:
                    snapshot = item.get("settings_snapshot")
                    key = json.dumps(
                        (snapshot or {}).get("providers")
                        if isinstance(snapshot, Mapping)
                        else {},
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    if key in checked_recipes:
                        continue
                    checked_recipes.add(key)
                    self._validate_provider_readiness(
                        snapshot if isinstance(snapshot, Mapping) else None
                    )
            else:
                self._validate_provider_readiness()
            self._queue.start()
            self._sync_job_records()
            return self._queue_jobs()

        return self._guard(operation)

    def cancel_queue(self) -> dict[str, Any]:
        record_ids = [
            str(item.get("production_record_id") or "")
            for item in self._queue.list_jobs()
            if str(item.get("production_record_id") or "")
            and str(item.get("status") or "")
            not in {"completed", "failed", "cancelled", "interrupted"}
        ]
        self._queue.cancel()
        if record_ids:
            try:
                self._catalog.request_record_cancellation(
                    record_ids,
                    reason="queue cancelled by operator",
                    actor_user_id=self._current_web_actor() or None,
                )
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                pass
        # Large batches have a durable tail which is intentionally absent
        # from the in-memory queue. Cancel those queued records in fixed-size
        # pages as well; using offset zero is deliberate because each update
        # removes the page from the ``status=queued`` result set.
        while True:
            try:
                page = self._catalog.list_records(
                    status="queued",
                    device_id=self._current_device_id(),
                    limit=500,
                    offset=0,
                )
                pending_ids = [
                    str(item.get("id") or "")
                    for item in page.get("items") or []
                    if str(item.get("id") or "")
                    and not bool((item.get("metadata") or {}).get("lease_gate"))
                ]
                if not pending_ids:
                    break
                self._catalog.request_record_cancellation(
                    pending_ids,
                    reason="queue cancelled by operator",
                    actor_user_id=self._current_web_actor() or None,
                )
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                break
        self._sync_job_records()
        return self._ok(self._queue_jobs())

    def get_jobs(self) -> dict[str, Any]:
        # Queue reads are deliberately side-effect free. The Worker publishes
        # terminal states through its callback, so closing the browser cannot
        # strand a completed record and rapid polling cannot create Hub writes.
        return self._ok(self._queue_jobs())

    def get_queue_connection(self) -> dict[str, Any]:
        """Return the durable-loader reconnect state for desktop/web clients."""

        return self._ok(self._queue.stream_status())

    def get_production_record_groups(
        self, filters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return the permission-scoped Novel -> Batch -> Task ledger."""

        def operation() -> dict[str, Any]:
            value = dict(filters or {})
            actor_user_id = self._current_web_actor()
            if actor_user_id and not self._can_manage_all_jobs(actor_user_id):
                value["created_by_user_id"] = actor_user_id
            allowed = {
                "status",
                "novel_id",
                "batch_id",
                "created_by_user_id",
                "device_id",
                "created_from",
                "created_to",
                "archived",
                "trashed",
                "limit",
            }
            return self._library.production_record_groups(
                {key: item for key, item in value.items() if key in allowed}
            )

        return self._guard(operation)

    def cancel_production_records(
        self, record_ids: list[str], reason: str = ""
    ) -> dict[str, Any]:
        """Cancel selected tasks and persist who/when/why immediately."""

        def operation() -> dict[str, Any]:
            normalized = list(dict.fromkeys(str(item) for item in record_ids if str(item)))
            if not normalized:
                raise ValueError("record_ids cannot be empty")
            records = [self._require_job_record_access(record_id) for record_id in normalized]
            job_ids = {
                str(record.get("job_id") or "")
                for record in records
                if str(record.get("job_id") or "")
            }
            queue_items = self._queue.cancel_jobs(job_ids)
            result = self._catalog.request_record_cancellation(
                normalized,
                reason=str(reason or ""),
                actor_user_id=self._current_web_actor() or None,
            )
            return {**result, "queue_items": queue_items}

        return self._guard(operation)

    def _require_record_admin(self) -> str | None:
        actor_user_id = self._current_web_actor()
        if not actor_user_id:
            return None
        permissions = self._catalog.get_effective_permissions(actor_user_id)
        effective = dict(permissions.get("effective") or {})
        if not any(
            bool(effective.get(permission))
            for permission in ("records.view_all", "hub.manage", "users.manage")
        ):
            raise PermissionError("Only administrators can manage the production recycle bin.")
        return actor_user_id

    def trash_production_records(self, record_ids: list[str]) -> dict[str, Any]:
        return self._guard(
            lambda: self._catalog.trash_production_records(
                list(record_ids), actor_user_id=self._require_record_admin()
            )
        )

    def restore_trashed_production_records(
        self, record_ids: list[str]
    ) -> dict[str, Any]:
        return self._guard(
            lambda: self._catalog.restore_trashed_records(
                list(record_ids), actor_user_id=self._require_record_admin()
            )
        )

    def delete_trashed_production_records(
        self, record_ids: list[str]
    ) -> dict[str, Any]:
        return self._guard(
            lambda: self._catalog.delete_trashed_records(
                list(record_ids), actor_user_id=self._require_record_admin()
            )
        )

    def get_archived_jobs(
        self, options: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return durable film-strip history visible to the current actor."""

        if options is None:
            return self._guard(self._archived_jobs)
        return self._guard(lambda: self._archived_jobs_page(options))

    def _archive_job_value(
        self,
        job_id: str,
        *,
        include_collections: bool = True,
        sync_record: bool = True,
    ) -> dict[str, Any]:
        if sync_record:
            self._sync_job_records()
        snapshot = self._queue.archive_snapshot(str(job_id))
        record_id = str(snapshot.get("production_record_id") or "")
        if not record_id:
            raise ValueError("该任务没有可归档的生产记录。")
        self._require_job_record_access(record_id)
        actor_user_id = self._current_web_actor() or None
        persisted = self._catalog.archive_job_snapshot(
            str(job_id),
            snapshot,
            actor_user_id=actor_user_id,
        )

        try:
            self._queue.remove_archived(str(job_id))
        except BaseException:
            # Do not leave an active card marked archived if the in-memory
            # removal unexpectedly fails after the durable write.
            self._catalog.restore_job_snapshot(
                str(job_id), actor_user_id=actor_user_id
            )
            raise
        result: dict[str, Any] = {"job": dict(persisted["job"])}
        if include_collections:
            archived_page = self._archived_jobs_page()
            result.update(
                {
                    "current_jobs": self._queue_jobs(),
                    "archived_jobs": [
                        dict(item) for item in archived_page.get("items") or []
                    ],
                    "archived_jobs_total": int(
                        archived_page.get("total") or 0
                    ),
                }
            )
        return result

    def archive_job(self, job_id: str) -> dict[str, Any]:
        """Move one finished task out of the active film strip."""

        return self._guard(lambda: self._archive_job_value(str(job_id)))

    def archive_batch(self, batch_id: str) -> dict[str, Any]:
        """Move a complete finished batch out of the active film strip."""

        def operation() -> dict[str, Any]:
            normalized = str(batch_id or "").strip()
            self._sync_job_records()
            self._batch_records(normalized)
            snapshots = self._queue.archive_batch_snapshots(normalized)
            actor_user_id = self._current_web_actor() or None
            persisted = self._catalog.archive_batch_snapshots(
                normalized,
                snapshots,
                actor_user_id=actor_user_id,
            )
            live_job_ids = [str(item.get("id") or "") for item in snapshots]
            try:
                self._queue.remove_archived_batch(normalized, live_job_ids)
            except BaseException:
                changed = [
                    str(item) for item in persisted.get("changed_job_ids") or []
                ]
                if changed:
                    self._catalog.restore_batch_snapshots(
                        normalized,
                        job_ids=changed,
                        actor_user_id=actor_user_id,
                    )
                raise
            archived_page = self._archived_jobs_page()
            return {
                "batch_id": normalized,
                "archived_count": int(persisted.get("archived_count") or 0),
                "changed_count": int(persisted.get("changed_count") or 0),
                "already_archived": bool(persisted.get("already_archived")),
                "archived_job_ids": [
                    str(item) for item in persisted.get("job_ids") or []
                ],
                "current_jobs": self._queue_jobs(),
                "archived_jobs": [
                    dict(item) for item in archived_page.get("items") or []
                ],
                "archived_jobs_total": int(archived_page.get("total") or 0),
            }

        return self._guard(operation)

    @staticmethod
    def _apply_worker_folders(
        job: RenderJob | dict[str, Any],
        worker_folders: Mapping[str, Any] | None,
    ) -> None:
        """Apply browser-validated, workstation-local media roots to one job."""

        if worker_folders is None:
            return
        if not isinstance(worker_folders, Mapping):
            raise ValueError("worker_folders must be an object")
        for key in ("video_folder", "music_folder", "output_folder"):
            value = str(worker_folders.get(key) or "").strip()
            if not value:
                raise ValueError(f"worker_folders.{key} is required")
            if isinstance(job, dict):
                job[key] = value
            else:
                setattr(job, key, value)

    def restore_job(
        self,
        job_id: str,
        worker_folders: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Restore an archived task card without rerendering any files."""

        def operation() -> dict[str, Any]:
            archived = dict(self._catalog.get_archived_job(str(job_id)))
            record_id = str(archived.get("production_record_id") or "")
            self._require_job_record_access(record_id)
            platform = self._state.platform_by_id(str(archived.get("platform_id") or ""))
            if platform is None:
                raise ValueError("归档任务对应的平台已经不存在，暂时无法恢复。")
            self._apply_worker_folders(archived, worker_folders)
            restored = self._queue.restore_archived(
                archived,
                self._platform_for_local_render(platform),
            )
            actor_user_id = self._current_web_actor() or None
            try:
                self._catalog.restore_job_snapshot(
                    str(job_id), actor_user_id=actor_user_id
                )
            except BaseException:
                self._queue.remove_archived(str(job_id))
                raise
            archived_page = self._archived_jobs_page()
            return {
                "job": restored.to_dict(),
                "current_jobs": self._queue_jobs(),
                "archived_jobs": [
                    dict(item) for item in archived_page.get("items") or []
                ],
                "archived_jobs_total": int(archived_page.get("total") or 0),
            }

        return self._guard(operation)

    def restore_batch(
        self,
        batch_id: str,
        worker_folders: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Restore every archived task card in one batch without rerendering."""

        def operation() -> dict[str, Any]:
            normalized = str(batch_id or "").strip()
            self._batch_records(normalized)
            actor_user_id = self._current_web_actor() or None
            archived = self._catalog.get_archived_batch(normalized)
            snapshots = [dict(item) for item in archived.get("jobs") or []]
            restored: list[RenderJob] = []
            if snapshots:
                platforms: dict[str, PlatformProfile] = {}
                for snapshot in snapshots:
                    self._apply_worker_folders(snapshot, worker_folders)
                    platform_id = str(snapshot.get("platform_id") or "")
                    platform = self._state.platform_by_id(platform_id)
                    if platform is None:
                        raise ValueError(
                            "归档批次中有任务对应的平台已经不存在，暂时无法恢复。"
                        )
                    platforms[platform_id] = self._platform_for_local_render(platform)
                restored = self._queue.restore_archived_batch(
                    snapshots,
                    platforms,
                )
                try:
                    self._catalog.restore_batch_snapshots(
                        normalized,
                        actor_user_id=actor_user_id,
                    )
                except BaseException:
                    self._queue.remove_archived_batch(
                        normalized, [item.id for item in restored]
                    )
                    raise
            archived_page = self._archived_jobs_page()
            active_batch_jobs = [
                item
                for item in self._queue_jobs()
                if str(item.get("batch_id") or "") == normalized
            ]
            return {
                "batch_id": normalized,
                "restored_count": len(restored),
                "already_restored": not snapshots,
                "jobs": [item.to_dict() for item in restored] or active_batch_jobs,
                "current_jobs": self._queue_jobs(),
                "archived_jobs": [
                    dict(item) for item in archived_page.get("items") or []
                ],
                "archived_jobs_total": int(archived_page.get("total") or 0),
            }

        return self._guard(operation)

    def archive_finished_jobs(self) -> dict[str, Any]:
        """Archive every finished task the current actor is allowed to manage."""

        def operation() -> dict[str, Any]:
            archived_ids: list[str] = []
            self._sync_job_records()
            for item in list(self._queue.list_jobs()):
                if str(item.get("status") or "") not in {
                    JobStatus.COMPLETED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.INTERRUPTED.value,
                }:
                    continue
                record_id = str(item.get("production_record_id") or "")
                try:
                    self._require_job_record_access(record_id)
                except PermissionError:
                    continue
                self._archive_job_value(
                    str(item["id"]),
                    include_collections=False,
                    sync_record=False,
                )
                archived_ids.append(str(item["id"]))
            archived_page = self._archived_jobs_page()
            return {
                "archived_count": len(archived_ids),
                "archived_job_ids": archived_ids,
                "current_jobs": self._queue_jobs(),
                "archived_jobs": [
                    dict(item) for item in archived_page.get("items") or []
                ],
                "archived_jobs_total": int(archived_page.get("total") or 0),
            }

        return self._guard(operation)

    def clear_finished_jobs(self) -> dict[str, Any]:
        def operation() -> list[dict[str, Any]]:
            self._sync_job_records()
            return self._jobs_with_batch_summaries(self._queue.clear_finished())

        return self._guard(operation)

    def approve_preview(self, job_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            job = self._queue.get_job(str(job_id))
            self._validate_provider_readiness(
                job.settings_snapshot if job is not None else None
            )
            item = self._queue.approve_preview(str(job_id))
            self._queue.start()
            self._sync_job_records()
            return item

        return self._guard(operation)

    def regenerate_preview(self, job_id: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            job = self._queue.get_job(str(job_id))
            if job is not None:
                self._refresh_job_render_context(job)
            self._validate_provider_readiness(
                job.settings_snapshot if job is not None else None
            )
            item = self._queue.regenerate_preview(str(job_id))
            self._queue.start()
            self._sync_job_records()
            return item

        return self._guard(operation)

    def _refresh_job_render_context(self, job: RenderJob) -> None:
        """Apply the current draft recipe before intentionally regenerating a sample."""

        live_snapshot = self._library.production_settings_snapshot(self._state.settings)
        related = (
            self._queue.jobs_for_draft(job.production_draft_id)
            if job.production_draft_id
            else [job]
        )
        novel = self._catalog.get_novel(job.novel_id) if job.novel_id else None
        novel_metadata = dict((novel or {}).get("metadata") or {})
        draft = (
            self._catalog.get_draft(job.production_draft_id)
            if job.production_draft_id
            else None
        )
        draft_metadata = dict((draft or {}).get("metadata") or {})
        saved_recipe = draft_metadata.get("production_settings")
        snapshot = self._library._validated_production_settings(
            saved_recipe if isinstance(saved_recipe, Mapping) else None,
            base=(saved_recipe if isinstance(saved_recipe, Mapping) else live_snapshot),
        )
        voice = (
            dict(draft_metadata.get("voice") or {})
            if isinstance(draft_metadata.get("voice"), Mapping)
            else {}
        )
        voice_provider = str(
            voice.get("provider")
            or novel_metadata.get("locked_voice_provider")
            or job.locked_voice_provider
            or ""
        ).strip()
        voice_id = str(
            voice.get("voice_id")
            or novel_metadata.get("locked_voice_id")
            or job.locked_voice_id
            or ""
        ).strip()
        providers = dict(snapshot.get("providers") or {})
        if voice_provider:
            providers["tts_provider"] = voice_provider
        snapshot["providers"] = providers
        voice_profile = str(voice.get("profile") or (draft or {}).get("voice_profile") or "")
        if voice_profile in {"dramatic", "warm", "calm", "confident"}:
            snapshot["voice_by_mood"] = {
                mood: voice_profile
                for mood in ("suspense", "romance", "sad", "revenge")
            }
        for candidate in related:
            candidate.settings_snapshot = deepcopy(snapshot)
            candidate.cover_outro_enabled = bool(
                snapshot.get("cover_outro_enabled", True)
            )
            candidate.locked_voice_provider = voice_provider
            candidate.locked_voice_id = voice_id
            candidate.video_folder = str(
                draft_metadata.get("video_folder") or candidate.video_folder
            )
            candidate.music_folder = str(
                draft_metadata.get("music_folder") or candidate.music_folder
            )
            candidate.output_folder = str(
                draft_metadata.get("output_folder") or candidate.output_folder
            )
            candidate.cover_path = str(
                (novel or {}).get("cover_path") or candidate.cover_path
            )
            if self._hub_reference(candidate.cover_path) is not None:
                candidate.cover_path = self._resolve_shared_file(
                    candidate.cover_path, group="covers"
                )

    def retry_failed(
        self,
        job_id: str,
        worker_folders: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            existing = self._queue.get_job(str(job_id))
            if existing is not None and existing.job_kind == "preview":
                raise ValueError(
                    "历史预览任务不再重试；请从小说重新建立批次并直接生成完整视频。"
                )
            if existing is not None:
                self._apply_worker_folders(existing, worker_folders)
                if existing.production_record_id:
                    self._require_job_record_access(existing.production_record_id)
            # A cancelled task is terminal in the UI before its FFmpeg/TTS
            # process has necessarily finished unwinding.  Validate the local
            # attempt first so the durable ledger is never reopened for a
            # retry which the queue must still reject.
            self._queue.assert_retryable(str(job_id))
            self._validate_provider_readiness(
                existing.settings_snapshot if existing is not None else None
            )
            if existing is not None and existing.production_record_id:
                self._catalog.begin_record_retry(
                    existing.production_record_id,
                    actor_user_id=self._current_web_actor() or None,
                )
            item = self._queue.retry_failed(str(job_id))
            self._queue.start()
            self._sync_job_records()
            return item

        return self._guard(operation)

    @staticmethod
    def _record_status(job: RenderJob) -> str:
        if job.status in {JobStatus.QUEUED, JobStatus.WAITING_PREVIEW, JobStatus.APPROVED}:
            return "queued"
        if job.status in {JobStatus.PREFLIGHT, JobStatus.PREPARING}:
            return "preflight"
        if job.status == JobStatus.AWAITING_APPROVAL:
            return "awaiting_approval"
        if job.status in {
            JobStatus.POLISHING,
            JobStatus.NARRATING,
            JobStatus.COMPOSING,
            JobStatus.PREVIEWING,
            JobStatus.RENDERING,
        }:
            return "running"
        return job.status.value

    @staticmethod
    def _media_fingerprint(path: Path) -> str:
        resolved = path.resolve()
        stat = resolved.stat()
        payload = f"{resolved.as_posix().casefold()}|{stat.st_size}|{stat.st_mtime_ns}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _artifact_mime(kind: str, path: Path) -> str:
        if path.suffix.casefold() == ".mp4":
            return "video/mp4"
        if path.suffix.casefold() == ".wav":
            return "audio/wav"
        if path.suffix.casefold() == ".mp3":
            return "audio/mpeg"
        if path.suffix.casefold() == ".ass":
            return "text/x-ssa"
        if path.suffix.casefold() == ".json":
            return "application/json"
        return "application/octet-stream"

    def _share_artifact(
        self,
        job: RenderJob,
        kind: str,
        path: Path,
        sha256: str,
    ) -> tuple[str, dict[str, Any]]:
        # Hub stores the artifact's business metadata and the originating
        # workstation reference only.  The bytes (MP4, MP3, preview, WAV, ASS)
        # remain on that workstation and are never copied to Hub.
        local_path = str(path.resolve())
        return local_path, {
            "source_local_path": local_path,
            "storage_scope": "workstation_local",
            "hub_media_uploaded": False,
        }

    def _record_artifact(self, job: RenderJob, kind: str, raw_path: str) -> None:
        if not job.production_record_id:
            return
        path = Path(raw_path)
        if not path.is_file():
            return
        try:
            sha256 = self._file_sha256(path)
            key = (job.production_record_id, kind, sha256)
            if key in self._recorded_artifacts:
                return
            catalog_path, shared_metadata = self._share_artifact(
                job, kind, path, sha256
            )
            self._catalog.add_artifact(
                {
                    "record_id": job.production_record_id,
                    "kind": kind,
                    "device_id": self._current_device_id(),
                    "local_path": catalog_path,
                    "sha256": sha256,
                    "mime_type": self._artifact_mime(kind, path),
                    "size_bytes": path.stat().st_size,
                    "duration_seconds": (
                        int(
                            job.settings_snapshot.get(
                                "preview_seconds",
                                self._state.settings.preview_seconds,
                            )
                        )
                        if kind == "sample"
                        else None
                    ),
                    "metadata": {
                        "episode_id": job.episode_id,
                        "episode_ids": list(
                            job.episode_ids
                            or ((job.episode_id,) if job.episode_id else ())
                        ),
                        "episode_label": (
                            job.episode_label
                            or f"E{max(1, int(job.episode_number)):03d}"
                        ),
                        "episode_number": job.episode_number,
                        "variant_index": job.variant_index,
                        **shared_metadata,
                    },
                }
            )
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return
        self._recorded_artifacts.add(key)

    def _record_job_sidecars(self, job: RenderJob, *, preview: bool) -> None:
        from .pipeline import job_workspace_directory

        output = Path(job.preview_file if preview else job.output_file)
        if not output.is_file():
            return
        job_root = job_workspace_directory(
            job,
            self._repository.data_dir / "render-work",
        )
        if not job_root.exists():
            # Read sidecars from pre-refactor jobs without moving or deleting
            # their historical files.
            job_root = output.parent.parent if preview else output.parent
        work = job_root / ".work"
        narration_path = (
            Path(job.narration_audio_file)
            if not preview and job.narration_audio_file
            else work / ("preview-narration.wav" if preview else "narration.wav")
        )
        pairs = (
            (
                "preview_narration" if preview else "narration",
                narration_path,
            ),
            (
                "preview_alignment" if preview else "alignment",
                work / ("preview-subtitles.ass" if preview else "subtitles.ass"),
            ),
        )
        for kind, path in pairs:
            self._record_artifact(job, kind, str(path))

    def _record_media_from_manifest(self, job: RenderJob) -> None:
        if not job.output_file or job.id in self._recorded_media_jobs:
            return
        from .pipeline import job_workspace_directory

        manifest_path = job_workspace_directory(
            job,
            self._repository.data_dir / "render-work",
        ) / "manifest.json"
        if not manifest_path.is_file():
            # Compatibility with outputs created before technical sidecars
            # moved out of the employee-facing publish directory.
            manifest_path = Path(job.output_file).parent / "manifest.json"
        if not manifest_path.is_file():
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            media = dict(manifest.get("media") or {})
            video_selection = dict(media.get("video_selection") or {})
            self._job_media_selection[job.id] = video_selection
            paths: list[tuple[str, str]] = []
            for raw_path in media.get("videos") or []:
                paths.append(("video", str(raw_path)))
            music_path = str(media.get("music") or "")
            if music_path:
                paths.append(("music", music_path))
            counts = Counter(paths)
            materials: list[dict[str, Any]] = []
            for (media_type, raw_path), count in counts.items():
                path = Path(raw_path)
                if not path.is_file():
                    continue
                self._catalog.record_media_usage(
                    {
                        "record_id": job.production_record_id or None,
                        "fingerprint": self._media_fingerprint(path),
                        "media_type": media_type,
                        "display_name": path.name,
                        "local_path": str(path.resolve()),
                        "device_id": self._current_device_id(),
                        "use_count": count,
                        "metadata": {
                            "episode_id": job.episode_id,
                            "variant_index": job.variant_index,
                        },
                    }
                )
                usage = self._catalog.list_media_usage(
                    media_type=media_type,
                    fingerprint=self._media_fingerprint(path),
                    limit=1,
                ).get("items", [])
                total_uses = int(usage[0].get("total_uses") or count) if usage else count
                materials.append(
                    {
                        "name": path.name,
                        "type": media_type,
                        "usage_count": total_uses,
                        "fingerprint": self._media_fingerprint(path),
                        "selection_mode": (
                            str(video_selection.get("mode") or "")
                            if media_type == "video"
                            else ""
                        ),
                        "generic_fallback": bool(
                            media_type == "video"
                            and video_selection.get("fallback")
                        ),
                        "requested_category": (
                            str(video_selection.get("requested_category") or "")
                            if media_type == "video"
                            else ""
                        ),
                    }
                )
            self._job_materials[job.id] = materials
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, json.JSONDecodeError):
            return
        self._recorded_media_jobs.add(job.id)

    def _sync_one_job_record(
        self,
        job: RenderJob,
        *,
        shutdown_confirmed: bool = False,
    ) -> None:
        """Project one in-memory job into its durable production record."""

        if self._shutdown_in_progress.is_set() and not shutdown_confirmed:
            return
        if not job.production_record_id:
            return
        # The render thread may advance this mutable job while the catalog RPC
        # is in flight.  Use one status snapshot for both the write and lease
        # release decision; otherwise a queued/preflight write can race a fast
        # failure and accidentally release its lease before the failure sync.
        projected_status = self._record_status(job)
        projected_progress = float(job.progress)
        output_path = job.output_file or job.preview_file
        if projected_status == "completed":
            self._record_media_from_manifest(job)
        publish_folder = (
            job.publish_batch_folder
            or (str(Path(output_path).parent) if output_path else "")
            or job.output_folder
        )
        episode_label = job.episode_label or f"E{max(1, job.episode_number):03d}"
        episode_ids = list(
            job.episode_ids or ((job.episode_id,) if job.episode_id else ())
        )
        record = self._catalog.save_production_record(
            {
                "id": job.production_record_id,
                "job_id": job.id,
                "device_id": self._current_device_id(),
                "expected_lease_owner_device": self._current_device_id(),
                "status": projected_status,
                "progress": projected_progress,
                "output_path": output_path,
                "error_message": sanitize_failure_log(job.message),
                "metadata": {
                    "platform_id": job.platform_id,
                    "episode_label": episode_label,
                    "episode_ids": episode_ids,
                    "stage_label": job.stage_label,
                    "output_folder": publish_folder,
                    "publish_batch_folder": job.publish_batch_folder,
                    "job_kind": job.job_kind,
                    "variant_count": job.variant_count,
                    "production_run_id": job.production_run_id,
                    "production_preset": {
                        "id": job.production_preset_id,
                        "revision": int(job.production_preset_revision or 0),
                        "content_hash": job.production_preset_hash,
                        "adjusted_after_apply": bool(job.production_preset_dirty),
                    },
                    "resolved_production_settings": deepcopy(job.settings_snapshot),
                    "preview_required": job.job_kind == "preview",
                    "preview_file": job.preview_file,
                    "preview_uri": job.preview_uri,
                    "narration_audio_file": job.narration_audio_file,
                    "failure_diagnostics": _sanitize_hub_diagnostic_value(
                        deepcopy(job.failure_diagnostics)
                    ),
                    "materials": list(self._job_materials.get(job.id) or []),
                    "media_selection": _sanitize_hub_diagnostic_value(
                        dict(self._job_media_selection.get(job.id) or {})
                    ),
                },
            }
        )
        job.batch_id = str(record.get("batch_id") or job.batch_id)
        if projected_status == "awaiting_approval":
            self._record_artifact(job, "sample", job.preview_file)
            self._record_job_sidecars(job, preview=True)
        elif projected_status == "completed":
            output_mode = str(
                job.settings_snapshot.get("output_mode") or "video_and_mp3"
            ).strip().casefold()
            if output_mode == "audio_only":
                # In audio-only mode output_file intentionally points at the
                # MP3. Never misclassify it as a completed video artifact.
                self._record_artifact(
                    job,
                    "narration",
                    job.narration_audio_file or job.output_file,
                )
            else:
                self._record_artifact(job, "video", job.output_file)
                self._record_job_sidecars(job, preview=False)
        if projected_status in {"completed", "failed", "cancelled", "interrupted"}:
            self._release_record_lease(job.production_record_id)

    def _sync_terminal_job_record(self, job: RenderJob) -> None:
        """Persist one terminal Worker result without relying on UI polling."""

        if self._shutdown_in_progress.is_set():
            return
        try:
            self._sync_one_job_record(job)
        except CatalogConflictError:
            with self._lease_lock:
                superseded = (
                    job.production_record_id in self._superseded_lease_records
                )
                if superseded:
                    self._superseded_lease_records.discard(job.production_record_id)
                    self._leased_records.discard(job.production_record_id)
                    self._lease_health.pop(job.production_record_id, None)
            if not superseded:
                raise
        except CatalogNotFoundError:
            # An administrator may intentionally delete a historical batch
            # while the originating workstation is finishing its local
            # process. The missing record is authoritative; retrying forever
            # would freeze every younger batch without anything to update.
            with self._lease_lock:
                self._leased_records.discard(job.production_record_id)
                self._lease_health.pop(job.production_record_id, None)
        except HubRemoteError as error:
            status = int(error.status)
            if status == 409:
                with self._lease_lock:
                    superseded = (
                        job.production_record_id in self._superseded_lease_records
                    )
                    if superseded:
                        self._superseded_lease_records.discard(job.production_record_id)
                        self._leased_records.discard(job.production_record_id)
                        self._lease_health.pop(job.production_record_id, None)
                if superseded:
                    self._release_finished_draft_gates()
                    return
            if status != 404:
                raise
            with self._lease_lock:
                self._leased_records.discard(job.production_record_id)
                self._lease_health.pop(job.production_record_id, None)
        self._release_finished_draft_gates()

    # Kept for older tests/extensions which used the pre-unified callback name.
    def _sync_streamed_job_record(self, job: RenderJob) -> None:
        self._sync_terminal_job_record(job)

    def _sync_job_records(self) -> None:
        """Best-effort queue-to-catalog projection used by local UI and Hub."""

        if self._shutdown_in_progress.is_set():
            return
        for job in [
            item
            for item in (self._queue.get_job(raw["id"]) for raw in self._queue.list_jobs())
            if item is not None and item.production_draft_id
        ]:
            try:
                self._sync_one_job_record(job)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                continue
        self._release_finished_draft_gates()

    def _reconcile_interrupted_records(self) -> None:
        """Recover durable queued work and mark only vanished active work interrupted."""

        device_id = self._current_device_id()
        try:
            records = self._catalog.list_reconciliation_records(device_id)["items"]
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return
        current_job_ids = {
            str(item.get("id") or "") for item in self._queue.list_jobs()
        }
        recoverable_by_record_id: dict[str, RenderJob] = {}
        recoverable_batch_ids: set[str] = set()
        for raw in records:
            record = dict(raw)
            metadata = dict(record.get("metadata") or {})
            if bool(metadata.get("lease_gate")):
                continue
            if str(record.get("status") or "") != "queued":
                continue
            snapshot = metadata.get("job_snapshot")
            if not isinstance(snapshot, dict):
                continue
            try:
                job = RenderJob.from_dict(snapshot)
            except (TypeError, ValueError):
                continue
            job.production_record_id = str(record.get("id") or "")
            job.batch_id = str(record.get("batch_id") or job.batch_id)
            job.status = JobStatus.QUEUED
            job.progress = float(record.get("progress") or 0.0)
            job.stage_label = "软件重启，已恢复排队"
            job.message = "任务已从主机记录恢复，将按原顺序继续制作。"
            if job.id in current_job_ids:
                continue
            recoverable_by_record_id[job.production_record_id] = job
            if job.batch_id:
                recoverable_batch_ids.add(job.batch_id)

        blocked_batches: set[str] = set()
        # Restore the durable draft gate first so the recovered batch cannot be
        # queued a second time while its task records are being reattached.
        for raw in records:
            record = dict(raw)
            metadata = dict(record.get("metadata") or {})
            if not bool(metadata.get("lease_gate")):
                continue
            draft_id = str(
                record.get("draft_id") or metadata.get("draft_id") or ""
            ).strip()
            durable_batch_id = str(metadata.get("durable_batch_id") or "").strip()
            if (
                str(record.get("status") or "") == "queued"
                and draft_id
                and durable_batch_id in recoverable_batch_ids
            ):
                try:
                    self._claim_record_lease(str(record["id"]))
                except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                    blocked_batches.add(durable_batch_id)
                    continue
                with self._lease_lock:
                    self._draft_gate_leases[draft_id] = (
                        str(record["id"]),
                        durable_batch_id,
                    )
                continue
            if str(record.get("lease_owner_device") or "") == device_id:
                self._release_record_lease(str(record["id"]))
            if draft_id:
                self._clear_draft_claim(draft_id)
            if str(record.get("status") or "") in {
                "queued",
                "preflight",
                "sample_ready",
                "awaiting_approval",
                "running",
            }:
                metadata["stage_label"] = "上次软件关闭时中断"
                try:
                    self._catalog.save_production_record(
                        {
                            "id": record["id"],
                            "status": "interrupted",
                            "progress": record.get("progress") or 0,
                            "error_message": "批次创建锁已在重启恢复时清理。",
                            "metadata": metadata,
                        }
                    )
                except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                    pass

        recovered_by_platform: dict[tuple[str, str], list[RenderJob]] = {}
        recovered_any = False
        for record in records:
            metadata = dict(record.get("metadata") or {})
            if bool(metadata.get("lease_gate")):
                continue
            status = str(record.get("status") or "")
            if status == "queued":
                recovered = recoverable_by_record_id.get(str(record.get("id") or ""))
                if recovered is not None and recovered.batch_id not in blocked_batches:
                    platform = self._state.platform_by_id(recovered.platform_id)
                    if platform is not None:
                        try:
                            self._claim_record_lease(recovered.production_record_id)
                        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                            continue
                        recovered_by_platform.setdefault(
                            (recovered.batch_id, recovered.platform_id), []
                        ).append(recovered)
                        continue
                metadata["stage_label"] = "重启恢复失败"
                metadata["recovery_available"] = False
                interruption_message = (
                    "任务缺少可恢复快照，已标记中断；后续排队任务不受影响。"
                )
            elif status in {
                "preflight",
                "sample_ready",
                "awaiting_approval",
                "running",
            }:
                metadata["stage_label"] = "上次软件关闭时中断"
                metadata["recovery_available"] = isinstance(
                    metadata.get("job_snapshot"), dict
                )
                interruption_message = (
                    "软件关闭或设备重启，正在执行的任务已中断，可从生产记录重试。"
                )
            else:
                if str(record.get("lease_owner_device") or "") == device_id:
                    try:
                        self._catalog.release_record_lease(record["id"], device_id)
                    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                        pass
                continue
            try:
                self._catalog.save_production_record(
                    {
                        "id": record["id"],
                        "status": "interrupted",
                        "progress": record.get("progress") or 0,
                        "output_path": record.get("output_path") or "",
                        "error_message": interruption_message,
                        "metadata": metadata,
                    }
                )
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                continue
            # Queue jobs live in memory, but their cross-device lease and the
            # short optimistic draft claim live in SQLite.  When this same
            # machine restarts, both must be released together with the record
            # status; otherwise the user is incorrectly told that the vanished
            # process is still producing the batch for up to three minutes.
            if str(record.get("lease_owner_device") or "") == device_id:
                try:
                    self._catalog.release_record_lease(record["id"], device_id)
                except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                    pass
        for (_batch_id, platform_id), jobs in recovered_by_platform.items():
            platform = self._state.platform_by_id(platform_id)
            if platform is None:
                for job in jobs:
                    self._release_record_lease(job.production_record_id)
                continue
            self._queue.enqueue_jobs(jobs, self._platform_for_local_render(platform))
            recovered_any = True
        if recovered_any:
            try:
                self._queue.start()
            except RuntimeError:
                # PipelineRunner is attached after StoryForgeApi construction;
                # JobQueue.set_processor starts recovered work at that point.
                pass

    def open_output_folder(self, path: str) -> dict[str, Any]:
        def operation() -> dict[str, str]:
            if os.name != "nt":
                raise RuntimeError("打开输出文件夹仅支持 Windows。")
            raw_path = str(path or "").strip()
            if not raw_path:
                raise ValueError("请先选择输出文件夹。")
            folder = Path(raw_path).expanduser()
            if not folder.is_dir():
                raise ValueError("输出文件夹不存在或不是文件夹。")
            resolved = folder.resolve(strict=True)
            os.startfile(str(resolved))
            return {"path": str(resolved)}

        return self._guard(operation)

    def analyze_story(self, file_path: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            from dataclasses import asdict

            from .pipeline import read_manuscript
            from .services.text_processing import analyze_manuscript

            path = Path(file_path)
            if not path.is_file():
                raise ValueError("小说文件不存在。")
            text = read_manuscript(path)
            analysis = analyze_manuscript(
                text,
                path.name,
                wpm=self._state.settings.narration_wpm,
                chapter_pause_seconds=self._state.settings.chapter_pause_seconds,
            )
            return asdict(analysis)

        return self._guard(operation)

    def _validate_provider_readiness(
        self, settings_snapshot: Mapping[str, Any] | None = None
    ) -> None:
        providers = self._state.settings.providers
        frozen = (
            dict(settings_snapshot.get("providers") or {})
            if isinstance(settings_snapshot, Mapping)
            and isinstance(settings_snapshot.get("providers"), Mapping)
            else {}
        )
        text_provider = str(
            frozen.get("text_provider") or providers.text_provider
        ).strip().casefold().replace("-", "_")
        tts_provider = str(
            frozen.get("tts_provider") or providers.tts_provider
        ).strip().casefold().replace("-", "_")
        text_endpoint = str(
            frozen.get("text_endpoint") or providers.text_endpoint
        ).strip()
        kokoro_endpoint = str(
            frozen.get("kokoro_endpoint") or providers.kokoro_endpoint
        ).strip()
        kokoro_command = str(
            frozen.get("kokoro_command") or providers.kokoro_command
        ).strip()

        # Client rendering delegates every text request to HubTextProvider.
        # A stale/missing local cloud text key must not block that path; local
        # TTS readiness is still validated by the checks below.
        if self._runtime_hub_mode == "client":
            text_provider = "hub_text"

        if text_provider == "groq" and not providers.text_api_key.strip():
            raise ValueError("Groq 英文润色尚未填写 API Key。")
        if text_provider == "cloudflare":
            if not providers.text_api_key.strip():
                raise ValueError("Cloudflare Workers AI 尚未填写 API Token。")
            if not text_endpoint:
                raise ValueError(
                    "Cloudflare Workers AI 必须填写包含 Account ID 和模型的完整 API 地址。"
                )

        if tts_provider in {"deepgram", "deepgram_aura", "aura", "aura_2"}:
            if not providers.tts_api_key.strip():
                raise ValueError("Deepgram Aura 女声服务尚未填写 API Key。")
        elif tts_provider in {
            "edge",
            "edge_tts",
            "microsoft_edge",
            "microsoft_edge_tts",
        }:
            if not edge_tts_runtime_available():
                raise ValueError(
                    "当前制作电脑未安装 Edge TTS 组件。请安装 requirements.txt "
                    "中的 edge-tts 后重启 StoryForge；该服务无需 API Key，但生成时必须联网。"
                )
        elif tts_provider in {
            "local_kokoro",
            "kokoro",
            "local",
            "kokoro_local",
            "kokoro_http",
            "kokoro_cli",
        }:
            configured = bool(kokoro_endpoint or kokoro_command)
            if not configured and not embedded_kokoro_available():
                if bool(getattr(sys, "frozen", False)):
                    message = (
                        "当前电脑未安装或未检测到 Kokoro 本地组件。请安装 Kokoro 组件，"
                        "或切换到 Edge TTS / Deepgram Aura。"
                    )
                else:
                    message = (
                        "当前网页服务启动到了不含 Kokoro 的 Python 环境。"
                        "请使用项目 .build-venv 启动 StoryForge；模型文件没有丢失。"
                    )
                raise ValueError(message)

    @staticmethod
    def _batch_from_payload(value: dict[str, Any]) -> BatchSpec:
        required = ("platform_id", "text_folder", "output_folder")
        cleaned = {
            key: str(value.get(key) or "").strip()
            for key in (
                *required,
                "video_folder",
                "music_folder",
                "source_narration_audio",
            )
        }
        missing = [key for key in required if not cleaned[key]]
        if missing:
            raise ValueError("批次信息不完整，请选择平台、小说和输出文件夹。")
        raw_mode = value.get("output_mode")
        if raw_mode is not None:
            output_mode = str(raw_mode or "").strip().casefold()
            if output_mode not in {"video_and_mp3", "audio_only", "reuse_audio"}:
                raise ValueError("输出内容必须选择常规视频生成、仅生成配音或已有配音更换素材。")
        elif "export_narration_audio" in value:
            # V0.3 sent a boolean "extra narration export" checkbox. False
            # meant video-only, not audio-only. Keep the legacy boolean as an
            # immutable compatibility promise; current clients explicitly
            # submit False because regular video now publishes MP4 only.
            legacy_export = value["export_narration_audio"]
            if not isinstance(legacy_export, bool):
                raise ValueError("旧版纯旁白导出设置必须是开启或关闭。")
            output_mode = "video_and_mp3"
        else:
            output_mode = "video_and_mp3"
        cleaned["output_mode"] = output_mode
        raw_export = value.get("export_narration_audio", False)
        if not isinstance(raw_export, bool):
            raise ValueError("配音导出设置必须是开启或关闭。")
        cleaned["export_narration_audio"] = bool(
            output_mode == "video_and_mp3" and raw_export
        )
        return BatchSpec(**cleaned)
