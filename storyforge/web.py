from __future__ import annotations

import cgi
import hashlib
import hmac
import inspect
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
import zipfile
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import url2pathname

from . import __version__
from .credentials import (
    PASSWORD_MIN_LENGTH,
    hash_password as _password_hash,
    password_matches as _password_matches,
    validate_new_password,
)
from .config import SecretProtector
from .rpc_contract import (
    CLIENT_LOCAL_MEDIA_METHODS,
    WEB_DESKTOP_ONLY_MEDIA_METHODS,
    WEB_RPC_PERMISSIONS,
)


SESSION_COOKIE = "storyforge_session"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024 + 2 * 1024 * 1024
MAX_FILE_REFERENCES_PER_SESSION = 256
MAX_SESSIONS = 512
MAX_SESSIONS_PER_IP = 32
MAX_LOGIN_ATTEMPTS_PER_IP_MINUTE = 30
MAX_FAILED_LOGIN_ENTRIES = 2048
PERSISTENT_SESSION_SCHEMA_VERSION = 1
PERSISTENT_SESSION_FILENAME = "web-sessions.json"

UPLOAD_KIND_LIMITS = {
    "txt": 20 * 1024 * 1024,
    "docx": 50 * 1024 * 1024,
    "cover": 20 * 1024 * 1024,
    "platform_logo": 20 * 1024 * 1024,
    "update_package": 2 * 1024 * 1024 * 1024,
    "component_package": 2 * 1024 * 1024 * 1024,
}


UPLOAD_EXTENSIONS: dict[str, frozenset[str]] = {
    "novel": frozenset({".txt", ".docx"}),
    "txt": frozenset({".txt"}),
    "docx": frozenset({".docx"}),
    "summary": frozenset({".txt", ".docx"}),
    "cover": frozenset({".jpg", ".jpeg", ".png", ".webp"}),
    "platform_logo": frozenset({".jpg", ".jpeg", ".png", ".webp"}),
    "update_package": frozenset({".zip"}),
    "component_package": frozenset({".zip"}),
}


CONTROLLED_UPLOAD_ARGUMENTS: dict[str, tuple[Any, ...]] = {
    "import_novel_file": (0, "file_path"),
    "read_text_document": (0,),
    "save_novel": (0, "cover_path"),
    "save_platform": (0, "logo_path"),
    "publish_update": (0,),
    "publish_component_update": (0,),
    "analyze_story": (0,),
}


@dataclass(slots=True)
class _WebSession:
    id: str
    csrf_token: str
    actor_user_id: str
    username: str
    display_name: str
    role: str
    permissions: frozenset[str]
    password_configured: bool
    expires_at: float
    remember: bool
    client_ip: str = ""
    cookie_hash: str = ""
    credential_fingerprint: str = ""


@dataclass(slots=True)
class _FileReference:
    id: str
    session_id: str
    actor_user_id: str
    path: Path
    filename: str
    expires_at: float
    uploaded: bool = False
    kind: str = "media"


@dataclass(frozen=True, slots=True)
class _WebActorAccess:
    user_id: str
    permissions: frozenset[str]


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _safe_filename(value: str, fallback: str = "upload.bin") -> str:
    name = Path(str(value or "").replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", name).strip(" .")
    return (name[:180] or fallback)


class ClientLocalWebAuthority:
    """Identity/ownership adapter for a workstation's loopback Web UI.

    Shared catalog data and authorization remain authoritative on the Hub.
    The adapter deliberately obtains a fresh device session for every browser
    request; it never authenticates the browser with, or returns, the stored
    bearer token.
    """

    def __init__(self, api: Any) -> None:
        self.api = api

    @property
    def catalog(self) -> Any:
        return self.api._catalog

    def session_identity(self) -> dict[str, Any]:
        if str(getattr(self.api, "_runtime_hub_mode", "")) != "client":
            raise PermissionError("client-local browser requires Hub client mode")
        client = getattr(self.api, "_hub_client", None)
        if client is None:
            raise PermissionError("the bound Hub session is unavailable")
        identity = client.get_device_session()
        user = dict(identity.get("user") or {})
        device = dict(identity.get("device") or {})
        configured = self.api._state.settings.hub
        configured_device = str(configured.device_id or "").strip()
        configured_account = str(configured.account_username or "").strip().casefold()
        if (
            not user.get("id")
            or not bool(user.get("active"))
            or not device.get("id")
            or not bool(device.get("active"))
            or (configured_device and str(device.get("id")) != configured_device)
            or (
                configured_account
                and str(user.get("username") or "").strip().casefold()
                != configured_account
            )
        ):
            raise PermissionError("the bound workstation identity changed")
        return {
            "user": user,
            "device": device,
            "permissions": [str(item) for item in identity.get("permissions") or []],
        }

    def _actor_access(self, actor_user_id: str | None) -> _WebActorAccess:
        identity = self.session_identity()
        user_id = str((identity.get("user") or {}).get("id") or "")
        if not actor_user_id or str(actor_user_id) != user_id:
            raise PermissionError("the browser session is not bound to this account")
        return _WebActorAccess(
            user_id=user_id,
            permissions=frozenset(identity.get("permissions") or []),
        )

    @staticmethod
    def _can_manage_all_drafts(access: _WebActorAccess) -> bool:
        return bool({"drafts.manage_all", "hub.manage"} & access.permissions)

    @staticmethod
    def _can_manage_all_records(access: _WebActorAccess) -> bool:
        return bool({"jobs.retry_all", "drafts.manage_all", "hub.manage"} & access.permissions)

    def _require_own_draft(
        self, draft_id: str, access: _WebActorAccess
    ) -> dict[str, Any]:
        draft = self.catalog.get_draft(str(draft_id))
        if (
            not self._can_manage_all_drafts(access)
            and str(draft.get("created_by_user_id") or "") != access.user_id
        ):
            raise PermissionError("this draft belongs to another software user")
        return draft

    def _require_own_record(
        self, record_id: str, access: _WebActorAccess
    ) -> dict[str, Any]:
        record = self.catalog.get_record(str(record_id))
        if (
            not self._can_manage_all_records(access)
            and str(record.get("created_by_user_id") or "") != access.user_id
        ):
            raise PermissionError("this production record belongs to another software user")
        return record

    def _record_error(self, error: BaseException) -> None:
        self.api._hub_error = f"{type(error).__name__}: {error}"


class StoryForgeWebApplication:
    """Authenticated browser facade attached to the existing Hub listener."""

    def __init__(
        self,
        api: Any,
        hub: Any,
        ui_root: str | Path,
        upload_root: str | Path,
        *,
        client_local: bool = False,
    ) -> None:
        self.api = api
        self.hub = hub
        self.client_local = bool(client_local)
        self.ui_root = Path(ui_root).resolve(strict=True)
        if not self.ui_root.is_dir():
            raise ValueError("ui_root must be a directory")
        # Freeze the browser UI together with the backend process that loaded
        # it.  An updater may replace these files before restarting; serving
        # them immediately would pair a newer RPC client with an older Python
        # backend for the remainder of that process lifetime.
        self._static_assets = {
            filename: (self.ui_root / filename).read_bytes()
            for filename in ("index.html", "app.js", "styles.css")
        }
        self.upload_root = Path(upload_root).resolve()
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._sessions: dict[str, _WebSession] = {}
        self._persistent_sessions: dict[str, _WebSession] = {}
        self._file_refs: dict[str, _FileReference] = {}
        self._failed_logins: dict[str, tuple[int, float]] = {}
        self._login_attempts_by_ip: dict[str, list[float]] = {}
        repository = getattr(api, "_repository", None)
        data_dir = Path(getattr(repository, "data_dir", self.upload_root)).resolve()
        self._session_store_path = data_dir / PERSISTENT_SESSION_FILENAME
        self._session_protector = SecretProtector()
        self._session_persistence_error = ""
        if not self.client_local:
            self._load_persistent_sessions()
        self._cleanup_stale_uploads()

    @staticmethod
    def _cookie_hash(value: str) -> str:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _credential_fingerprint(user: dict[str, Any] | None) -> str:
        verifier = str((user or {}).get("password_hash") or "")
        return hashlib.sha256(verifier.encode("utf-8")).hexdigest() if verifier else ""

    @staticmethod
    def _persistent_session_payload(
        cookie_hash: str, session: _WebSession
    ) -> dict[str, Any]:
        return {
            "cookie_sha256": cookie_hash,
            "csrf_token": session.csrf_token,
            "actor_user_id": session.actor_user_id,
            "username": session.username,
            "display_name": session.display_name,
            "role": session.role,
            "permissions": sorted(session.permissions),
            "password_configured": bool(session.password_configured),
            "expires_at": float(session.expires_at),
            "remember": bool(session.remember),
            "client_ip": session.client_ip,
            "credential_fingerprint": session.credential_fingerprint,
        }

    def _load_persistent_sessions(self) -> None:
        """Restore password sessions without ever reading a raw cookie from disk."""

        try:
            wrapper = json.loads(self._session_store_path.read_text(encoding="utf-8"))
            if not isinstance(wrapper, dict):
                return
            protected = str(wrapper.get("protected") or "")
            if os.name == "nt" and not protected:
                # Windows Hub sessions are accepted only from a DPAPI envelope.
                return
            if protected:
                value = json.loads(self._session_protector.unprotect(protected))
            else:
                value = wrapper
            if not isinstance(value, dict):
                return
            if int(value.get("schema_version") or 0) != PERSISTENT_SESSION_SCHEMA_VERSION:
                return
            rows = value.get("sessions")
            if not isinstance(rows, list):
                return
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
            return

        now = time.time()
        restored: dict[str, _WebSession] = {}
        per_ip: dict[str, int] = {}
        for raw in rows:
            if not isinstance(raw, dict) or len(restored) >= MAX_SESSIONS:
                continue
            try:
                cookie_hash = str(raw.get("cookie_sha256") or "").lower()
                csrf_token = str(raw.get("csrf_token") or "")
                actor_user_id = str(raw.get("actor_user_id") or "")
                expires_at = float(raw.get("expires_at") or 0)
                credential_fingerprint = str(
                    raw.get("credential_fingerprint") or ""
                ).lower()
                client_ip = str(raw.get("client_ip") or "")[:200]
                if (
                    not re.fullmatch(r"[0-9a-f]{64}", cookie_hash)
                    or not re.fullmatch(r"[0-9a-f]{64}", credential_fingerprint)
                    or not actor_user_id
                    or len(actor_user_id) > 200
                    or len(csrf_token) < 20
                    or len(csrf_token) > 512
                    or expires_at <= now
                    or per_ip.get(client_ip, 0) >= MAX_SESSIONS_PER_IP
                ):
                    continue
                user = self.hub.catalog._web_user_by_id(actor_user_id)
                if (
                    not user
                    or not bool(user.get("active"))
                    or not hmac.compare_digest(
                        credential_fingerprint,
                        self._credential_fingerprint(user),
                    )
                ):
                    continue
                access = self.hub._actor_access(actor_user_id)
                restored[cookie_hash] = _WebSession(
                    id="",
                    csrf_token=csrf_token,
                    actor_user_id=actor_user_id,
                    username=str(user.get("username") or ""),
                    display_name=str(user.get("display_name") or ""),
                    role=str(user.get("role") or "producer"),
                    permissions=access.permissions,
                    password_configured=bool(user.get("password_hash")),
                    expires_at=expires_at,
                    remember=bool(raw.get("remember")),
                    client_ip=client_ip,
                    cookie_hash=cookie_hash,
                    credential_fingerprint=credential_fingerprint,
                )
                per_ip[client_ip] = per_ip.get(client_ip, 0) + 1
            except Exception:
                continue
        with self._lock:
            self._persistent_sessions = restored

    def _save_persistent_sessions_locked(self) -> None:
        """Atomically persist durable sessions; Windows encrypts the payload."""

        payload = {
            "schema_version": PERSISTENT_SESSION_SCHEMA_VERSION,
            "sessions": [
                self._persistent_session_payload(cookie_hash, session)
                for cookie_hash, session in sorted(self._persistent_sessions.items())
            ],
        }
        try:
            if os.name == "nt":
                serialized = json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                )
                wrapper: dict[str, Any] = {
                    "schema_version": PERSISTENT_SESSION_SCHEMA_VERSION,
                    "protected": self._session_protector.protect(serialized),
                }
            else:
                wrapper = payload
            self._session_store_path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{self._session_store_path.stem}-",
                suffix=".tmp",
                dir=self._session_store_path.parent,
            )
            try:
                try:
                    os.fchmod(handle, 0o600)
                except (AttributeError, OSError):
                    pass
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(wrapper, stream, ensure_ascii=False, separators=(",", ":"))
                    stream.write("\n")
                os.replace(temporary_name, self._session_store_path)
                try:
                    self._session_store_path.chmod(0o600)
                except OSError:
                    pass
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise
            self._session_persistence_error = ""
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            # Never fall back to plaintext on Windows when DPAPI is unavailable.
            self._session_persistence_error = f"{type(error).__name__}: {error}"
            # A stale durable file is less safe than signing every browser out.
            # If an update cannot be committed, invalidate all saved sessions.
            try:
                self._session_store_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _revoke_session_locked(self, session: _WebSession) -> None:
        if session.id:
            self._sessions.pop(session.id, None)
        cookie_hash = session.cookie_hash or (
            self._cookie_hash(session.id) if session.id else ""
        )
        if cookie_hash:
            self._persistent_sessions.pop(cookie_hash, None)

    def _cleanup_stale_uploads(self) -> None:
        cutoff = time.time() - 48 * 3600
        try:
            candidates = tuple(self.upload_root.glob("*/*"))
        except OSError:
            return
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(self.upload_root)
                if resolved.is_file() and resolved.stat().st_mtime < cutoff:
                    resolved.unlink()
            except (OSError, RuntimeError, ValueError):
                continue

    @staticmethod
    def _local_absolute_path(value: Any) -> Path:
        raw = str(value or "").strip()
        normalized = raw.replace("/", "\\")
        if (
            not raw
            or "\x00" in raw
            or normalized.startswith("\\\\")
            or normalized.startswith("\\??\\")
            or any(character in raw for character in ("*", "?"))
        ):
            raise ValueError("网页端不允许 UNC、设备或通配符路径。")
        path = Path(raw).expanduser()
        if not path.is_absolute() or path.parent == path:
            raise ValueError("请使用管理员允许的非根目录本机绝对路径。")
        drive, tail = os.path.splitdrive(str(path))
        if os.name == "nt" and (not re.fullmatch(r"[A-Za-z]:", drive) or ":" in tail):
            raise ValueError("网页端不允许设备路径或备用数据流。")
        return path.resolve(strict=False)

    def _web_workspace_root(self) -> Path:
        root = (self.api._repository.data_dir / "web-workspace").resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _web_default_folders(self) -> dict[str, str]:
        if not self.client_local:
            # A Hub browser is a control surface, not a renderer. Returning
            # paths from the Hub computer made employees accidentally render
            # against the administrator's disks.
            return {}
        from .worker import LocalWorkerProfileStore

        return LocalWorkerProfileStore(self.api._repository.data_dir).load()

    def _legacy_web_default_folders(self) -> dict[str, str]:
        """Kept only for migration tests; never exposed by the Hub page."""

        root = self._web_workspace_root()
        values: dict[str, str] = {}
        for key, name in (
            ("video_folder", "videos"),
            ("music_folder", "music"),
            ("output_folder", "output"),
        ):
            folder = (root / name).resolve()
            folder.mkdir(parents=True, exist_ok=True)
            values[key] = str(folder)
        return values

    def _effective_web_roots(self) -> tuple[Path, ...]:
        if not self.client_local:
            return ()
        candidates: list[Any] = list(self._web_default_folders().values())
        # Keep previously configured workstation roots as a compatibility
        # allowlist. New Hub pages use LocalWorkerProfileStore directly and do
        # not require an administrator to manage employee paths.
        candidates.extend(
            list(getattr(self.api._state.settings.hub, "web_allowed_roots", ()) or ())
        )
        roots: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                root = self._local_absolute_path(candidate)
                if not root.is_dir():
                    continue
                key = os.path.normcase(str(root))
                if key in seen:
                    continue
                seen.add(key)
                roots.append(root)
            except (OSError, RuntimeError, ValueError):
                continue
        return tuple(roots)

    def _validated_web_folder(self, value: Any) -> Path:
        candidate = self._local_absolute_path(value)
        for root in self._effective_web_roots():
            try:
                candidate.relative_to(root)
                return candidate
            except ValueError:
                continue
        raise ValueError(
            "该路径不在管理员允许的网页工作目录内。"
        )

    def _validate_draft_folders(
        self, value: dict[str, Any], *, existing: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = dict(value)
        if not self.client_local:
            # Shared drafts contain logical workstation markers only. Actual
            # paths are injected by the authenticated localhost worker at the
            # moment a production run is queued.
            payload.update(
                {
                    "video_folder": "worker://local/videos",
                    "music_folder": "worker://local/music",
                    "output_folder": "worker://local/output",
                }
            )
            return payload
        # A workstation-local browser must never inherit paths persisted by a
        # different rendering PC. Only values supplied for this request, or
        # this installation's private defaults, are eligible.
        metadata = (
            {}
            if self.client_local
            else dict((existing or {}).get("metadata") or {})
        )
        defaults = self._web_default_folders()
        for key in ("video_folder", "music_folder", "output_folder"):
            raw = payload.get(key) or metadata.get(key) or defaults[key]
            payload[key] = str(self._validated_web_folder(raw))
        return payload

    @staticmethod
    def _discard_reference_file(reference: _FileReference) -> None:
        if not reference.uploaded:
            return
        try:
            reference.path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def owns_path(path: str) -> bool:
        return path in {
            "/",
            "/index.html",
            "/app.js",
            "/styles.css",
            "/web/api/health",
            "/web/api/session",
            "/web/api/session/login",
            "/web/api/session/password",
            "/web/api/local-worker-ticket",
            "/web/api/rpc",
            "/web/api/upload",
            "/web/api/media",
            "/web/api/download",
        }

    def close(self) -> None:
        with self._lock:
            references = tuple(self._file_refs.values())
            self._sessions.clear()
            self._file_refs.clear()
        for reference in references:
            self._discard_reference_file(reference)

    # HTTP helpers -----------------------------------------------------
    @staticmethod
    def _send_json(
        handler: Any,
        status: int,
        *,
        ok: bool,
        data: Any = None,
        error: str = "",
        code: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            {"ok": bool(ok), "data": data, "error": str(error), "code": str(code)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        handler.send_response(int(status))
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        pending_cookie = str(
            getattr(handler, "_storyforge_set_cookie", "") or ""
        )
        if pending_cookie and not (headers and "Set-Cookie" in headers):
            handler.send_header("Set-Cookie", pending_cookie)
        if headers:
            for key, value in headers.items():
                handler.send_header(key, value)
        handler.send_header("Connection", "close")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)
        handler.close_connection = True

    @classmethod
    def _failure(
        cls,
        handler: Any,
        status: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        cls._send_json(
            handler,
            status,
            ok=False,
            error=message,
            code=code,
            data=None,
            headers=headers,
        )

    @staticmethod
    def _read_json(handler: Any, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
        content_type = (
            handler.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        )
        if content_type != "application/json":
            raise ValueError("Content-Type 必须是 application/json。")
        try:
            length = int(handler.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            raise ValueError("请求缺少 Content-Length。") from None
        if not 0 < length <= maximum:
            raise ValueError("请求内容为空或超过大小限制。")
        raw = handler.rfile.read(length)
        if len(raw) != length:
            raise ValueError("请求内容不完整。")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求不是有效的 UTF-8 JSON。") from None
        if not isinstance(value, dict):
            raise ValueError("请求 JSON 必须是对象。")
        return value

    def _send_static(self, handler: Any, filename: str) -> None:
        body = self._static_assets.get(filename)
        if body is None:
            self._failure(handler, HTTPStatus.NOT_FOUND, "not_found", "页面资源不存在。")
            return
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; media-src 'self' blob: http://127.0.0.1:*; "
            "connect-src 'self' http://127.0.0.1:*; "
            "font-src 'self' data:; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        handler.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        handler.send_header("Connection", "close")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)
        handler.close_connection = True

    # Session/authentication -----------------------------------------
    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            expired_sessions = {
                key for key, value in self._sessions.items() if value.expires_at <= now
            }
            for key in expired_sessions:
                session = self._sessions.pop(key, None)
                if session is not None and session.cookie_hash:
                    self._persistent_sessions.pop(session.cookie_hash, None)
            expired_persistent_sessions = {
                key
                for key, value in self._persistent_sessions.items()
                if value.expires_at <= now
            }
            for key in expired_persistent_sessions:
                self._persistent_sessions.pop(key, None)
            if not self.client_local and (
                expired_sessions or expired_persistent_sessions
            ):
                self._save_persistent_sessions_locked()
            for key, value in tuple(self._file_refs.items()):
                if value.expires_at <= now or value.session_id in expired_sessions:
                    self._file_refs.pop(key, None)
                    self._discard_reference_file(value)
            for key, (_count, until) in tuple(self._failed_logins.items()):
                if until <= now:
                    self._failed_logins.pop(key, None)
            cutoff = now - 60.0
            for address, attempts in tuple(self._login_attempts_by_ip.items()):
                current = [item for item in attempts if item >= cutoff]
                if current:
                    self._login_attempts_by_ip[address] = current
                else:
                    self._login_attempts_by_ip.pop(address, None)

    @staticmethod
    def _cookie_session_id(handler: Any) -> str:
        raw = str(handler.headers.get("Cookie") or "")
        if not raw:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return ""
        morsel = cookie.get(SESSION_COOKIE)
        return str(morsel.value if morsel is not None else "")

    def _session(self, handler: Any, *, csrf: bool = False) -> _WebSession | None:
        self._prune()
        session_id = self._cookie_session_id(handler)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None and session_id and not self.client_local:
                cookie_hash = self._cookie_hash(session_id)
                persistent = self._persistent_sessions.get(cookie_hash)
                if persistent is not None:
                    session = replace(
                        persistent,
                        id=session_id,
                        cookie_hash=cookie_hash,
                    )
                    self._sessions[session_id] = session
        if self.client_local:
            try:
                identity = self.hub.session_identity()
                user = dict(identity.get("user") or {})
                device = dict(identity.get("device") or {})
                permissions = frozenset(
                    str(item) for item in identity.get("permissions") or []
                )
                if not user.get("id") or not device.get("id"):
                    raise PermissionError("bound account or device is unavailable")
            except Exception:
                if session is not None:
                    with self._lock:
                        self._sessions.pop(session.id, None)
                self._failure(
                    handler,
                    HTTPStatus.UNAUTHORIZED,
                    "session_revoked",
                    "The bound StoryForge account or workstation is no longer available.",
                )
                return None
            if session is None or session.expires_at <= time.time():
                lifetime = 30 * 24 * 3600
                candidate = _WebSession(
                    id=secrets.token_urlsafe(32),
                    csrf_token=secrets.token_urlsafe(32),
                    actor_user_id=str(user["id"]),
                    username=str(user.get("username") or ""),
                    display_name=str(user.get("display_name") or ""),
                    role=str(user.get("role") or "producer"),
                    permissions=permissions,
                    password_configured=True,
                    expires_at=time.time() + lifetime,
                    remember=False,
                    client_ip=str(handler.client_address[0] or ""),
                )
                capacity_error = ""
                with self._lock:
                    if len(self._sessions) >= MAX_SESSIONS:
                        capacity_error = "The workstation browser session limit was reached."
                    elif sum(
                        1
                        for item in self._sessions.values()
                        if item.client_ip == candidate.client_ip
                    ) >= MAX_SESSIONS_PER_IP:
                        capacity_error = "Too many workstation browser sessions are open."
                    else:
                        self._sessions[candidate.id] = candidate
                if capacity_error:
                    self._failure(
                        handler,
                        HTTPStatus.TOO_MANY_REQUESTS,
                        "session_capacity_reached",
                        capacity_error,
                    )
                    return None
                session = candidate
                handler._storyforge_set_cookie = (
                    f"{SESSION_COOKIE}={session.id}; Path=/; HttpOnly; "
                    "SameSite=Strict"
                )
            elif session.actor_user_id != str(user["id"]):
                with self._lock:
                    self._sessions.pop(session.id, None)
                self._failure(
                    handler,
                    HTTPStatus.UNAUTHORIZED,
                    "session_revoked",
                    "The workstation is now bound to a different account.",
                )
                return None
            session.permissions = permissions
            session.username = str(user.get("username") or "")
            session.display_name = str(user.get("display_name") or "")
            session.role = str(user.get("role") or "producer")
            session.password_configured = True
            if csrf:
                supplied = str(handler.headers.get("X-StoryForge-CSRF") or "")
                if not supplied or not hmac.compare_digest(
                    supplied.encode("utf-8"), session.csrf_token.encode("utf-8")
                ):
                    self._failure(
                        handler,
                        HTTPStatus.FORBIDDEN,
                        "csrf_failed",
                        "Page security validation failed; refresh and try again.",
                    )
                    return None
            return session
        if session is None or session.expires_at <= time.time():
            self._failure(
                handler, HTTPStatus.UNAUTHORIZED, "not_authenticated", "请先登录。"
            )
            return None
        # Re-evaluate account activity and effective permissions on every
        # request. Role changes, explicit permission overrides and disabled
        # accounts therefore take effect without waiting for session expiry.
        user = self.hub.catalog._web_user_by_id(session.actor_user_id)
        credential_fingerprint = self._credential_fingerprint(user)
        if (
            not user
            or not bool(user.get("active"))
            or not session.credential_fingerprint
            or not hmac.compare_digest(
                session.credential_fingerprint, credential_fingerprint
            )
        ):
            with self._lock:
                self._revoke_session_locked(session)
                self._save_persistent_sessions_locked()
            self._failure(
                handler,
                HTTPStatus.UNAUTHORIZED,
                "session_revoked",
                "账号已停用，请重新登录。",
            )
            return None
        try:
            access = self.hub._actor_access(session.actor_user_id)
        except Exception:
            with self._lock:
                self._revoke_session_locked(session)
                self._save_persistent_sessions_locked()
            self._failure(
                handler,
                HTTPStatus.UNAUTHORIZED,
                "session_revoked",
                "账号权限已失效，请重新登录。",
            )
            return None
        session.permissions = access.permissions
        session.username = str(user["username"])
        session.display_name = str(user.get("display_name") or "")
        session.role = str(user.get("role") or "producer")
        session.password_configured = bool(user.get("password_hash"))
        session.credential_fingerprint = credential_fingerprint
        if csrf:
            supplied = str(handler.headers.get("X-StoryForge-CSRF") or "")
            if not supplied or not hmac.compare_digest(
                supplied.encode("utf-8"), session.csrf_token.encode("utf-8")
            ):
                self._failure(
                    handler,
                    HTTPStatus.FORBIDDEN,
                    "csrf_failed",
                    "页面安全校验失败，请刷新页面后重试。",
                )
                return None
        return session

    @staticmethod
    def _public_user(user: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(user.get("id") or ""),
            "username": str(user.get("username") or ""),
            "display_name": str(user.get("display_name") or ""),
            "role": str(user.get("role") or "producer"),
            "active": bool(user.get("active")),
        }

    def _session_payload(self, session: _WebSession) -> dict[str, Any]:
        payload = {
            "authenticated": True,
            "user": {
                "id": session.actor_user_id,
                "username": session.username,
                "display_name": session.display_name,
                "role": session.role,
                "active": True,
            },
            "permissions": sorted(session.permissions),
            "csrf_token": session.csrf_token,
            "expires_at": _utc_iso(session.expires_at),
            "password_configured": session.password_configured,
            "must_set_password": not session.password_configured,
        }
        if self.client_local:
            device_name = str(
                self.api._state.settings.hub.device_name or "This workstation"
            )
            payload.update(
                {
                    "password_configured": True,
                    "must_set_password": False,
                    "host_name": device_name,
                    "capabilities": {
                        "client_local": True,
                        "media_rpc": True,
                        "password_change": False,
                        "logout": False,
                    },
                }
            )
        return payload

    def _login_key(self, handler: Any, username: str) -> str:
        return f"{handler.client_address[0]}\0{username.casefold()}"

    def _login_blocked(self, key: str) -> bool:
        with self._lock:
            value = self._failed_logins.get(key)
        return bool(value and value[1] > time.time() and value[0] >= 5)

    def _record_login_attempt(self, client_ip: str) -> bool:
        now = time.time()
        cutoff = now - 60.0
        with self._lock:
            attempts = [
                value
                for value in self._login_attempts_by_ip.get(client_ip, [])
                if value >= cutoff
            ]
            if len(attempts) >= MAX_LOGIN_ATTEMPTS_PER_IP_MINUTE:
                self._login_attempts_by_ip[client_ip] = attempts
                return False
            attempts.append(now)
            self._login_attempts_by_ip[client_ip] = attempts
            return True

    def _login_failed(self, key: str) -> None:
        with self._lock:
            if key not in self._failed_logins and len(self._failed_logins) >= MAX_FAILED_LOGIN_ENTRIES:
                oldest = min(
                    self._failed_logins,
                    key=lambda item: self._failed_logins[item][1],
                    default="",
                )
                if oldest:
                    self._failed_logins.pop(oldest, None)
            count, _until = self._failed_logins.get(key, (0, 0.0))
            count += 1
            delay = min(300.0, 2.0 ** min(count, 8)) if count >= 5 else 1.0
            self._failed_logins[key] = (count, time.time() + delay)

    def _login(self, handler: Any) -> None:
        if self.client_local:
            self._failure(
                handler,
                HTTPStatus.METHOD_NOT_ALLOWED,
                "client_local_session",
                "This loopback UI uses the workstation's bound account automatically.",
            )
            return
        self._prune()
        client_ip = str(handler.client_address[0] or "")
        try:
            value = self._read_json(handler, maximum=64 * 1024)
            username = str(value.get("username") or "").strip()
            password = str(value.get("password") or "")
            remember = bool(value.get("remember"))
            if not username or not password or len(username) > 200 or len(password) > 4096:
                raise ValueError("请输入账号和密码。")
        except ValueError as error:
            self._failure(handler, HTTPStatus.BAD_REQUEST, "invalid_login", str(error))
            return
        if not self._record_login_attempt(client_ip):
            self._failure(
                handler,
                HTTPStatus.TOO_MANY_REQUESTS,
                "login_rate_limited",
                "登录请求过于频繁，请一分钟后再试。",
            )
            return
        key = self._login_key(handler, username)
        if self._login_blocked(key):
            self._failure(
                handler,
                HTTPStatus.TOO_MANY_REQUESTS,
                "login_rate_limited",
                "登录尝试过多，请稍后再试。",
            )
            return

        user: dict[str, Any] | None = None
        candidate = self.hub.catalog._web_user_by_username(username)
        if candidate and _password_matches(
            password, str(candidate.get("password_hash") or "")
        ):
            user = candidate
        if not user or not bool(user.get("active")):
            self._login_failed(key)
            # Keep account existence and password mismatch indistinguishable.
            time.sleep(0.12)
            self._failure(
                handler,
                HTTPStatus.UNAUTHORIZED,
                "login_failed",
                "账号或密码不正确。",
            )
            return
        try:
            access = self.hub._actor_access(str(user["id"]))
        except Exception:
            self._login_failed(key)
            self._failure(
                handler, HTTPStatus.FORBIDDEN, "account_unavailable", "该账号当前不可用。"
            )
            return
        # A successful sign-in stays valid for thirty days on both the browser
        # and installed workstation surfaces. Account deactivation and password
        # changes still invalidate affected sessions immediately.
        lifetime = 30 * 24 * 3600
        session = _WebSession(
            id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            actor_user_id=str(user["id"]),
            username=str(user["username"]),
            display_name=str(user.get("display_name") or ""),
            role=str(user.get("role") or "producer"),
            permissions=access.permissions,
            password_configured=bool(user.get("password_hash")),
            expires_at=time.time() + lifetime,
            remember=remember,
            client_ip=client_ip,
            credential_fingerprint=self._credential_fingerprint(user),
        )
        session.cookie_hash = self._cookie_hash(session.id)
        with self._lock:
            if len(self._persistent_sessions) >= MAX_SESSIONS:
                self._failure(
                    handler,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "session_capacity_reached",
                    "网页会话数量已达到上限，请先退出其他设备。",
                )
                return
            address_sessions = sum(
                1
                for item in self._persistent_sessions.values()
                if item.client_ip == client_ip
            )
            if address_sessions >= MAX_SESSIONS_PER_IP:
                self._failure(
                    handler,
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "session_capacity_reached",
                    "当前网络地址的登录会话过多，请先退出旧会话。",
                )
                return
            self._failed_logins.pop(key, None)
            self._sessions[session.id] = session
            self._persistent_sessions[session.cookie_hash] = replace(session, id="")
            self._save_persistent_sessions_locked()
        cookie = (
            f"{SESSION_COOKIE}={session.id}; Path=/; HttpOnly; SameSite=Strict"
            + f"; Max-Age={lifetime}"
        )
        self._send_json(
            handler,
            HTTPStatus.OK,
            ok=True,
            data=self._session_payload(session),
            headers={"Set-Cookie": cookie},
        )

    def _logout(self, handler: Any) -> None:
        if self.client_local:
            self._failure(
                handler,
                HTTPStatus.METHOD_NOT_ALLOWED,
                "client_local_session",
                "Disconnect this workstation from StoryForge settings instead.",
            )
            return
        session = self._session(handler, csrf=True)
        if session is None:
            return
        with self._lock:
            self._revoke_session_locked(session)
            self._save_persistent_sessions_locked()
            discarded: list[_FileReference] = []
            for key, reference in tuple(self._file_refs.items()):
                if reference.session_id == session.id:
                    self._file_refs.pop(key, None)
                    discarded.append(reference)
        for reference in discarded:
            self._discard_reference_file(reference)
        self._send_json(
            handler,
            HTTPStatus.OK,
            ok=True,
            data={"authenticated": False},
            headers={
                "Set-Cookie": (
                    f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                )
            },
        )

    def _change_password(self, handler: Any) -> None:
        if self.client_local:
            self._failure(
                handler,
                HTTPStatus.FORBIDDEN,
                "password_change_disabled",
                "Password changes are disabled in the workstation-local browser.",
            )
            return
        session = self._session(handler, csrf=True)
        if session is None:
            return
        try:
            value = self._read_json(handler, maximum=64 * 1024)
            current = str(value.get("current_password") or "")
            new = str(value.get("new_password") or "")
            validate_new_password(new)
            user = self.hub.catalog._web_user_by_id(session.actor_user_id)
            if not user or not user.get("active"):
                raise ValueError("该账号当前不可用。")
            old_verifier = str(user.get("password_hash") or "")
            current_ok = bool(old_verifier) and _password_matches(
                current, old_verifier
            )
            if not current_ok:
                raise ValueError("当前密码不正确。")
            self.hub.catalog._set_web_password_hash(
                session.actor_user_id, _password_hash(new)
            )
            updated_user = self.hub.catalog._web_user_by_id(session.actor_user_id)
            session.password_configured = True
            session.credential_fingerprint = self._credential_fingerprint(updated_user)
            with self._lock:
                revoked_session_ids = {
                    item.id
                    for item in self._sessions.values()
                    if item.actor_user_id == session.actor_user_id
                    and item.id != session.id
                }
                for session_id in revoked_session_ids:
                    self._sessions.pop(session_id, None)
                for cookie_hash, stored in tuple(self._persistent_sessions.items()):
                    if (
                        stored.actor_user_id == session.actor_user_id
                        and cookie_hash != session.cookie_hash
                    ):
                        self._persistent_sessions.pop(cookie_hash, None)
                if session.cookie_hash:
                    self._persistent_sessions[session.cookie_hash] = replace(
                        session, id=""
                    )
                self._save_persistent_sessions_locked()
                revoked_references = [
                    reference
                    for reference in self._file_refs.values()
                    if reference.session_id in revoked_session_ids
                ]
                for reference in revoked_references:
                    self._file_refs.pop(reference.id, None)
            for reference in revoked_references:
                self._discard_reference_file(reference)
        except ValueError as error:
            self._failure(
                handler, HTTPStatus.BAD_REQUEST, "password_change_failed", str(error)
            )
            return
        self._send_json(
            handler,
            HTTPStatus.OK,
            ok=True,
            data=self._session_payload(session),
        )

    def _local_worker_ticket(self, handler: Any) -> None:
        """Bridge a logged-in Hub page to one enrolled localhost worker."""

        if self.client_local:
            self._failure(
                handler,
                HTTPStatus.METHOD_NOT_ALLOWED,
                "already_local",
                "This page already runs on the workstation-local service.",
            )
            return
        session = self._session(handler, csrf=True)
        if session is None:
            return
        try:
            value = self._read_json(handler, maximum=64 * 1024)
            origin = str(handler.headers.get("Origin") or "").strip()
            expected = f"http://{str(handler.headers.get('Host') or '').strip()}"
            if not origin or origin.casefold() != expected.casefold():
                raise PermissionError("worker tickets require a same-origin Hub page")
            requested_device_id = str(value.get("device_id") or "")
            if requested_device_id == "hub-host-local":
                raise PermissionError(
                    "the Hub host is not an employee rendering workstation"
                )
            result = self.hub.issue_local_worker_ticket(
                session.actor_user_id,
                device_id=requested_device_id,
                worker_nonce=str(value.get("worker_nonce") or ""),
                browser_origin=origin,
            )
        except PermissionError as error:
            self._failure(handler, HTTPStatus.FORBIDDEN, "worker_forbidden", str(error))
            return
        except ValueError as error:
            self._failure(handler, HTTPStatus.BAD_REQUEST, "invalid_worker", str(error))
            return
        except Exception as error:
            # Hub authorization errors intentionally expose only their safe
            # public message, not a traceback or stored credential detail.
            status = int(getattr(error, "status", HTTPStatus.FORBIDDEN))
            message = str(getattr(error, "message", "") or str(error) or "local worker is unavailable")
            self._failure(handler, status, "worker_unavailable", message)
            return
        self._send_json(handler, HTTPStatus.OK, ok=True, data=result)

    # RPC -------------------------------------------------------------
    def _authorize_rpc(
        self, session: _WebSession, method: str, args: list[Any]
    ) -> None:
        if method in WEB_DESKTOP_ONLY_MEDIA_METHODS and not (
            self.client_local and method in CLIENT_LOCAL_MEDIA_METHODS
        ):
            raise PermissionError("请在制作电脑客户端执行")
        required = WEB_RPC_PERMISSIONS.get(method)
        if method in {
            "check_for_updates",
            "download_update",
            "schedule_update_on_restart",
            "cancel_scheduled_update",
            "save_local_update_preferences",
        } and not self.client_local:
            # Self-service update actions operate on the process that receives
            # the RPC.  A producer may update their installed workstation via
            # its loopback-only service, but must never be able to update or
            # reconfigure the Hub host from a remote browser session.
            required = ("hub.manage",)
        if self.client_local and method in {"start_queue", "cancel_queue"}:
            required = ("drafts.create", "drafts.manage_all", "hub.manage")
        if required is None:
            raise PermissionError("该功能未开放给网页端。")
        if required and not any(item in session.permissions for item in required):
            raise PermissionError("当前账号没有执行该操作的权限。")
        if method in {
            "delete_novel",
            "delete_promo_code",
            "delete_publishing_account",
        } and session.role != "admin":
            raise PermissionError("only administrators can delete shared library records")
        if method == "get_effective_permissions":
            target = str(args[0] if args else "")
            if target != session.actor_user_id and not {
                "permissions.manage",
                "users.manage",
            }.intersection(session.permissions):
                raise PermissionError("只能查看自己的权限。")
        if method == "publish_update" and "hub.manage" not in session.permissions:
            raise PermissionError("只有管理员可以发布更新。")
        access = self.hub._actor_access(session.actor_user_id)
        try:
            if method == "get_record_artifacts" and args:
                self.hub._require_own_record(str(args[0]), access)
            if method == "queue_production_draft" and args and isinstance(args[0], dict):
                draft_id = str(args[0].get("draft_id") or "")
                if draft_id:
                    self.hub._require_own_draft(draft_id, access)
            if method in {
                "approve_preview",
                "regenerate_preview",
                "retry_failed",
                "archive_job",
                "restore_job",
            } and args:
                job = self.api._queue.get_job(str(args[0]))
                record_id = ""
                if job is not None:
                    record_id = str(job.production_record_id or "")
                elif method == "restore_job":
                    archived = self.hub.catalog.get_record_by_job_id(str(args[0]))
                    record_id = str(archived.get("id") or "")
                else:
                    raise ValueError("没有找到该制作任务。")
                can_manage_all = (
                    "samples.approve_all" in access.permissions
                    if method in {"approve_preview", "regenerate_preview"}
                    else "jobs.retry_all" in access.permissions
                )
                if not can_manage_all:
                    if not record_id:
                        raise PermissionError("该任务没有可验证的所属记录。")
                    self.hub._require_own_record(record_id, access)
            if method in {"archive_batch", "restore_batch"} and args:
                batch_id = str(args[0] or "").strip()
                can_manage_all = "jobs.retry_all" in access.permissions
                offset = 0
                found = False
                while True:
                    page = self.hub.catalog.list_records(
                        batch_id=batch_id,
                        trashed=None,
                        limit=500,
                        offset=offset,
                    )
                    items = [
                        dict(item)
                        for item in page.get("items") or []
                        if str(item.get("job_id") or "")
                    ]
                    for record in items:
                        found = True
                        if not can_manage_all:
                            self.hub._require_own_record(str(record["id"]), access)
                    raw_items = list(page.get("items") or [])
                    offset += len(raw_items)
                    if offset >= int(page.get("total") or 0) or not raw_items:
                        break
                if not found:
                    raise ValueError("没有找到该制作批次。")
        except PermissionError:
            raise
        except Exception as error:
            # Hub ownership helpers use a transport-specific exception. Do not
            # leak its internals across the browser contract.
            raise PermissionError("只能操作自己创建的草稿、样片和生产记录。") from error

    def _prepare_actor_arguments(
        self, session: _WebSession, method: str, args: list[Any]
    ) -> list[Any]:
        prepared = list(args)
        if method == "save_production_draft" and prepared:
            try:
                value = dict(prepared[0])
            except (TypeError, ValueError):
                raise ValueError("生产草稿参数格式不正确。") from None
            value.pop("created_by_user_id", None)
            existing: dict[str, Any] | None = None
            if value.get("id"):
                try:
                    existing = self.hub._require_own_draft(
                        str(value["id"]), self.hub._actor_access(session.actor_user_id)
                    )
                except Exception as error:
                    raise PermissionError("只能修改自己创建的生产草稿。") from error
            value = self._validate_draft_folders(
                value, existing=None if self.client_local else existing
            )
            value["created_by_user_id"] = session.actor_user_id
            prepared[0] = value
        if method == "queue_production_draft" and prepared:
            try:
                value = dict(prepared[0])
                draft_id = str(value.get("draft_id") or "")
                if not draft_id:
                    raise ValueError("请先保存生产草稿。")
                draft = self.hub._require_own_draft(
                    draft_id, self.hub._actor_access(session.actor_user_id)
                )
                local_folders = self._validate_draft_folders(
                    value if self.client_local else {},
                    existing=None if self.client_local else draft,
                )
                if self.client_local:
                    value.update(
                        {
                            key: local_folders[key]
                            for key in (
                                "video_folder",
                                "music_folder",
                                "output_folder",
                            )
                        }
                    )
                    prepared[0] = value
            except PermissionError:
                raise
            except Exception as error:
                raise ValueError(
                    "草稿的视频、音乐或输出路径不在管理员允许的目录内。"
                ) from error
        if self.client_local and method in {
            "restore_job",
            "restore_batch",
            "retry_failed",
        } and prepared:
            # Archived records live in the shared Hub catalog and may have
            # originated on another rendering PC. Never restore or retry with
            # those persisted filesystem paths on this workstation.
            source: dict[str, Any] = {}
            try:
                if method == "restore_job":
                    archived = self.hub.catalog.get_archived_job(str(prepared[0]))
                    record = self.hub.catalog.get_record(
                        str(archived.get("production_record_id") or "")
                    )
                    current_device_id = str(
                        self.api._state.settings.hub.device_id or ""
                    )
                    record_device_id = str(record.get("device_id") or "")
                    if (
                        current_device_id
                        and record_device_id
                        and record_device_id != current_device_id
                    ):
                        raise PermissionError(
                            "Restore this archived task on the workstation that created it."
                        )
                    source = {
                        key: archived.get(key)
                        for key in ("video_folder", "music_folder", "output_folder")
                    }
                elif method == "restore_batch":
                    batch_id = str(prepared[0] or "").strip()
                    current_device_id = str(
                        self.api._state.settings.hub.device_id or ""
                    )
                    offset = 0
                    found = False
                    while True:
                        page = self.hub.catalog.list_records(
                            batch_id=batch_id,
                            trashed=None,
                            limit=500,
                            offset=offset,
                        )
                        raw_items = list(page.get("items") or [])
                        for record in raw_items:
                            if not str(record.get("job_id") or ""):
                                continue
                            found = True
                            record_device_id = str(record.get("device_id") or "")
                            if (
                                current_device_id
                                and record_device_id
                                and record_device_id != current_device_id
                            ):
                                raise PermissionError(
                                    "Restore this archived batch on the workstation that created it."
                                )
                        offset += len(raw_items)
                        if offset >= int(page.get("total") or 0) or not raw_items:
                            break
                    if not found:
                        raise ValueError("production batch was not found")
                else:
                    job = self.api._queue.get_job(str(prepared[0]))
                    if job is not None:
                        source = {
                            key: getattr(job, key, "")
                            for key in (
                                "video_folder",
                                "music_folder",
                                "output_folder",
                            )
                        }
                try:
                    local_folders = self._validate_draft_folders(source)
                except (OSError, RuntimeError, ValueError):
                    local_folders = self._validate_draft_folders({})
                prepared = [prepared[0], local_folders]
            except PermissionError:
                raise
            except Exception as error:
                raise ValueError(
                    "The archived task cannot be mapped to this workstation's media folders."
                ) from error
        return prepared

    def _scope_browser_result(
        self, session: _WebSession, method: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        if not bool(result.get("ok")) or not isinstance(result.get("data"), (dict, list)):
            return result
        can_view_all_records = bool(
            {"records.view_all", "hub.manage"} & session.permissions
        )
        can_manage_all_drafts = bool(
            {"drafts.manage_all", "hub.manage"} & session.permissions
        )
        can_view_all_jobs = bool(
            {"records.view_all", "drafts.manage_all", "hub.manage"}
            & session.permissions
        )
        own_record_ids: set[str] | None = None
        own_draft_cache: dict[str, dict[str, Any]] = {}

        def own_ids() -> set[str]:
            nonlocal own_record_ids
            if own_record_ids is None:
                # ``CatalogRepository.list_records`` deliberately caps one
                # page at 500 rows.  A producer can easily exceed that after
                # several large batches, so collect every ownership page
                # instead of asking the catalog for an invalid 5,000-row
                # response (which used to break the real browser bootstrap).
                own_record_ids = set()
                offset = 0
                page_size = 500
                while True:
                    page = self.hub.catalog.list_records(
                        created_by_user_id=session.actor_user_id,
                        limit=page_size,
                        offset=offset,
                    )
                    items = list(page.get("items") or [])
                    own_record_ids.update(
                        str(item.get("id") or "")
                        for item in items
                        if str(item.get("id") or "")
                    )
                    offset += len(items)
                    total = max(0, int(page.get("total") or 0))
                    if not items or len(items) < page_size or offset >= total:
                        break
            return own_record_ids

        def own_draft_for_novel(novel_id: str) -> dict[str, Any]:
            if novel_id not in own_draft_cache:
                drafts = self.hub.catalog.list_drafts(
                    novel_id=novel_id,
                    created_by_user_id=session.actor_user_id,
                    limit=1,
                ).get("items", [])
                own_draft_cache[novel_id] = self.api._library._ui_draft(
                    drafts[0] if drafts else None
                )
            return dict(own_draft_cache[novel_id])

        def scope_novel(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            novel_id = str(value.get("id") or "")
            if not novel_id:
                return value
            scoped_novel = dict(value)
            scoped_novel["draft"] = own_draft_for_novel(novel_id)
            return scoped_novel

        data = result["data"]
        if method == "get_library_bootstrap" and isinstance(data, dict):
            scoped = dict(data)
            if not can_manage_all_drafts:
                scoped["novels"] = [
                    scope_novel(item) for item in scoped.get("novels") or []
                ]
            if not can_view_all_records:
                scoped["production_records"] = [
                    item
                    for item in scoped.get("production_records") or []
                    if str(item.get("id") or "") in own_ids()
                ]
            if "users.manage" not in session.permissions:
                scoped["users"] = []
            result = {**result, "data": scoped}
            data = scoped
        if not can_manage_all_drafts and isinstance(data, dict):
            scoped_data = dict(data)
            if isinstance(scoped_data.get("novel"), dict):
                scoped_data["novel"] = scope_novel(scoped_data["novel"])
            elif "episodes" in scoped_data and "draft" in scoped_data:
                scoped_data = scope_novel(scoped_data)
            draft_value = scoped_data.get("draft")
            if isinstance(draft_value, dict) and draft_value.get("id"):
                try:
                    raw_draft = self.hub.catalog.get_draft(str(draft_value["id"]))
                except Exception:
                    raw_draft = None
                if not raw_draft or raw_draft.get("created_by_user_id") != session.actor_user_id:
                    novel_value = scoped_data.get("novel")
                    novel_id = (
                        str(novel_value.get("id") or "")
                        if isinstance(novel_value, dict)
                        else ""
                    )
                    scoped_data["draft"] = (
                        own_draft_for_novel(novel_id)
                        if novel_id
                        else self.api._library._ui_draft(None)
                    )
            result = {**result, "data": scoped_data}
            data = scoped_data
        if method in {"get_jobs", "get_archived_jobs", "get_bootstrap"}:
            if method == "get_bootstrap" and isinstance(data, dict):
                # A Hub-hosted page must not inherit the Hub process's private
                # in-memory render queue. It obtains the current workstation's
                # queue from the authenticated localhost worker after startup.
                jobs = [] if not self.client_local else list(data.get("jobs") or [])
                container = dict(data)
            elif isinstance(data, list):
                jobs = list(data)
                container = None
            else:
                jobs, container = [], None
            if not can_view_all_jobs:
                jobs = [
                    item
                    for item in jobs
                    if str(item.get("production_record_id") or "") in own_ids()
                ]
            if container is not None:
                archived_jobs = (
                    []
                    if not self.client_local
                    else list(container.get("archived_jobs") or [])
                )
                if not can_view_all_jobs:
                    archived_jobs = [
                        item
                        for item in archived_jobs
                        if str(item.get("production_record_id") or "") in own_ids()
                    ]
                container["archived_jobs"] = archived_jobs
                if method == "get_bootstrap":
                    settings = dict(container.get("settings") or {})
                    if self.client_local or "hub.manage" not in session.permissions:
                        # Producers need the render/style defaults, but not the
                        # host's executable command lines, internal service
                        # endpoints, access-token state, or legacy desktop
                        # batch paths.
                        providers = dict(settings.get("providers") or {})
                        providers.pop("text_api_key", None)
                        providers.pop("tts_api_key", None)
                        for key in (
                            "text_endpoint",
                            "tts_endpoint",
                            "kokoro_command",
                        ):
                            providers[key] = ""
                        if providers.get("kokoro_endpoint"):
                            providers["kokoro_endpoint"] = "configured"
                        settings["providers"] = providers
                        container["batches"] = []
                    hub_settings = dict(settings.get("hub") or {})
                    if self.client_local or "hub.manage" not in session.permissions:
                        for key in (
                            "access_token",
                            "has_access_token",
                            "endpoint",
                            "listen_host",
                            "listen_port",
                        ):
                            hub_settings.pop(key, None)
                    hub_settings["web_allowed_roots"] = [
                        str(item) for item in self._effective_web_roots()
                    ]
                    settings["hub"] = hub_settings
                    container["settings"] = settings
                    container["web_default_folders"] = self._web_default_folders()
                container["jobs"] = jobs
                result = {**result, "data": container}
            else:
                result = {**result, "data": jobs}
        if method in {
            "archive_job",
            "restore_job",
            "archive_batch",
            "restore_batch",
            "archive_finished_jobs",
        } and isinstance(data, dict) and not can_view_all_jobs:
            scoped = dict(data)
            for key in ("current_jobs", "archived_jobs", "jobs"):
                if isinstance(scoped.get(key), list):
                    scoped[key] = [
                        item
                        for item in scoped[key]
                        if str(item.get("production_record_id") or "") in own_ids()
                    ]
            if isinstance(scoped.get("archived_jobs"), list):
                scoped["archived_jobs_total"] = len(scoped["archived_jobs"])
            result = {**result, "data": scoped}
        return result

    def _upload_reference(
        self,
        session: _WebSession,
        value: Any,
        *,
        allowed_kinds: frozenset[str] | None = None,
    ) -> str:
        ref_id = str(value or "")
        if not ref_id.startswith("upload:"):
            raise ValueError("请先通过网页选择并上传文件。")
        with self._lock:
            reference = self._file_refs.get(ref_id[7:])
        if (
            reference is None
            or not reference.uploaded
            or reference.session_id != session.id
            or reference.actor_user_id != session.actor_user_id
            or reference.expires_at <= time.time()
            or not reference.path.is_file()
        ):
            raise ValueError("上传文件已失效，请重新选择。")
        if allowed_kinds is not None and reference.kind not in allowed_kinds:
            raise ValueError("上传文件类型与当前操作不匹配。")
        if reference.path.suffix.casefold() == ".docx":
            self._validate_docx(reference.path)
        return str(reference.path)

    def _resolve_controlled_uploads(
        self, session: _WebSession, method: str, args: list[Any]
    ) -> list[Any]:
        location = CONTROLLED_UPLOAD_ARGUMENTS.get(method)
        resolved = list(args)
        if location is not None:
            try:
                argument_index = int(location[0])
                if len(location) == 1:
                    allowed = {
                        "read_text_document": frozenset({"txt", "docx", "novel", "summary"}),
                        "publish_update": frozenset({"update_package"}),
                        "analyze_story": frozenset({"txt", "docx", "novel"}),
                    }.get(method)
                    resolved[argument_index] = self._upload_reference(
                        session, resolved[argument_index], allowed_kinds=allowed
                    )
                else:
                    payload = dict(resolved[argument_index])
                    field = str(location[1])
                    field_value = payload.get(field)
                    if field_value not in (None, ""):
                        if method in {"save_novel", "save_platform"} and not str(
                            field_value
                        ).startswith("upload:"):
                            # Browser responses expose only the leaf name of an
                            # existing host file. Treat that value as unchanged;
                            # a replacement must always be a fresh upload handle.
                            payload.pop(field, None)
                        else:
                            allowed = {
                                "import_novel_file": frozenset({"txt", "docx", "novel"}),
                                "save_novel": frozenset({"cover"}),
                                "save_platform": frozenset({"cover", "platform_logo"}),
                            }.get(method)
                            payload[field] = self._upload_reference(
                                session, field_value, allowed_kinds=allowed
                            )
                    resolved[argument_index] = payload
            except (IndexError, TypeError):
                raise ValueError("网页文件参数格式不正确。") from None
        if method in {"import_novel_text", "import_novel_file"} and resolved:
            try:
                payload = dict(resolved[0])
                cover_value = payload.get("cover_path")
                if cover_value not in (None, ""):
                    payload["cover_path"] = self._upload_reference(
                        session, cover_value, allowed_kinds=frozenset({"cover"})
                    )
                resolved[0] = payload
            except (TypeError, ValueError):
                raise ValueError("小说导入参数格式不正确。") from None
        return resolved

    def _register_media(
        self,
        session: _WebSession,
        path: Path,
        *,
        uploaded: bool = False,
        kind: str = "media",
    ) -> str:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("媒体文件不存在。")
        with self._lock:
            for existing in self._file_refs.values():
                if existing.session_id == session.id and existing.path == resolved:
                    existing.expires_at = min(
                        session.expires_at, time.time() + 12 * 3600
                    )
                    if uploaded:
                        existing.uploaded = True
                        existing.kind = str(kind or existing.kind)
                    return existing.id
            reference_count = sum(
                1 for item in self._file_refs.values() if item.session_id == session.id
            )
            if reference_count >= MAX_FILE_REFERENCES_PER_SESSION:
                raise ValueError("当前网页会话的文件引用过多，请刷新页面后重试。")
            ref_id = secrets.token_urlsafe(28)
            reference = _FileReference(
                id=ref_id,
                session_id=session.id,
                actor_user_id=session.actor_user_id,
                path=resolved,
                filename=_safe_filename(resolved.name),
                expires_at=min(session.expires_at, time.time() + 12 * 3600),
                uploaded=uploaded,
                kind=str(kind or "media"),
            )
            self._file_refs[ref_id] = reference
        return ref_id

    @staticmethod
    def _path_from_file_uri(value: str) -> Path | None:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() != "file":
            return None
        raw_path = url2pathname(unquote(parsed.path))
        if os.name == "nt" and raw_path.startswith("/") and re.match(
            r"/[A-Za-z]:", raw_path
        ):
            raw_path = raw_path[1:]
        try:
            path = Path(raw_path).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        return path if path.is_file() else None

    @staticmethod
    def _validate_docx(path: Path) -> None:
        """Reject malformed and highly-compressed DOCX payloads before read."""

        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 2000:
                    raise ValueError("DOCX 内部文件数量过多。")
                names = [item.filename for item in entries]
                if len(names) != len(set(names)):
                    raise ValueError("DOCX 包含重复的内部路径。")
                if "word/document.xml" not in names:
                    raise ValueError("DOCX 缺少 word/document.xml。")
                total_uncompressed = sum(max(0, int(item.file_size)) for item in entries)
                if total_uncompressed > 200 * 1024 * 1024:
                    raise ValueError("DOCX 解压后体积过大。")
                document = archive.getinfo("word/document.xml")
                if document.file_size <= 0 or document.file_size > 50 * 1024 * 1024:
                    raise ValueError("DOCX 正文 XML 大小异常。")
                compressed = max(1, int(document.compress_size))
                if document.file_size / compressed > 200:
                    raise ValueError("DOCX 正文压缩比过高。")
                # Reading a bounded prefix forces the CRC/header path now,
                # before the manuscript importer attempts full XML parsing.
                with archive.open(document) as stream:
                    stream.read(min(4096, document.file_size))
        except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            raise ValueError("DOCX 文件损坏或格式无效。") from error

    def _browser_payload(
        self, session: _WebSession, value: Any, *, parent_key: str = ""
    ) -> Any:
        if isinstance(value, dict):
            return {
                str(key): self._browser_payload(
                    session, item, parent_key=str(key).casefold()
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._browser_payload(session, item) for item in value]
        if isinstance(value, tuple):
            return [self._browser_payload(session, item) for item in value]
        if not isinstance(value, str):
            return value
        if parent_key == "uri" or parent_key.endswith("_uri"):
            path = self._path_from_file_uri(value)
            if path is not None:
                ref_id = self._register_media(session, path)
                return f"/web/api/media?ref={quote(ref_id)}"
            return value
        if parent_key.endswith("_path") or parent_key in {
            "local_path",
            "cached_path",
            "cover_path",
            "logo_path",
            "audio_path",
            "source_file",
            "output_file",
            "output_folder",
            "text_folder",
            "video_folder",
            "music_folder",
            "file_path",
            "package_path",
        }:
            if value.startswith("hub://"):
                return value
            if parent_key in {"video_folder", "music_folder", "output_folder"}:
                try:
                    return str(self._validated_web_folder(value))
                except (OSError, RuntimeError, ValueError):
                    return ""
            if value and (Path(value).is_absolute() or value.startswith("file:")):
                return _safe_filename(value, "[主机文件]")
        return value

    def _rpc(self, handler: Any) -> None:
        session = self._session(handler, csrf=True)
        if session is None:
            return
        try:
            request = self._read_json(handler)
            method = str(request.get("method") or "").strip()
            args = request.get("args", [])
            if not method or not isinstance(args, list) or len(args) > 12:
                raise ValueError("RPC 请求需要 method 和 args 数组。")
            self._authorize_rpc(session, method, args)
            args = self._resolve_controlled_uploads(session, method, args)
            args = self._prepare_actor_arguments(session, method, args)
            target = getattr(self.api, method, None)
            if method not in WEB_RPC_PERMISSIONS or not callable(target):
                raise PermissionError("该功能未开放给网页端。")
            inspect.signature(target).bind(*args)
            actor_scope = getattr(self.api, "_web_actor_scope", None)
            request_scope = (
                actor_scope(session.actor_user_id)
                if callable(actor_scope)
                else nullcontext()
            )
            with request_scope:
                result = target(*args)
            if not isinstance(result, dict) or "ok" not in result:
                raise RuntimeError("后端返回了无效结果。")
            result = self._scope_browser_result(session, method, result)
            browser_result = self._browser_payload(session, result)
        except PermissionError as error:
            self._failure(handler, HTTPStatus.FORBIDDEN, "forbidden", str(error))
            return
        except (TypeError, ValueError) as error:
            self._failure(handler, HTTPStatus.BAD_REQUEST, "invalid_params", str(error))
            return
        except Exception as error:
            self.hub._record_error(error)
            self._failure(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "rpc_failed",
                "网页操作执行失败，请查看主机日志。",
            )
            return
        # Preserve StoryForgeApi's established {ok,data,error} result contract.
        body = json.dumps(browser_result, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)
        handler.close_connection = True

    # Upload/media ----------------------------------------------------
    @staticmethod
    def _upload_limit(kind: str, suffix: str) -> int:
        if kind in {"novel", "summary"}:
            return UPLOAD_KIND_LIMITS["docx" if suffix == ".docx" else "txt"]
        return UPLOAD_KIND_LIMITS.get(kind, 0)

    @staticmethod
    def _authorize_upload_kind(session: _WebSession, kind: str) -> None:
        if kind in {"update_package", "component_package"}:
            if "hub.manage" not in session.permissions:
                raise PermissionError("Only administrators can upload update packages.")
            return
        required = "platforms.manage" if kind == "platform_logo" else "library.edit"
        if required not in session.permissions:
            raise PermissionError("This account cannot upload this type of file.")

    def _upload(self, handler: Any, parsed: Any) -> None:
        session = self._session(handler, csrf=True)
        if session is None:
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        query_kinds = query.get("kind", [])
        if len(query_kinds) != 1 or str(query_kinds[0]) not in UPLOAD_EXTENSIONS:
            self._failure(
                handler,
                HTTPStatus.BAD_REQUEST,
                "invalid_upload_kind",
                "上传地址必须包含唯一的受支持 kind。",
            )
            return
        query_kind = str(query_kinds[0])
        try:
            self._authorize_upload_kind(session, query_kind)
        except PermissionError as error:
            # Reject unauthorized large requests before parsing or writing the
            # multipart body to disk.
            self._failure(handler, HTTPStatus.FORBIDDEN, "forbidden", str(error))
            return
        content_type = str(handler.headers.get("Content-Type") or "")
        if not content_type.casefold().startswith("multipart/form-data"):
            self._failure(
                handler,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "上传必须使用 multipart/form-data。",
            )
            return
        try:
            length = int(handler.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            length = -1
        if not 0 < length <= MAX_UPLOAD_BYTES:
            self._failure(
                handler,
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "upload_too_large",
                "上传请求为空或超过更新包总上限。",
            )
            return
        early_limit = (
            max(UPLOAD_KIND_LIMITS["txt"], UPLOAD_KIND_LIMITS["docx"])
            if query_kind in {"novel", "summary"}
            else UPLOAD_KIND_LIMITS.get(query_kind, 0)
        )
        if early_limit <= 0 or length > early_limit + 1024 * 1024:
            self._failure(
                handler,
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "upload_too_large",
                "上传请求超过当前 kind 的大小上限。",
            )
            return
        destination: Path | None = None
        try:
            form = cgi.FieldStorage(
                fp=handler.rfile,
                headers=handler.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(length),
                },
                keep_blank_values=True,
            )
            file_item = form["file"]
            if isinstance(file_item, list):
                raise ValueError("每次只能上传一个文件。")
            form_kind = str(form.getfirst("kind") or "").strip()
            if form_kind and form_kind != query_kind:
                raise ValueError("表单 kind 与上传地址不一致。")
            kind = query_kind
            if kind not in UPLOAD_EXTENSIONS:
                raise ValueError("不支持该上传类型。")
            filename = _safe_filename(str(file_item.filename or ""))
            suffix = Path(filename).suffix.casefold()
            if suffix not in UPLOAD_EXTENSIONS[kind]:
                raise ValueError("文件扩展名与上传类型不匹配。")
            kind_limit = self._upload_limit(kind, suffix)
            if kind_limit <= 0:
                raise ValueError("该上传类型没有安全大小上限。")
            # Multipart framing is small; a clearly oversized request can be
            # rejected before its body reaches disk.
            if length > kind_limit + 1024 * 1024:
                raise ValueError("文件超过当前类型的上传上限。")
            actor_root = (
                self.upload_root / hashlib.sha256(session.actor_user_id.encode()).hexdigest()[:20]
            ).resolve()
            actor_root.mkdir(parents=True, exist_ok=True)
            destination = (actor_root / f"{secrets.token_hex(12)}-{filename}").resolve()
            if actor_root not in destination.parents:
                raise ValueError("上传目标无效。")
            with destination.open("xb") as stream:
                shutil.copyfileobj(file_item.file, stream, length=1024 * 1024)
            size = destination.stat().st_size
            if size <= 0 or size > kind_limit:
                destination.unlink(missing_ok=True)
                raise ValueError("上传文件为空或超过当前类型的大小限制。")
            with destination.open("rb") as stream:
                signature = stream.read(16)
            if suffix in {".docx", ".zip"} and not signature.startswith(b"PK"):
                destination.unlink(missing_ok=True)
                raise ValueError("文件内容不是有效的 ZIP/DOCX。")
            if suffix == ".png" and not signature.startswith(b"\x89PNG\r\n\x1a\n"):
                destination.unlink(missing_ok=True)
                raise ValueError("文件内容不是有效的 PNG。")
            if suffix in {".jpg", ".jpeg"} and not signature.startswith(b"\xff\xd8\xff"):
                destination.unlink(missing_ok=True)
                raise ValueError("文件内容不是有效的 JPEG。")
            if suffix == ".webp" and not (
                signature.startswith(b"RIFF") and signature[8:12] == b"WEBP"
            ):
                destination.unlink(missing_ok=True)
                raise ValueError("文件内容不是有效的 WEBP。")
            if suffix == ".docx":
                try:
                    self._validate_docx(destination)
                except ValueError:
                    destination.unlink(missing_ok=True)
                    raise
            ref_id = self._register_media(
                session, destination, uploaded=True, kind=kind
            )
        except PermissionError as error:
            if destination is not None:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
            self._failure(handler, HTTPStatus.FORBIDDEN, "forbidden", str(error))
            return
        except (KeyError, OSError, TypeError, ValueError) as error:
            if destination is not None:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
            self._failure(handler, HTTPStatus.BAD_REQUEST, "upload_failed", str(error))
            return
        self._send_json(
            handler,
            HTTPStatus.OK,
            ok=True,
            data={
                "ref": f"upload:{ref_id}",
                "file_path": f"upload:{ref_id}",
                "name": filename,
                "kind": kind,
                "size_bytes": size,
                "media_url": f"/web/api/media?ref={quote(ref_id)}",
            },
        )

    def _reference(self, handler: Any, parsed: Any) -> _FileReference | None:
        session = self._session(handler)
        if session is None:
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        values = query.get("ref", [])
        if len(values) != 1:
            self._failure(handler, HTTPStatus.BAD_REQUEST, "invalid_ref", "文件引用无效。")
            return None
        with self._lock:
            reference = self._file_refs.get(str(values[0]))
        if (
            reference is None
            or reference.session_id != session.id
            or reference.actor_user_id != session.actor_user_id
            or reference.expires_at <= time.time()
        ):
            self._failure(handler, HTTPStatus.NOT_FOUND, "ref_expired", "文件引用不存在或已过期。")
            return None
        try:
            if not reference.path.resolve(strict=True).is_file():
                raise OSError
        except (OSError, RuntimeError):
            self._failure(handler, HTTPStatus.NOT_FOUND, "file_missing", "文件已不存在。")
            return None
        return reference

    def _serve_file(self, handler: Any, parsed: Any, *, download: bool) -> None:
        reference = self._reference(handler, parsed)
        if reference is None:
            return
        path = reference.path
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = str(handler.headers.get("Range") or "").strip()
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or size <= 0:
                self._failure(
                    handler,
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    "invalid_range",
                    "请求的媒体范围无效。",
                    headers={"Content-Range": f"bytes */{size}"},
                )
                return
            left, right = match.groups()
            if not left:
                length = int(right or "0")
                if length <= 0:
                    self._failure(
                        handler,
                        HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                        "invalid_range",
                        "请求的媒体范围无效。",
                        headers={"Content-Range": f"bytes */{size}"},
                    )
                    return
                start = max(0, size - length)
            else:
                start = int(left)
                end = int(right) if right else size - 1
            if start >= size or start > end:
                self._failure(
                    handler,
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    "invalid_range",
                    "请求的媒体范围超出文件。",
                    headers={"Content-Range": f"bytes */{size}"},
                )
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        length = 0 if size == 0 else max(0, end - start + 1)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(length))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "private, no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        if status == HTTPStatus.PARTIAL_CONTENT:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            encoded = quote(reference.filename)
            handler.send_header(
                "Content-Disposition",
                f"attachment; filename=download{path.suffix}; filename*=UTF-8''{encoded}",
            )
        handler.send_header("Connection", "close")
        handler.end_headers()
        if handler.command != "HEAD" and length:
            remaining = length
            with path.open("rb") as stream:
                stream.seek(start)
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    remaining -= len(chunk)
        handler.close_connection = True

    def _published_browser_update(self, handler: Any) -> dict[str, Any] | None:
        """Return the current release for an authenticated browser session.

        Workstation accounts deliberately cannot open the administrator-only
        Settings view.  They still need a recovery path when an older desktop
        build cannot expose its own update controls.  This helper authorizes
        with the normal password-session cookie and only returns the one
        release already published by the Hub; it never exposes the Hub bearer
        token, publisher controls, or a server-side filesystem path.
        """

        if self.client_local:
            self._failure(
                handler,
                HTTPStatus.METHOD_NOT_ALLOWED,
                "already_local",
                "请从主电脑网页下载已发布的软件更新。",
            )
            return None
        session = self._session(handler)
        if session is None:
            return None
        try:
            manifest = self.hub.published_update_manifest()
        except (OSError, RuntimeError, TypeError, ValueError):
            self._failure(
                handler,
                HTTPStatus.SERVICE_UNAVAILABLE,
                "update_unavailable",
                "主电脑暂时无法读取已发布的软件更新。",
            )
            return None
        return dict(manifest) if manifest is not None else {}

    def _browser_update_status(self, handler: Any) -> None:
        manifest = self._published_browser_update(handler)
        if manifest is None:
            return
        if not manifest:
            self._send_json(
                handler,
                HTTPStatus.OK,
                ok=True,
                data={
                    "available": False,
                    "hub_version": __version__,
                    "message": "主电脑当前没有发布软件更新。",
                },
            )
            return
        version = str(manifest["version"])
        self._send_json(
            handler,
            HTTPStatus.OK,
            ok=True,
            data={
                "available": True,
                "hub_version": __version__,
                "version": version,
                "size_bytes": int(manifest["size_bytes"]),
                "sha256": str(manifest["sha256"]),
                "release_notes": str(manifest.get("release_notes") or ""),
                "published_at": str(manifest.get("published_at") or ""),
                "download_url": (
                    "/web/api/update/package?version=" + quote(version, safe="")
                ),
            },
        )

    def _serve_browser_update_package(self, handler: Any, parsed: Any) -> None:
        manifest = self._published_browser_update(handler)
        if manifest is None:
            return
        if not manifest:
            self._failure(
                handler,
                HTTPStatus.NOT_FOUND,
                "update_not_found",
                "主电脑当前没有发布软件更新。",
            )
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        versions = query.get("version", [])
        if versions and (
            len(versions) != 1
            or str(versions[0] or "").strip() != str(manifest["version"])
        ):
            self._failure(
                handler,
                HTTPStatus.NOT_FOUND,
                "update_not_found",
                "请求的软件版本已不再发布，请刷新后重试。",
            )
            return
        if any(key != "version" for key in query):
            self._failure(
                handler,
                HTTPStatus.BAD_REQUEST,
                "invalid_update_request",
                "软件更新下载地址包含不支持的参数。",
            )
            return
        try:
            package, checked = self.hub.resolve_update_package(manifest["version"])
        except Exception as error:
            status = int(getattr(error, "status", HTTPStatus.SERVICE_UNAVAILABLE))
            message = str(
                getattr(error, "message", "")
                or "主电脑暂时无法提供软件更新包。"
            )
            self._failure(
                handler,
                status,
                "update_unavailable" if status >= 500 else "update_not_found",
                message,
            )
            return

        try:
            size = package.stat().st_size
            if size != int(checked["size_bytes"]):
                raise OSError("published update size changed")
        except OSError:
            self._failure(
                handler,
                HTTPStatus.SERVICE_UNAVAILABLE,
                "update_unavailable",
                "主电脑上的软件更新包未通过完整性检查。",
            )
            return

        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = str(handler.headers.get("Range") or "").strip()
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or size <= 0:
                self._failure(
                    handler,
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    "invalid_range",
                    "请求的软件更新范围无效。",
                    headers={"Content-Range": f"bytes */{size}"},
                )
                return
            left, right = match.groups()
            if not left:
                length = int(right or "0")
                if length <= 0:
                    self._failure(
                        handler,
                        HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                        "invalid_range",
                        "请求的软件更新范围无效。",
                        headers={"Content-Range": f"bytes */{size}"},
                    )
                    return
                start = max(0, size - length)
            else:
                start = int(left)
                end = int(right) if right else size - 1
            if start >= size or start > end:
                self._failure(
                    handler,
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    "invalid_range",
                    "请求的软件更新范围超出文件。",
                    headers={"Content-Range": f"bytes */{size}"},
                )
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT

        length = 0 if size == 0 else max(0, end - start + 1)
        encoded_name = quote(package.name, safe="")
        digest = str(checked["sha256"])
        handler.send_response(status)
        handler.send_header("Content-Type", "application/zip")
        handler.send_header("Content-Length", str(length))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("X-Content-SHA256", digest)
        handler.send_header("ETag", f'"{digest}"')
        handler.send_header(
            "Content-Disposition", f"attachment; filename*=UTF-8''{encoded_name}"
        )
        handler.send_header("Cache-Control", "private, no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        if status == HTTPStatus.PARTIAL_CONTENT:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        handler.send_header("Connection", "close")
        handler.end_headers()
        if handler.command != "HEAD" and length:
            remaining = length
            try:
                with package.open("rb") as stream:
                    stream.seek(start)
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        handler.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        handler.close_connection = True

    # Route entrypoints ----------------------------------------------
    def handle_get(self, handler: Any, parsed: Any) -> bool:
        path = parsed.path
        if path in {"/", "/index.html"}:
            self._send_static(handler, "index.html")
            return True
        if path in {"/app.js", "/styles.css"}:
            self._send_static(handler, path.lstrip("/"))
            return True
        if path == "/web/api/health":
            backup_status_getter = getattr(self.api, "_backup_status_value", None)
            if callable(backup_status_getter):
                try:
                    backup_status = backup_status_getter(include_error=False)
                except (OSError, RuntimeError, TypeError, ValueError):
                    backup_status = {
                        "available": False,
                        "enabled": False,
                        "running": False,
                        "state": "error",
                        "has_error": True,
                    }
            else:
                backup_status = {
                    "available": False,
                    "enabled": False,
                    "running": False,
                    "state": "unavailable",
                    "has_error": False,
                }
            self._send_json(
                handler,
                HTTPStatus.OK,
                ok=True,
                data={
                    "service": "storyforge-web",
                    "version": __version__,
                    "authenticated": bool(
                        self._cookie_session_id(handler) in self._sessions
                    ),
                    "backup": backup_status,
                    "features": [
                        "rpc",
                        "upload",
                        "range-media",
                        (
                            "client-local-device-session"
                            if self.client_local
                            else "session-auth"
                        ),
                    ],
                },
            )
            return True
        if path == "/web/api/session":
            session = self._session(handler)
            if session is not None:
                self._send_json(
                    handler,
                    HTTPStatus.OK,
                    ok=True,
                    data=self._session_payload(session),
                )
            return True
        if path == "/web/api/update":
            self._browser_update_status(handler)
            return True
        if path == "/web/api/update/package":
            self._serve_browser_update_package(handler, parsed)
            return True
        if path in {"/web/api/media", "/web/api/download"}:
            self._serve_file(handler, parsed, download=path.endswith("download"))
            return True
        return False

    def handle_head(self, handler: Any, parsed: Any) -> bool:
        return self.handle_get(handler, parsed)

    def handle_post(self, handler: Any, parsed: Any) -> bool:
        path = parsed.path
        if path == "/web/api/session/login":
            self._login(handler)
            return True
        if path == "/web/api/session/password":
            self._change_password(handler)
            return True
        if path == "/web/api/local-worker-ticket":
            self._local_worker_ticket(handler)
            return True
        if path == "/web/api/rpc":
            self._rpc(handler)
            return True
        if path == "/web/api/upload":
            self._upload(handler, parsed)
            return True
        return False

    def handle_delete(self, handler: Any, parsed: Any) -> bool:
        if parsed.path == "/web/api/session":
            self._logout(handler)
            return True
        return False


class _ClientLocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # A localhost worker owns private media paths and render state for exactly
    # one StoryForge process.  On Windows SO_REUSEADDR can let two processes
    # bind the same loopback port and randomly receive each other's browser
    # requests.  Refuse that sharing so startup can move to the next discovery
    # port instead of connecting a Hub page to the wrong workstation runtime.
    allow_reuse_address = False
    request_queue_size = 32

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        super().server_bind()

    def __init__(self, server_address: tuple[str, int], owner: "ClientLocalWebServer") -> None:
        self.owner = owner
        super().__init__(server_address, _ClientLocalRequestHandler)


class _ClientLocalRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StoryForgeLocalWeb/1"
    sys_version = ""

    @property
    def owner(self) -> "ClientLocalWebServer":
        return self.server.owner  # type: ignore[attr-defined, no-any-return]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _not_found(self) -> None:
        StoryForgeWebApplication._send_json(
            self,
            HTTPStatus.NOT_FOUND,
            ok=False,
            error="route not found",
            code="not_found",
        )

    def _worker_cors(self, origin: str = "*") -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": origin or "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": (
                "Content-Type, X-StoryForge-Worker-Session"
            ),
            # Chromium treats a Hub page reaching a loopback worker as a
            # private/local-network request. Without this opt-in its CORS
            # preflight is rejected before the ticketed worker protocol can
            # run, leaving the full browser UI in a misleading disconnected
            # state even though the local worker is listening.
            "Access-Control-Allow-Private-Network": "true",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }

    def _worker_json(
        self,
        status: int,
        *,
        ok: bool,
        data: Any = None,
        error: str = "",
        code: str = "",
        origin: str = "*",
    ) -> None:
        StoryForgeWebApplication._send_json(
            self,
            status,
            ok=ok,
            data=data,
            error=error,
            code=code,
            headers=self._worker_cors(origin),
        )

    def _worker_origin(self) -> str:
        return str(self.headers.get("Origin") or "").strip()

    def _worker_request(self, parsed: Any) -> bool:
        path = parsed.path
        if not path.startswith("/worker/api/"):
            return False
        origin = self._worker_origin()
        try:
            expected_host = f"127.0.0.1:{int(self.server.server_address[1])}"
            supplied_host = str(self.headers.get("Host") or "").strip().casefold()
            if supplied_host != expected_host:
                raise PermissionError(
                    "the local worker is available only through its loopback address"
                )
            if self.command == "POST" and not origin:
                raise PermissionError("local-worker POST requests require a browser origin")
            if self.command == "OPTIONS":
                self.send_response(HTTPStatus.NO_CONTENT)
                for key, value in self._worker_cors(origin or "*").items():
                    self.send_header(key, value)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return True
            if path == "/worker/api/health" and self.command in {"GET", "HEAD"}:
                self._worker_json(
                    HTTPStatus.OK,
                    ok=True,
                    data=self.owner.worker_gateway.health(),
                    origin="*",
                )
                return True
            if path == "/worker/api/media" and self.command in {"GET", "HEAD"}:
                query = parse_qs(parsed.query, keep_blank_values=True)
                refs = query.get("ref", [])
                if len(refs) != 1:
                    raise ValueError("media reference is required")
                media = self.owner.worker_gateway.media(refs[0])
                body_size = media.stat().st_size
                content_type = mimetypes.guess_type(media.name)[0] or "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(body_size))
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for key, value in self._worker_cors("*").items():
                    self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    with media.open("rb") as stream:
                        shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
                self.close_connection = True
                return True
            if path == "/worker/api/connect" and self.command == "POST":
                value = StoryForgeWebApplication._read_json(self, maximum=64 * 1024)
                data = self.owner.worker_gateway.connect(
                    value.get("ticket"),
                    origin,
                    browser_protocol_version=value.get("browser_protocol_version"),
                    minimum_worker_protocol_version=value.get(
                        "minimum_worker_protocol_version"
                    ),
                )
                self._worker_json(
                    HTTPStatus.OK, ok=True, data=data, origin=origin
                )
                return True
            if path == "/worker/api/rpc" and self.command == "POST":
                value = StoryForgeWebApplication._read_json(self)
                token = str(
                    self.headers.get("X-StoryForge-Worker-Session") or ""
                )
                result = self.owner.worker_gateway.rpc(
                    token,
                    origin,
                    value.get("method"),
                    value.get("args", []),
                )
                if not isinstance(result, dict) or "ok" not in result:
                    raise RuntimeError("local worker returned an invalid result")
                body = json.dumps(
                    result, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for key, header_value in self._worker_cors(origin).items():
                    self.send_header(key, header_value)
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True
                return True
            self._worker_json(
                HTTPStatus.NOT_FOUND,
                ok=False,
                error="worker route not found",
                code="not_found",
                origin=origin or "*",
            )
            return True
        except PermissionError as error:
            self._worker_json(
                HTTPStatus.FORBIDDEN,
                ok=False,
                error=str(error),
                code="worker_forbidden",
                origin=origin or "*",
            )
            return True
        except FileNotFoundError as error:
            self._worker_json(
                HTTPStatus.NOT_FOUND,
                ok=False,
                error=str(error),
                code="worker_media_missing",
                origin=origin or "*",
            )
            return True
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self._worker_json(
                HTTPStatus.BAD_REQUEST,
                ok=False,
                error=str(error),
                code="worker_request_failed",
                origin=origin or "*",
            )
            return True
        except Exception as error:
            # In-process Hub ticket failures use the Hub transport's private
            # exception type.  Convert them to the same bounded worker response
            # instead of dropping the localhost connection.
            self._worker_json(
                HTTPStatus.BAD_REQUEST,
                ok=False,
                error=(str(error) or "local worker request failed")[:500],
                code="worker_request_failed",
                origin=origin or "*",
            )
            return True

    def _trusted_browser_request(self, *, mutating: bool = False) -> bool:
        """Reject DNS-rebinding and cross-origin access to the implicit login."""

        expected_host = f"127.0.0.1:{int(self.server.server_address[1])}"
        supplied_host = str(self.headers.get("Host") or "").strip().casefold()
        if supplied_host != expected_host:
            StoryForgeWebApplication._send_json(
                self,
                HTTPStatus.FORBIDDEN,
                ok=False,
                error="This workstation UI is available only through its loopback URL.",
                code="untrusted_host",
            )
            return False
        if mutating:
            expected_origin = f"http://{expected_host}"
            supplied_origin = str(self.headers.get("Origin") or "").strip().casefold()
            if supplied_origin != expected_origin:
                StoryForgeWebApplication._send_json(
                    self,
                    HTTPStatus.FORBIDDEN,
                    ok=False,
                    error="Cross-origin requests are not accepted by the workstation UI.",
                    code="untrusted_origin",
                )
                return False
        return True

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        parsed = urlsplit(self.path)
        if self._worker_request(parsed):
            return
        if not self.owner.serve_ui:
            self._not_found()
            return
        if not self._trusted_browser_request():
            return
        if not self.owner.application.handle_get(self, parsed):
            self._not_found()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        parsed = urlsplit(self.path)
        if self._worker_request(parsed):
            return
        if not self.owner.serve_ui:
            self._not_found()
            return
        if not self._trusted_browser_request():
            return
        if not self.owner.application.handle_head(self, parsed):
            self._not_found()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        parsed = urlsplit(self.path)
        if self._worker_request(parsed):
            return
        if not self.owner.serve_ui:
            self._not_found()
            return
        if not self._trusted_browser_request(mutating=True):
            return
        if not self.owner.application.handle_post(self, parsed):
            self._not_found()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler contract
        if not self.owner.serve_ui:
            self._not_found()
            return
        if not self._trusted_browser_request(mutating=True):
            return
        parsed = urlsplit(self.path)
        if not self.owner.application.handle_delete(self, parsed):
            self._not_found()

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler contract
        parsed = urlsplit(self.path)
        if not self._worker_request(parsed):
            self._not_found()


class ClientLocalWebServer:
    """Loopback-only browser UI backed by one rendering workstation.

    The HTTP listener has no Hub catalog RPC, enrollment, update, or file-share
    routes. Shared data is reached only through ``StoryForgeApi``'s authenticated
    ``HubCatalogProxy`` while media work runs on the API's local queue.
    """

    def __init__(
        self,
        api: Any,
        *,
        ui_root: str | Path,
        upload_root: str | Path,
        port: int = 0,
        serve_ui: bool = True,
    ) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        self.authority = ClientLocalWebAuthority(api)
        self.application = StoryForgeWebApplication(
            api,
            self.authority,
            ui_root=ui_root,
            upload_root=upload_root,
            client_local=True,
        )
        from .worker import LocalWorkerGateway

        self.worker_gateway = LocalWorkerGateway(api)
        self.port = port
        self.serve_ui = bool(serve_ui)
        self._httpd: _ClientLocalHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return bool(self._httpd and self._thread and self._thread.is_alive())

    @property
    def address(self) -> tuple[str, int]:
        with self._lock:
            if self._httpd is None:
                raise RuntimeError("client-local Web server is not running")
            return "127.0.0.1", int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.address[1]}"

    def start(self) -> "ClientLocalWebServer":
        # Host is intentionally not configurable: this UI owns local media
        # paths and must never become another LAN-facing Hub surface.
        with self._lock:
            if self.is_running:
                return self
            httpd = _ClientLocalHTTPServer(("127.0.0.1", self.port), self)
            thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="storyforge-client-local-web",
                daemon=True,
            )
            self._httpd = httpd
            self._thread = thread
            thread.start()
        return self

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            httpd = self._httpd
            thread = self._thread
        if httpd is None:
            self.application.close()
            return
        httpd.shutdown()
        httpd.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
            if thread.is_alive():
                raise RuntimeError("client-local Web server did not stop before timeout")
        with self._lock:
            if self._httpd is httpd:
                self._httpd = None
                self._thread = None
        self.application.close()


__all__ = [
    "CLIENT_LOCAL_MEDIA_METHODS",
    "ClientLocalWebAuthority",
    "ClientLocalWebServer",
    "CONTROLLED_UPLOAD_ARGUMENTS",
    "MAX_UPLOAD_BYTES",
    "SESSION_COOKIE",
    "StoryForgeWebApplication",
    "UPLOAD_EXTENSIONS",
    "WEB_DESKTOP_ONLY_MEDIA_METHODS",
    "WEB_RPC_PERMISSIONS",
]
