from __future__ import annotations

import ipaddress
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


CONNECTION_PROFILE_FILENAME = "storyforge-connection.json"
CONNECTION_PROFILE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    """Non-secret Hub location shipped beside an employee executable."""

    endpoint: str
    site_name: str = "StoryForge Hub"
    source: str = ""


def _normalize_endpoint(value: object) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    if not endpoint or len(endpoint) > 2048:
        raise ValueError("预置的 StoryForge Hub 地址无效。")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("预置的 StoryForge Hub 地址无效。")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("预置的 StoryForge Hub 端口无效。") from error
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("预置的 StoryForge Hub 端口无效。")
    if parsed.scheme == "http":
        hostname = str(parsed.hostname or "").strip().casefold()
        try:
            address = ipaddress.ip_address(hostname)
            private_http = bool(
                address.is_private or address.is_loopback or address.is_link_local
            )
        except ValueError:
            # Plain NetBIOS names and mDNS names are local-network targets.
            private_http = bool(
                hostname == "localhost"
                or "." not in hostname
                or hostname.endswith(".local")
            )
        if not private_http:
            raise ValueError("HTTP Hub 地址只允许可信局域网；跨网络连接必须使用 HTTPS。")
    return endpoint


def connection_profile_candidates() -> tuple[Path, ...]:
    """Return explicit or packaged profile paths in priority order.

    A frozen onedir app keeps the profile next to the visible executable, not
    under PyInstaller's private ``_internal`` resource directory. Source runs
    deliberately have no implicit project profile so tests and standalone
    development never become accidental Hub clients.
    """

    candidates: list[Path] = []
    override = str(os.environ.get("STORYFORGE_CONNECTION_PROFILE") or "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    if bool(getattr(sys, "frozen", False)):
        candidates.append(Path(sys.executable).resolve().parent / CONNECTION_PROFILE_FILENAME)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return tuple(unique)


def load_connection_profile() -> ConnectionProfile | None:
    """Load the first valid packaged Hub location without exposing secrets."""

    endpoint_override = str(os.environ.get("STORYFORGE_HUB_ENDPOINT") or "").strip()
    if endpoint_override:
        return ConnectionProfile(
            endpoint=_normalize_endpoint(endpoint_override),
            site_name="StoryForge Hub",
            source="environment",
        )
    for path in connection_profile_candidates():
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("StoryForge 连接配置损坏，请让管理员重新复制完整软件文件夹。") from error
        if not isinstance(payload, dict):
            raise ValueError("StoryForge 连接配置格式无效。")
        try:
            schema_version = int(payload.get("schema_version") or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("StoryForge 连接配置版本无效。") from error
        if schema_version != CONNECTION_PROFILE_SCHEMA_VERSION:
            raise ValueError("StoryForge 连接配置版本不受当前程序支持。")
        site_name = str(payload.get("site_name") or "StoryForge Hub").strip()
        return ConnectionProfile(
            endpoint=_normalize_endpoint(payload.get("endpoint")),
            site_name=site_name[:160] or "StoryForge Hub",
            source=str(path),
        )
    return None


def write_connection_profile(
    path: str | Path,
    endpoint: str,
    *,
    site_name: str = "StoryForge Hub",
) -> Path:
    """Write the build-time public connection profile atomically."""

    destination = Path(path).resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CONNECTION_PROFILE_SCHEMA_VERSION,
        "endpoint": _normalize_endpoint(endpoint),
        "site_name": str(site_name or "StoryForge Hub").strip()[:160]
        or "StoryForge Hub",
    }
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination
