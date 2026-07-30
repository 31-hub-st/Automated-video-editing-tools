from __future__ import annotations

import argparse
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
