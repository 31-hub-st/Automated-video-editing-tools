from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from . import __version__
from .catalog import (
    CatalogConflictError,
    CatalogError,
    CatalogNotFoundError,
    CatalogPermissionError,
    CatalogRepository,
    CatalogValidationError,
    installation_id_sha256,
)
from .credentials import DUMMY_PASSWORD_VERIFIER, password_matches
from .component_updater import (
    ComponentPackageError,
    ComponentRepository,
    ComponentUpdater,
    sign_component_catalog,
    validate_component_catalog,
    validate_component_publication,
    verify_component_catalog_signature,
)
from .providers.base import ProviderConfig, ProviderError
from .providers.text import TextRequest, TextResult, create_text_provider
from .rpc_contract import (
    ACCOUNT_PASSWORD_VERIFY_RPC_METHOD,
    CATALOG_READ_METHODS,
    CATALOG_RPC_METHODS,
    CATALOG_WRITE_METHODS,
    DEVICE_ADMIN_RPC_METHODS,
    DEVICE_CLIENT_RPC_METHODS,
    DEVICE_SERVICE_RPC_METHODS,
    HUB_RPC_PERMISSION_ANY,
    LOCAL_WORKER_TICKET_RPC_METHOD,
    TEXT_POLISH_RPC_METHOD,
)
from .updater import (
    UpdateRepository,
    sign_update_manifest,
    validate_update_manifest,
    verify_update_manifest_signature,
)


HUB_PROTOCOL_VERSION = 1
HUB_MINIMUM_CLIENT_PROTOCOL_VERSION = 1
HUB_MINIMUM_SERVER_PROTOCOL_VERSION = 1
_MINIMUM_RENDER_CLIENT_CORE = (0, 4, 7)
_MINIMUM_RENDER_CLIENT_RC: int | None = None
MINIMUM_RENDER_CLIENT_VERSION = ".".join(
    str(item) for item in _MINIMUM_RENDER_CLIENT_CORE
)
_RENDER_CLIENT_VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)(?:-rc(?P<rc>0|[1-9]\d*))?$",
    re.IGNORECASE,
)
LOCAL_WORKER_TICKET_TTL_SECONDS = 90
DEVICE_CAPABILITY_FIELDS = frozenset(
    {
        "device_config_sync",
        "local_render",
        "local_tts",
        "local_subtitles",
        "worker_state",
        "worker_reason",
        "worker_message",
    }
)
MAX_TEXT_POLISH_CHARACTERS = 200_000
MAX_TEXT_POLISH_CONCURRENCY = 2
MAX_DEVICE_ENROLL_BYTES = 32 * 1024
MAX_DEVICE_ENROLL_ATTEMPTS_PER_IP_MINUTE = 20
MAX_DEVICE_ENROLL_FAILURE_ENTRIES = 2048

# Service RPCs deliberately stay outside CATALOG_RPC_METHODS.  In particular,
# HubCatalogProxy must continue to expose only repository-shaped operations.
# Text work is called explicitly through HubTextProvider so provider secrets and
# host-only runtime configuration can never be mistaken for shared catalog data.
_TEXT_REQUEST_FIELDS = frozenset(item.name for item in fields(TextRequest))
_TEXT_RESULT_FIELDS = frozenset(
    {
        "polished_text",
        "hook",
        "ending_cta",
        "mood",
        "provider",
        "model",
        "retention_ratio",
    }
)

DEFAULT_DOWNLOAD_EXTENSIONS = frozenset(
    {
        ".aac",
        ".ass",
        ".flac",
        ".jpeg",
        ".jpg",
        ".json",
        ".m4a",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".opus",
        ".png",
        ".srt",
        ".wav",
        ".webm",
        ".webp",
        ".bmp",
    }
)

_FILE_CONTENT_TYPES = {
    ".ass": "text/x-ssa; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".srt": "application/x-subrip; charset=utf-8",
}

DEFAULT_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_SHA256_HEADER = "X-Content-SHA256"

# Permissions are evaluated from CatalogRepository.get_effective_permissions
# for every RPC call.  Tuples mean "any one of" so an administrator can grant
# narrowly-scoped access without silently giving a producer a broader role.
_RPC_PERMISSION_ANY = HUB_RPC_PERMISSION_ANY

_RECORD_LEASE_RPC_METHODS = frozenset(
    {
        "claim_record_lease",
        "heartbeat_record_lease",
        "release_record_lease",
        "bind_lease_gate_batch",
    }
)
_DEVICE_BOUND_LEASE_RPC_METHODS = frozenset(
    {"claim_record_lease", "heartbeat_record_lease", "release_record_lease"}
)
_JOB_ARCHIVE_RPC_METHODS = frozenset(
    {"archive_job_snapshot", "restore_job_snapshot"}
)
_BATCH_ARCHIVE_RPC_METHODS = frozenset(
    {"archive_batch_snapshots", "restore_batch_snapshots"}
)

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render_client_version_is_supported(value: Any) -> bool:
    """Return whether a workstation can safely claim current render jobs.

    The desktop releases use compact prerelease labels such as ``-rc6``.
    Parsing that numeric suffix here avoids the lexical ``rc10 < rc5`` bug
    while deliberately failing closed for missing or unknown version labels.
    A later core version (for example 0.5.0-rc1) remains compatible.
    """

    matched = _RENDER_CLIENT_VERSION_PATTERN.fullmatch(str(value or "").strip())
    if matched is None:
        return False
    core = tuple(
        int(matched.group(name)) for name in ("major", "minor", "patch")
    )
    if core != _MINIMUM_RENDER_CLIENT_CORE:
        return core > _MINIMUM_RENDER_CLIENT_CORE
    rc_value = matched.group("rc")
    if _MINIMUM_RENDER_CLIENT_RC is None:
        return rc_value is None
    return rc_value is None or int(rc_value) >= _MINIMUM_RENDER_CLIENT_RC


def _file_content_type(path: Path) -> str:
    return _FILE_CONTENT_TYPES.get(path.suffix.casefold()) or (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )


class HubError(RuntimeError):
    """Base exception for StoryForge Hub transport failures."""


class HubServerStateError(HubError):
    """Raised when a server lifecycle operation is invalid."""


class HubConnectionError(HubError):
    """Raised when the client cannot reach or decode the Hub."""


class HubRemoteError(HubError):
    """A structured error returned by a remote Hub."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        request_id: str | int | None = None,
    ) -> None:
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.request_id = request_id
        super().__init__(f"Hub returned HTTP {self.status} ({self.code}): {self.message}")


class HubAuthenticationError(HubRemoteError):
    """Raised when a bearer token is absent or rejected."""


class _HubHTTPError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = int(status)
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _Authentication:
    authenticated: bool
    actor_user_id: str | None = None
    token_id: str | None = None
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ActorAccess:
    user_id: str
    permissions: frozenset[str]


def _clean_token(value: Any) -> str:
    token = str(value or "")
    if not token:
        raise ValueError("bearer tokens cannot be empty")
    if any(character.isspace() for character in token):
        raise ValueError("bearer tokens cannot contain whitespace")
    if "\r" in token or "\n" in token:
        raise ValueError("bearer tokens cannot contain newlines")
    if len(token) > 4096:
        raise ValueError("bearer token is too long")
    try:
        token.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("bearer tokens must contain ASCII characters only") from error
    return token


def _bounded_text(value: Any, label: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if required and not value.strip():
        raise ValueError(f"{label} cannot be empty")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds the {maximum}-character limit")
    return value


def _device_capabilities(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("capabilities must be an object")
    unsupported = sorted(set(map(str, value)) - DEVICE_CAPABILITY_FIELDS)
    if unsupported:
        raise ValueError(
            "capabilities contain unsupported fields: " + ", ".join(unsupported)
        )
    result = {
        "device_config_sync": 1,
        "local_render": True,
        "local_tts": True,
        "local_subtitles": True,
    }
    if "device_config_sync" in value:
        raw_version = value["device_config_sync"]
        if isinstance(raw_version, bool):
            raise ValueError("device_config_sync must be an integer")
        try:
            parsed_version = int(raw_version)
        except (TypeError, ValueError) as error:
            raise ValueError("device_config_sync must be an integer") from error
        if not 1 <= parsed_version <= 100:
            raise ValueError("device_config_sync is unsupported")
        result["device_config_sync"] = parsed_version
    for name in ("local_render", "local_tts", "local_subtitles"):
        if name in value:
            if not isinstance(value[name], bool):
                raise ValueError(f"{name} must be true or false")
            result[name] = value[name]
    if "worker_state" in value:
        worker_state = _bounded_text(
            value["worker_state"], "worker_state", 32
        ).strip().casefold()
        if worker_state not in {
            "ready",
            "busy",
            "paused",
            "draining",
            "cooling",
            "degraded",
            "fault",
        }:
            raise ValueError("worker_state is unsupported")
        result["worker_state"] = worker_state
    if "worker_reason" in value:
        worker_reason = _bounded_text(
            value["worker_reason"], "worker_reason", 64
        ).strip().casefold()
        if worker_reason and not all(
            character.isascii()
            and (character.isalnum() or character in {"_", "-"})
            for character in worker_reason
        ):
            raise ValueError("worker_reason contains unsupported characters")
        result["worker_reason"] = worker_reason
    if "worker_message" in value:
        result["worker_message"] = " ".join(
            _bounded_text(
                value["worker_message"], "worker_message", 240
            ).split()
        )
    return result


def _text_request_from_rpc(value: Any) -> TextRequest:
    """Build a TextRequest from the intentionally small service contract."""

    if not isinstance(value, Mapping):
        raise ValueError("text_polish request must be an object")
    unknown = set(value) - _TEXT_REQUEST_FIELDS
    if unknown:
        raise ValueError(
            "text_polish request contains unsupported fields: "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    if "text" not in value:
        raise ValueError("text_polish request requires text")

    prepared = dict(value)
    limits = {
        "text": MAX_TEXT_POLISH_CHARACTERS,
        "title": 500,
        "platform": 200,
        "code": 200,
        "ending_template": 2_000,
        "adult_mode": 32,
        "language": 100,
        "purpose": 32,
    }
    for name, maximum in limits.items():
        if name in prepared:
            prepared[name] = _bounded_text(
                prepared[name], name, maximum, required=name == "text"
            )
    for name in ("retention_min", "retention_max"):
        if name in prepared:
            raw = prepared[name]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"{name} must be a number")
            prepared[name] = float(raw)
    for name in ("creative_line_index", "creative_line_count"):
        if name in prepared:
            raw = prepared[name]
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise ValueError(f"{name} must be an integer")
    if "enforce_retention" in prepared and not isinstance(
        prepared["enforce_retention"], bool
    ):
        raise ValueError("enforce_retention must be a boolean")
    return TextRequest(**prepared)


def _text_request_to_rpc(request: TextRequest) -> dict[str, Any]:
    # asdict is safe here because every TextRequest field is a scalar and the
    # server independently repeats all validation before contacting a provider.
    return asdict(request)


def _text_result_from_rpc(value: Any) -> TextResult:
    if not isinstance(value, Mapping):
        raise ValueError("Hub text response must be an object")
    unknown = set(value) - _TEXT_RESULT_FIELDS
    missing = {
        "polished_text",
        "hook",
        "ending_cta",
        "mood",
        "provider",
        "retention_ratio",
    } - set(value)
    if unknown or missing:
        raise ValueError("Hub text response does not match the service contract")
    strings: dict[str, str] = {}
    for name in ("polished_text", "hook", "ending_cta", "mood", "provider", "model"):
        raw = value.get(name, "")
        if not isinstance(raw, str):
            raise ValueError(f"Hub text response field {name} must be a string")
        strings[name] = raw
    ratio = value.get("retention_ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        raise ValueError("Hub text response retention_ratio must be a number")
    return TextResult(
        polished_text=strings["polished_text"],
        hook=strings["hook"],
        ending_cta=strings["ending_cta"],
        mood=strings["mood"],
        provider=strings["provider"],
        model=strings["model"],
        retention_ratio=float(ratio),
    )


def _normalize_tokens(
    tokens: str | Mapping[str, str | None] | Sequence[str],
) -> tuple[tuple[str, str | None], ...]:
    normalized: list[tuple[str, str | None]] = []
    if isinstance(tokens, str):
        normalized.append((_clean_token(tokens), None))
    elif isinstance(tokens, Mapping):
        for raw_token, raw_actor in tokens.items():
            actor = str(raw_actor).strip() if raw_actor not in (None, "") else None
            normalized.append((_clean_token(raw_token), actor))
    elif isinstance(tokens, Sequence) and not isinstance(
        tokens, (bytes, bytearray)
    ):
        normalized.extend((_clean_token(item), None) for item in tokens)
    else:
        raise TypeError("tokens must be a token, token sequence, or token-to-user mapping")
    if not normalized:
        raise ValueError("at least one bearer token is required")
    if len({token for token, _actor in normalized}) != len(normalized):
        raise ValueError("bearer tokens must be unique")
    return tuple(normalized)


class _StoryForgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        owner: "HubServer",
    ) -> None:
        self.owner = owner
        super().__init__(server_address, handler_class)

    def get_request(self) -> tuple[Any, Any]:
        request, client_address = super().get_request()
        request.settimeout(self.owner.request_timeout_seconds)
        return request, client_address


class _HubRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StoryForgeHub/1"
    sys_version = ""

    @property
    def owner(self) -> "HubServer":
        return self.server.owner  # type: ignore[attr-defined, no-any-return]

    def log_message(self, _format: str, *_args: Any) -> None:
        # Never print Authorization headers or noisy per-request logs from a
        # desktop/background process. Unexpected exceptions are retained by the
        # owner in ``last_error`` for diagnostics.
        return

    def _send_json(
        self,
        status: int,
        payload: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            dict(payload), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_html(self, status: int, document: str) -> None:
        body = document.encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Language", "zh-CN")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _request_endpoint(self) -> str:
        """Return the local address used by this connection, never Host input."""

        try:
            socket_address = self.connection.getsockname()
            host = str(socket_address[0]).strip()
            port = int(socket_address[1])
        except (AttributeError, IndexError, OSError, TypeError, ValueError):
            return self.owner.base_url
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{port}"

    def _landing_page(self) -> str:
        # The page intentionally contains no catalog, user, token, or request
        # query data.  It is only a human-readable signpost for someone who
        # opens the LAN API endpoint in a browser.
        endpoint = escape(self._request_endpoint(), quote=True)
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>StoryForge Hub 正在运行</title>
  <style>
    :root {{ color-scheme: light; font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 32px 20px;
      color: #18243d; background: #f3f6fb; }}
    main {{ width: min(680px, 100%); padding: 38px; border: 1px solid #dbe3f0; border-radius: 20px;
      background: #fff; box-shadow: 0 20px 55px rgba(24, 36, 61, .10); }}
    .status {{ display: inline-flex; align-items: center; gap: 9px; padding: 7px 12px; border-radius: 999px;
      color: #087d5b; background: #eaf8f3; font-size: 14px; font-weight: 700; }}
    .status::before {{ content: ""; width: 9px; height: 9px; border-radius: 50%; background: #18a978; }}
    h1 {{ margin: 20px 0 10px; font-size: clamp(28px, 5vw, 40px); letter-spacing: -.03em; }}
    .lead {{ margin: 0; color: #53627b; font-size: 18px; line-height: 1.7; }}
    .endpoint {{ margin: 26px 0; padding: 16px 18px; border-radius: 12px; background: #17243d; color: #fff; }}
    .endpoint span {{ display: block; margin-bottom: 7px; color: #9fb2d4; font-size: 12px; font-weight: 700;
      letter-spacing: .08em; text-transform: uppercase; }}
    code {{ font-family: Consolas, monospace; font-size: 16px; word-break: break-all; }}
    h2 {{ margin: 28px 0 12px; font-size: 18px; }}
    ol {{ margin: 0; padding-left: 24px; color: #34435d; line-height: 1.9; }}
    .note {{ margin: 24px 0 0; padding: 14px 16px; border-left: 4px solid #3264e8; border-radius: 8px;
      color: #4a5972; background: #f0f4ff; line-height: 1.65; }}
  </style>
</head>
<body>
  <main>
    <div class="status">Hub 接口正在运行</div>
    <h1>StoryForge Hub 已启动</h1>
    <p class="lead"><strong>这里不是网页后台。</strong>这是 StoryForge 在多台电脑之间同步资料和制作任务的数据接口。</p>
    <div class="endpoint"><span>Hub 服务地址（仅供管理员维修）</span><code>{endpoint}</code></div>
    <h2>员工电脑首次使用</h2>
    <ol>
      <li>把完整的 StoryForge 发布文件夹复制到员工电脑。</li>
      <li>双击 StoryForge Studio.exe。</li>
      <li>输入员工账号和密码即可开始使用。</li>
    </ol>
    <p class="note">软件会自动找到 Hub、读取 Windows 电脑名称、登记设备并启动本机制作服务。员工只输入账号密码，不填写其他连接信息。</p>
  </main>
</body>
</html>"""

    def _send_failure(
        self,
        status: int,
        code: str,
        message: str,
        *,
        request_id: str | int | None = None,
        authenticate: bool = False,
    ) -> None:
        headers = {"WWW-Authenticate": 'Bearer realm="StoryForge Hub"'} if authenticate else None
        self._send_json(
            status,
            {
                "ok": False,
                "id": request_id,
                "error": {"code": code, "message": message},
            },
            extra_headers=headers,
        )

    def _authenticate(self) -> _Authentication | None:
        authentication = self.owner.authenticate(self.headers.get("Authorization", ""))
        if not authentication.authenticated:
            self._send_failure(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                "a valid bearer token is required",
                authenticate=True,
            )
            return None
        return authentication

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        parsed_request = urlsplit(self.path)
        path = parsed_request.path
        web_application = self.owner.web_application
        if web_application is not None and web_application.handle_get(
            self, parsed_request
        ):
            return
        if path in {"/", "/hub"}:
            self._send_html(HTTPStatus.OK, self._landing_page())
            return
        if path == "/health":
            try:
                health = self.owner.health()
            except Exception as error:  # health must fail closed without details
                self.owner._record_error(error)
                self._send_failure(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "unhealthy",
                    "catalog health check failed",
                )
                return
            self._send_json(HTTPStatus.OK, health)
            return
        if path == "/updates/manifest":
            authentication = self._authenticate()
            if authentication is None:
                return
            try:
                self.owner.authorize_update_access(authentication.actor_user_id)
                _scheme, _separator, bearer_token = str(
                    self.headers.get("Authorization", "")
                ).partition(" ")
                update = self.owner.published_update_manifest()
                signature = sign_update_manifest(bearer_token, update)
            except _HubHTTPError as error:
                self._send_failure(error.status, error.code, error.message)
                return
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "update": update, "signature": signature},
            )
            return
        if path == "/updates/package":
            authentication = self._authenticate()
            if authentication is None:
                return
            try:
                self.owner.authorize_update_access(authentication.actor_user_id)
                query = parse_qs(parsed_request.query, keep_blank_values=True)
                if len(query.get("version", [])) != 1:
                    raise _HubHTTPError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_update_request",
                        "update download requires exactly one version",
                    )
                package, manifest = self.owner.resolve_update_package(
                    query["version"][0]
                )
            except _HubHTTPError as error:
                self._send_failure(error.status, error.code, error.message)
                return
            self._serve_update_package(package, manifest)
            return
        if path == "/components/manifest":
            authentication = self._authenticate()
            if authentication is None:
                return
            try:
                self.owner.authorize_update_access(authentication.actor_user_id)
                _scheme, _separator, bearer_token = str(
                    self.headers.get("Authorization", "")
                ).partition(" ")
                catalog = self.owner.published_component_catalog()
                signature = sign_component_catalog(bearer_token, catalog)
            except _HubHTTPError as error:
                self._send_failure(error.status, error.code, error.message)
                return
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "catalog": catalog, "signature": signature},
            )
            return
        if path == "/components/package":
            authentication = self._authenticate()
            if authentication is None:
                return
            try:
                self.owner.authorize_update_access(authentication.actor_user_id)
                query = parse_qs(parsed_request.query, keep_blank_values=True)
                if any(
                    len(query.get(name, [])) != 1
                    for name in ("component_id", "version")
                ):
                    raise _HubHTTPError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_component_request",
                        "component download requires exactly one component_id and version",
                    )
                package, publication = self.owner.resolve_component_package(
                    query["component_id"][0], query["version"][0]
                )
            except _HubHTTPError as error:
                self._send_failure(error.status, error.code, error.message)
                return
            self._serve_update_package(package, publication)
            return
        if path == "/file-upload-check":
            authentication = self._authenticate()
            if authentication is None:
                return
            try:
                self.owner.authorize_file_access(
                    authentication.actor_user_id, write=True
                )
                query = parse_qs(parsed_request.query, keep_blank_values=True)
                if any(
                    len(query.get(name, [])) != 1
                    for name in ("root", "path", "size_bytes", "sha256")
                ):
                    raise _HubHTTPError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_upload_check",
                        "upload check requires root, path, size_bytes, and sha256",
                    )
                checked = self.owner.prepare_file_upload(
                    query["root"][0],
                    query["path"][0],
                    query["size_bytes"][0],
                    query["sha256"][0],
                )
            except _HubHTTPError as error:
                self._send_failure(error.status, error.code, error.message)
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "file": checked})
            return
        if path.startswith("/files/"):
            authentication = self._authenticate()
            if authentication is None:
                return
            try:
                self.owner.authorize_file_access(
                    authentication.actor_user_id, write=False
                )
            except _HubHTTPError as error:
                self._send_failure(error.status, error.code, error.message)
                return
            self._serve_download(path)
            return
        self._send_failure(HTTPStatus.NOT_FOUND, "not_found", "route not found")

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler contract
        path = urlsplit(self.path).path
        if not path.startswith("/files/"):
            self._send_failure(HTTPStatus.NOT_FOUND, "not_found", "route not found")
            return
        authentication = self._authenticate()
        if authentication is None:
            return
        try:
            self.owner.authorize_file_access(authentication.actor_user_id, write=True)
        except _HubHTTPError as error:
            self._send_failure(error.status, error.code, error.message)
            return
        self._receive_upload(path)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        parsed_request = urlsplit(self.path)
        path = parsed_request.path
        web_application = self.owner.web_application
        if web_application is not None and web_application.handle_post(
            self, parsed_request
        ):
            return
        if path == "/device-enroll":
            self._enroll_device()
            return
        if path != "/rpc":
            self._send_failure(HTTPStatus.NOT_FOUND, "not_found", "route not found")
            return
        authentication = self._authenticate()
        if authentication is None:
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            self._send_failure(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/json",
            )
            return
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self._send_failure(
                HTTPStatus.LENGTH_REQUIRED,
                "length_required",
                "Content-Length is required",
            )
            return
        if length <= 0:
            self._send_failure(
                HTTPStatus.BAD_REQUEST, "empty_request", "request body is empty"
            )
            return
        if length > self.owner.max_request_bytes:
            self._send_failure(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                "request body exceeds the configured limit",
            )
            return
        try:
            raw_body = self.rfile.read(length)
        except (TimeoutError, OSError):
            self._send_failure(
                HTTPStatus.REQUEST_TIMEOUT,
                "request_timeout",
                "request body was not received before timeout",
            )
            return
        if len(raw_body) != length:
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "incomplete_request",
                "request body ended before Content-Length bytes were received",
            )
            return
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_failure(
                HTTPStatus.BAD_REQUEST, "invalid_json", "request body is not valid UTF-8 JSON"
            )
            return
        if not isinstance(payload, Mapping):
            self._send_failure(
                HTTPStatus.BAD_REQUEST, "invalid_request", "RPC request must be an object"
            )
            return
        request_id = payload.get("id")
        if request_id is not None and (
            isinstance(request_id, bool) or not isinstance(request_id, (str, int))
        ):
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "RPC id must be a string, integer, or null",
            )
            return
        method = payload.get("method")
        params = payload.get("params", {})
        if not isinstance(method, str) or not method.strip():
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "RPC method is required",
                request_id=request_id,
            )
            return
        if not isinstance(params, Mapping):
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "invalid_params",
                "RPC params must be an object",
                request_id=request_id,
            )
            return
        try:
            result = self.owner.dispatch_rpc(
                method.strip(), dict(params), authentication
            )
        except _HubHTTPError as error:
            self._send_failure(
                error.status,
                error.code,
                error.message,
                request_id=request_id,
            )
            return
        except CatalogValidationError as error:
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "validation_error",
                str(error),
                request_id=request_id,
            )
            return
        except CatalogNotFoundError as error:
            self._send_failure(
                HTTPStatus.NOT_FOUND,
                "catalog_not_found",
                str(error),
                request_id=request_id,
            )
            return
        except CatalogPermissionError as error:
            self._send_failure(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                str(error),
                request_id=request_id,
            )
            return
        except CatalogConflictError as error:
            self._send_failure(
                HTTPStatus.CONFLICT,
                "catalog_conflict",
                str(error),
                request_id=request_id,
            )
            return
        except (TypeError, ValueError) as error:
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "invalid_params",
                str(error),
                request_id=request_id,
            )
            return
        except CatalogError as error:
            self.owner._record_error(error)
            self._send_failure(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "catalog_error",
                "catalog operation failed",
                request_id=request_id,
            )
            return
        except Exception as error:
            self.owner._record_error(error)
            self._send_failure(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "internal Hub error",
                request_id=request_id,
            )
            return
        self._send_json(
            HTTPStatus.OK, {"ok": True, "id": request_id, "result": result}
        )

    def _enroll_device(self) -> None:
        """Exchange one account password for a revocable device token."""

        content_type = (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        )
        if content_type != "application/json":
            self._send_failure(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/json",
            )
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            self._send_failure(
                HTTPStatus.LENGTH_REQUIRED,
                "length_required",
                "Content-Length is required",
            )
            return
        if length <= 0 or length > min(
            self.owner.max_request_bytes, MAX_DEVICE_ENROLL_BYTES
        ):
            status = (
                HTTPStatus.BAD_REQUEST
                if length <= 0
                else HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            )
            self._send_failure(
                status,
                "invalid_enrollment_request",
                "device enrollment request is invalid",
            )
            return
        try:
            raw_body = self.rfile.read(length)
        except (TimeoutError, OSError):
            self._send_failure(
                HTTPStatus.REQUEST_TIMEOUT,
                "request_timeout",
                "request body was not received before timeout",
            )
            return
        if len(raw_body) != length:
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "invalid_enrollment_request",
                "device enrollment request is invalid",
            )
            return
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "invalid_enrollment_request",
                "device enrollment request is invalid",
            )
            return
        if not isinstance(payload, Mapping):
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "invalid_enrollment_request",
                "device enrollment request is invalid",
            )
            return
        allowed_fields = {
            "username",
            "password",
            "device_name",
            "installation_id",
            "app_version",
            "capabilities",
            "hostname",
            "os_name",
            "architecture",
        }
        if set(payload) - allowed_fields:
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "invalid_enrollment_request",
                "device enrollment request is invalid",
            )
            return
        try:
            result = self.owner.enroll_device(
                username=payload.get("username"),
                password=payload.get("password"),
                device_name=payload.get("device_name"),
                installation_id=payload.get("installation_id"),
                app_version=payload.get("app_version"),
                capabilities=payload.get("capabilities"),
                hostname=payload.get("hostname"),
                os_name=payload.get("os_name"),
                architecture=payload.get("architecture"),
                client_ip=str(self.client_address[0] or ""),
            )
        except _HubHTTPError as error:
            self._send_failure(error.status, error.code, error.message)
            return
        except Exception as error:
            self.owner._record_error(error)
            self._send_failure(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "device enrollment is temporarily unavailable",
            )
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "device": result})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler contract
        parsed_request = urlsplit(self.path)
        web_application = self.owner.web_application
        if web_application is not None and web_application.handle_delete(
            self, parsed_request
        ):
            return
        self._send_failure(HTTPStatus.NOT_FOUND, "not_found", "route not found")

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        parsed_request = urlsplit(self.path)
        path = parsed_request.path
        web_application = self.owner.web_application
        if web_application is not None and web_application.handle_head(
            self, parsed_request
        ):
            return
        if path.startswith("/files/"):
            authentication = self._authenticate()
            if authentication is None:
                return
            try:
                self.owner.authorize_file_access(
                    authentication.actor_user_id, write=False
                )
            except _HubHTTPError as error:
                self._send_failure(error.status, error.code, error.message)
                return
            self._serve_download(path, head_only=True)
            return
        self._send_failure(HTTPStatus.NOT_FOUND, "not_found", "route not found")

    def _serve_download(self, request_path: str, *, head_only: bool = False) -> None:
        try:
            alias, relative_path = self._decode_file_route(request_path)
            alias = alias.strip()
            file_path = self.owner.resolve_download(alias, relative_path)
        except (UnicodeDecodeError, _HubHTTPError) as error:
            if isinstance(error, _HubHTTPError):
                self._send_failure(error.status, error.code, error.message)
            else:
                self._send_failure(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_file_path",
                    "file path is not valid UTF-8",
                )
            return
        try:
            metadata = self.owner.download_file_metadata(file_path)
            stream = None if head_only else file_path.open("rb")
        except _HubHTTPError as error:
            self._send_failure(error.status, error.code, error.message)
            return
        except OSError:
            self._send_failure(
                HTTPStatus.NOT_FOUND, "file_not_found", "download file not found"
            )
            return
        content_type = _file_content_type(file_path)
        encoded_name = quote(file_path.name, safe="")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(metadata["size_bytes"]))
        self.send_header(UPLOAD_SHA256_HEADER, str(metadata["sha256"]))
        self.send_header("ETag", str(metadata["etag"]))
        self.send_header(
            "Content-Disposition", f"attachment; filename*=UTF-8''{encoded_name}"
        )
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        if stream is not None:
            with stream:
                try:
                    shutil.copyfileobj(stream, self.wfile, length=64 * 1024)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
        self.close_connection = True

    def _serve_update_package(
        self,
        package: Path,
        manifest: Mapping[str, Any],
    ) -> None:
        try:
            stream = package.open("rb")
        except OSError:
            self._send_failure(
                HTTPStatus.NOT_FOUND,
                "update_not_found",
                "published update package not found",
            )
            return
        with stream:
            size = os.fstat(stream.fileno()).st_size
            encoded_name = quote(package.name, safe="")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-SHA256", str(manifest["sha256"]))
            self.send_header(
                "Content-Disposition", f"attachment; filename*=UTF-8''{encoded_name}"
            )
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self.close_connection = True

    @staticmethod
    def _decode_file_route(request_path: str) -> tuple[str, str]:
        encoded = request_path[len("/files/") :]
        root_alias, separator, encoded_relative = encoded.partition("/")
        if not separator:
            raise _HubHTTPError(
                HTTPStatus.BAD_REQUEST,
                "invalid_file_path",
                "file path must include a root alias and relative path",
            )
        return (
            unquote(root_alias, errors="strict"),
            unquote(encoded_relative, errors="strict"),
        )

    def _receive_upload(self, request_path: str) -> None:
        content_type = (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        )
        if content_type != "application/octet-stream":
            self._send_failure(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/octet-stream",
            )
            return
        if self.headers.get("Transfer-Encoding"):
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "unsupported_transfer_encoding",
                "uploads require a fixed Content-Length",
            )
            return
        length_headers = self.headers.get_all("Content-Length", [])
        if len(length_headers) != 1:
            self._send_failure(
                HTTPStatus.LENGTH_REQUIRED,
                "length_required",
                "one Content-Length header is required",
            )
            return
        raw_length = length_headers[0].strip()
        if not raw_length.isdecimal():
            self._send_failure(
                HTTPStatus.LENGTH_REQUIRED,
                "length_required",
                "Content-Length must be a non-negative integer",
            )
            return
        length = int(raw_length)
        if length <= 0:
            self._send_failure(
                HTTPStatus.BAD_REQUEST, "empty_upload", "upload body is empty"
            )
            return
        if length > self.owner.max_upload_bytes:
            self._send_failure(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "upload_too_large",
                "upload exceeds the configured limit",
            )
            return

        digest_headers = self.headers.get_all(UPLOAD_SHA256_HEADER, [])
        if len(digest_headers) != 1:
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "sha256_required",
                f"one {UPLOAD_SHA256_HEADER} header is required",
            )
            return
        expected_sha256 = digest_headers[0].strip().casefold()
        if len(expected_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in expected_sha256
        ):
            self._send_failure(
                HTTPStatus.BAD_REQUEST,
                "invalid_sha256",
                f"{UPLOAD_SHA256_HEADER} must contain a 64-character hexadecimal digest",
            )
            return

        try:
            alias, relative_path = self._decode_file_route(request_path)
            alias = alias.strip()
            destination = self.owner.resolve_upload(alias, relative_path)
        except (UnicodeDecodeError, _HubHTTPError) as error:
            if isinstance(error, _HubHTTPError):
                self._send_failure(error.status, error.code, error.message)
            else:
                self._send_failure(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_file_path",
                    "file path is not valid UTF-8",
                )
            return

        replaced_existing = destination.exists()
        temporary_path: Path | None = None
        try:
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".upload",
                dir=destination.parent,
            )
            temporary_path = Path(temporary_name)
            digest = hashlib.sha256()
            remaining = length
            with os.fdopen(handle, "wb") as stream:
                while remaining:
                    try:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                    except (TimeoutError, OSError):
                        raise _HubHTTPError(
                            HTTPStatus.REQUEST_TIMEOUT,
                            "upload_timeout",
                            "upload body was not received before timeout",
                        ) from None
                    if not chunk:
                        raise _HubHTTPError(
                            HTTPStatus.BAD_REQUEST,
                            "incomplete_upload",
                            "upload body ended before Content-Length bytes were received",
                        )
                    stream.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                stream.flush()
                os.fsync(stream.fileno())

            actual_sha256 = digest.hexdigest()
            if not hmac.compare_digest(actual_sha256, expected_sha256):
                raise _HubHTTPError(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    "sha256_mismatch",
                    "uploaded bytes do not match the declared SHA-256 digest",
                )

            # Revalidate after streaming so a path or link changed during a long
            # upload cannot redirect the final atomic replacement.
            confirmed_destination = self.owner.resolve_upload(alias, relative_path)
            if confirmed_destination != destination:
                raise _HubHTTPError(
                    HTTPStatus.CONFLICT,
                    "upload_path_changed",
                    "upload destination changed while receiving the file",
                )
            replaced_existing = confirmed_destination.exists()
            os.replace(temporary_path, confirmed_destination)
            temporary_path = None
        except _HubHTTPError as error:
            self._send_failure(error.status, error.code, error.message)
            return
        except OSError as error:
            self.owner._record_error(error)
            self._send_failure(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "upload_failed",
                "Hub could not store the uploaded file",
            )
            return
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

        root = self.owner.download_roots[alias]
        stored_relative = destination.relative_to(root).as_posix()
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "file": {
                    "root": alias,
                    "path": stored_relative,
                    "size_bytes": length,
                    "sha256": expected_sha256,
                    "content_type": _file_content_type(destination),
                    "replaced": replaced_existing,
                },
            },
        )


class HubServer:
    """Threaded LAN HTTP facade over one :class:`CatalogRepository`.

    SQLite remains local to this server process. Studio clients communicate via
    the fixed JSON-RPC allowlist and never open the database file themselves.
    """

    def __init__(
        self,
        catalog: CatalogRepository,
        tokens: str | Mapping[str, str | None] | Sequence[str],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        data_root: str | Path | None = None,
        attachment_root: str | Path | None = None,
        update_repository: UpdateRepository | None = None,
        component_repository: ComponentRepository | None = None,
        enabled_methods: Sequence[str] | None = None,
        allowed_download_extensions: Sequence[str] | None = None,
        max_request_bytes: int = 16 * 1024 * 1024,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        request_timeout_seconds: float = 30.0,
        text_provider_config_getter: Callable[[], Any] | None = None,
        text_provider_factory: Callable[[Any], Any] = create_text_provider,
        text_polish_max_concurrency: int = MAX_TEXT_POLISH_CONCURRENCY,
        text_polish_queue_timeout_seconds: float = 10.0,
    ) -> None:
        if not isinstance(catalog, CatalogRepository):
            raise TypeError("catalog must be a CatalogRepository")
        self.catalog = catalog
        self._tokens = _normalize_tokens(tokens)
        self.host = str(host or "").strip()
        if not self.host:
            raise ValueError("host cannot be empty")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        self.port = port
        if isinstance(max_request_bytes, bool) or int(max_request_bytes) < 1024:
            raise ValueError("max_request_bytes must be at least 1024")
        self.max_request_bytes = int(max_request_bytes)
        if isinstance(max_upload_bytes, bool) or int(max_upload_bytes) < 1:
            raise ValueError("max_upload_bytes must be a positive integer")
        self.max_upload_bytes = int(max_upload_bytes)
        self.request_timeout_seconds = float(request_timeout_seconds)
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if text_provider_config_getter is not None and not callable(
            text_provider_config_getter
        ):
            raise TypeError("text_provider_config_getter must be callable")
        if not callable(text_provider_factory):
            raise TypeError("text_provider_factory must be callable")
        if (
            isinstance(text_polish_max_concurrency, bool)
            or not isinstance(text_polish_max_concurrency, int)
            or not 1 <= text_polish_max_concurrency <= 16
        ):
            raise ValueError("text_polish_max_concurrency must be between 1 and 16")
        if (
            isinstance(text_polish_queue_timeout_seconds, bool)
            or float(text_polish_queue_timeout_seconds) <= 0
            or float(text_polish_queue_timeout_seconds) > 60
        ):
            raise ValueError(
                "text_polish_queue_timeout_seconds must be between 0 and 60"
            )
        self._text_provider_config_getter = text_provider_config_getter
        self._text_provider_factory = text_provider_factory
        self._text_polish_slots = threading.BoundedSemaphore(
            text_polish_max_concurrency
        )
        self._text_polish_queue_timeout_seconds = float(
            text_polish_queue_timeout_seconds
        )
        if update_repository is not None and not isinstance(
            update_repository, UpdateRepository
        ):
            raise TypeError("update_repository must be an UpdateRepository")
        self.update_repository = update_repository
        if component_repository is not None and not isinstance(
            component_repository, ComponentRepository
        ):
            raise TypeError("component_repository must be a ComponentRepository")
        self.component_repository = component_repository

        methods = set(CATALOG_RPC_METHODS if enabled_methods is None else enabled_methods)
        unknown_methods = methods - CATALOG_RPC_METHODS
        if unknown_methods:
            raise ValueError(
                "enabled_methods contains methods outside the fixed allowlist: "
                + ", ".join(sorted(unknown_methods))
            )
        if not methods:
            raise ValueError("enabled_methods cannot be empty")
        self.rpc_methods = frozenset(methods)

        self.download_roots: dict[str, Path] = {}
        for alias, root_value in (
            ("data", data_root),
            ("attachments", attachment_root),
        ):
            if root_value is None:
                continue
            root = Path(root_value).expanduser().resolve(strict=True)
            if not root.is_dir():
                raise ValueError(f"{alias} download root is not a directory")
            self.download_roots[alias] = root
        extensions = allowed_download_extensions or DEFAULT_DOWNLOAD_EXTENSIONS
        cleaned_extensions: set[str] = set()
        for item in extensions:
            extension = str(item or "").strip().casefold()
            if not extension:
                continue
            if not extension.startswith("."):
                extension = "." + extension
            if any(character in extension for character in ("/", "\\", "\r", "\n", "\x00")):
                raise ValueError("download extensions must be simple suffixes")
            cleaned_extensions.add(extension)
        if not cleaned_extensions:
            raise ValueError("at least one download extension is required")
        self.allowed_download_extensions = frozenset(cleaned_extensions)

        self._state_lock = threading.RLock()
        self._httpd: _StoryForgeHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._bound_address: tuple[str, int] | None = None
        self._last_error: str = ""
        self._web_application: Any = None
        self._device_enroll_attempts_by_ip: dict[str, list[float]] = {}
        self._device_enroll_failures: dict[str, tuple[int, float]] = {}
        # File metadata is requested much more often than file bodies. Cache
        # digests by stable stat fields so repeated HEAD/GET requests do not
        # read a large shared file twice. Atomic uploads/replacements change at
        # least the inode, size, mtime or ctime and therefore invalidate it.
        self._download_metadata_cache: dict[
            str, tuple[tuple[int, int, int, int, int], dict[str, Any]]
        ] = {}
        # Browser-to-workstation handoff tickets are deliberately opaque,
        # short-lived and one-time-use.  They exist only in Hub memory, so a
        # restart invalidates every outstanding ticket without touching the
        # durable device bearer credentials.
        self._local_worker_tickets: dict[str, dict[str, Any]] = {}

    @property
    def web_application(self) -> Any:
        """Return the optional authenticated browser facade.

        The import and attachment are explicit so existing Hub-only tests and
        desktop clients retain the exact legacy route surface by default.
        """

        with self._state_lock:
            return self._web_application

    def attach_web_application(
        self,
        api: Any,
        *,
        ui_root: str | Path,
        upload_root: str | Path,
    ) -> Any:
        """Serve the StoryForge browser UI on this Hub's existing listener."""

        from .web import StoryForgeWebApplication

        application = StoryForgeWebApplication(
            api,
            self,
            ui_root=ui_root,
            upload_root=upload_root,
        )
        with self._state_lock:
            previous = self._web_application
            self._web_application = application
        if previous is not None:
            previous.close()
        return application

    @staticmethod
    def _normalized_browser_origin(value: Any) -> str:
        raw = str(value or "").strip()
        parsed = urlsplit(raw)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("browser origin must be an HTTP(S) origin")
        return f"{parsed.scheme}://{parsed.netloc}".casefold()

    def issue_local_worker_ticket(
        self,
        actor_user_id: str,
        *,
        device_id: str,
        worker_nonce: str,
        browser_origin: str,
    ) -> dict[str, Any]:
        """Issue a one-use browser handoff for one enrolled workstation.

        The browser never receives the workstation bearer token.  The local
        worker must redeem this opaque value through its already-enrolled Hub
        client, which proves both the account and the specific device.
        """

        access = self._actor_access(str(actor_user_id or ""))
        self._require_any_permission(
            access, ("drafts.create", "drafts.manage_all", "hub.manage")
        )
        clean_device_id = _bounded_text(
            str(device_id or "").strip(), "device_id", 120, required=True
        )
        clean_nonce = _bounded_text(
            str(worker_nonce or "").strip(), "worker_nonce", 256, required=True
        )
        if len(clean_nonce) < 20:
            raise ValueError("worker_nonce is too short")
        clean_origin = self._normalized_browser_origin(browser_origin)
        device = self.catalog.get_hub_device(clean_device_id)
        if not bool(device.get("active")):
            raise PermissionError("the selected workstation is disabled")
        if str(device.get("last_user_id") or "") != access.user_id:
            raise PermissionError("the workstation belongs to another account")

        now = time.time()
        expires_at = now + LOCAL_WORKER_TICKET_TTL_SECONDS
        raw_ticket = secrets.token_urlsafe(40)
        digest = hashlib.sha256(raw_ticket.encode("ascii")).hexdigest()
        with self._state_lock:
            self._local_worker_tickets = {
                key: value
                for key, value in self._local_worker_tickets.items()
                if float(value.get("expires_at") or 0) > now
            }
            # Bound memory even if a logged-in browser deliberately requests
            # many tickets without redeeming them.
            if len(self._local_worker_tickets) >= 2048:
                oldest = min(
                    self._local_worker_tickets,
                    key=lambda key: float(
                        self._local_worker_tickets[key].get("expires_at") or 0
                    ),
                )
                self._local_worker_tickets.pop(oldest, None)
            self._local_worker_tickets[digest] = {
                "actor_user_id": access.user_id,
                "device_id": clean_device_id,
                "worker_nonce": clean_nonce,
                "browser_origin": clean_origin,
                "expires_at": expires_at,
            }
        return {
            "ticket": raw_ticket,
            "device_id": clean_device_id,
            "expires_at": datetime.fromtimestamp(
                expires_at, timezone.utc
            ).isoformat(),
        }

    def _consume_local_worker_ticket(
        self,
        *,
        ticket: Any,
        worker_nonce: Any,
        browser_origin: Any,
        device_id: Any,
        expected_actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        clean_ticket = _bounded_text(
            str(ticket or ""), "ticket", 512, required=True
        )
        clean_nonce = _bounded_text(
            str(worker_nonce or ""), "worker_nonce", 256, required=True
        )
        clean_device_id = _bounded_text(
            str(device_id or ""), "device_id", 120, required=True
        )
        try:
            origin = self._normalized_browser_origin(browser_origin)
        except ValueError as error:
            raise _HubHTTPError(
                HTTPStatus.BAD_REQUEST, "invalid_worker_origin", str(error)
            ) from None
        digest = hashlib.sha256(clean_ticket.encode("utf-8")).hexdigest()
        with self._state_lock:
            value = self._local_worker_tickets.pop(digest, None)
        now = time.time()
        actor_user_id = str((value or {}).get("actor_user_id") or "")
        if (
            value is None
            or float(value.get("expires_at") or 0) <= now
            or (
                expected_actor_user_id is not None
                and not hmac.compare_digest(actor_user_id, expected_actor_user_id)
            )
            or not hmac.compare_digest(
                str(value.get("device_id") or ""), clean_device_id
            )
            or not hmac.compare_digest(
                str(value.get("worker_nonce") or ""), clean_nonce
            )
            or not hmac.compare_digest(
                str(value.get("browser_origin") or ""), origin
            )
        ):
            raise _HubHTTPError(
                HTTPStatus.UNAUTHORIZED,
                "worker_ticket_invalid",
                "the local-worker ticket is invalid, expired, or already used",
            )
        access = self._actor_access(actor_user_id)
        device = self.catalog.get_hub_device(clean_device_id)
        if (
            not bool(device.get("active"))
            or str(device.get("last_user_id") or "") != access.user_id
        ):
            raise _HubHTTPError(
                HTTPStatus.UNAUTHORIZED,
                "worker_ticket_revoked",
                "the workstation is disabled or belongs to another account",
            )
        return {
            "actor_user_id": access.user_id,
            "device_id": clean_device_id,
            "device_name": str(device.get("name") or ""),
            "browser_origin": origin,
            "permissions": sorted(access.permissions),
            "expires_at": datetime.fromtimestamp(
                now + 30 * 60, timezone.utc
            ).isoformat(),
        }

    def redeem_local_worker_ticket_in_process(
        self,
        ticket: Any,
        *,
        worker_nonce: Any,
        browser_origin: Any,
        device_id: Any,
    ) -> dict[str, Any]:
        """Redeem a ticket from the Hub computer's loopback-only worker."""

        return self._consume_local_worker_ticket(
            ticket=ticket,
            worker_nonce=worker_nonce,
            browser_origin=browser_origin,
            device_id=device_id,
        )

    def _redeem_local_worker_ticket(
        self,
        params: Mapping[str, Any],
        authentication: _Authentication,
        access: _ActorAccess,
    ) -> dict[str, Any]:
        self._require_exact_rpc_params(
            params, frozenset({"ticket", "worker_nonce", "browser_origin"})
        )
        device_id = str(authentication.device_id or "")
        if not device_id:
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "device_identity_required",
                "worker ticket redemption requires a device-bound token",
            )
        return self._consume_local_worker_ticket(
            ticket=params.get("ticket"),
            worker_nonce=params.get("worker_nonce"),
            browser_origin=params.get("browser_origin"),
            device_id=device_id,
            expected_actor_user_id=access.user_id,
        )

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return bool(self._thread and self._thread.is_alive() and self._httpd)

    @property
    def address(self) -> tuple[str, int]:
        with self._state_lock:
            if self._bound_address is None:
                raise HubServerStateError("Hub server is not running")
            return self._bound_address

    @property
    def base_url(self) -> str:
        host, port = self.address
        advertised_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
        if ":" in advertised_host and not advertised_host.startswith("["):
            advertised_host = f"[{advertised_host}]"
        return f"http://{advertised_host}:{port}"

    @property
    def last_error(self) -> str:
        with self._state_lock:
            return self._last_error

    def _record_error(self, error: BaseException) -> None:
        with self._state_lock:
            self._last_error = f"{type(error).__name__}: {error}"

    def start(self) -> "HubServer":
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return self
            httpd = _StoryForgeHTTPServer(
                (self.host, self.port), _HubRequestHandler, self
            )
            bound_host = str(httpd.server_address[0])
            bound_port = int(httpd.server_address[1])
            thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="storyforge-hub",
                daemon=True,
            )
            self._httpd = httpd
            self._thread = thread
            self._bound_address = (bound_host, bound_port)
            thread.start()
            return self

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._state_lock:
            httpd = self._httpd
            thread = self._thread
            web_application = self._web_application
        if httpd is None:
            if web_application is not None:
                web_application.close()
            return
        httpd.shutdown()
        httpd.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
            if thread.is_alive():
                raise HubServerStateError("Hub server did not stop before timeout")
        with self._state_lock:
            if self._httpd is httpd:
                self._httpd = None
                self._thread = None
                self._bound_address = None
                self._web_application = None
        if web_application is not None:
            web_application.close()

    def __enter__(self) -> "HubServer":
        return self.start()

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.stop()

    def authenticate(self, authorization_header: str) -> _Authentication:
        scheme, separator, provided = str(authorization_header or "").partition(" ")
        if not separator or scheme.casefold() != "bearer" or not provided:
            return _Authentication(False)
        matched = False
        actor_user_id: str | None = None
        token_id: str | None = None
        device_id: str | None = None
        for expected, actor in self._tokens:
            equal = hmac.compare_digest(
                provided.encode("utf-8"), str(expected).encode("utf-8")
            )
            if equal:
                matched = True
                actor_user_id = actor
        if not matched:
            # User-scoped tokens are stored as hashes in the catalog and are
            # resolved on every request.  Newly issued and revoked tokens take
            # effect without restarting the long-running Hub computer.
            try:
                identity = self.catalog.resolve_hub_access_identity(provided)
            except (CatalogError, OSError, sqlite3.Error) as error:
                self._record_error(error)
                identity = {"authenticated": False}
            matched = bool(identity.get("authenticated"))
            if matched:
                actor_user_id = str(identity.get("user_id") or "") or None
                token_id = str(identity.get("token_id") or "") or None
                device_id = str(identity.get("device_id") or "") or None
        return _Authentication(
            matched,
            actor_user_id if matched else None,
            token_id if matched else None,
            device_id if matched else None,
        )

    @staticmethod
    def _device_enrollment_key(client_ip: str, username: str) -> str:
        normalized = str(username or "").strip().casefold()
        return hashlib.sha256(
            f"{str(client_ip or '').strip()}\0{normalized}".encode("utf-8")
        ).hexdigest()

    def _begin_device_enrollment(self, client_ip: str, username: str) -> str:
        now = time.time()
        cutoff = now - 60.0
        key = self._device_enrollment_key(client_ip, username)
        with self._state_lock:
            attempts = [
                value
                for value in self._device_enroll_attempts_by_ip.get(client_ip, [])
                if value >= cutoff
            ]
            if len(attempts) >= MAX_DEVICE_ENROLL_ATTEMPTS_PER_IP_MINUTE:
                self._device_enroll_attempts_by_ip[client_ip] = attempts
                raise _HubHTTPError(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "enrollment_rate_limited",
                    "connection attempts are temporarily limited; please try again later",
                )
            attempts.append(now)
            self._device_enroll_attempts_by_ip[client_ip] = attempts
            _count, blocked_until = self._device_enroll_failures.get(key, (0, 0.0))
            if blocked_until > now:
                raise _HubHTTPError(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "enrollment_rate_limited",
                    "connection attempts are temporarily limited; please try again later",
                )
        return key

    def _record_device_enrollment_failure(self, key: str) -> None:
        with self._state_lock:
            if (
                key not in self._device_enroll_failures
                and len(self._device_enroll_failures)
                >= MAX_DEVICE_ENROLL_FAILURE_ENTRIES
            ):
                oldest = min(
                    self._device_enroll_failures,
                    key=lambda item: self._device_enroll_failures[item][1],
                    default="",
                )
                if oldest:
                    self._device_enroll_failures.pop(oldest, None)
            count, _blocked_until = self._device_enroll_failures.get(key, (0, 0.0))
            count += 1
            delay = min(300.0, 2.0 ** min(count, 8)) if count >= 5 else 1.0
            self._device_enroll_failures[key] = (count, time.time() + delay)

    def enroll_device(
        self,
        *,
        username: Any,
        password: Any,
        device_name: Any,
        installation_id: Any,
        app_version: Any = "",
        capabilities: Any = None,
        hostname: Any = "",
        os_name: Any = "",
        architecture: Any = "",
        client_ip: str,
    ) -> dict[str, Any]:
        """Authenticate a member and rotate one stable installation token."""

        clean_username = str(username or "").strip()
        clean_password = str(password or "")
        clean_device_name = str(device_name or "").strip()
        try:
            clean_installation_id = str(UUID(str(installation_id or "").strip()))
        except (AttributeError, TypeError, ValueError):
            clean_installation_id = ""
        clean_version = str(app_version or "").strip()
        clean_hostname = str(hostname or "").strip()
        clean_os_name = str(os_name or "").strip()
        clean_architecture = str(architecture or "").strip()
        try:
            clean_capabilities = _device_capabilities(capabilities)
        except (TypeError, ValueError):
            clean_capabilities = {}
        if (
            not clean_username
            or not clean_password
            or not clean_device_name
            or not clean_installation_id
            or len(clean_username) > 200
            or len(clean_password) > 512
            or len(clean_device_name) > 120
            or len(clean_version) > 80
            or len(clean_hostname) > 255
            or len(clean_os_name) > 120
            or len(clean_architecture) > 80
            or not clean_version
            or not clean_capabilities
        ):
            raise _HubHTTPError(
                HTTPStatus.BAD_REQUEST,
                "invalid_enrollment_request",
                "account, password, computer name, and installation identity are required",
            )
        key = self._begin_device_enrollment(client_ip, clean_username)
        candidate = self.catalog._web_user_by_username(clean_username)
        verifier = str((candidate or {}).get("password_hash") or "")
        matched = password_matches(
            clean_password, verifier or DUMMY_PASSWORD_VERIFIER
        )
        if not candidate or not bool(candidate.get("active")) or not verifier or not matched:
            self._record_device_enrollment_failure(key)
            # Keep account existence, disabled state and password mismatch
            # indistinguishable. PBKDF2 has already consumed the dominant time.
            time.sleep(0.12)
            raise _HubHTTPError(
                HTTPStatus.UNAUTHORIZED,
                "enrollment_failed",
                "account or password is incorrect",
            )
        try:
            registration = self.catalog.register_hub_device(
                {
                    "installation_id_hash": installation_id_sha256(
                        clean_installation_id
                    ),
                    "name": clean_device_name,
                    "hostname": clean_hostname,
                    "app_version": clean_version,
                    "os_name": clean_os_name,
                    "architecture": clean_architecture,
                    "capabilities": clean_capabilities,
                    "last_user_id": str(candidate["id"]),
                },
                actor_user_id=str(candidate["id"]),
            )
            device = dict(registration["device"])
            issued = self.catalog.rotate_hub_device_access_token(
                str(candidate["id"]),
                str(device["id"]),
                label=str(device.get("name") or clean_device_name),
                actor_user_id=str(candidate["id"]),
            )
        except (CatalogError, KeyError, TypeError, ValueError):
            self._record_device_enrollment_failure(key)
            raise _HubHTTPError(
                HTTPStatus.UNAUTHORIZED,
                "enrollment_failed",
                "account or password is incorrect",
            ) from None
        with self._state_lock:
            self._device_enroll_failures.pop(key, None)
        return {
            "token": str(issued["token"]),
            "token_id": str(issued["id"]),
            "device_id": str(device["id"]),
            "device_name": str(device.get("name") or clean_device_name),
            "device": device,
            "user": {
                "id": str(candidate["id"]),
                "username": str(candidate["username"]),
                "display_name": str(candidate.get("display_name") or ""),
                "role": str(candidate.get("role") or "producer"),
            },
        }

    def health(self) -> dict[str, Any]:
        summary = self.catalog.bootstrap_summary()
        return {
            "ok": True,
            "service": "storyforge-hub",
            "app_version": __version__,
            "protocol_version": HUB_PROTOCOL_VERSION,
            "minimum_client_protocol_version": HUB_MINIMUM_CLIENT_PROTOCOL_VERSION,
            "schema_version": summary["schema_version"],
            "site": summary["site"],
            "time": _utc_now(),
        }

    def _actor_access(self, actor_user_id: str | None) -> _ActorAccess:
        if not actor_user_id:
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "this operation requires a mapped software user",
            )
        try:
            value = self.catalog.get_effective_permissions(actor_user_id)
        except CatalogError:
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "the mapped software user is unavailable",
            ) from None
        if not bool(value.get("active")):
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "the mapped software user is inactive",
            )
        effective = value.get("effective")
        if not isinstance(effective, Mapping):
            raise _HubHTTPError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "invalid_permission_state",
                "software user permissions are unavailable",
            )
        return _ActorAccess(
            actor_user_id,
            frozenset(
                str(permission)
                for permission, allowed in effective.items()
                if bool(allowed)
            ),
        )

    @staticmethod
    def _require_any_permission(
        access: _ActorAccess, permissions: Sequence[str]
    ) -> None:
        if not any(permission in access.permissions for permission in permissions):
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "one of these permissions is required: " + ", ".join(permissions),
            )

    def authorize_file_access(
        self, actor_user_id: str | None, *, write: bool
    ) -> None:
        access = self._actor_access(actor_user_id)
        permissions = (
            ("drafts.create", "hub.manage")
            if write
            else ("records.view_own", "records.view_all", "hub.manage")
        )
        self._require_any_permission(access, permissions)

    def authorize_update_access(self, actor_user_id: str | None) -> None:
        # Every active software user may receive a release. Publishing remains
        # a local host-only API operation and is never exposed through RPC.
        self._actor_access(actor_user_id)

    def published_update_manifest(self) -> dict[str, Any] | None:
        if self.update_repository is None:
            return None
        return self.update_repository.get_manifest()

    def resolve_update_package(
        self, version: str
    ) -> tuple[Path, dict[str, Any]]:
        manifest = self.published_update_manifest()
        if manifest is None:
            raise _HubHTTPError(
                HTTPStatus.NOT_FOUND,
                "update_not_found",
                "no update is currently published",
            )
        if str(version or "").strip() != manifest["version"]:
            raise _HubHTTPError(
                HTTPStatus.NOT_FOUND,
                "update_not_found",
                "requested update version is not published",
            )
        assert self.update_repository is not None
        try:
            package = self.update_repository.resolve_package(manifest)
            if package.stat().st_size != manifest["size_bytes"]:
                raise ValueError("published update size changed")
        except (OSError, ValueError) as error:
            self._record_error(error)
            raise _HubHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "update_unavailable",
                "published update package failed integrity verification",
            ) from None
        return package, manifest

    def published_component_catalog(self) -> dict[str, Any]:
        if self.component_repository is None:
            return validate_component_catalog(None)
        return self.component_repository.get_catalog()

    def resolve_component_package(
        self,
        component_id: str,
        version: str,
    ) -> tuple[Path, dict[str, Any]]:
        try:
            requested_id = str(component_id or "").strip().casefold()
            requested_version = str(version or "").strip()
            publication = next(
                (
                    item
                    for item in self.published_component_catalog()["components"]
                    if item["component_id"] == requested_id
                    and item["version"] == requested_version
                ),
                None,
            )
        except (KeyError, TypeError, ValueError):
            publication = None
        if publication is None:
            raise _HubHTTPError(
                HTTPStatus.NOT_FOUND,
                "component_not_found",
                "requested component release is not published",
            )
        if self.component_repository is None:
            raise _HubHTTPError(
                HTTPStatus.NOT_FOUND,
                "component_not_found",
                "no component repository is configured",
            )
        try:
            package = self.component_repository.resolve_package(publication)
            if package.stat().st_size != publication["size_bytes"]:
                raise ValueError("published component size changed")
        except (OSError, ValueError, ComponentPackageError) as error:
            self._record_error(error)
            raise _HubHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "component_unavailable",
                "published component package failed integrity verification",
            ) from None
        return package, publication

    def _authorize_rpc(
        self,
        method: str,
        arguments: Mapping[str, Any],
        actor_user_id: str | None,
    ) -> _ActorAccess:
        if not actor_user_id:
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "anonymous Hub tokens cannot call this RPC method",
            )
        access = self._actor_access(actor_user_id)
        if method == "bootstrap_summary":
            return access
        if method == "get_effective_permissions":
            target_user_id = str(arguments.get("user_id") or "")
            if target_user_id != access.user_id:
                self._require_any_permission(
                    access, ("permissions.manage", "users.manage")
                )
            return access
        required = _RPC_PERMISSION_ANY.get(method)
        if not required:
            raise _HubHTTPError(
                HTTPStatus.NOT_IMPLEMENTED,
                "permission_rule_unavailable",
                "this RPC method has no Hub permission rule",
            )
        self._require_any_permission(access, required)
        draft_value = arguments.get("value") if method == "save_draft" else None
        if method == "save_draft" and not (
            isinstance(draft_value, Mapping) and draft_value.get("id")
        ):
            self._require_any_permission(
                access, ("promo_codes.use", "promo_codes.manage")
            )
        return access

    @staticmethod
    def _page_value(
        value: Any, label: str, *, minimum: int, maximum: int
    ) -> int:
        if isinstance(value, bool):
            raise CatalogValidationError(f"{label} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as error:
            raise CatalogValidationError(f"{label} must be an integer") from error
        if not minimum <= parsed <= maximum:
            raise CatalogValidationError(
                f"{label} must be between {minimum} and {maximum}"
            )
        return parsed

    @staticmethod
    def _can_manage_all_drafts(access: _ActorAccess) -> bool:
        return "drafts.manage_all" in access.permissions

    @staticmethod
    def _can_manage_all_records(access: _ActorAccess) -> bool:
        # records.view_all is deliberately not a write capability.
        return bool({"drafts.manage_all", "hub.manage"} & access.permissions)

    def _require_own_draft(
        self, draft_id: str, access: _ActorAccess
    ) -> dict[str, Any]:
        draft = self.catalog.get_draft(draft_id)
        if (
            not self._can_manage_all_drafts(access)
            and draft.get("created_by_user_id") != access.user_id
        ):
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "this draft belongs to another software user",
            )
        return draft

    def _require_own_record(
        self, record_id: str, access: _ActorAccess
    ) -> dict[str, Any]:
        record = self.catalog.get_record(record_id)
        if (
            not self._can_manage_all_records(access)
            and record.get("created_by_user_id") != access.user_id
        ):
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "this production record belongs to another software user",
            )
        return record

    def _require_own_batch(
        self, batch_id: str, access: _ActorAccess
    ) -> list[dict[str, Any]]:
        """Authorize a complete durable batch, never a filtered subset."""

        normalized = str(batch_id or "").strip()
        if not normalized:
            raise _HubHTTPError(
                HTTPStatus.BAD_REQUEST, "invalid_request", "batch_id is required"
            )
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.catalog.list_records(
                batch_id=normalized,
                trashed=None,
                limit=500,
                offset=offset,
            )
            items = [dict(item) for item in page.get("items") or []]
            records.extend(items)
            offset += len(items)
            if offset >= int(page.get("total") or 0) or not items:
                break
        records = [item for item in records if str(item.get("job_id") or "")]
        if not records:
            raise _HubHTTPError(
                HTTPStatus.NOT_FOUND,
                "batch_not_found",
                "production batch was not found",
            )
        if not self._can_manage_all_records(access) and any(
            str(item.get("created_by_user_id") or "") != access.user_id
            for item in records
        ):
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "this production batch contains another software user's tasks",
            )
        return records

    def _prepare_write_arguments(
        self,
        method: str,
        arguments: dict[str, Any],
        access: _ActorAccess,
    ) -> None:
        if method == "save_production_records_bulk":
            raw_values = arguments.get("values") or []
            if not isinstance(raw_values, list) or len(raw_values) > 500:
                raise _HubHTTPError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "bulk production records must be a list of at most 500 items",
                )
            prepared: list[dict[str, Any]] = []
            for raw_item in raw_values:
                if not isinstance(raw_item, Mapping):
                    raise _HubHTTPError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_request",
                        "bulk production records must be objects",
                    )
                item = dict(raw_item)
                item.pop("created_by_user_id", None)
                if item.get("id"):
                    self._require_own_record(str(item["id"]), access)
                elif item.get("draft_id"):
                    self._require_own_draft(str(item["draft_id"]), access)
                prepared.append(item)
            arguments["values"] = prepared
            return
        raw_value = arguments.get("value")
        if not isinstance(raw_value, Mapping):
            return
        value = dict(raw_value)
        arguments["value"] = value
        if method in {"save_draft", "save_production_record"}:
            # Catalog payloads support trusted local imports of historical
            # ownership.  Remote callers must never be able to use that escape
            # hatch: ownership always comes from the bearer token actor.
            value.pop("created_by_user_id", None)
        if method == "save_draft" and value.get("id"):
            self._require_own_draft(str(value["id"]), access)
        elif method == "save_production_record" and value.get("id"):
            self._require_own_record(str(value["id"]), access)
        elif method == "save_production_record" and value.get("draft_id"):
            self._require_own_draft(str(value["draft_id"]), access)
        elif method in {"add_artifact", "record_media_usage"} and value.get(
            "record_id"
        ):
            self._require_own_record(str(value["record_id"]), access)

    def _list_own_drafts(
        self,
        callback: Any,
        arguments: Mapping[str, Any],
        access: _ActorAccess,
    ) -> dict[str, Any]:
        query = dict(arguments)
        query.pop("created_by_user_id", None)
        requested_limit = self._page_value(
            query.pop("limit", 100), "limit", minimum=1, maximum=500
        )
        requested_offset = self._page_value(
            query.pop("offset", 0), "offset", minimum=0, maximum=10_000_000
        )
        own_items: list[dict[str, Any]] = []
        source_offset = 0
        while True:
            page = callback(**query, limit=500, offset=source_offset)
            raw_items = page.get("items")
            if not isinstance(raw_items, list):
                raise _HubHTTPError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "invalid_catalog_result",
                    "catalog draft listing did not return an item array",
                )
            own_items.extend(
                item
                for item in raw_items
                if isinstance(item, dict)
                and item.get("created_by_user_id") == access.user_id
            )
            source_offset += len(raw_items)
            try:
                source_total = int(page.get("total", source_offset))
            except (TypeError, ValueError):
                source_total = source_offset
            if not raw_items or source_offset >= source_total:
                break
        return {
            "items": own_items[
                requested_offset : requested_offset + requested_limit
            ],
            "total": len(own_items),
            "limit": requested_limit,
            "offset": requested_offset,
        }

    def _dispatch_text_polish(
        self,
        params: Mapping[str, Any],
        actor_user_id: str | None,
    ) -> dict[str, Any]:
        """Run text-only AI on the Hub without exposing host configuration."""

        access = self._actor_access(actor_user_id)
        self._require_any_permission(access, ("text.assist", "library.edit"))
        if set(params) != {"request"}:
            raise _HubHTTPError(
                HTTPStatus.BAD_REQUEST,
                "invalid_params",
                "text_polish accepts exactly one request object",
            )
        try:
            request = _text_request_from_rpc(params["request"])
        except (TypeError, ValueError) as error:
            raise _HubHTTPError(
                HTTPStatus.BAD_REQUEST,
                "invalid_text_request",
                str(error),
            ) from None
        if self._text_provider_config_getter is None:
            raise _HubHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "text_service_unavailable",
                "Hub text service is not configured",
            )
        if not self._text_polish_slots.acquire(
            timeout=self._text_polish_queue_timeout_seconds
        ):
            raise _HubHTTPError(
                HTTPStatus.TOO_MANY_REQUESTS,
                "text_service_busy",
                "Hub text service is busy; try again shortly",
            )
        try:
            config = self._text_provider_config_getter()
            provider = self._text_provider_factory(config)
            result = provider.polish(request)
            if not isinstance(result, TextResult):
                raise TypeError("text provider returned an invalid result")
            # TextResult.to_dict is an explicit safe projection. It contains no
            # endpoint, key, command, provider options, or raw response body.
            safe_result = result.to_dict()
            if set(safe_result) - _TEXT_RESULT_FIELDS:
                raise TypeError("text provider result contains unsupported fields")
            return safe_result
        except _HubHTTPError:
            raise
        except (ProviderError, OSError, RuntimeError, TypeError, ValueError) as error:
            self._record_error(error)
            raise _HubHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "text_provider_failed",
                "Hub text provider could not complete the request",
            ) from None
        finally:
            self._text_polish_slots.release()

    @staticmethod
    def _require_exact_rpc_params(
        params: Mapping[str, Any], allowed: frozenset[str]
    ) -> None:
        unsupported = sorted(set(params) - allowed)
        if unsupported:
            raise _HubHTTPError(
                HTTPStatus.BAD_REQUEST,
                "invalid_params",
                "unsupported parameters: " + ", ".join(unsupported),
            )

    def _dispatch_device_service(
        self,
        method: str,
        params: Mapping[str, Any],
        authentication: _Authentication,
    ) -> dict[str, Any]:
        """Dispatch the small typed fleet protocol.

        Workstations never submit a device id.  The bound bearer identity is
        the sole source of that value, preventing one client from reading or
        acknowledging another workstation's desired configuration.
        """

        access = self._actor_access(authentication.actor_user_id)
        if method in DEVICE_ADMIN_RPC_METHODS:
            self._require_any_permission(access, ("hub.manage",))
            if method == "devices_list":
                self._require_exact_rpc_params(
                    params,
                    frozenset(
                        {
                            "active",
                            "online",
                            "offline_after_seconds",
                            "limit",
                            "offset",
                        }
                    ),
                )
                return self.catalog.list_hub_devices(**dict(params))
            if method == "device_get":
                self._require_exact_rpc_params(
                    params, frozenset({"device_id", "offline_after_seconds"})
                )
                return self.catalog.get_hub_device(**dict(params))
            if method == "device_acknowledge":
                self._require_exact_rpc_params(
                    params, frozenset({"device_id"})
                )
                return self.catalog.acknowledge_hub_device(
                    str(params.get("device_id") or ""),
                    actor_user_id=access.user_id,
                )
            if method == "device_rename":
                self._require_exact_rpc_params(
                    params, frozenset({"device_id", "name"})
                )
                return self.catalog.rename_hub_device(
                    str(params.get("device_id") or ""),
                    str(params.get("name") or ""),
                    actor_user_id=access.user_id,
                )
            if method == "device_set_active":
                self._require_exact_rpc_params(
                    params,
                    frozenset({"device_id", "active", "revoke_tokens"}),
                )
                arguments = dict(params)
                device_id = str(arguments.pop("device_id", ""))
                return self.catalog.set_hub_device_active(
                    device_id,
                    actor_user_id=access.user_id,
                    **arguments,
                )
            if method == "device_delete":
                self._require_exact_rpc_params(
                    params,
                    frozenset({"device_id"}),
                )
                return self.catalog.delete_hub_device(
                    str(params.get("device_id") or ""),
                    actor_user_id=access.user_id,
                )
            if method == "device_config_create":
                self._require_exact_rpc_params(params, frozenset({"value"}))
                return self.catalog.create_device_config_revision(
                    params.get("value"), actor_user_id=access.user_id
                )
            if method == "device_config_list":
                self._require_exact_rpc_params(
                    params, frozenset({"limit", "offset"})
                )
                return self.catalog.list_device_config_revisions(**dict(params))
            if method == "device_config_get":
                self._require_exact_rpc_params(params, frozenset({"revision_id"}))
                return self.catalog.get_device_config_revision(
                    str(params.get("revision_id") or "")
                )

        device_id = str(authentication.device_id or "")
        if not device_id:
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "device_identity_required",
                "this operation requires a device-bound token",
            )
        if method == LOCAL_WORKER_TICKET_RPC_METHOD:
            return self._redeem_local_worker_ticket(params, authentication, access)
        if method == ACCOUNT_PASSWORD_VERIFY_RPC_METHOD:
            self._require_exact_rpc_params(
                params, frozenset({"username", "password"})
            )
            username = str(params.get("username") or "").strip()
            password = str(params.get("password") or "")
            candidate = self.catalog._web_user_by_username(username) if username else None
            verifier = str((candidate or {}).get("password_hash") or "")
            matched = password_matches(
                password, verifier or DUMMY_PASSWORD_VERIFIER
            )
            if (
                not matched
                or not candidate
                or not bool(candidate.get("active"))
                or str(candidate.get("id") or "") != access.user_id
            ):
                time.sleep(0.12)
                raise _HubHTTPError(
                    HTTPStatus.UNAUTHORIZED,
                    "login_failed",
                    "account or password is incorrect",
                )
            device = self.catalog.heartbeat_hub_device(
                device_id,
                user_id=access.user_id,
                app_version="",
            )["device"]
            return {
                "user": {
                    "id": str(candidate["id"]),
                    "username": str(candidate["username"]),
                    "display_name": str(candidate.get("display_name") or ""),
                    "role": str(candidate.get("role") or "producer"),
                    "active": True,
                    "row_version": int(candidate.get("row_version") or 1),
                },
                "device": device,
                "permissions": sorted(access.permissions),
            }
        if method == "device_session":
            self._require_exact_rpc_params(params, frozenset())
            user = self.catalog._web_user_by_id(access.user_id)
            if not user or not bool(user.get("active")):
                raise _HubHTTPError(
                    HTTPStatus.UNAUTHORIZED,
                    "device_session_revoked",
                    "the bound software account is unavailable",
                )
            device = self.catalog.get_hub_device(device_id)
            if not bool(device.get("active")):
                raise _HubHTTPError(
                    HTTPStatus.UNAUTHORIZED,
                    "device_session_revoked",
                    "the bound workstation is unavailable",
                )
            return {
                "user": {
                    "id": str(user["id"]),
                    "username": str(user["username"]),
                    "display_name": str(user.get("display_name") or ""),
                    "role": str(user.get("role") or "producer"),
                    "active": True,
                    "row_version": int(user.get("row_version") or 1),
                },
                "device": {
                    "id": str(device["id"]),
                    "name": str(device.get("name") or ""),
                    "active": True,
                },
                "permissions": sorted(access.permissions),
            }
        if method == "device_heartbeat":
            self._require_exact_rpc_params(
                params, frozenset({"app_version", "capabilities"})
            )
            heartbeat_capabilities = (
                _device_capabilities(params.get("capabilities"))
                if "capabilities" in params
                else None
            )
            return self.catalog.heartbeat_hub_device(
                device_id,
                user_id=access.user_id,
                app_version=str(params.get("app_version") or ""),
                capabilities=heartbeat_capabilities,
            )
        if method == "device_desired_config":
            self._require_exact_rpc_params(
                params, frozenset({"current_revision_id"})
            )
            return self.catalog.get_device_desired_config(
                device_id,
                current_revision_id=str(params.get("current_revision_id") or ""),
            )
        if method == "device_config_ack":
            self._require_exact_rpc_params(
                params,
                frozenset(
                    {
                        "revision_id",
                        "status",
                        "message",
                        "reported_config_hash",
                    }
                ),
            )
            arguments = dict(params)
            revision_id = str(arguments.pop("revision_id", ""))
            return self.catalog.ack_device_config(
                device_id,
                revision_id,
                actor_user_id=access.user_id,
                **arguments,
            )
        raise _HubHTTPError(
            HTTPStatus.NOT_FOUND,
            "method_not_allowed",
            "RPC method is not exposed by this Hub",
        )

    def dispatch_rpc(
        self,
        method: str,
        params: Mapping[str, Any],
        authentication: _Authentication | str | None,
    ) -> dict[str, Any]:
        if isinstance(authentication, _Authentication):
            auth = authentication
        else:
            # Compatibility for internal callers of the pre-device service
            # signature. HTTP requests always pass the full context above.
            auth = _Authentication(bool(authentication), authentication)
        actor_user_id = auth.actor_user_id
        if method == TEXT_POLISH_RPC_METHOD:
            return self._dispatch_text_polish(params, actor_user_id)
        if method in DEVICE_SERVICE_RPC_METHODS:
            return self._dispatch_device_service(method, params, auth)
        if method not in self.rpc_methods:
            raise _HubHTTPError(
                HTTPStatus.NOT_FOUND,
                "method_not_allowed",
                "RPC method is not exposed by this Hub",
            )
        callback = getattr(self.catalog, method, None)
        if not callable(callback):
            raise _HubHTTPError(
                HTTPStatus.NOT_IMPLEMENTED,
                "method_unavailable",
                "RPC method is unavailable in this catalog version",
            )
        arguments = dict(params)
        access = self._authorize_rpc(method, arguments, actor_user_id)
        atomic_retry_claim = (
            method == "begin_record_retry"
            and bool(str(arguments.get("device_id") or "").strip())
        )
        if method in _DEVICE_BOUND_LEASE_RPC_METHODS or atomic_retry_claim:
            if auth.device_id:
                supplied_device = str(arguments.get("device_id") or "").strip()
                if supplied_device and supplied_device != auth.device_id:
                    raise _HubHTTPError(
                        HTTPStatus.FORBIDDEN,
                        "device_identity_mismatch",
                        "lease device does not match the authenticated workstation",
                    )
                # Never trust a workstation-supplied owner. The bearer token is
                # the authoritative device identity for the lease lifecycle.
                arguments["device_id"] = auth.device_id
            elif "hub.manage" not in access.permissions:
                raise _HubHTTPError(
                    HTTPStatus.FORBIDDEN,
                    "device_identity_required",
                    "production leases require an enrolled workstation",
                )
        if (method == "claim_record_lease" or atomic_retry_claim) and auth.device_id:
            # Only gate new work. Older clients may still heartbeat or release
            # an already-running lease, so updating never strands a job.
            device = self.catalog.get_hub_device(auth.device_id)
            client_version = str(device.get("app_version") or "").strip()
            if not _render_client_version_is_supported(client_version):
                shown_version = client_version or "未知版本"
                raise _HubHTTPError(
                    HTTPStatus.UPGRADE_REQUIRED,
                    "client_update_required",
                    "版本过旧，请更新：当前制作电脑为 "
                    f"{shown_version}，请先升级 StoryForge 至 "
                    f"{MINIMUM_RENDER_CLIENT_VERSION} 或更高版本后再开始制作。",
                )
        if method == "list_production_presets":
            # Listing is actor-scoped too: a caller may not forge another
            # user id to enumerate that employee's personal presets.
            arguments.pop("actor_user_id", None)
            arguments["actor_user_id"] = access.user_id
        if method == "get_archived_batch":
            self._require_own_batch(str(arguments.get("batch_id") or ""), access)
        if method == "list_records":
            if "records.view_all" not in access.permissions:
                arguments["created_by_user_id"] = access.user_id
        elif method == "list_record_groups":
            if "records.view_all" not in access.permissions:
                arguments["created_by_user_id"] = access.user_id
        elif method == "get_production_batch_summaries":
            if "records.view_all" not in access.permissions:
                arguments["created_by_user_id"] = access.user_id
        elif method in {"find_active_draft_gate", "list_reconciliation_records"}:
            if "hub.manage" not in access.permissions:
                arguments["created_by_user_id"] = access.user_id
        elif method == "list_archived_jobs":
            if "records.view_all" not in access.permissions:
                arguments["created_by_user_id"] = access.user_id
        elif method == "list_drafts":
            # This is a Hub-level filter; CatalogRepository intentionally has
            # no public created_by filter for local administrators.
            arguments.pop("created_by_user_id", None)
        if method in CATALOG_WRITE_METHODS:
            # An HTTP client must never be able to forge audit identity. Tokens
            # are mapped to active users before this point.
            arguments.pop("actor_user_id", None)
            if method in _RECORD_LEASE_RPC_METHODS and arguments.get("record_id"):
                self._require_own_record(str(arguments["record_id"]), access)
            if method in _JOB_ARCHIVE_RPC_METHODS and arguments.get("job_id"):
                record = self.catalog.get_record_by_job_id(str(arguments["job_id"]))
                self._require_own_record(str(record["id"]), access)
            if method in _BATCH_ARCHIVE_RPC_METHODS:
                self._require_own_batch(str(arguments.get("batch_id") or ""), access)
            if method in {"begin_record_retry"} and arguments.get("record_id"):
                self._require_own_record(str(arguments["record_id"]), access)
            if method == "request_record_cancellation":
                for record_id in arguments.get("record_ids") or []:
                    self._require_own_record(str(record_id), access)
            self._prepare_write_arguments(method, arguments, access)
            if method == "save_production_record" and auth.device_id:
                value = arguments.get("value")
                if isinstance(value, dict):
                    supplied_device = str(value.get("device_id") or "").strip()
                    if supplied_device and supplied_device != auth.device_id:
                        raise _HubHTTPError(
                            HTTPStatus.FORBIDDEN,
                            "device_identity_mismatch",
                            "record device does not match the authenticated workstation",
                        )
                    value["device_id"] = auth.device_id
                    if value.get("id"):
                        # Updating an existing render record is a lease-owned
                        # operation.  Do not let a second workstation enrolled
                        # to the same member omit this optional catalog guard
                        # and overwrite the first workstation's live result.
                        value["expected_lease_owner_device"] = auth.device_id
            elif method == "save_production_records_bulk" and auth.device_id:
                for value in arguments.get("values") or []:
                    if not isinstance(value, dict):
                        continue
                    supplied_device = str(value.get("device_id") or "").strip()
                    if supplied_device and supplied_device != auth.device_id:
                        raise _HubHTTPError(
                            HTTPStatus.FORBIDDEN,
                            "device_identity_mismatch",
                            "record device does not match the authenticated workstation",
                        )
                    value["device_id"] = auth.device_id
                    if value.get("id"):
                        value["expected_lease_owner_device"] = auth.device_id
            arguments["actor_user_id"] = access.user_id

        if (
            method == "list_drafts"
            and not self._can_manage_all_drafts(access)
        ):
            result = self._list_own_drafts(callback, arguments, access)
        else:
            result = callback(**arguments)
        if not isinstance(result, dict):
            raise _HubHTTPError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "invalid_catalog_result",
                "catalog method did not return a JSON object",
            )
        if method == "get_draft":
            if (
                not self._can_manage_all_drafts(access)
                and result.get("created_by_user_id") != access.user_id
            ):
                raise _HubHTTPError(
                    HTTPStatus.FORBIDDEN,
                    "forbidden",
                    "this draft belongs to another software user",
                )
        elif method in {"get_record", "get_record_by_job_id"}:
            if (
                "records.view_all" not in access.permissions
                and result.get("created_by_user_id") != access.user_id
            ):
                raise _HubHTTPError(
                    HTTPStatus.FORBIDDEN,
                    "forbidden",
                    "this production record belongs to another software user",
                )
        elif method == "get_archived_job":
            if (
                "records.view_all" not in access.permissions
                and result.get("created_by_user_id") != access.user_id
            ):
                raise _HubHTTPError(
                    HTTPStatus.FORBIDDEN,
                    "forbidden",
                    "this archived job belongs to another software user",
                )
        return result

    def prepare_file_upload(
        self,
        root_alias: str,
        relative_path: str,
        size_bytes: Any,
        sha256: Any,
    ) -> dict[str, Any]:
        raw_size = str(size_bytes).strip()
        if not raw_size.isdecimal():
            raise _HubHTTPError(
                HTTPStatus.LENGTH_REQUIRED,
                "length_required",
                "upload size must be a non-negative integer",
            )
        parsed_size = int(raw_size)
        if parsed_size <= 0:
            raise _HubHTTPError(
                HTTPStatus.BAD_REQUEST, "empty_upload", "upload body is empty"
            )
        if parsed_size > self.max_upload_bytes:
            raise _HubHTTPError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "upload_too_large",
                "upload exceeds the configured limit",
            )
        digest = str(sha256 or "").strip().casefold()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise _HubHTTPError(
                HTTPStatus.BAD_REQUEST,
                "invalid_sha256",
                f"{UPLOAD_SHA256_HEADER} must contain a 64-character hexadecimal digest",
            )
        alias = str(root_alias).strip()
        destination = self.resolve_upload(alias, relative_path)
        root = self.download_roots[alias]
        return {
            "root": alias,
            "path": destination.relative_to(root).as_posix(),
            "size_bytes": parsed_size,
            "sha256": digest,
            "content_type": _file_content_type(destination),
            "replaced": destination.exists(),
        }

    def resolve_download(self, root_alias: str, relative_path: str) -> Path:
        root = self.download_roots.get(str(root_alias).strip())
        if root is None:
            raise _HubHTTPError(
                HTTPStatus.NOT_FOUND, "download_root_not_found", "download root not found"
            )
        relative = str(relative_path or "")
        if not relative or len(relative) > 4096 or "\x00" in relative:
            raise _HubHTTPError(
                HTTPStatus.BAD_REQUEST, "invalid_file_path", "invalid relative file path"
            )
        try:
            candidate = (root / relative).resolve(strict=False)
            candidate.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "path_outside_root",
                "requested file is outside the configured download root",
            ) from None
        if not candidate.exists():
            raise _HubHTTPError(
                HTTPStatus.NOT_FOUND, "file_not_found", "download file not found"
            )
        try:
            candidate = candidate.resolve(strict=True)
            candidate.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "path_outside_root",
                "requested file or link target is outside the configured root",
            ) from None
        if not candidate.is_file():
            raise _HubHTTPError(
                HTTPStatus.NOT_FOUND, "file_not_found", "download file not found"
            )
        if candidate.suffix.casefold() not in self.allowed_download_extensions:
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "file_type_not_allowed",
                "this file type is not available for download",
            )
        return candidate

    @staticmethod
    def _download_file_fingerprint(stat: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            int(getattr(stat, "st_dev", 0)),
            int(getattr(stat, "st_ino", 0)),
            int(stat.st_size),
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            int(getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000))),
        )

    def download_file_metadata(self, file_path: Path) -> dict[str, Any]:
        """Return stable size/hash metadata, hashing only when the file changes."""

        resolved = file_path.resolve(strict=True)
        cache_key = str(resolved)
        for _attempt in range(3):
            before = resolved.stat()
            fingerprint = self._download_file_fingerprint(before)
            with self._state_lock:
                cached = self._download_metadata_cache.get(cache_key)
                if cached is not None and cached[0] == fingerprint:
                    return dict(cached[1])

            digest = hashlib.sha256()
            total = 0
            with resolved.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    total += len(chunk)
                    digest.update(chunk)
            after = resolved.stat()
            if (
                fingerprint != self._download_file_fingerprint(after)
                or total != int(after.st_size)
            ):
                continue
            sha256 = digest.hexdigest()
            metadata = {
                "size_bytes": total,
                "sha256": sha256,
                "etag": f'"sha256-{sha256}"',
            }
            with self._state_lock:
                if len(self._download_metadata_cache) >= 4096:
                    for stale_key in tuple(self._download_metadata_cache)[:1024]:
                        self._download_metadata_cache.pop(stale_key, None)
                self._download_metadata_cache[cache_key] = (fingerprint, metadata)
            return dict(metadata)
        raise _HubHTTPError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "file_changed_during_read",
            "download file changed while its metadata was being prepared",
        )

    def resolve_upload(self, root_alias: str, relative_path: str) -> Path:
        """Return a safe upload destination and create its parent directories.

        Resolution is deliberately separate from receiving bytes so invalid
        roots, extensions and link escapes are rejected before a temporary file
        is created.  The handler calls this method again immediately before
        ``os.replace`` to narrow the race window for long uploads.
        """

        root = self.download_roots.get(str(root_alias).strip())
        if root is None:
            raise _HubHTTPError(
                HTTPStatus.NOT_FOUND,
                "download_root_not_found",
                "upload root not found",
            )
        relative = str(relative_path or "")
        if not relative or len(relative) > 4096 or "\x00" in relative:
            raise _HubHTTPError(
                HTTPStatus.BAD_REQUEST, "invalid_file_path", "invalid relative file path"
            )
        requested = root / relative
        requested_is_link = requested.is_symlink()
        try:
            candidate = requested.resolve(strict=False)
            candidate.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "path_outside_root",
                "upload destination is outside the configured root",
            ) from None
        if candidate.suffix.casefold() not in self.allowed_download_extensions:
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "file_type_not_allowed",
                "this file type is not available for upload",
            )
        # Replacing a symlink by name is safe on common filesystems, but
        # rejecting it is clearer and exactly matches the fail-closed download
        # rule for links whose targets leave the configured root.
        if requested_is_link:
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "path_outside_root",
                "upload destinations cannot be symbolic links",
            )
        if candidate.exists() and not candidate.is_file():
            raise _HubHTTPError(
                HTTPStatus.CONFLICT,
                "upload_target_invalid",
                "upload destination is not a regular file",
            )
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            safe_parent = candidate.parent.resolve(strict=True)
            safe_parent.relative_to(root)
            candidate = safe_parent / candidate.name
            if candidate.is_symlink():
                raise ValueError("upload destination became a symbolic link")
            if candidate.exists():
                resolved_existing = candidate.resolve(strict=True)
                resolved_existing.relative_to(root)
                if not resolved_existing.is_file():
                    raise _HubHTTPError(
                        HTTPStatus.CONFLICT,
                        "upload_target_invalid",
                        "upload destination is not a regular file",
                    )
                candidate = resolved_existing
        except _HubHTTPError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise _HubHTTPError(
                HTTPStatus.FORBIDDEN,
                "path_outside_root",
                "upload destination or link target is outside the configured root",
            ) from None
        return candidate


class HubClient:
    """Small standard-library client for :class:`HubServer`."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 30.0,
        text_timeout_seconds: float = 120.0,
        max_json_response_bytes: int = 32 * 1024 * 1024,
        authentication_failure_callback: (
            Callable[[HubAuthenticationError], None] | None
        ) = None,
    ) -> None:
        parsed = urlsplit(str(base_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain a query or fragment")
        self.base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        self.token = _clean_token(token)
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.text_timeout_seconds = float(text_timeout_seconds)
        if self.text_timeout_seconds <= 0:
            raise ValueError("text_timeout_seconds must be positive")
        self.max_json_response_bytes = int(max_json_response_bytes)
        if self.max_json_response_bytes < 1024:
            raise ValueError("max_json_response_bytes must be at least 1024")
        self.authentication_failure_callback = authentication_failure_callback

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        content_type: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        authenticated: bool = True,
        timeout_seconds: float | None = None,
    ) -> Any:
        headers = {
            "Accept": "application/json, application/octet-stream;q=0.9",
            "Connection": "close",
        }
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type:
            headers["Content-Type"] = content_type
        if extra_headers:
            headers.update({str(key): str(value) for key, value in extra_headers.items()})
        request = Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            request_timeout = (
                self.timeout_seconds
                if timeout_seconds is None
                else float(timeout_seconds)
            )
            if request_timeout <= 0:
                raise ValueError("timeout_seconds must be positive")
            return urlopen(request, timeout=request_timeout)
        except HTTPError as error:
            try:
                raw = error.read(self.max_json_response_bytes + 1)
            finally:
                error.close()
            code = "http_error"
            message = error.reason or "Hub request failed"
            request_id: str | int | None = None
            if len(raw) <= self.max_json_response_bytes:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    if isinstance(payload, Mapping):
                        request_id = payload.get("id")  # type: ignore[assignment]
                        error_value = payload.get("error")
                        if isinstance(error_value, Mapping):
                            code = str(error_value.get("code") or code)
                            message = str(error_value.get("message") or message)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            exception_type = (
                HubAuthenticationError
                if int(error.code) == HTTPStatus.UNAUTHORIZED
                else HubRemoteError
            )
            remote_error = exception_type(
                int(error.code), code, str(message), request_id=request_id
            )
            # Only an explicitly invalid device credential disconnects the
            # workstation runtime.  Other HTTP 401 responses can describe a
            # bad account password or an expired one-time worker ticket while
            # the installed device token remains perfectly valid.
            if (
                isinstance(remote_error, HubAuthenticationError)
                and code in {"unauthorized", "device_session_revoked"}
                and self.authentication_failure_callback is not None
            ):
                try:
                    self.authentication_failure_callback(remote_error)
                except Exception:
                    # Connection-state reporting must never replace the
                    # authoritative Hub exception seen by the caller.
                    pass
            raise remote_error from None
        except (URLError, TimeoutError, OSError) as error:
            raise HubConnectionError(f"could not reach StoryForge Hub: {error}") from error

    def _read_json_response(self, response: Any) -> dict[str, Any]:
        with response:
            raw = response.read(self.max_json_response_bytes + 1)
            if len(raw) > self.max_json_response_bytes:
                raise HubConnectionError("Hub JSON response exceeds the configured limit")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HubConnectionError("Hub returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise HubConnectionError("Hub JSON response must be an object")
        return payload

    def health(self) -> dict[str, Any]:
        payload = self._read_json_response(self._request("/health"))
        if not payload.get("ok"):
            raise HubConnectionError("Hub health response was not successful")
        return payload

    @classmethod
    def enroll_device(
        cls,
        base_url: str,
        username: str,
        password: str,
        device_name: str,
        *,
        installation_id: str | None = None,
        app_version: str = __version__,
        capabilities: Mapping[str, Any] | None = None,
        hostname: str = "",
        os_name: str = "",
        architecture: str = "",
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        """Enroll one computer without ever persisting the supplied password."""

        clean_username = str(username or "").strip()
        clean_password = str(password or "")
        clean_device_name = str(device_name or "").strip()
        if not clean_username or not clean_password or not clean_device_name:
            raise ValueError("account, password, and computer name are required")
        try:
            clean_installation_id = str(
                UUID(str(installation_id or uuid4()).strip())
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("installation_id must be a UUID") from error
        clean_capabilities = _device_capabilities(
            capabilities
            or {
                "device_config_sync": 1,
                "local_render": True,
                "local_tts": True,
                "local_subtitles": True,
            }
        )
        transport = cls(
            base_url,
            "device-enrollment-transport",
            timeout_seconds=timeout_seconds,
        )
        body = json.dumps(
            {
                "username": clean_username,
                "password": clean_password,
                "device_name": clean_device_name,
                "installation_id": clean_installation_id,
                "app_version": str(app_version or "").strip(),
                "capabilities": clean_capabilities,
                "hostname": str(hostname or "").strip(),
                "os_name": str(os_name or "").strip(),
                "architecture": str(architecture or "").strip(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        payload = transport._read_json_response(
            transport._request(
                "/device-enroll",
                method="POST",
                body=body,
                content_type="application/json",
                authenticated=False,
            )
        )
        if not payload.get("ok") or not isinstance(payload.get("device"), Mapping):
            raise HubConnectionError("Hub device enrollment response was invalid")
        result = dict(payload["device"])
        result["token"] = _clean_token(result.get("token"))
        if not str(result.get("device_id") or "").strip():
            raise HubConnectionError("Hub enrollment did not return a device identity")
        return result

    def verify_identity(self) -> dict[str, Any]:
        """Validate protocol compatibility before activating authenticated RPC."""

        health = self.health()
        if str(health.get("service") or "") != "storyforge-hub":
            raise HubConnectionError("endpoint is not a StoryForge Hub")
        try:
            server_protocol = int(health.get("protocol_version"))
            minimum_client = int(
                health.get("minimum_client_protocol_version")
                or server_protocol
            )
        except (TypeError, ValueError) as error:
            raise HubConnectionError("Hub compatibility response is invalid") from error
        if server_protocol < HUB_MINIMUM_SERVER_PROTOCOL_VERSION:
            raise HubConnectionError(
                "Hub protocol is too old; update the Hub computer before connecting"
            )
        if HUB_PROTOCOL_VERSION < minimum_client:
            raise HubConnectionError(
                "this StoryForge workstation is too old for the Hub; update it before connecting"
            )
        identity = self.call("bootstrap_summary")
        identity["hub_compatibility"] = {
            "server_protocol_version": server_protocol,
            "minimum_client_protocol_version": minimum_client,
            "negotiated_protocol_version": min(
                HUB_PROTOCOL_VERSION, server_protocol
            ),
            "server_app_version": str(health.get("app_version") or ""),
        }
        return identity

    def get_device_session(self) -> dict[str, Any]:
        """Return the safe account/device identity bound to this token.

        The Hub resolves the bearer token for every call, so revoking the
        token, disabling the workstation, disabling the account, or changing
        permissions takes effect before the next client-local browser RPC.
        No token or credential material is returned.
        """

        return self.call("device_session")

    def verify_account_password(
        self, username: str, password: str
    ) -> dict[str, Any]:
        """Verify the bound member before opening the installed desktop UI."""

        clean_username = str(username or "").strip()
        clean_password = str(password or "")
        if not clean_username or not clean_password:
            raise ValueError("account and password are required")
        return self.call(
            ACCOUNT_PASSWORD_VERIFY_RPC_METHOD,
            {"username": clean_username, "password": clean_password},
        )

    def redeem_local_worker_ticket(
        self,
        ticket: str,
        *,
        worker_nonce: str,
        browser_origin: str,
    ) -> dict[str, Any]:
        """Redeem a Hub-browser handoff without exposing this device token."""

        return self.call(
            LOCAL_WORKER_TICKET_RPC_METHOD,
            {
                "ticket": str(ticket or ""),
                "worker_nonce": str(worker_nonce or ""),
                "browser_origin": str(browser_origin or ""),
            },
        )

    # Typed fleet methods intentionally mirror the fixed service RPC surface.
    # There is no generic command, path, process, or file-browser endpoint.
    def heartbeat_device(
        self,
        *,
        app_version: str = "",
        capabilities: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"app_version": str(app_version or "")}
        if capabilities is not None:
            params["capabilities"] = dict(capabilities)
        return self.call("device_heartbeat", params)

    def get_desired_device_config(
        self, *, current_revision_id: str = ""
    ) -> dict[str, Any]:
        return self.call(
            "device_desired_config",
            {"current_revision_id": str(current_revision_id or "")},
        )

    def acknowledge_device_config(
        self,
        revision_id: str,
        *,
        status: str = "applied",
        message: str = "",
        reported_config_hash: str = "",
    ) -> dict[str, Any]:
        return self.call(
            "device_config_ack",
            {
                "revision_id": str(revision_id or ""),
                "status": str(status or ""),
                "message": str(message or ""),
                "reported_config_hash": str(reported_config_hash or ""),
            },
        )

    def get_update_manifest(self) -> dict[str, Any] | None:
        payload = self._read_json_response(self._request("/updates/manifest"))
        if not payload.get("ok"):
            raise HubConnectionError("Hub update response was not successful")
        raw_update = payload.get("update")
        if raw_update is not None and not isinstance(raw_update, Mapping):
            raise HubConnectionError("Hub update manifest must be an object or null")
        try:
            verify_update_manifest_signature(
                self.token,
                raw_update if isinstance(raw_update, Mapping) else None,
                payload.get("signature"),
            )
            return (
                validate_update_manifest(raw_update)
                if isinstance(raw_update, Mapping)
                else None
            )
        except ValueError as error:
            raise HubConnectionError(str(error)) from error

    def download_update_package(
        self,
        manifest: Mapping[str, Any],
        *,
        destination: str | Path,
    ) -> dict[str, Any]:
        """Download the signed manifest's package and atomically verify it."""

        try:
            checked = validate_update_manifest(manifest)
        except ValueError as error:
            raise HubConnectionError(str(error)) from error
        destination_path = Path(destination).expanduser().resolve()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        response = self._request(
            "/updates/package?"
            + urlencode({"version": checked["version"]})
        )
        temporary_path: Path | None = None
        try:
            with response:
                try:
                    response_size = int(response.headers.get("Content-Length") or -1)
                except (TypeError, ValueError):
                    response_size = -1
                if response_size != checked["size_bytes"]:
                    raise HubConnectionError(
                        "Hub update Content-Length does not match the signed manifest"
                    )
                header_digest = str(
                    response.headers.get(UPLOAD_SHA256_HEADER) or ""
                ).strip().casefold()
                if header_digest != checked["sha256"]:
                    raise HubConnectionError(
                        "Hub update digest header does not match the signed manifest"
                    )
                handle, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination_path.name}.",
                    suffix=".part",
                    dir=destination_path.parent,
                )
                temporary_path = Path(temporary_name)
                digest = hashlib.sha256()
                total = 0
                with os.fdopen(handle, "wb") as stream:
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        total += len(chunk)
                        if total > checked["size_bytes"]:
                            raise HubConnectionError(
                                "Hub update body exceeds the signed size"
                            )
                        digest.update(chunk)
                        stream.write(chunk)
            if total != checked["size_bytes"]:
                raise HubConnectionError("Hub update body is incomplete")
            if digest.hexdigest() != checked["sha256"]:
                raise HubConnectionError("Hub update SHA-256 verification failed")
            os.replace(temporary_path, destination_path)
            temporary_path = None
            return {
                "path": str(destination_path),
                "version": checked["version"],
                "size_bytes": total,
                "sha256": checked["sha256"],
            }
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def get_component_manifests(self) -> list[dict[str, Any]]:
        payload = self._read_json_response(self._request("/components/manifest"))
        if not payload.get("ok"):
            raise HubConnectionError("Hub component response was not successful")
        raw_catalog = payload.get("catalog")
        if not isinstance(raw_catalog, Mapping):
            raise HubConnectionError("Hub component catalog must be an object")
        try:
            verify_component_catalog_signature(
                self.token,
                raw_catalog,
                payload.get("signature"),
            )
            catalog = validate_component_catalog(raw_catalog)
        except (ValueError, ComponentPackageError) as error:
            raise HubConnectionError(str(error)) from error
        return [dict(item) for item in catalog["components"]]

    def download_component_package(
        self,
        publication: Mapping[str, Any],
        *,
        destination: str | Path,
    ) -> dict[str, Any]:
        """Download, authenticate and structurally inspect one component ZIP."""

        try:
            checked = validate_component_publication(publication)
        except (ValueError, ComponentPackageError) as error:
            raise HubConnectionError(str(error)) from error
        destination_path = Path(destination).expanduser().resolve()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        response = self._request(
            "/components/package?"
            + urlencode(
                {
                    "component_id": checked["component_id"],
                    "version": checked["version"],
                }
            )
        )
        temporary_path: Path | None = None
        try:
            with response:
                try:
                    response_size = int(response.headers.get("Content-Length") or -1)
                except (TypeError, ValueError):
                    response_size = -1
                if response_size != checked["size_bytes"]:
                    raise HubConnectionError(
                        "Hub component Content-Length does not match the signed catalog"
                    )
                header_digest = str(
                    response.headers.get(UPLOAD_SHA256_HEADER) or ""
                ).strip().casefold()
                if header_digest != checked["sha256"]:
                    raise HubConnectionError(
                        "Hub component digest header does not match the signed catalog"
                    )
                handle, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination_path.name}.",
                    suffix=".part.zip",
                    dir=destination_path.parent,
                )
                temporary_path = Path(temporary_name)
                digest = hashlib.sha256()
                total = 0
                with os.fdopen(handle, "wb") as stream:
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        total += len(chunk)
                        if total > checked["size_bytes"]:
                            raise HubConnectionError(
                                "Hub component body exceeds the signed size"
                            )
                        digest.update(chunk)
                        stream.write(chunk)
            if total != checked["size_bytes"]:
                raise HubConnectionError("Hub component body is incomplete")
            if digest.hexdigest() != checked["sha256"]:
                raise HubConnectionError("Hub component SHA-256 verification failed")
            try:
                inspection = ComponentUpdater.inspect_package(
                    temporary_path,
                    expected_package_sha256=checked["sha256"],
                )
            except ComponentPackageError as error:
                raise HubConnectionError(str(error)) from error
            if inspection.manifest.to_dict() != checked["component_manifest"]:
                raise HubConnectionError(
                    "Downloaded component manifest does not match the signed catalog"
                )
            os.replace(temporary_path, destination_path)
            temporary_path = None
            return {
                "path": str(destination_path),
                "component_id": checked["component_id"],
                "version": checked["version"],
                "size_bytes": total,
                "sha256": checked["sha256"],
            }
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        request_id: str | int | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        method = str(method or "").strip()
        if not method:
            raise ValueError("method cannot be empty")
        if params is not None and not isinstance(params, Mapping):
            raise TypeError("params must be an object")
        rpc_id = request_id if request_id is not None else uuid4().hex
        body = json.dumps(
            {"id": rpc_id, "method": method, "params": dict(params or {})},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        payload = self._read_json_response(
            self._request(
                "/rpc",
                method="POST",
                body=body,
                content_type="application/json",
                timeout_seconds=timeout_seconds,
            )
        )
        if not payload.get("ok"):
            error = payload.get("error") or {}
            raise HubRemoteError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                str(error.get("code") or "rpc_error")
                if isinstance(error, Mapping)
                else "rpc_error",
                str(error.get("message") or "RPC failed")
                if isinstance(error, Mapping)
                else "RPC failed",
                request_id=payload.get("id"),
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise HubConnectionError("Hub RPC result must be an object")
        return result

    def text_polish(self, request: TextRequest) -> TextResult:
        """Call the authenticated text-only service RPC explicitly."""

        if not isinstance(request, TextRequest):
            raise TypeError("request must be a TextRequest")
        result = self.call(
            TEXT_POLISH_RPC_METHOD,
            {"request": _text_request_to_rpc(request)},
            timeout_seconds=self.text_timeout_seconds,
        )
        return _text_result_from_rpc(result)

    def download_file(
        self,
        root_alias: str,
        relative_path: str,
        *,
        destination: str | Path | None = None,
    ) -> bytes | dict[str, Any]:
        alias = quote(str(root_alias or "").strip(), safe="")
        relative = str(relative_path or "").replace("\\", "/")
        encoded_relative = "/".join(
            quote(segment, safe="") for segment in relative.split("/")
        )
        response = self._request(f"/files/{alias}/{encoded_relative}")
        if destination is None:
            with response:
                expected_size, expected_sha256 = self._download_integrity_headers(
                    response
                )
                payload = response.read()
            actual_size = len(payload)
            if actual_size != expected_size:
                raise HubConnectionError(
                    "Hub file download is incomplete "
                    f"(expected {expected_size} bytes, received {actual_size})"
                )
            actual_sha256 = hashlib.sha256(payload).hexdigest()
            if expected_sha256 and not hmac.compare_digest(
                actual_sha256, expected_sha256
            ):
                raise HubConnectionError("Hub file SHA-256 verification failed")
            return payload

        destination_path = Path(destination).expanduser().resolve()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with response:
                content_type = str(response.headers.get("Content-Type") or "")
                expected_size, expected_sha256 = self._download_integrity_headers(
                    response
                )
                handle, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination_path.name}.",
                    suffix=".part",
                    dir=destination_path.parent,
                )
                temporary_path = Path(temporary_name)
                digest = hashlib.sha256()
                total = 0
                with os.fdopen(handle, "wb") as stream:
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        total += len(chunk)
                        if total > expected_size:
                            raise HubConnectionError(
                                "Hub file body exceeds its Content-Length"
                            )
                        digest.update(chunk)
                        stream.write(chunk)
            if total != expected_size:
                raise HubConnectionError(
                    "Hub file download is incomplete "
                    f"(expected {expected_size} bytes, received {total})"
                )
            actual_sha256 = digest.hexdigest()
            if expected_sha256 and not hmac.compare_digest(
                actual_sha256, expected_sha256
            ):
                raise HubConnectionError("Hub file SHA-256 verification failed")
            os.replace(temporary_path, destination_path)
            temporary_path = None
            return {
                "path": str(destination_path),
                "size_bytes": total,
                "sha256": actual_sha256,
                "content_type": content_type,
            }
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def file_metadata(
        self,
        root_alias: str,
        relative_path: str,
    ) -> dict[str, Any] | None:
        """Fetch lightweight remote file version data without its body.

        ``None`` means the remote Hub predates the authenticated HEAD route.
        Other HTTP errors are retained so missing/revoked files cannot be
        mistaken for a valid cached asset.
        """

        route = self._encoded_file_route(root_alias, relative_path)
        try:
            response = self._request(route, method="HEAD")
        except HubRemoteError as error:
            if error.status in {
                HTTPStatus.NOT_FOUND,
                HTTPStatus.METHOD_NOT_ALLOWED,
                HTTPStatus.NOT_IMPLEMENTED,
            } and error.code in {
                "not_found",
                "method_not_allowed",
                "unsupported_method",
                "http_error",
            }:
                return None
            raise
        with response:
            expected_size, expected_sha256 = self._download_integrity_headers(
                response
            )
            etag = str(response.headers.get("ETag") or "").strip()
        return {
            "size_bytes": expected_size,
            "sha256": expected_sha256,
            "etag": etag,
        }

    @staticmethod
    def _download_integrity_headers(response: Any) -> tuple[int, str]:
        raw_size = str(response.headers.get("Content-Length") or "").strip()
        if not raw_size.isdecimal():
            raise HubConnectionError(
                "Hub file response is missing a valid Content-Length"
            )
        expected_size = int(raw_size)
        expected_sha256 = str(
            response.headers.get(UPLOAD_SHA256_HEADER) or ""
        ).strip().casefold()
        if expected_sha256 and (
            len(expected_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_sha256
            )
        ):
            raise HubConnectionError(
                f"Hub file response has an invalid {UPLOAD_SHA256_HEADER}"
            )
        return expected_size, expected_sha256

    @staticmethod
    def _encoded_file_route(root_alias: str, relative_path: str) -> str:
        alias = quote(str(root_alias or "").strip(), safe="")
        relative = str(relative_path or "").replace("\\", "/")
        encoded_relative = "/".join(
            quote(segment, safe="") for segment in relative.split("/")
        )
        return f"/files/{alias}/{encoded_relative}"

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def upload_file(
        self,
        root_alias: str,
        relative_path: str,
        source: str | Path,
        *,
        sha256: str | None = None,
    ) -> dict[str, Any]:
        """Stream one local media file to a configured Hub file root.

        The source is hashed before transmission.  ``sha256`` is primarily for
        callers that already maintain a trusted media ledger; when supplied it
        is still verified by the Hub against the received bytes.
        """

        try:
            source_path = Path(source).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("upload source file does not exist") from error
        if not source_path.is_file():
            raise ValueError("upload source must be a regular file")
        size_bytes = source_path.stat().st_size
        actual_sha256 = self._file_sha256(source_path)
        declared_sha256 = str(sha256 or actual_sha256).strip().casefold()
        if len(declared_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in declared_sha256
        ):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        alias = str(root_alias or "").strip()
        normalized_relative = str(relative_path or "").replace("\\", "/")
        check_query = urlencode(
            {
                "root": alias,
                "path": normalized_relative,
                "size_bytes": size_bytes,
                "sha256": declared_sha256,
            }
        )
        checked = self._read_json_response(
            self._request(f"/file-upload-check?{check_query}")
        )
        if not checked.get("ok"):
            raise HubConnectionError("Hub upload check was not successful")
        route = self._encoded_file_route(alias, normalized_relative)
        with source_path.open("rb") as stream:
            response = self._request(
                route,
                method="PUT",
                body=stream,
                content_type="application/octet-stream",
                extra_headers={
                    "Content-Length": str(size_bytes),
                    UPLOAD_SHA256_HEADER: declared_sha256,
                },
            )
            payload = self._read_json_response(response)
        if not payload.get("ok") or not isinstance(payload.get("file"), dict):
            raise HubConnectionError("Hub upload response was not successful")
        result = dict(payload["file"])
        if result.get("sha256") != declared_sha256:
            raise HubConnectionError("Hub upload response SHA-256 does not match")
        try:
            remote_size = int(result.get("size_bytes"))
        except (TypeError, ValueError) as error:
            raise HubConnectionError("Hub upload response has an invalid size") from error
        if remote_size != size_bytes:
            raise HubConnectionError("Hub upload response size does not match")
        return result


class HubTextProvider:
    """Text provider used by rendering PCs.

    The configured provider and all of its credentials live on the Hub.  A
    rendering PC sends only TextRequest data. If the Hub or its model is
    unavailable, quality-first production retries briefly and then raises a
    visible provider error. Deterministic local fallback is available only when
    a caller explicitly opts into the legacy behaviour; it is never silent.
    """

    def __init__(
        self,
        client: HubClient | None,
        *,
        local_provider_factory: Callable[[Any], Any] = create_text_provider,
        allow_local_fallback: bool = False,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.5,
    ) -> None:
        if client is not None and not isinstance(client, HubClient):
            raise TypeError("client must be a HubClient or None")
        if not callable(local_provider_factory):
            raise TypeError("local_provider_factory must be callable")
        if isinstance(max_attempts, bool) or not 1 <= int(max_attempts) <= 8:
            raise ValueError("max_attempts must be between 1 and 8")
        if (
            isinstance(retry_delay_seconds, bool)
            or float(retry_delay_seconds) < 0
            or float(retry_delay_seconds) > 10
        ):
            raise ValueError("retry_delay_seconds must be between 0 and 10")
        self.client = client
        self._local_provider_factory = local_provider_factory
        self.allow_local_fallback = bool(allow_local_fallback)
        # PipelineRunner uses this marker to avoid applying its legacy provider
        # fallback on top of the Hub provider's own bounded retry policy.
        self.strict_quality = not self.allow_local_fallback
        self.max_attempts = int(max_attempts)
        self.retry_delay_seconds = float(retry_delay_seconds)

    def _local(self, request: TextRequest) -> TextResult:
        provider = self._local_provider_factory(
            ProviderConfig(name="local", options={"mode": "rules"})
        )
        result = provider.polish(request)
        if not isinstance(result, TextResult):
            raise TypeError("local text provider returned an invalid result")
        return result

    def polish(self, request: TextRequest | str, **kwargs: Any) -> TextResult:
        if isinstance(request, TextRequest):
            if kwargs:
                raise TypeError(
                    "Keyword request fields cannot accompany a TextRequest."
                )
            normalized = request
        elif isinstance(request, str):
            normalized = TextRequest(text=request, **kwargs)
        else:
            raise TypeError("polish() expects TextRequest or story text")
        if self.client is None:
            if self.allow_local_fallback:
                return self._local(normalized)
            raise ProviderError(
                "Hub AI is not connected; this task was stopped instead of "
                "silently using local rules.",
                provider="hub_text",
                retryable=True,
            )

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self.client.text_polish(normalized)
            except HubAuthenticationError:
                raise
            except HubRemoteError as error:
                last_error = error
                retryable = error.status == HTTPStatus.TOO_MANY_REQUESTS or (
                    HTTPStatus.INTERNAL_SERVER_ERROR
                    <= error.status
                    <= HTTPStatus.NETWORK_AUTHENTICATION_REQUIRED
                )
                if not retryable or attempt >= self.max_attempts:
                    break
            except (HubConnectionError, OSError, TimeoutError) as error:
                last_error = error
                if attempt >= self.max_attempts:
                    break
            except (RuntimeError, TypeError, ValueError) as error:
                last_error = error
                break
            if self.retry_delay_seconds:
                time.sleep(self.retry_delay_seconds * attempt)

        if self.allow_local_fallback:
            return self._local(normalized)
        status_code = (
            int(last_error.status)
            if isinstance(last_error, HubRemoteError)
            else None
        )
        raise ProviderError(
            "Hub AI could not complete the request after retrying; the task "
            "was stopped to protect text quality.",
            provider="hub_text",
            status_code=status_code,
            retryable=True,
        ) from last_error


class HubCatalogProxy:
    """CatalogRepository-shaped JSON-RPC proxy backed by :class:`HubClient`.

    Catalog methods contain a mix of positional and keyword-only parameters.
    Binding against the repository's real signatures preserves those calling
    conventions for LibraryService while transmitting a JSON object over RPC.
    """

    def __init__(self, client: HubClient) -> None:
        if not isinstance(client, HubClient):
            raise TypeError("client must be a HubClient")
        self.client = client

    def __getattr__(self, name: str) -> Any:
        if name not in CATALOG_RPC_METHODS:
            raise AttributeError(name)
        repository_method = getattr(CatalogRepository, name, None)
        if not callable(repository_method):
            raise AttributeError(name)
        signature = inspect.signature(repository_method)
        public_signature = signature.replace(
            parameters=tuple(signature.parameters.values())[1:]
        )

        def invoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
            bound = signature.bind(None, *args, **kwargs)
            parameters = dict(bound.arguments)
            parameters.pop("self", None)
            return self.client.call(name, parameters)

        invoke.__name__ = name
        invoke.__qualname__ = f"{type(self).__name__}.{name}"
        invoke.__doc__ = getattr(repository_method, "__doc__", None)
        invoke.__signature__ = public_signature  # type: ignore[attr-defined]
        setattr(self, name, invoke)
        return invoke

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(CATALOG_RPC_METHODS))


__all__ = [
    "CATALOG_READ_METHODS",
    "CATALOG_RPC_METHODS",
    "CATALOG_WRITE_METHODS",
    "DEVICE_ADMIN_RPC_METHODS",
    "DEVICE_CLIENT_RPC_METHODS",
    "DEVICE_SERVICE_RPC_METHODS",
    "DEFAULT_DOWNLOAD_EXTENSIONS",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "HUB_PROTOCOL_VERSION",
    "HUB_MINIMUM_CLIENT_PROTOCOL_VERSION",
    "HUB_MINIMUM_SERVER_PROTOCOL_VERSION",
    "MAX_TEXT_POLISH_CHARACTERS",
    "TEXT_POLISH_RPC_METHOD",
    "UPLOAD_SHA256_HEADER",
    "HubAuthenticationError",
    "HubCatalogProxy",
    "HubClient",
    "HubConnectionError",
    "HubError",
    "HubRemoteError",
    "HubServer",
    "HubServerStateError",
    "HubTextProvider",
]
