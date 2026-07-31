from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storyforge import __version__  # noqa: E402
from storyforge.updater import (  # noqa: E402
    UPDATE_MANIFEST_SCHEMA,
    UPDATE_PACKAGE_METADATA,
    file_sha256,
    inspect_update_package,
    normalize_version,
)


FROZEN_BUILD_VALIDATION = "BUILD_STARTUP_VALIDATION.json"
FROZEN_KOKORO_VALIDATION = "BUILD_KOKORO_VALIDATION.json"
FROZEN_RELEASE_VALIDATION = "BUILD_RELEASE_VALIDATION.json"
PORTABLE_RUNTIME_DIRECTORY = "StoryForgeData"
RELEASE_VALIDATION_SCHEMA = 1


def _read_passed_frozen_validation(
    path: Path,
    *,
    label: str,
    requested_version: str,
) -> dict[str, object]:
    if not path.is_file():
        raise ValueError(f"Frozen StoryForge packages require {path.name}.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"{path.name} is not valid UTF-8 JSON.") from error
    if not isinstance(raw, dict) or raw.get("ok") is not True:
        raise ValueError(f"{path.name} does not contain a passed {label} self-test.")
    if raw.get("frozen") is not True:
        raise ValueError(f"{path.name} was not produced by the frozen application.")
    try:
        validated_version = normalize_version(raw.get("app_version"))
    except ValueError as error:
        raise ValueError(f"{path.name} contains an invalid app_version.") from error
    if validated_version != requested_version:
        raise ValueError(
            f"{label.capitalize()} validation version mismatch: "
            f"validated binary is {validated_version}, requested package is "
            f"{requested_version}."
        )
    return raw


def _bundle_manifest(source: Path) -> dict[str, object]:
    """Hash every release file except the attestation that contains the hash."""

    records: list[tuple[str, int, str]] = []
    normalized_paths: set[str] = set()
    total_bytes = 0
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Release directory cannot contain symlinks: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        if relative.split("/", 1)[0].casefold() == PORTABLE_RUNTIME_DIRECTORY.casefold():
            continue
        if relative.casefold() == FROZEN_RELEASE_VALIDATION.casefold():
            continue
        if relative.casefold() == UPDATE_PACKAGE_METADATA.casefold():
            continue
        if any(part in {".git", "__pycache__"} for part in Path(relative).parts):
            continue
        normalized = relative.casefold()
        if normalized in normalized_paths:
            raise ValueError(
                "Release directory contains case-insensitive duplicate paths: "
                f"{relative}"
            )
        normalized_paths.add(normalized)
        size = int(path.stat().st_size)
        digest = file_sha256(path)
        records.append((relative, size, digest))
        total_bytes += size
    records.sort(key=lambda item: item[0].casefold())
    manifest_digest = hashlib.sha256()
    for relative, size, digest in records:
        manifest_digest.update(relative.encode("utf-8"))
        manifest_digest.update(b"\0")
        manifest_digest.update(str(size).encode("ascii"))
        manifest_digest.update(b"\0")
        manifest_digest.update(digest.encode("ascii"))
        manifest_digest.update(b"\n")
    return {
        "bundle_manifest_sha256": manifest_digest.hexdigest(),
        "bundle_file_count": len(records),
        "bundle_size_bytes": total_bytes,
        # This exact, verified allow-list is consumed by future installers.
        # It lets an update remove files managed by the previous release that
        # disappeared from the new release without ever sweeping user files.
        "bundle_files": [relative for relative, _size, _digest in records],
    }


def _local_ai_assets_present(source: Path) -> bool:
    kokoro = source / "local-ai" / "kokoro"
    return bool(
        (kokoro / "kokoro-v1_0.pth").is_file()
        and (kokoro / "config.json").is_file()
        and (kokoro / "voices").is_dir()
        and any(path.is_file() for path in (kokoro / "voices").glob("*"))
    )


def write_release_validation(
    source_directory: str | Path,
    *,
    entrypoint: str,
    requested_version: str,
    with_local_ai: bool,
) -> dict[str, object]:
    """Attest the exact frozen directory after all runtime smoke tests pass."""

    source = Path(source_directory).expanduser().resolve(strict=True)
    version = normalize_version(requested_version)
    normalized_entrypoint = str(entrypoint or "").replace("\\", "/").strip("/")
    entrypoint_path = source / Path(normalized_entrypoint)
    if not normalized_entrypoint or not entrypoint_path.is_file():
        raise ValueError("Release entrypoint does not exist in the build directory.")
    startup_path = source / FROZEN_BUILD_VALIDATION
    _read_passed_frozen_validation(
        startup_path,
        label="startup",
        requested_version=version,
    )
    assets_present = _local_ai_assets_present(source)
    if bool(with_local_ai) != assets_present:
        raise ValueError(
            "WithLocalAI release identity does not match the bundled Kokoro assets."
        )
    kokoro_path = source / FROZEN_KOKORO_VALIDATION
    kokoro_digest = ""
    if with_local_ai:
        _read_passed_frozen_validation(
            kokoro_path,
            label="Kokoro",
            requested_version=version,
        )
        kokoro_digest = file_sha256(kokoro_path)
    elif kokoro_path.exists():
        raise ValueError(
            f"Lightweight releases cannot contain stale {FROZEN_KOKORO_VALIDATION}."
        )
    attestation: dict[str, object] = {
        "schema_version": RELEASE_VALIDATION_SCHEMA,
        "ok": True,
        "frozen": True,
        "app_version": version,
        "entrypoint": normalized_entrypoint,
        "entrypoint_sha256": file_sha256(entrypoint_path),
        "startup_validation_sha256": file_sha256(startup_path),
        "kokoro_validation_sha256": kokoro_digest,
        "with_local_ai": bool(with_local_ai),
        **_bundle_manifest(source),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    destination = source / FROZEN_RELEASE_VALIDATION
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-", suffix=".tmp", dir=source
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(attestation, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return attestation


def _verify_release_validation(
    source: Path,
    *,
    entrypoint: str,
    requested_version: str,
) -> dict[str, object]:
    validation_path = source / FROZEN_RELEASE_VALIDATION
    if not validation_path.is_file():
        raise ValueError(
            f"Frozen StoryForge packages require {FROZEN_RELEASE_VALIDATION}; "
            "run scripts/build_exe.ps1 and package that exact verified output."
        )
    try:
        raw = json.loads(validation_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"{FROZEN_RELEASE_VALIDATION} is not valid UTF-8 JSON.") from error
    if not isinstance(raw, dict):
        raise ValueError(f"{FROZEN_RELEASE_VALIDATION} must contain an object.")
    if raw.get("schema_version") != RELEASE_VALIDATION_SCHEMA:
        raise ValueError(f"{FROZEN_RELEASE_VALIDATION} schema is unsupported.")
    if raw.get("ok") is not True or raw.get("frozen") is not True:
        raise ValueError(f"{FROZEN_RELEASE_VALIDATION} is not a passed frozen build.")
    if normalize_version(raw.get("app_version")) != requested_version:
        raise ValueError("Release validation app_version does not match the package.")
    if str(raw.get("entrypoint") or "").replace("\\", "/") != entrypoint:
        raise ValueError("Release validation entrypoint does not match the package.")
    entrypoint_path = source / Path(entrypoint)
    if not hmac.compare_digest(
        str(raw.get("entrypoint_sha256") or "").casefold(),
        file_sha256(entrypoint_path),
    ):
        raise ValueError("Release entrypoint changed after frozen startup validation.")
    startup_path = source / FROZEN_BUILD_VALIDATION
    if not hmac.compare_digest(
        str(raw.get("startup_validation_sha256") or "").casefold(),
        file_sha256(startup_path),
    ):
        raise ValueError("Startup validation changed after release attestation.")
    with_local_ai = raw.get("with_local_ai")
    if not isinstance(with_local_ai, bool):
        raise ValueError("Release validation with_local_ai flag is invalid.")
    if with_local_ai != _local_ai_assets_present(source):
        raise ValueError("Release validation does not match bundled Kokoro assets.")
    kokoro_path = source / FROZEN_KOKORO_VALIDATION
    if with_local_ai:
        _read_passed_frozen_validation(
            kokoro_path,
            label="Kokoro",
            requested_version=requested_version,
        )
        if not hmac.compare_digest(
            str(raw.get("kokoro_validation_sha256") or "").casefold(),
            file_sha256(kokoro_path),
        ):
            raise ValueError("Kokoro validation changed after release attestation.")
    elif kokoro_path.exists() or str(raw.get("kokoro_validation_sha256") or ""):
        raise ValueError("Lightweight release contains stale Kokoro validation.")
    current_manifest = _bundle_manifest(source)
    for key in (
        "bundle_manifest_sha256",
        "bundle_file_count",
        "bundle_size_bytes",
        "bundle_files",
    ):
        if raw.get(key) != current_manifest[key]:
            raise ValueError(
                "Frozen build directory changed after release validation "
                f"({key} mismatch)."
            )
    return raw


def _validate_release_identity(
    source: Path,
    *,
    entrypoint: str,
    requested_version: str,
) -> None:
    """Refuse update archives whose binary and release labels disagree."""

    source_version = normalize_version(__version__)
    requested = normalize_version(requested_version)
    entry_name = Path(entrypoint).name.casefold()
    frozen_storyforge = (
        Path(entrypoint).suffix.casefold() == ".exe"
        and entry_name.startswith("storyforge")
    )
    validation_path = source / FROZEN_BUILD_VALIDATION
    if frozen_storyforge:
        _read_passed_frozen_validation(
            validation_path,
            label="startup",
            requested_version=requested,
        )
        _verify_release_validation(
            source,
            entrypoint=str(entrypoint).replace("\\", "/").strip("/"),
            requested_version=requested,
        )

    # Source packages have no frozen validation file, so the imported source
    # version itself is their release identity. Frozen builds must agree with
    # both checks; this prevents an old verified directory from being relabeled
    # by a newer copy of the packaging script.
    if requested != source_version:
        raise ValueError(
            "StoryForge source version mismatch: "
            f"source is {source_version}, requested package is {requested}."
        )


def _safe_files(source: Path, output: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"更新目录不能包含符号链接：{path}")
        if not path.is_file():
            continue
        if path.resolve() == output.resolve():
            continue
        relative = path.relative_to(source)
        if relative.parts and relative.parts[0].casefold() == PORTABLE_RUNTIME_DIRECTORY.casefold():
            continue
        if any(part in {".git", "__pycache__"} for part in relative.parts):
            continue
        if relative.as_posix() == UPDATE_PACKAGE_METADATA:
            continue
        files.append((path, relative.as_posix()))
    return sorted(files, key=lambda item: item[1].casefold())


def build_package(
    source_directory: str | Path,
    *,
    output_path: str | Path,
    entrypoint: str,
    version: str = __version__,
) -> dict[str, object]:
    source = Path(source_directory).expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError("source_directory 必须是已构建的 StoryForge 文件夹。")
    version = normalize_version(version)
    normalized_entrypoint = str(entrypoint or "").replace("\\", "/").strip("/")
    if (
        not normalized_entrypoint
        or normalized_entrypoint.startswith(".")
        or ".." in Path(normalized_entrypoint).parts
        or not (source / Path(normalized_entrypoint)).is_file()
    ):
        raise ValueError("entrypoint 必须是构建目录内存在的启动文件。")
    _validate_release_identity(
        source,
        entrypoint=normalized_entrypoint,
        requested_version=version,
    )
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    files = _safe_files(source, output)
    if not files:
        raise ValueError("构建目录中没有可发布文件。")
    metadata = {
        "schema_version": 1,
        "version": version,
        "entrypoint": normalized_entrypoint,
    }
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}-", suffix=".zip", dir=output.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            archive.writestr(
                UPDATE_PACKAGE_METADATA,
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            )
            for path, relative in files:
                archive.write(path, relative)
        inspect_update_package(temporary, expected_version=version)
        os.replace(temporary, output)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    manifest = {
        "schema_version": UPDATE_MANIFEST_SCHEMA,
        "version": version,
        "filename": output.name,
        "sha256": file_sha256(output),
        "size_bytes": output.stat().st_size,
        "entrypoint": normalized_entrypoint,
        "release_notes": "",
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "package_path": str(output),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把已经构建好的 StoryForge 文件夹生成可校验的 Hub 更新包。"
    )
    parser.add_argument("source_directory", help="已构建的软件文件夹")
    parser.add_argument(
        "--entrypoint",
        required=True,
        help="软件文件夹内的启动文件，例如 StoryForge.exe",
    )
    parser.add_argument(
        "--version",
        default=__version__,
        help=f"发布版本，默认读取 storyforge.__version__ ({__version__})",
    )
    parser.add_argument(
        "--output",
        help="输出 ZIP；默认写入 release/StoryForge-<version>-update.zip",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    version = normalize_version(args.version)
    output = args.output or str(
        PROJECT_ROOT / "release" / f"StoryForge-{version}-update.zip"
    )
    result = build_package(
        args.source_directory,
        output_path=output,
        entrypoint=args.entrypoint,
        version=version,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
