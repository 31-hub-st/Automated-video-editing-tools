"""Disposable embedded-Kokoro process used by the production worker."""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping, Sequence


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False), encoding="utf-8", newline="\n"
    )
    os.replace(temporary, path)


def run_request(request_path: str | Path, response_path: str | Path) -> int:
    request_file = Path(request_path).expanduser().resolve()
    response_file = Path(response_path).expanduser().resolve()
    try:
        raw = json.loads(request_file.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("request root must be an object")
        config_value = raw.get("config")
        if not isinstance(config_value, Mapping):
            raise ValueError("provider config is missing")
        sentences_value = raw.get("sentences")
        if not isinstance(sentences_value, Sequence) or isinstance(
            sentences_value, (str, bytes)
        ):
            raise ValueError("sentences must be a list")

        # This marker prevents create_tts_provider from recursively spawning a
        # second child if an integration later routes construction through it.
        os.environ["STORYFORGE_KOKORO_CHILD"] = "1"
        from .providers.base import ProviderConfig
        from .providers.tts import EmbeddedKokoroProvider

        config = ProviderConfig(
            name=str(config_value.get("name") or "local_kokoro"),
            model=str(config_value.get("model") or "kokoro"),
            timeout_seconds=float(config_value.get("timeout_seconds") or 90.0),
            options=(
                dict(config_value.get("options"))
                if isinstance(config_value.get("options"), Mapping)
                else {}
            ),
        )
        provider = EmbeddedKokoroProvider(config)
        result = provider.synthesize(
            [str(item) for item in sentences_value],
            str(raw.get("output_dir") or ""),
            voice=str(raw.get("voice") or ""),
            speed=float(raw.get("speed") or 1.0),
            file_stem=str(raw.get("file_stem") or "sentence"),
        )
        _atomic_json(response_file, {"ok": True, "result": result.to_dict()})
        return 0
    except BaseException as error:  # the parent turns this into a safe provider error
        try:
            _atomic_json(
                response_file,
                {
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                    "traceback": traceback.format_exc()[-8000:],
                },
            )
        except OSError:
            pass
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        return 2
    return run_request(arguments[0], arguments[1])


if __name__ == "__main__":
    raise SystemExit(main())
