from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scripts.build_update_package import (  # noqa: E402
    FROZEN_BUILD_VALIDATION,
    FROZEN_RELEASE_VALIDATION,
    _verify_release_validation,
    file_sha256,
)
from storyforge import __version__  # noqa: E402


UI_FILES = ("index.html", "app.js", "styles.css", "studio-theme.css")
STABLE_ACCEPTANCE_REPORT = "BUILD_STABILITY_ACCEPTANCE.json"
RECOVERY_PAYLOAD_FILES = (
    "一键恢复StoryForge-Hub.cmd",
    "Restore-StoryForge-Hub.cmd",
    "scripts/restore_storyforge_hub_new_machine.ps1",
    "scripts/bootstrap_storyforge.ps1",
    "scripts/verify_storyforge_deployment.ps1",
)


class PackageSmokeError(RuntimeError):
    """The frozen release package does not satisfy its smoke contract."""


def _json_for_console(payload: dict[str, Any]) -> str:
    # GitHub's Windows runner can expose a legacy stdout code page (for example
    # cp1252). JSON escapes preserve Unicode exactly while remaining ASCII-safe.
    return json.dumps(payload, ensure_ascii=True, indent=2)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise PackageSmokeError(f"{label} is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageSmokeError(f"{label} is not valid UTF-8 JSON: {path}") from error
    if not isinstance(payload, dict):
        raise PackageSmokeError(f"{label} must contain a JSON object: {path}")
    return payload


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _find_ui_root(package_root: Path) -> Path:
    candidates = (package_root / "ui", package_root / "_internal" / "ui")
    for candidate in candidates:
        if all((candidate / name).is_file() for name in UI_FILES):
            return candidate.resolve()
    expected = ", ".join(str(candidate) for candidate in candidates)
    raise PackageSmokeError(f"Packaged UI files were not found under: {expected}")


def _validate_ui_files(ui_root: Path, package_root: Path) -> list[dict[str, Any]]:
    resolved_root = package_root.resolve()
    resolved_ui = ui_root.resolve()
    if not _path_is_within(resolved_ui, resolved_root):
        raise PackageSmokeError(f"Packaged UI root escapes the package: {resolved_ui}")
    files: list[dict[str, Any]] = []
    for name in UI_FILES:
        path = resolved_ui / name
        if not path.is_file() or path.stat().st_size <= 0:
            raise PackageSmokeError(f"Packaged UI asset is missing or empty: {path}")
        files.append({"name": name, "bytes": path.stat().st_size})
    return files


def _validate_recovery_payload(package_root: Path) -> list[dict[str, Any]]:
    payload: dict[str, bytes] = {}
    files: list[dict[str, Any]] = []
    for relative in RECOVERY_PAYLOAD_FILES:
        path = package_root / Path(relative)
        if not path.is_file() or path.stat().st_size <= 0:
            raise PackageSmokeError(
                f"One-click Hub recovery payload is missing or empty: {path}"
            )
        raw = path.read_bytes()
        payload[relative] = raw
        files.append({"name": relative, "bytes": len(raw)})

    chinese_name, stable_name, restore_name, bootstrap_name, verify_name = (
        RECOVERY_PAYLOAD_FILES
    )
    try:
        chinese_launcher = payload[chinese_name].decode("ascii")
        stable_launcher = payload[stable_name].decode("ascii")
        payload[bootstrap_name].decode("ascii")
        payload[verify_name].decode("ascii")
    except UnicodeDecodeError as error:
        raise PackageSmokeError(
            "Recovery launchers and ASCII PowerShell helpers must be ASCII-safe."
        ) from error

    if 'Restore-StoryForge-Hub.cmd' not in chinese_launcher:
        raise PackageSmokeError(
            "Chinese recovery launcher does not delegate to the stable ASCII launcher."
        )
    for required in (
        "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
        "-ExecutionPolicy Bypass",
        "%~dp0scripts\\restore_storyforge_hub_new_machine.ps1",
    ):
        if required not in stable_launcher:
            raise PackageSmokeError(
                "Stable recovery launcher is missing its PowerShell 5.1 delegation contract."
            )

    restore_raw = payload[restore_name]
    if not restore_raw.startswith(b"\xef\xbb\xbf"):
        raise PackageSmokeError(
            "Chinese recovery PowerShell script must use a UTF-8 BOM for Windows PowerShell 5.1."
        )
    try:
        restore_script = restore_raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PackageSmokeError(
            "Chinese recovery PowerShell script is not valid UTF-8."
        ) from error
    for dependency in ("bootstrap_storyforge.ps1", "verify_storyforge_deployment.ps1"):
        if dependency not in restore_script:
            raise PackageSmokeError(
                f"Recovery PowerShell script does not delegate to {dependency}."
            )
    return files


def _validate_startup_payload(
    payload: dict[str, Any],
    *,
    package_root: Path,
    expected_version: str,
) -> dict[str, Any]:
    if payload.get("ok") is not True or payload.get("status") != "passed":
        raise PackageSmokeError(
            "Frozen startup self-test did not pass: "
            f"{payload.get('error') or payload.get('status') or 'unknown error'}"
        )
    if payload.get("frozen") is not True:
        raise PackageSmokeError("Startup self-test did not run from a frozen executable.")
    if str(payload.get("app_version") or "") != expected_version:
        raise PackageSmokeError(
            "Frozen application version does not match the release: "
            f"{payload.get('app_version')!r} != {expected_version!r}"
        )
    if payload.get("pythonnet_bridge_loaded") is not True:
        raise PackageSmokeError("Frozen Python.NET/WebView imports were not completed.")

    ui_root = Path(str(payload.get("ui_root") or ""))
    ui_files = _validate_ui_files(ui_root, package_root)
    ffmpeg_path = Path(str(payload.get("ffmpeg_path") or ""))
    if not ffmpeg_path.is_file() or ffmpeg_path.stat().st_size <= 0:
        raise PackageSmokeError(
            f"Frozen startup self-test did not resolve a usable FFmpeg: {ffmpeg_path}"
        )
    if not _path_is_within(ffmpeg_path.resolve(), package_root.resolve()):
        raise PackageSmokeError(
            f"Release smoke test resolved FFmpeg outside the package: {ffmpeg_path}"
        )
    return {
        "version": expected_version,
        "imports": {
            "status": "passed",
            "pythonnet_bridge_loaded": True,
            "webview_version": str(payload.get("webview_version") or ""),
        },
        "ui": {"status": "passed", "root": str(ui_root.resolve()), "files": ui_files},
        "ffmpeg": {
            "status": "passed",
            "path": str(ffmpeg_path.resolve()),
            "bytes": ffmpeg_path.stat().st_size,
        },
        "worker": {
            "status": "passed",
            "ready": bool(payload.get("worker_ready")),
            "url": str(payload.get("worker_url") or ""),
        },
    }


def _probe_ffmpeg(
    ffmpeg_path: Path,
    *,
    package_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    command = [str(ffmpeg_path), "-hide_banner", "-version"]
    try:
        process = subprocess.run(
            command,
            cwd=package_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, min(int(timeout_seconds), 30)),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise PackageSmokeError("Bundled FFmpeg version probe timed out.") from error
    except OSError as error:
        raise PackageSmokeError(
            f"Bundled FFmpeg could not be executed: {ffmpeg_path}"
        ) from error

    output = "\n".join(part for part in (process.stdout, process.stderr) if part)
    version_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if process.returncode != 0:
        raise PackageSmokeError(
            f"Bundled FFmpeg version probe exited with code {process.returncode}."
        )
    if not version_line:
        raise PackageSmokeError("Bundled FFmpeg version probe returned no version text.")
    return {
        "status": "passed",
        "path": str(ffmpeg_path.resolve()),
        "bytes": ffmpeg_path.stat().st_size,
        "probe_command": command[1:],
        "probe_exit_code": process.returncode,
        "version_line": version_line[:500],
    }


def _validate_stable_acceptance(
    path: Path,
    *,
    entrypoint: Path,
    expected_version: str,
) -> dict[str, Any]:
    payload = _read_json(path, label="Stable acceptance report")
    if payload.get("ok") is not True or payload.get("stable_release_eligible") is not True:
        raise PackageSmokeError("Stable acceptance did not approve this release package.")
    if payload.get("storyforge_version") != expected_version:
        raise PackageSmokeError("Stable acceptance version does not match the package.")
    if payload.get("code_under_test") != "frozen_executable_pipeline_runner":
        raise PackageSmokeError("Stable acceptance did not execute the frozen pipeline.")
    if payload.get("package_artifact_bound") is not True:
        raise PackageSmokeError("Stable acceptance is not bound to the package executable.")
    release_gate = payload.get("release_gate")
    if not isinstance(release_gate, dict) or release_gate.get(
        "frozen_executable_pipeline_executed"
    ) is not True:
        raise PackageSmokeError("Stable acceptance release gate is incomplete.")
    package = payload.get("package")
    if not isinstance(package, dict):
        raise PackageSmokeError("Stable acceptance package identity is missing.")
    if str(package.get("sha256") or "").casefold() != file_sha256(entrypoint):
        raise PackageSmokeError("Stable acceptance executable SHA-256 does not match.")
    if int(package.get("bytes") or 0) != entrypoint.stat().st_size:
        raise PackageSmokeError("Stable acceptance executable size does not match.")
    return {
        "status": "passed",
        "report": str(path),
        "verdict": str(payload.get("verdict") or ""),
        "scenario_count": len(payload.get("scenarios") or []),
    }


def run_package_smoke(
    *,
    package_root: Path,
    expected_version: str,
    require_stable_acceptance: bool,
    skip_runtime_reason: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    started = datetime.now(UTC)
    root = package_root.expanduser().resolve(strict=True)
    entrypoint = root / "StoryForge Studio.exe"
    if not entrypoint.is_file() or entrypoint.stat().st_size <= 0:
        raise PackageSmokeError(f"Frozen entrypoint is missing or empty: {entrypoint}")

    ui_root = _find_ui_root(root)
    packaged_ui = _validate_ui_files(ui_root, root)
    recovery_payload = _validate_recovery_payload(root)
    startup_validation = _read_json(
        root / FROZEN_BUILD_VALIDATION,
        label="Build startup validation",
    )
    if startup_validation.get("ok") is not True:
        raise PackageSmokeError("Build startup validation is not a passed result.")
    if startup_validation.get("frozen") is not True:
        raise PackageSmokeError("Build startup validation is not frozen.")
    if startup_validation.get("app_version") != expected_version:
        raise PackageSmokeError("Build startup validation version does not match.")

    _verify_release_validation(
        root,
        entrypoint=entrypoint.name,
        requested_version=expected_version,
    )
    stable = {"status": "not_required"}
    if require_stable_acceptance:
        stable = _validate_stable_acceptance(
            root / STABLE_ACCEPTANCE_REPORT,
            entrypoint=entrypoint,
            expected_version=expected_version,
        )

    skip_reason = str(skip_runtime_reason or "").strip()
    if require_stable_acceptance and skip_reason:
        raise PackageSmokeError("A stable release cannot skip the frozen runtime smoke test.")

    runtime: dict[str, Any]
    if skip_reason:
        runtime = {
            "status": "skipped",
            "reason": skip_reason,
            "imports": {"status": "skipped", "reason": skip_reason},
            "ui": {
                "status": "passed",
                "root": str(ui_root),
                "files": packaged_ui,
            },
            "ffmpeg": {"status": "skipped", "reason": skip_reason},
        }
    else:
        if os.name != "nt":
            raise PackageSmokeError(
                "Frozen runtime smoke requires Windows. For metadata-only diagnostics, "
                "provide --skip-runtime-reason with a concrete reason."
            )
        with tempfile.TemporaryDirectory(prefix="storyforge-package-smoke-") as temporary:
            temporary_root = Path(temporary)
            data_root = temporary_root / "data"
            result_root = temporary_root / "result"
            data_root.mkdir()
            result_root.mkdir()
            environment = os.environ.copy()
            # The smoke executable must prove the employee/standalone package
            # path, regardless of whether this gate was launched from a Hub
            # administration shell. A copied Hub identity could otherwise
            # authorize the temporary DataRoot and make the gate pass without
            # exercising portable startup.
            for name in (
                "STORYFORGE_DEPLOYMENT_ROLE",
                "STORYFORGE_FROZEN_HUB_DATA_ROOT",
                "STORYFORGE_PORTABLE_MODE",
            ):
                environment.pop(name, None)
            environment["STORYFORGE_DATA_DIR"] = str(data_root)
            try:
                process = subprocess.run(
                    [str(entrypoint), "--startup-self-test", str(result_root)],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise PackageSmokeError(
                    f"Frozen runtime smoke timed out after {timeout_seconds} seconds."
                ) from error
            runtime_payload = _read_json(
                result_root / "startup-self-test.json",
                label="Fresh package startup self-test",
            )
            if process.returncode != 0:
                raise PackageSmokeError(
                    "Frozen runtime smoke exited with code "
                    f"{process.returncode}: {runtime_payload.get('error') or 'no error text'}"
                )
            validated_runtime = _validate_startup_payload(
                runtime_payload,
                package_root=root,
                expected_version=expected_version,
            )
            validated_runtime["ffmpeg"] = _probe_ffmpeg(
                Path(validated_runtime["ffmpeg"]["path"]),
                package_root=root,
                timeout_seconds=timeout_seconds,
            )
            runtime = {
                "status": "passed",
                "exit_code": process.returncode,
                **validated_runtime,
            }

    finished = datetime.now(UTC)
    return {
        "schema_version": 1,
        "ok": True,
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "expected_version": expected_version,
        "package_root": str(root),
        "entrypoint": str(entrypoint),
        "entrypoint_sha256": file_sha256(entrypoint),
        "entrypoint_bytes": entrypoint.stat().st_size,
        "release_validation": {
            "status": "passed",
            "path": str(root / FROZEN_RELEASE_VALIDATION),
        },
        "recovery": {"status": "passed", "files": recovery_payload},
        "stable_acceptance": stable,
        "runtime": runtime,
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smoke-test an exact frozen StoryForge onedir package."
    )
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--expected-version", default=__version__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--require-stable-acceptance", action="store_true")
    parser.add_argument(
        "--skip-runtime-reason",
        default="",
        help="Explicit reason for a metadata-only diagnostic; forbidden for stable releases.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=180)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report_path = args.report.expanduser().resolve()
    package_root = args.package_root.expanduser().resolve()
    if _path_is_within(report_path, package_root):
        print(
            "Package smoke report must be outside the attested package directory.",
            file=sys.stderr,
        )
        return 2
    try:
        payload = run_package_smoke(
            package_root=package_root,
            expected_version=str(args.expected_version).strip(),
            require_stable_acceptance=bool(args.require_stable_acceptance),
            skip_runtime_reason=str(args.skip_runtime_reason),
            timeout_seconds=max(1, int(args.timeout_seconds)),
        )
    except BaseException as error:
        payload = {
            "schema_version": 1,
            "ok": False,
            "finished_utc": datetime.now(UTC).isoformat(),
            "expected_version": str(args.expected_version).strip(),
            "package_root": str(package_root),
            "error_type": type(error).__name__,
            "error": str(error) or type(error).__name__,
        }
    _write_report(report_path, payload)
    print(_json_for_console(payload))
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
