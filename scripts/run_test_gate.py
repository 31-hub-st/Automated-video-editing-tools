from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


RAN_PATTERN = re.compile(r"Ran\s+(?P<count>\d+)\s+tests?\s+in\s+(?P<seconds>[\d.]+)s")
COUNT_PATTERN = re.compile(r"(?P<name>failures|errors|skipped)=(?P<count>\d+)")


def _subprocess_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def parse_unittest_output(output: str, *, exit_code: int) -> dict[str, Any]:
    ran_matches = list(RAN_PATTERN.finditer(output))
    tests_run = int(ran_matches[-1].group("count")) if ran_matches else None
    unittest_seconds = (
        float(ran_matches[-1].group("seconds")) if ran_matches else None
    )
    counts = {"failures": 0, "errors": 0, "skipped": 0}
    result_line = ""
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped.startswith("FAILED") or stripped.startswith("OK"):
            result_line = stripped
            for match in COUNT_PATTERN.finditer(stripped):
                counts[match.group("name")] = int(match.group("count"))
            break
    return {
        "ok": exit_code == 0,
        "exit_code": int(exit_code),
        "tests_run": tests_run,
        "unittest_seconds": unittest_seconds,
        "result": result_line or "unittest did not emit a final result line",
        **counts,
    }


def _markdown_summary(payload: dict[str, Any]) -> str:
    status = "PASS" if payload["ok"] else "FAIL"
    tests_run = payload.get("tests_run")
    tests_text = str(tests_run) if tests_run is not None else "unknown"
    lines = [
        f"# {payload['name']} test gate",
        "",
        f"- Status: **{status}**",
        f"- Tests: **{tests_text}**",
        f"- Failures: **{payload['failures']}**",
        f"- Errors: **{payload['errors']}**",
        f"- Skipped: **{payload['skipped']}**",
        f"- Wall time: **{payload['duration_seconds']} s**",
        f"- Result: `{payload['result']}`",
        f"- Python: `{payload['python']}`",
        f"- Platform: `{payload['platform']}`",
        "",
        f"Command: `{' '.join(payload['command'])}`",
        "",
    ]
    return "\n".join(lines)


def _append_github_summary(markdown: str) -> None:
    destination = str(os.environ.get("GITHUB_STEP_SUMMARY") or "").strip()
    if not destination:
        return
    with Path(destination).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(markdown)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run unittest with console tee and machine-readable summaries."
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("unittest_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    unittest_args = list(args.unittest_args)
    if unittest_args and unittest_args[0] == "--":
        unittest_args.pop(0)
    if not unittest_args:
        unittest_args = ["discover", "-s", "tests", "-p", "test_*.py"]

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "unittest", *unittest_args]
    started_utc = datetime.now(UTC)
    started = time.perf_counter()
    lines: list[str] = []
    process = subprocess.Popen(
        command,
        env=_subprocess_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    exit_code = process.wait()
    finished_utc = datetime.now(UTC)
    duration = round(time.perf_counter() - started, 3)
    output = "".join(lines)
    (output_dir / "unittest.log").write_text(output, encoding="utf-8")

    payload = {
        "schema_version": 1,
        "name": str(args.name),
        "started_utc": started_utc.isoformat(),
        "finished_utc": finished_utc.isoformat(),
        "duration_seconds": duration,
        "command": command,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "git_sha": str(os.environ.get("GITHUB_SHA") or ""),
        **parse_unittest_output(output, exit_code=exit_code),
    }
    summary_json = output_dir / "summary.json"
    summary_markdown = output_dir / "summary.md"
    summary_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown = _markdown_summary(payload)
    summary_markdown.write_text(markdown, encoding="utf-8")
    _append_github_summary(markdown)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
