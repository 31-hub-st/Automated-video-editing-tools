"""Install optional StoryForge language components safely.

The component updater is deliberately independent from the current TTS runtime.
Bundled Japanese support therefore keeps working exactly as it does today.  A
future local installer or Hub endpoint can build/download a component ZIP and
pass its path to :class:`ComponentUpdater` without replacing the whole EXE.

Package layout::

    component.json
    payload/<component files...>

Every payload file is declared in ``component.json`` with its byte size and
SHA-256 digest.  The archive's SHA-256 is returned separately by the builder so
that a Hub can authenticate the downloaded ZIP without creating a circular
"ZIP contains its own hash" requirement.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Mapping

from . import __version__


COMPONENT_MANIFEST_FILENAME = "component.json"
COMPONENT_MANIFEST_SCHEMA = 1
COMPONENT_PAYLOAD_PREFIX = "payload"
COMPONENT_STATE_FILENAME = "state.json"
COMPONENT_CATALOG_FILENAME = "manifest.json"
COMPONENT_CATALOG_SCHEMA = 1
MAX_COMPONENT_ENTRIES = 20_000
MAX_COMPONENT_MANIFEST_BYTES = 256 * 1024
MAX_COMPONENT_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MAX_COMPONENT_COMPRESSION_RATIO = 2_000
MAX_PUBLISHED_COMPONENTS = 128
MAX_COMPONENT_RELEASE_NOTES = 8_000

_COMPONENT_CATALOG_SIGNATURE_DOMAIN = b"storyforge-component-catalog-v1\0"
_ACTIVE_COMPONENT_PATHS: set[str] = set()
_ACTIVE_COMPONENT_DLL_HANDLES: list[Any] = []
_ACTIVE_COMPONENT_DLL_PATHS: set[str] = set()
_RUNTIME_ACTIVATION_LOCK = threading.RLock()

_COMPONENT_ID_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
)
_VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?P<suffix>-(?:[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*))?$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class ComponentPackageError(ValueError):
    """Base error for an invalid or unusable component package."""


class ComponentSecurityError(ComponentPackageError):
    """The archive contains an unsafe path or unsupported entry type."""


class ComponentIntegrityError(ComponentPackageError):
    """A package or payload digest/size does not match its declaration."""


class ComponentCompatibilityError(ComponentPackageError):
    """The component does not support the running StoryForge version."""


class ComponentNotInstalledError(ComponentPackageError):
    """The requested component, or its rollback release, is unavailable."""


class ComponentRepositoryError(ComponentPackageError):
    """A Hub component publication is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class ComponentFile:
    path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ComponentManifest:
    component_id: str
    version: str
    min_app_version: str
    max_app_version: str | None
    files: tuple[ComponentFile, ...]
    schema_version: int = COMPONENT_MANIFEST_SCHEMA

    @property
    def app_compatibility(self) -> dict[str, str | None]:
        return {
            "min_version": self.min_app_version,
            "max_version": self.max_app_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "component_id": self.component_id,
            "version": self.version,
            "app_compatibility": self.app_compatibility,
            "files": [item.to_dict() for item in self.files],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ComponentManifest":
        if not isinstance(value, Mapping):
            raise ComponentPackageError("Component manifest must be a JSON object.")
        try:
            schema_version = int(value.get("schema_version") or 0)
        except (TypeError, ValueError) as error:
            raise ComponentPackageError("Component manifest schema is invalid.") from error
        if schema_version != COMPONENT_MANIFEST_SCHEMA:
            raise ComponentPackageError(
                f"Unsupported component manifest schema: {schema_version}."
            )

        component_id = _normalize_component_id(value.get("component_id"))
        version = _normalize_version(value.get("version"), label="component version")

        compatibility = value.get("app_compatibility")
        if not isinstance(compatibility, Mapping):
            raise ComponentPackageError("app_compatibility must be an object.")
        min_app_version = _normalize_version(
            compatibility.get("min_version"), label="minimum app version"
        )
        raw_max = compatibility.get("max_version")
        max_app_version = (
            _normalize_version(raw_max, label="maximum app version")
            if raw_max not in (None, "")
            else None
        )
        if (
            max_app_version is not None
            and _version_key(max_app_version) < _version_key(min_app_version)
        ):
            raise ComponentPackageError(
                "maximum app version cannot be older than minimum app version."
            )

        raw_files = value.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ComponentPackageError("Component manifest files must be a non-empty list.")
        files: list[ComponentFile] = []
        seen: set[str] = set()
        total_size = 0
        for raw_file in raw_files:
            if not isinstance(raw_file, Mapping):
                raise ComponentPackageError("Every component file entry must be an object.")
            relative = _safe_relative_path(raw_file.get("path"), label="payload file")
            comparison_key = _path_comparison_key(relative)
            if comparison_key in seen:
                raise ComponentPackageError(
                    f"Duplicate component file path: {relative}."
                )
            seen.add(comparison_key)
            try:
                size = int(raw_file.get("size"))
            except (TypeError, ValueError) as error:
                raise ComponentPackageError(
                    f"Invalid payload size for {relative}."
                ) from error
            if size < 0:
                raise ComponentPackageError(f"Invalid payload size for {relative}.")
            total_size += size
            if total_size > MAX_COMPONENT_UNCOMPRESSED_BYTES:
                raise ComponentPackageError(
                    "Component payload exceeds the uncompressed size limit."
                )
            digest = str(raw_file.get("sha256") or "").strip().casefold()
            if not _SHA256_PATTERN.fullmatch(digest):
                raise ComponentPackageError(
                    f"Invalid SHA-256 digest for payload file {relative}."
                )
            files.append(ComponentFile(relative, size, digest))

        return cls(
            component_id=component_id,
            version=version,
            min_app_version=min_app_version,
            max_app_version=max_app_version,
            files=tuple(files),
            schema_version=schema_version,
        )


@dataclass(frozen=True, slots=True)
class ComponentPackageArtifact:
    path: Path
    manifest: ComponentManifest
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ComponentPackageInspection:
    path: Path
    manifest: ComponentManifest
    sha256: str
    size_bytes: int
    uncompressed_size_bytes: int


@dataclass(frozen=True, slots=True)
class InstalledComponent:
    component_id: str
    version: str
    root: Path
    manifest: ComponentManifest
    package_sha256: str

    def resolve(self, relative_path: str | Path = "") -> Path:
        if str(relative_path or "") in {"", "."}:
            return self.root
        relative = _safe_relative_path(
            str(relative_path).replace("\\", "/"), label="component file"
        )
        candidate = (self.root / Path(*PurePosixPath(relative).parts)).resolve(
            strict=False
        )
        try:
            candidate.relative_to(self.root.resolve(strict=False))
        except ValueError as error:
            raise ComponentSecurityError("Component path escapes its payload root.") from error
        return candidate


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def component_file_sha256(path: str | Path) -> str:
    with Path(path).open("rb") as stream:
        return _sha256_stream(stream)


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_component_catalog_bytes(
    value: Mapping[str, Any] | None,
) -> bytes:
    return _COMPONENT_CATALOG_SIGNATURE_DOMAIN + json.dumps(
        dict(value) if value is not None else None,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_component_catalog(token: str, value: Mapping[str, Any] | None) -> str:
    """Authenticate a component catalog with the existing Hub bearer secret.

    Domain separation prevents a valid signature from another Hub protocol
    object from being replayed as a component catalog.  This does not create a
    second credential or require any workstation-side configuration.
    """

    return hmac.new(
        str(token).encode("utf-8"),
        _canonical_component_catalog_bytes(value),
        hashlib.sha256,
    ).hexdigest()


def verify_component_catalog_signature(
    token: str,
    value: Mapping[str, Any] | None,
    signature: Any,
) -> None:
    expected = sign_component_catalog(token, value)
    if not hmac.compare_digest(expected, str(signature or "").strip().casefold()):
        raise ComponentIntegrityError("Component catalog signature verification failed.")


def _normalize_component_id(value: Any) -> str:
    component_id = str(value or "").strip().casefold()
    if len(component_id) > 96 or not _COMPONENT_ID_PATTERN.fullmatch(component_id):
        raise ComponentPackageError(
            "component_id must be a lowercase dotted identifier such as "
            "kokoro.language.ja."
        )
    return component_id


def _normalize_version(value: Any, *, label: str) -> str:
    version = str(value or "").strip()
    if not _VERSION_PATTERN.fullmatch(version):
        raise ComponentPackageError(
            f"{label} must use semantic version format major.minor.patch."
        )
    return version


def _version_key(value: str) -> tuple[int, int, int, int, tuple[tuple[int, Any], ...]]:
    matched = _VERSION_PATTERN.fullmatch(value)
    if matched is None:
        raise ComponentPackageError(f"Invalid semantic version: {value}.")
    raw_suffix = str(matched.group("suffix") or "")
    if not raw_suffix:
        final = 1
        prerelease: tuple[tuple[int, Any], ...] = ()
    else:
        final = 0
        identifiers = re.findall(r"[A-Za-z]+|\d+", raw_suffix[1:])
        prerelease = tuple(
            (0, int(item)) if item.isdecimal() else (1, item.casefold())
            for item in identifiers
        )
    return (
        int(matched.group("major")),
        int(matched.group("minor")),
        int(matched.group("patch")),
        final,
        prerelease,
    )


def _path_comparison_key(value: str) -> str:
    # Windows is case-insensitive and normalises many Unicode names.  Rejecting
    # collisions here prevents one archive entry from replacing another.
    return unicodedata.normalize("NFC", value).casefold()


def _safe_relative_path(value: Any, *, label: str) -> str:
    raw = str(value or "")
    if (
        not raw
        or raw != raw.strip()
        or len(raw) > 1024
        or "\x00" in raw
        or "\\" in raw
        or raw.startswith("/")
    ):
        raise ComponentSecurityError(f"Unsafe {label} path.")
    raw_parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ComponentSecurityError(f"Unsafe {label} path.")
    for part in raw_parts:
        if (
            part.endswith((" ", "."))
            or ":" in part
            or any(ord(character) < 32 for character in part)
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        ):
            raise ComponentSecurityError(f"Unsafe {label} path.")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ComponentSecurityError(f"Unsafe {label} path.")
    return pure.as_posix()


def _validate_compatible(manifest: ComponentManifest, app_version: str) -> None:
    checked_app = _normalize_version(app_version, label="app version")
    app_key = _version_key(checked_app)
    if app_key < _version_key(manifest.min_app_version):
        raise ComponentCompatibilityError(
            f"{manifest.component_id} {manifest.version} requires StoryForge "
            f"{manifest.min_app_version} or newer; current app is {checked_app}."
        )
    if (
        manifest.max_app_version is not None
        and app_key > _version_key(manifest.max_app_version)
    ):
        raise ComponentCompatibilityError(
            f"{manifest.component_id} {manifest.version} supports StoryForge up to "
            f"{manifest.max_app_version}; current app is {checked_app}."
        )


def validate_component_publication(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return one normalized, JSON-safe Hub publication entry."""

    if not isinstance(value, Mapping):
        raise ComponentRepositoryError("Component publication must be an object.")
    try:
        schema_version = int(value.get("schema_version") or 0)
    except (TypeError, ValueError) as error:
        raise ComponentRepositoryError("Component publication schema is invalid.") from error
    if schema_version != COMPONENT_CATALOG_SCHEMA:
        raise ComponentRepositoryError(
            f"Unsupported component publication schema: {schema_version}."
        )
    manifest_value = value.get("component_manifest")
    if not isinstance(manifest_value, Mapping):
        raise ComponentRepositoryError(
            "Component publication has no embedded component manifest."
        )
    manifest = ComponentManifest.from_mapping(manifest_value)
    component_id = _normalize_component_id(value.get("component_id"))
    version = _normalize_version(value.get("version"), label="component version")
    if component_id != manifest.component_id or version != manifest.version:
        raise ComponentRepositoryError(
            "Component publication identity does not match its package manifest."
        )
    filename_value = _safe_relative_path(value.get("filename"), label="package")
    if "/" in filename_value or not filename_value.casefold().endswith(".zip"):
        raise ComponentRepositoryError("Component publication filename must be one ZIP name.")
    digest = str(value.get("sha256") or "").strip().casefold()
    if not _SHA256_PATTERN.fullmatch(digest):
        raise ComponentRepositoryError("Component package SHA-256 is invalid.")
    try:
        size_bytes = int(value.get("size_bytes"))
    except (TypeError, ValueError) as error:
        raise ComponentRepositoryError("Component package size is invalid.") from error
    if size_bytes <= 0:
        raise ComponentRepositoryError("Component package cannot be empty.")
    release_notes = str(value.get("release_notes") or "").strip()
    if len(release_notes) > MAX_COMPONENT_RELEASE_NOTES:
        raise ComponentRepositoryError("Component release notes are too long.")
    published_at = str(value.get("published_at") or "").strip()
    if not published_at:
        raise ComponentRepositoryError("Component publication time is missing.")
    return {
        "schema_version": COMPONENT_CATALOG_SCHEMA,
        "component_id": component_id,
        "version": version,
        "filename": filename_value,
        "sha256": digest,
        "size_bytes": size_bytes,
        "app_compatibility": manifest.app_compatibility,
        "component_manifest": manifest.to_dict(),
        "release_notes": release_notes,
        "published_at": published_at,
    }


def validate_component_catalog(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the complete signed catalog returned by a Hub."""

    if value is None:
        return {
            "schema_version": COMPONENT_CATALOG_SCHEMA,
            "components": [],
        }
    if not isinstance(value, Mapping):
        raise ComponentRepositoryError("Component catalog must be an object.")
    try:
        schema_version = int(value.get("schema_version") or 0)
    except (TypeError, ValueError) as error:
        raise ComponentRepositoryError("Component catalog schema is invalid.") from error
    if schema_version != COMPONENT_CATALOG_SCHEMA:
        raise ComponentRepositoryError(
            f"Unsupported component catalog schema: {schema_version}."
        )
    raw_components = value.get("components")
    if not isinstance(raw_components, list):
        raise ComponentRepositoryError("Component catalog entries must be a list.")
    if len(raw_components) > MAX_PUBLISHED_COMPONENTS:
        raise ComponentRepositoryError("Component catalog has too many entries.")
    components: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for raw_component in raw_components:
        if not isinstance(raw_component, Mapping):
            raise ComponentRepositoryError("Every component publication must be an object.")
        checked = validate_component_publication(raw_component)
        if checked["component_id"] in identifiers:
            raise ComponentRepositoryError(
                f"Component catalog publishes {checked['component_id']} more than once."
            )
        identifiers.add(checked["component_id"])
        components.append(checked)
    components.sort(key=lambda item: item["component_id"])
    return {
        "schema_version": COMPONENT_CATALOG_SCHEMA,
        "components": components,
    }


def _validate_zip_entry(entry: zipfile.ZipInfo) -> str:
    relative = _safe_relative_path(entry.filename.rstrip("/"), label="archive entry")
    if entry.flag_bits & 0x1:
        raise ComponentSecurityError("Encrypted component archives are not supported.")
    unix_mode = (entry.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type == stat.S_IFLNK:
        raise ComponentSecurityError("Component archives cannot contain symlinks.")
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ComponentSecurityError(
            "Component archives can contain only regular files and directories."
        )
    if entry.file_size < 0 or entry.compress_size < 0:
        raise ComponentSecurityError("Component archive entry has an invalid size.")
    if entry.file_size and not entry.is_dir():
        if entry.compress_size <= 0:
            raise ComponentSecurityError("Component archive has an unsafe compression ratio.")
        if entry.file_size / entry.compress_size > MAX_COMPONENT_COMPRESSION_RATIO:
            raise ComponentSecurityError("Component archive has an unsafe compression ratio.")
    return relative


def _archive_index(
    archive: zipfile.ZipFile,
) -> tuple[zipfile.ZipInfo, dict[str, zipfile.ZipInfo], int]:
    entries = archive.infolist()
    if not entries or len(entries) > MAX_COMPONENT_ENTRIES:
        raise ComponentPackageError("Component archive entry count is invalid.")
    seen: set[str] = set()
    manifest_entry: zipfile.ZipInfo | None = None
    payload_entries: dict[str, zipfile.ZipInfo] = {}
    total_size = 0
    for entry in entries:
        relative = _validate_zip_entry(entry)
        comparison_key = _path_comparison_key(relative)
        if comparison_key in seen:
            raise ComponentSecurityError(
                f"Duplicate archive path after Windows normalisation: {relative}."
            )
        seen.add(comparison_key)
        total_size += int(entry.file_size)
        if total_size > MAX_COMPONENT_UNCOMPRESSED_BYTES:
            raise ComponentPackageError(
                "Component archive exceeds the uncompressed size limit."
            )
        if entry.is_dir():
            continue
        if relative == COMPONENT_MANIFEST_FILENAME:
            manifest_entry = entry
            continue
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] != COMPONENT_PAYLOAD_PREFIX or len(parts) < 2:
            raise ComponentSecurityError(
                f"Unexpected archive file outside payload/: {relative}."
            )
        payload_relative = PurePosixPath(*parts[1:]).as_posix()
        payload_entries[payload_relative] = entry
    if manifest_entry is None:
        raise ComponentPackageError(
            f"Component archive is missing {COMPONENT_MANIFEST_FILENAME}."
        )
    if manifest_entry.file_size > MAX_COMPONENT_MANIFEST_BYTES:
        raise ComponentPackageError("Component manifest is too large.")
    archived_file_keys = {
        _path_comparison_key(f"{COMPONENT_PAYLOAD_PREFIX}/{relative}")
        for relative in payload_entries
    }
    for archived_path in archived_file_keys:
        parts = PurePosixPath(archived_path).parts
        for depth in range(1, len(parts)):
            if PurePosixPath(*parts[:depth]).as_posix() in archived_file_keys:
                raise ComponentSecurityError(
                    "A component archive file cannot also be another file's directory."
                )
    return manifest_entry, payload_entries, total_size


def _read_manifest(
    archive: zipfile.ZipFile, entry: zipfile.ZipInfo
) -> ComponentManifest:
    try:
        raw = json.loads(archive.read(entry).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ComponentPackageError(
            "Component manifest must be valid UTF-8 JSON."
        ) from error
    return ComponentManifest.from_mapping(raw)


def _verify_payload(
    archive: zipfile.ZipFile,
    manifest: ComponentManifest,
    payload_entries: Mapping[str, zipfile.ZipInfo],
) -> None:
    declared = {_path_comparison_key(item.path): item for item in manifest.files}
    archived = {_path_comparison_key(path): path for path in payload_entries}
    if set(declared) != set(archived):
        missing = sorted(item.path for key, item in declared.items() if key not in archived)
        extra = sorted(path for key, path in archived.items() if key not in declared)
        detail = []
        if missing:
            detail.append("missing: " + ", ".join(missing[:5]))
        if extra:
            detail.append("undeclared: " + ", ".join(extra[:5]))
        raise ComponentIntegrityError(
            "Archive payload does not match component manifest"
            + (" (" + "; ".join(detail) + ")" if detail else "")
            + "."
        )
    for comparison_key, declared_file in declared.items():
        entry = payload_entries[archived[comparison_key]]
        if entry.file_size != declared_file.size:
            raise ComponentIntegrityError(
                f"Payload size mismatch for {declared_file.path}."
            )
        with archive.open(entry, "r") as stream:
            digest = _sha256_stream(stream)
        if digest != declared_file.sha256:
            raise ComponentIntegrityError(
                f"Payload SHA-256 mismatch for {declared_file.path}."
            )


class ComponentPackageBuilder:
    """Build a deterministic component ZIP for local or Hub publication."""

    @classmethod
    def build(
        cls,
        source_root: str | Path,
        destination: str | Path,
        *,
        component_id: str,
        version: str,
        min_app_version: str = __version__,
        max_app_version: str | None = None,
    ) -> ComponentPackageArtifact:
        source = Path(source_root).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ComponentPackageError("Component source must be a directory.")
        output = Path(destination).expanduser().resolve(strict=False)
        if output.suffix.casefold() != ".zip":
            raise ComponentPackageError("Component package destination must end in .zip.")
        try:
            output.relative_to(source)
        except ValueError:
            pass
        else:
            raise ComponentPackageError(
                "Component package destination cannot be inside its source directory."
            )

        normalized_id = _normalize_component_id(component_id)
        normalized_version = _normalize_version(version, label="component version")
        normalized_min = _normalize_version(
            min_app_version, label="minimum app version"
        )
        normalized_max = (
            _normalize_version(max_app_version, label="maximum app version")
            if max_app_version not in (None, "")
            else None
        )

        source_files: list[tuple[str, Path]] = []
        for candidate in source.rglob("*"):
            if candidate.is_symlink():
                raise ComponentSecurityError(
                    f"Component source cannot contain symlinks: {candidate}."
                )
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(source).as_posix()
            relative = _safe_relative_path(relative, label="payload file")
            source_files.append((relative, candidate))
        source_files.sort(key=lambda item: _path_comparison_key(item[0]))
        if not source_files:
            raise ComponentPackageError("Component source directory is empty.")

        files = tuple(
            ComponentFile(
                path=relative,
                size=path.stat().st_size,
                sha256=component_file_sha256(path),
            )
            for relative, path in source_files
        )
        manifest = ComponentManifest(
            component_id=normalized_id,
            version=normalized_version,
            min_app_version=normalized_min,
            max_app_version=normalized_max,
            files=files,
        )
        # Round-trip through validation so packages built by StoryForge obey
        # exactly the same rules as packages received from elsewhere.
        manifest = ComponentManifest.from_mapping(manifest.to_dict())

        output.parent.mkdir(parents=True, exist_ok=True)
        file_handle, temporary_name = tempfile.mkstemp(
            prefix=f".{output.stem}-", suffix=".zip", dir=output.parent
        )
        os.close(file_handle)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(
                temporary,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
                allowZip64=True,
            ) as archive:
                cls._write_bytes(
                    archive,
                    COMPONENT_MANIFEST_FILENAME,
                    _canonical_json_bytes(manifest.to_dict()) + b"\n",
                )
                for relative, source_path in source_files:
                    cls._write_file(
                        archive,
                        f"{COMPONENT_PAYLOAD_PREFIX}/{relative}",
                        source_path,
                    )
            # Verify the complete artifact before it becomes visible.
            inspection = ComponentUpdater.inspect_package(
                temporary, app_version=normalized_min
            )
            if inspection.manifest != manifest:
                raise ComponentIntegrityError(
                    "Built component manifest did not survive package verification."
                )
            os.replace(temporary, output)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
        return ComponentPackageArtifact(
            path=output,
            manifest=manifest,
            sha256=component_file_sha256(output),
            size_bytes=output.stat().st_size,
        )

    @staticmethod
    def _zip_info(name: str) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        return info

    @classmethod
    def _write_bytes(
        cls, archive: zipfile.ZipFile, name: str, value: bytes
    ) -> None:
        archive.writestr(cls._zip_info(name), value)

    @classmethod
    def _write_file(
        cls, archive: zipfile.ZipFile, name: str, source: Path
    ) -> None:
        info = cls._zip_info(name)
        info.file_size = source.stat().st_size
        with archive.open(info, "w", force_zip64=True) as destination:
            with source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, destination, length=1024 * 1024)


class ComponentUpdater:
    """Verify, atomically activate, and roll back optional components.

    ``components_root`` must be the user's selected
    ``StoryForgeData/components`` directory.  Releases are immutable.  One
    atomic ``state.json`` selects both the active and previous release, so an
    interrupted install never exposes a partially extracted component.
    """

    def __init__(
        self,
        components_root: str | Path,
        *,
        app_version: str = __version__,
    ) -> None:
        self.components_root = Path(components_root).expanduser().resolve(
            strict=False
        )
        self.app_version = _normalize_version(app_version, label="app version")
        self._lock = threading.RLock()

    @classmethod
    def inspect_package(
        cls,
        package_path: str | Path,
        *,
        app_version: str = __version__,
        expected_package_sha256: str | None = None,
    ) -> ComponentPackageInspection:
        path = Path(package_path).expanduser().resolve(strict=True)
        if not path.is_file() or path.suffix.casefold() != ".zip":
            raise ComponentPackageError("Component package must be an existing ZIP file.")
        archive_digest = component_file_sha256(path)
        if expected_package_sha256 not in (None, ""):
            expected = str(expected_package_sha256).strip().casefold()
            if not _SHA256_PATTERN.fullmatch(expected):
                raise ComponentIntegrityError("Expected package SHA-256 is invalid.")
            if archive_digest != expected:
                raise ComponentIntegrityError("Component package SHA-256 mismatch.")
        try:
            with zipfile.ZipFile(path, "r", allowZip64=True) as archive:
                manifest_entry, payload_entries, total_size = _archive_index(archive)
                manifest = _read_manifest(archive, manifest_entry)
                _validate_compatible(manifest, app_version)
                _verify_payload(archive, manifest, payload_entries)
        except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            raise ComponentPackageError("Component package ZIP is damaged.") from error
        if component_file_sha256(path) != archive_digest:
            raise ComponentIntegrityError(
                "Component package changed while it was being verified."
            )
        return ComponentPackageInspection(
            path=path,
            manifest=manifest,
            sha256=archive_digest,
            size_bytes=path.stat().st_size,
            uncompressed_size_bytes=total_size,
        )

    def install(
        self,
        package_path: str | Path,
        *,
        expected_package_sha256: str | None = None,
    ) -> InstalledComponent:
        inspection = self.inspect_package(
            package_path,
            app_version=self.app_version,
            expected_package_sha256=expected_package_sha256,
        )
        manifest = inspection.manifest
        with self._lock:
            self.components_root.mkdir(parents=True, exist_ok=True)
            component_dir = self._component_dir(manifest.component_id)
            releases_dir = component_dir / "releases"
            releases_dir.mkdir(parents=True, exist_ok=True)
            release_name = f"{manifest.version}-{inspection.sha256[:12]}"
            release_dir = releases_dir / release_name
            state_path = component_dir / COMPONENT_STATE_FILENAME
            old_state = self._load_state(component_dir, required=False)
            if (
                old_state is not None
                and old_state["active"]["release"] == release_name
            ):
                return self._installed_from_pointer(
                    component_dir, old_state["active"]
                )

            staged = Path(
                tempfile.mkdtemp(prefix=".staging-", dir=releases_dir)
            )
            created_release = False
            try:
                self._extract_verified(inspection, staged)
                if release_dir.exists():
                    self._verify_release(release_dir, manifest)
                    shutil.rmtree(staged)
                else:
                    os.replace(staged, release_dir)
                    created_release = True

                new_pointer = self._pointer(
                    release_name=release_name,
                    manifest=manifest,
                    package_sha256=inspection.sha256,
                )
                new_state = {
                    "schema_version": COMPONENT_MANIFEST_SCHEMA,
                    "active": new_pointer,
                    "previous": old_state["active"] if old_state is not None else None,
                }
                self._write_json_atomic(state_path, new_state)
            except BaseException:
                if created_release and not self._release_is_referenced(
                    component_dir, release_name
                ):
                    shutil.rmtree(release_dir, ignore_errors=True)
                raise
            finally:
                shutil.rmtree(staged, ignore_errors=True)

            self._cleanup_unreferenced_releases(component_dir)
            return self._installed_from_pointer(component_dir, new_pointer)

    def current(self, component_id: str) -> InstalledComponent | None:
        with self._lock:
            component_dir = self._component_dir(component_id)
            state = self._load_state(component_dir, required=False)
            if state is None:
                return None
            return self._installed_from_pointer(component_dir, state["active"])

    def list_installed(self) -> tuple[InstalledComponent, ...]:
        with self._lock:
            if not self.components_root.is_dir():
                return ()
            installed: list[InstalledComponent] = []
            for candidate in sorted(
                self.components_root.iterdir(), key=lambda item: item.name.casefold()
            ):
                if not candidate.is_dir() or candidate.name.startswith("."):
                    continue
                try:
                    component_id = _normalize_component_id(candidate.name)
                    current = self.current(component_id)
                except ComponentPackageError:
                    continue
                if current is not None:
                    installed.append(current)
            return tuple(installed)

    def activate_runtime(self) -> tuple[InstalledComponent, ...]:
        """Expose verified active payloads to this process and child workers.

        Installation never imports a package and never executes a component
        script.  Activation only adds the immutable, hash-verified payload
        roots to Python's normal import/resource search path.  The environment
        value is inherited by the disposable Kokoro child process.
        """

        global _ACTIVE_COMPONENT_PATHS
        installed = self.list_installed()
        for item in installed:
            self._verify_release(item.root.parent, item.manifest)
        roots = tuple(str(item.root.resolve(strict=True)) for item in installed)
        with _RUNTIME_ACTIVATION_LOCK:
            if _ACTIVE_COMPONENT_PATHS:
                sys.path[:] = [
                    entry
                    for entry in sys.path
                    if str(Path(entry).resolve(strict=False))
                    not in _ACTIVE_COMPONENT_PATHS
                ]
            # Insert in reverse so the stable component-id ordering is also
            # the effective Python import precedence.
            for root in reversed(roots):
                if root not in sys.path:
                    sys.path.insert(0, root)
            _ACTIVE_COMPONENT_PATHS = set(roots)
            os.environ["STORYFORGE_COMPONENT_PATHS"] = os.pathsep.join(roots)
            self._activate_windows_dll_directories(installed)
            importlib.invalidate_caches()
            try:
                from .tts_components import clear_kokoro_component_probe_cache

                clear_kokoro_component_probe_cache()
            except (ImportError, AttributeError):
                # Component activation is general-purpose; a minimal build
                # without the TTS probe module can still use other packs.
                pass
        return installed

    @staticmethod
    def _activate_windows_dll_directories(
        installed: tuple[InstalledComponent, ...],
    ) -> None:
        if os.name != "nt" or not hasattr(os, "add_dll_directory"):
            return
        directories: set[Path] = set()
        for component in installed:
            for declared in component.manifest.files:
                if Path(declared.path).suffix.casefold() not in {".dll", ".pyd"}:
                    continue
                directories.add(component.resolve(declared.path).parent)
        for directory in sorted(directories, key=lambda item: str(item).casefold()):
            resolved = str(directory.resolve(strict=True))
            if resolved in _ACTIVE_COMPONENT_DLL_PATHS:
                continue
            try:
                handle = os.add_dll_directory(resolved)
            except OSError:
                continue
            # The handle must remain alive for future imports.  Retaining a
            # previous release's directory is safer than invalidating a DLL
            # that Python may already have loaded before rollback.
            _ACTIVE_COMPONENT_DLL_HANDLES.append(handle)
            _ACTIVE_COMPONENT_DLL_PATHS.add(resolved)

    def rollback(
        self,
        component_id: str,
        *,
        activate: Callable[[], Any] | None = None,
    ) -> InstalledComponent:
        with self._lock:
            component_dir = self._component_dir(component_id)
            state = self._load_state(component_dir, required=True)
            previous = state.get("previous")
            if not isinstance(previous, Mapping):
                raise ComponentNotInstalledError(
                    f"No previous release is available for {component_id}."
                )
            # The pointer/manifest checks alone are not enough here: a previous
            # release may have been truncated by a disk fault or antivirus
            # quarantine after it was installed.  Verify every declared payload
            # byte before making that release active.
            restored = self._installed_from_pointer(component_dir, previous)
            self._verify_release(restored.root.parent, restored.manifest)
            state_path = component_dir / COMPONENT_STATE_FILENAME
            next_state = {
                "schema_version": COMPONENT_MANIFEST_SCHEMA,
                "active": dict(previous),
                "previous": dict(state["active"]),
            }
            self._write_json_atomic(state_path, next_state)
            if activate is not None:
                try:
                    activate()
                except BaseException as activation_error:
                    # Activation can still fail for a native-runtime or import
                    # reason that static payload verification cannot predict.
                    # Restore the exact prior state first, then reactivate it so
                    # a failed rollback never strands a previously working
                    # workstation on the candidate release.
                    self._write_json_atomic(state_path, state)
                    try:
                        activate()
                    except BaseException as recovery_error:
                        raise RuntimeError(
                            "Component rollback failed and the previous runtime "
                            f"could not be reactivated: {recovery_error}"
                        ) from activation_error
                    raise
            self._cleanup_unreferenced_releases(component_dir)
            return restored

    def resolve(
        self, component_id: str, relative_path: str | Path = ""
    ) -> Path:
        installed = self.current(component_id)
        if installed is None:
            raise ComponentNotInstalledError(
                f"Component {component_id} is not installed."
            )
        return installed.resolve(relative_path)

    def _component_dir(self, component_id: Any) -> Path:
        normalized = _normalize_component_id(component_id)
        candidate = (self.components_root / normalized).resolve(strict=False)
        try:
            candidate.relative_to(self.components_root)
        except ValueError as error:
            raise ComponentSecurityError("Component directory escapes its root.") from error
        return candidate

    def _extract_verified(
        self, inspection: ComponentPackageInspection, staged: Path
    ) -> None:
        payload_root = staged / COMPONENT_PAYLOAD_PREFIX
        payload_root.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(inspection.path, "r", allowZip64=True) as archive:
                manifest_entry, payload_entries, _ = _archive_index(archive)
                manifest = _read_manifest(archive, manifest_entry)
                if manifest != inspection.manifest:
                    raise ComponentIntegrityError(
                        "Component manifest changed after package inspection."
                    )
                declared = {
                    _path_comparison_key(item.path): item for item in manifest.files
                }
                archived = {
                    _path_comparison_key(path): path for path in payload_entries
                }
                if set(declared) != set(archived):
                    raise ComponentIntegrityError(
                        "Component payload changed after package inspection."
                    )
                for comparison_key, declared_file in declared.items():
                    entry = payload_entries[archived[comparison_key]]
                    destination = payload_root.joinpath(
                        *PurePosixPath(declared_file.path).parts
                    ).resolve(strict=False)
                    try:
                        destination.relative_to(payload_root.resolve(strict=False))
                    except ValueError as error:
                        raise ComponentSecurityError(
                            "Payload extraction escaped its staging directory."
                        ) from error
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    written = 0
                    with archive.open(entry, "r") as source_stream:
                        with destination.open("xb") as output_stream:
                            while True:
                                chunk = source_stream.read(1024 * 1024)
                                if not chunk:
                                    break
                                written += len(chunk)
                                if written > declared_file.size:
                                    raise ComponentIntegrityError(
                                        f"Payload size mismatch for {declared_file.path}."
                                    )
                                digest.update(chunk)
                                output_stream.write(chunk)
                    if written != declared_file.size:
                        raise ComponentIntegrityError(
                            f"Payload size mismatch for {declared_file.path}."
                        )
                    if digest.hexdigest() != declared_file.sha256:
                        raise ComponentIntegrityError(
                            f"Payload SHA-256 mismatch for {declared_file.path}."
                        )
                manifest_path = staged / COMPONENT_MANIFEST_FILENAME
                manifest_path.write_bytes(
                    _canonical_json_bytes(manifest.to_dict()) + b"\n"
                )
        except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            raise ComponentPackageError("Component package ZIP is damaged.") from error

    @staticmethod
    def _pointer(
        *,
        release_name: str,
        manifest: ComponentManifest,
        package_sha256: str,
    ) -> dict[str, Any]:
        return {
            "release": release_name,
            "component_id": manifest.component_id,
            "version": manifest.version,
            "package_sha256": package_sha256,
            "manifest_sha256": hashlib.sha256(
                _canonical_json_bytes(manifest.to_dict())
            ).hexdigest(),
            "installed_at": _utc_now(),
        }

    @staticmethod
    def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
        except BaseException:
            try:
                os.close(handle)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    def _load_state(
        self, component_dir: Path, *, required: bool
    ) -> dict[str, Any] | None:
        path = component_dir / COMPONENT_STATE_FILENAME
        if not path.is_file():
            if required:
                raise ComponentNotInstalledError(
                    f"Component {component_dir.name} is not installed."
                )
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ComponentIntegrityError(
                f"Component state is damaged for {component_dir.name}."
            ) from error
        if not isinstance(raw, Mapping):
            raise ComponentIntegrityError("Component state must be a JSON object.")
        if int(raw.get("schema_version") or 0) != COMPONENT_MANIFEST_SCHEMA:
            raise ComponentIntegrityError("Unsupported component state schema.")
        active = raw.get("active")
        if not isinstance(active, Mapping):
            raise ComponentIntegrityError("Component state has no active release.")
        self._validate_pointer(component_dir, active)
        previous = raw.get("previous")
        if previous is not None:
            if not isinstance(previous, Mapping):
                raise ComponentIntegrityError("Component previous release is invalid.")
            self._validate_pointer(component_dir, previous)
        return {
            "schema_version": COMPONENT_MANIFEST_SCHEMA,
            "active": dict(active),
            "previous": dict(previous) if isinstance(previous, Mapping) else None,
        }

    def _validate_pointer(
        self, component_dir: Path, pointer: Mapping[str, Any]
    ) -> None:
        release = _safe_relative_path(pointer.get("release"), label="release")
        if "/" in release:
            raise ComponentSecurityError("Component release must be one directory name.")
        component_id = _normalize_component_id(pointer.get("component_id"))
        if component_id != component_dir.name:
            raise ComponentIntegrityError("Component state identifier does not match its directory.")
        _normalize_version(pointer.get("version"), label="component version")
        for field in ("package_sha256", "manifest_sha256"):
            digest = str(pointer.get(field) or "").strip().casefold()
            if not _SHA256_PATTERN.fullmatch(digest):
                raise ComponentIntegrityError(f"Component state {field} is invalid.")

    def _installed_from_pointer(
        self, component_dir: Path, pointer: Mapping[str, Any]
    ) -> InstalledComponent:
        self._validate_pointer(component_dir, pointer)
        release_name = str(pointer["release"])
        release_dir = (component_dir / "releases" / release_name).resolve(
            strict=False
        )
        try:
            release_dir.relative_to((component_dir / "releases").resolve(strict=False))
        except ValueError as error:
            raise ComponentSecurityError("Component release escapes its directory.") from error
        manifest_path = release_dir / COMPONENT_MANIFEST_FILENAME
        try:
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ComponentIntegrityError("Installed component manifest is unavailable.") from error
        manifest = ComponentManifest.from_mapping(raw_manifest)
        _validate_compatible(manifest, self.app_version)
        if (
            manifest.component_id != pointer["component_id"]
            or manifest.version != pointer["version"]
            or hashlib.sha256(_canonical_json_bytes(manifest.to_dict())).hexdigest()
            != pointer["manifest_sha256"]
        ):
            raise ComponentIntegrityError(
                "Installed component manifest does not match its active state."
            )
        payload_root = release_dir / COMPONENT_PAYLOAD_PREFIX
        if not payload_root.is_dir():
            raise ComponentIntegrityError("Installed component payload is unavailable.")
        return InstalledComponent(
            component_id=manifest.component_id,
            version=manifest.version,
            root=payload_root,
            manifest=manifest,
            package_sha256=str(pointer["package_sha256"]),
        )

    @staticmethod
    def _verify_release(release_dir: Path, expected: ComponentManifest) -> None:
        manifest_path = release_dir / COMPONENT_MANIFEST_FILENAME
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ComponentIntegrityError("Existing component release is damaged.") from error
        installed = ComponentManifest.from_mapping(raw)
        if installed != expected:
            raise ComponentIntegrityError(
                "Existing component release conflicts with the package being installed."
            )
        payload_root = release_dir / COMPONENT_PAYLOAD_PREFIX
        for item in installed.files:
            candidate = payload_root.joinpath(*PurePosixPath(item.path).parts)
            if not candidate.is_file() or candidate.stat().st_size != item.size:
                raise ComponentIntegrityError(
                    f"Existing component payload is damaged: {item.path}."
                )
            if component_file_sha256(candidate) != item.sha256:
                raise ComponentIntegrityError(
                    f"Existing component payload is damaged: {item.path}."
                )

    def _release_is_referenced(self, component_dir: Path, release_name: str) -> bool:
        try:
            state = self._load_state(component_dir, required=False)
        except ComponentPackageError:
            return False
        if state is None:
            return False
        return any(
            isinstance(pointer, Mapping) and pointer.get("release") == release_name
            for pointer in (state.get("active"), state.get("previous"))
        )

    def _cleanup_unreferenced_releases(self, component_dir: Path) -> None:
        state = self._load_state(component_dir, required=True)
        retained = {
            str(pointer["release"])
            for pointer in (state.get("active"), state.get("previous"))
            if isinstance(pointer, Mapping)
        }
        releases_dir = component_dir / "releases"
        for candidate in releases_dir.iterdir():
            if candidate.name in retained or candidate.name.startswith(".staging-"):
                continue
            if candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate, ignore_errors=True)


class ComponentRepository:
    """Atomic multi-component publisher store owned by the Hub computer."""

    def __init__(
        self,
        root: str | Path,
        *,
        app_version: str = __version__,
    ) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        self.app_version = _normalize_version(app_version, label="app version")
        self.catalog_path = self.root / COMPONENT_CATALOG_FILENAME
        self._lock = threading.RLock()

    def get_catalog(self) -> dict[str, Any]:
        """Return only publications whose package still passes verification."""

        with self._lock:
            if not self.catalog_path.is_file():
                return validate_component_catalog(None)
            try:
                raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
                checked = validate_component_catalog(raw)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                return validate_component_catalog(None)
            available: list[dict[str, Any]] = []
            for publication in checked["components"]:
                try:
                    package = self.resolve_package(publication)
                    inspection = ComponentUpdater.inspect_package(
                        package,
                        app_version=self.app_version,
                        expected_package_sha256=publication["sha256"],
                    )
                    if package.stat().st_size != publication["size_bytes"]:
                        raise ComponentIntegrityError(
                            "Published component package size changed."
                        )
                    if inspection.manifest.to_dict() != publication["component_manifest"]:
                        raise ComponentIntegrityError(
                            "Published component manifest changed."
                        )
                except (OSError, ValueError, ComponentPackageError):
                    continue
                available.append(publication)
            return validate_component_catalog(
                {
                    "schema_version": COMPONENT_CATALOG_SCHEMA,
                    "components": available,
                }
            )

    def list_manifests(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self.get_catalog()["components"])

    def get_manifest(self, component_id: str) -> dict[str, Any] | None:
        normalized = _normalize_component_id(component_id)
        for publication in self.list_manifests():
            if publication["component_id"] == normalized:
                return publication
        return None

    def publish(
        self,
        package_path: str | Path,
        release_notes: str = "",
    ) -> dict[str, Any]:
        source = Path(package_path).expanduser().resolve(strict=True)
        notes = str(release_notes or "").strip()
        if len(notes) > MAX_COMPONENT_RELEASE_NOTES:
            raise ComponentRepositoryError("Component release notes are too long.")
        inspection = ComponentUpdater.inspect_package(
            source,
            app_version=self.app_version,
        )
        manifest = inspection.manifest
        filename = (
            f"{manifest.component_id}-{manifest.version}-"
            f"{inspection.sha256[:12]}.zip"
        )
        destination = (self.root / filename).resolve(strict=False)
        try:
            destination.relative_to(self.root)
        except ValueError as error:
            raise ComponentSecurityError(
                "Published component path escapes its repository."
            ) from error
        with self._lock:
            catalog = self.get_catalog()
            existing = next(
                (
                    item
                    for item in catalog["components"]
                    if item["component_id"] == manifest.component_id
                ),
                None,
            )
            if existing is not None and existing["version"] == manifest.version:
                if existing["sha256"] != inspection.sha256:
                    raise ComponentRepositoryError(
                        "The same component version cannot publish a different package."
                    )
                return dict(existing)

            # Keep the ZIP suffix because the structural verifier refuses a
            # misleading extension even for a private staging artifact.
            temporary = self.root / f".{filename}.{os.getpid()}.part.zip"
            try:
                shutil.copy2(source, temporary)
                if (
                    temporary.stat().st_size != inspection.size_bytes
                    or component_file_sha256(temporary) != inspection.sha256
                ):
                    raise ComponentIntegrityError(
                        "Component package copy failed verification."
                    )
                # Reinspect the repository copy before the catalog commit.
                ComponentUpdater.inspect_package(
                    temporary,
                    app_version=self.app_version,
                    expected_package_sha256=inspection.sha256,
                )
                os.replace(temporary, destination)
            finally:
                try:
                    temporary.unlink()
                except OSError:
                    pass

            publication = validate_component_publication(
                {
                    "schema_version": COMPONENT_CATALOG_SCHEMA,
                    "component_id": manifest.component_id,
                    "version": manifest.version,
                    "filename": filename,
                    "sha256": inspection.sha256,
                    "size_bytes": destination.stat().st_size,
                    "app_compatibility": manifest.app_compatibility,
                    "component_manifest": manifest.to_dict(),
                    "release_notes": notes,
                    "published_at": _utc_now(),
                }
            )
            remaining = [
                item
                for item in catalog["components"]
                if item["component_id"] != manifest.component_id
            ]
            next_catalog = validate_component_catalog(
                {
                    "schema_version": COMPONENT_CATALOG_SCHEMA,
                    "components": [*remaining, publication],
                }
            )
            ComponentUpdater._write_json_atomic(self.catalog_path, next_catalog)
            self._remove_unreferenced_packages(next_catalog)
            return publication

    def clear(self, component_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            catalog = self.get_catalog()
            if component_id in (None, ""):
                remaining: list[dict[str, Any]] = []
            else:
                normalized = _normalize_component_id(component_id)
                remaining = [
                    item
                    for item in catalog["components"]
                    if item["component_id"] != normalized
                ]
            next_catalog = validate_component_catalog(
                {
                    "schema_version": COMPONENT_CATALOG_SCHEMA,
                    "components": remaining,
                }
            )
            ComponentUpdater._write_json_atomic(self.catalog_path, next_catalog)
            # Clearing publication stops advertisement but preserves the ZIP,
            # mirroring the full updater's rollback-friendly behaviour.
            return next_catalog

    def resolve_package(self, publication: Mapping[str, Any]) -> Path:
        checked = validate_component_publication(publication)
        candidate = (self.root / checked["filename"]).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise ComponentSecurityError(
                "Published component path escapes its repository."
            ) from error
        if not candidate.is_file():
            raise FileNotFoundError("Published component package is unavailable.")
        return candidate

    def _remove_unreferenced_packages(self, catalog: Mapping[str, Any]) -> None:
        retained = {
            str(item["filename"])
            for item in catalog.get("components", [])
            if isinstance(item, Mapping)
        }
        for candidate in self.root.glob("*.zip"):
            if candidate.name in retained:
                continue
            try:
                candidate.unlink()
            except OSError:
                # An active download or antivirus scan may briefly hold it.
                pass


def activate_component_runtime_from_environment(
    data_root: str | Path | None,
    *,
    app_version: str = __version__,
) -> tuple[InstalledComponent, ...]:
    """Activate installed packs early in desktop and Kokoro child startup."""

    root_value = str(data_root or os.environ.get("STORYFORGE_DATA_DIR") or "").strip()
    if not root_value:
        # A source process can still inherit the exact verified roots from its
        # parent.  Do not accept arbitrary non-directory values.
        inherited = [
            Path(item).expanduser().resolve(strict=False)
            for item in str(os.environ.get("STORYFORGE_COMPONENT_PATHS") or "").split(
                os.pathsep
            )
            if item.strip()
        ]
        with _RUNTIME_ACTIVATION_LOCK:
            for path in reversed(inherited):
                if path.is_dir() and str(path) not in sys.path:
                    sys.path.insert(0, str(path))
            importlib.invalidate_caches()
        return ()
    updater = ComponentUpdater(Path(root_value) / "components", app_version=app_version)
    return updater.activate_runtime()


__all__ = [
    "COMPONENT_CATALOG_FILENAME",
    "COMPONENT_CATALOG_SCHEMA",
    "COMPONENT_MANIFEST_FILENAME",
    "COMPONENT_MANIFEST_SCHEMA",
    "COMPONENT_PAYLOAD_PREFIX",
    "ComponentCompatibilityError",
    "ComponentFile",
    "ComponentIntegrityError",
    "ComponentManifest",
    "ComponentNotInstalledError",
    "ComponentPackageArtifact",
    "ComponentPackageBuilder",
    "ComponentPackageError",
    "ComponentPackageInspection",
    "ComponentRepository",
    "ComponentRepositoryError",
    "ComponentSecurityError",
    "ComponentUpdater",
    "InstalledComponent",
    "activate_component_runtime_from_environment",
    "component_file_sha256",
    "sign_component_catalog",
    "validate_component_catalog",
    "validate_component_publication",
    "verify_component_catalog_signature",
]
