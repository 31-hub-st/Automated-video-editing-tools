from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__


_ZONE_MARKER_SCHEMA = 1
_ZONE_MARKER_NAME = ".windows-download-zone-v1.json"
_LOADABLE_SUFFIXES = frozenset({".dll", ".exe", ".pyd"})


def _remove_zone_identifier(path: Path) -> bool:
    """Remove only Windows' Mark-of-the-Web stream from one bundled file."""

    try:
        os.remove(str(path) + ":Zone.Identifier")
    except FileNotFoundError:
        return False
    return True


def _bundle_identity(executable: Path) -> dict[str, Any]:
    try:
        stat = executable.stat()
    except OSError:
        return {"version": __version__, "executable": str(executable)}
    return {
        "version": __version__,
        "executable": str(executable),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _read_completed_marker(path: Path, identity: dict[str, Any]) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(value, dict)
        and value.get("completed") is True
        and value.get("bundle") == identity
    )


def _write_marker(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def unblock_frozen_installation(
    data_dir: str | Path,
    *,
    frozen: bool | None = None,
    executable: str | Path | None = None,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """Best-effort repair for portable ZIPs carrying Mark-of-the-Web.

    The full/update ZIP is independently verified before release or install.
    This startup repair is intentionally narrow: it touches only executable
    modules inside the currently running StoryForge folder and only removes the
    ``Zone.Identifier`` alternate data stream. Employee media and StoryForgeData
    are never traversed or changed.
    """

    active_platform = os.name if platform_name is None else str(platform_name)
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if active_platform != "nt" or not is_frozen:
        return {"completed": True, "skipped": True, "reason": "not_frozen_windows"}

    entrypoint = Path(executable or sys.executable).expanduser().resolve(strict=False)
    install_root = entrypoint.parent
    root = Path(data_dir).expanduser().resolve(strict=False)
    marker = root / "runtime" / _ZONE_MARKER_NAME
    identity = _bundle_identity(entrypoint)
    if _read_completed_marker(marker, identity):
        return {"completed": True, "skipped": True, "reason": "already_checked"}

    candidates: list[Path] = [entrypoint]
    internal = install_root / "_internal"
    if internal.is_dir():
        try:
            candidates.extend(
                path
                for path in internal.rglob("*")
                if path.is_file() and path.suffix.casefold() in _LOADABLE_SUFFIXES
            )
        except OSError:
            # The .NET config beside the EXE remains the compatibility fallback.
            pass

    removed = 0
    failures: list[str] = []
    for path in candidates:
        try:
            removed += int(_remove_zone_identifier(path))
        except OSError as error:
            if len(failures) < 20:
                failures.append(f"{path}: {type(error).__name__}: {error}")

    result = {
        "schema_version": _ZONE_MARKER_SCHEMA,
        "completed": not failures,
        "checked_at": datetime.now(UTC).isoformat(),
        "bundle": identity,
        "files_checked": len(candidates),
        "zone_markers_removed": removed,
        "errors": failures,
    }
    try:
        _write_marker(marker, result)
    except OSError:
        # Startup must not fail because an antivirus briefly locks a marker.
        pass
    return result


__all__ = ["unblock_frozen_installation"]
