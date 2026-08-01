from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


TTS_COMPONENT_MANIFEST_SCHEMA = 1

# These are runtime imports performed by Misaki/Kokoro for languages whose
# grapheme-to-phoneme stack is not part of the small English core.  Keep the
# inventory independent from the provider implementation so the desktop app,
# the disposable Kokoro child and future language-pack installers can use the
# same contract.
KOKORO_LANGUAGE_COMPONENTS: dict[str, dict[str, Any]] = {
    "j": {
        "component_id": "kokoro.language.ja",
        "language": "ja",
        "install_requirement": "pyopenjtalk-plus==0.4.1.post8, fugashi, jaconv, mojimoji, unidic-lite",
        "modules": (
            "pyopenjtalk",
            "fugashi",
            "jaconv",
            "mojimoji",
            "unidic_lite",
        ),
        "resources": {
            "unidic_lite": (
                "dicdir/version",
                "dicdir/sys.dic",
                "dicdir/matrix.bin",
                "dicdir/char.bin",
                "dicdir/unk.dic",
            )
        },
    },
    "z": {
        "component_id": "kokoro.language.zh",
        "language": "zh",
        "install_requirement": "misaki[zh]",
        "modules": (
            "jieba",
            "ordered_set",
            "pypinyin",
            "cn2an",
            "pypinyin_dict",
        ),
        "resources": {},
    },
}


@dataclass(frozen=True, slots=True)
class TTSComponentIssue:
    code: str
    component_id: str
    subject: str
    message: str
    remediation: str = ""
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TTSComponentHealth:
    component_id: str
    language_code: str
    ready: bool
    issues: tuple[TTSComponentIssue, ...] = ()
    manifest_schema: int = TTS_COMPONENT_MANIFEST_SCHEMA

    @property
    def error_code(self) -> str:
        return self.issues[0].code if self.issues else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_schema": self.manifest_schema,
            "component_id": self.component_id,
            "language_code": self.language_code,
            "ready": self.ready,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def kokoro_component_manifest() -> dict[str, Any]:
    """Return the small, serializable component inventory used by releases."""

    language_packs: list[dict[str, Any]] = []
    for value in KOKORO_LANGUAGE_COMPONENTS.values():
        language_packs.append(
            {
                "component_id": value["component_id"],
                "language": value["language"],
                "install_requirement": value["install_requirement"],
                "modules": list(value["modules"]),
                "resources": {
                    package: list(paths)
                    for package, paths in dict(value["resources"]).items()
                },
            }
        )
    return {
        "schema": TTS_COMPONENT_MANIFEST_SCHEMA,
        "engine": {
            "component_id": "kokoro.engine",
            "provider": "local_kokoro",
        },
        "language_packs": language_packs,
    }


def _module_root(spec: object) -> Path | None:
    locations = getattr(spec, "submodule_search_locations", None)
    if locations:
        for value in locations:
            if value:
                return Path(str(value)).expanduser().resolve()
    origin = str(getattr(spec, "origin", "") or "").strip()
    if origin and origin not in {"built-in", "frozen"}:
        return Path(origin).expanduser().resolve().parent
    return None


def kokoro_language_component_health(
    lang_code: str,
    *,
    module_finder: Callable[[str], object | None] | None = None,
) -> TTSComponentHealth:
    """Validate a Kokoro language pack without importing its native stack.

    Import-only checks were insufficient for ``unidic_lite``: PyInstaller
    could freeze ``__init__.py`` while omitting ``dicdir/version`` and the large
    dictionary payload.  This probe verifies every file opened by the Japanese
    tokenizer before the worker launches a memory-heavy model process.
    """

    normalized = str(lang_code or "").strip().casefold()
    definition = KOKORO_LANGUAGE_COMPONENTS.get(normalized)
    if definition is None:
        return TTSComponentHealth(
            component_id=f"kokoro.language.{normalized or 'default'}",
            language_code=normalized,
            ready=True,
        )

    finder = module_finder or importlib.util.find_spec
    component_id = str(definition["component_id"])
    remediation = (
        "请安装/更新包含该语言包的 StoryForge 完整版；源码环境运行 "
        "python -m pip install -r requirements-ai.txt。"
    )
    specs: dict[str, object] = {}
    issues: list[TTSComponentIssue] = []
    for module in tuple(definition["modules"]):
        try:
            spec = finder(module)
        except (ImportError, ModuleNotFoundError, ValueError, OSError):
            spec = None
        if spec is None:
            issues.append(
                TTSComponentIssue(
                    code="tts_component_module_missing",
                    component_id=component_id,
                    subject=module,
                    message=f"缺少配音语言组件模块：{module}",
                    remediation=remediation,
                )
            )
        else:
            specs[module] = spec

    resources: Mapping[str, tuple[str, ...]] = definition.get("resources", {})
    for package, relative_paths in resources.items():
        spec = specs.get(package)
        if spec is None:
            continue
        root = _module_root(spec)
        if root is None:
            issues.append(
                TTSComponentIssue(
                    code="tts_component_resource_root_missing",
                    component_id=component_id,
                    subject=package,
                    message=f"无法定位配音语言组件资源目录：{package}",
                    remediation=remediation,
                )
            )
            continue
        for relative_path in relative_paths:
            path = root.joinpath(*relative_path.split("/"))
            try:
                valid = path.is_file() and path.stat().st_size > 0
            except OSError:
                valid = False
            if valid:
                continue
            issues.append(
                TTSComponentIssue(
                    code="tts_component_resource_missing",
                    component_id=component_id,
                    subject=f"{package}:{relative_path}",
                    message=f"配音语言组件资源缺失或为空：{relative_path}",
                    remediation=remediation,
                    path=str(path),
                )
            )

    return TTSComponentHealth(
        component_id=component_id,
        language_code=normalized,
        ready=not issues,
        issues=tuple(issues),
    )


def _probe_kokoro_language_runtime_uncached(
    lang_code: str,
    *,
    module_finder: Callable[[str], object | None] | None = None,
    module_importer: Callable[[str], Any] | None = None,
) -> TTSComponentHealth:
    """Run a small real G2P probe after the non-importing resource check."""

    health = kokoro_language_component_health(
        lang_code,
        module_finder=module_finder,
    )
    if not health.ready or health.language_code != "j":
        return health
    importer = module_importer or importlib.import_module
    try:
        module = importer("misaki.ja")
        factory = getattr(module, "JAG2P")
        phonemes, _tokens = factory()("電話が鳴った。")
        if not str(phonemes or "").strip():
            raise RuntimeError("Japanese G2P returned no phonemes")
    except Exception as error:
        issue = TTSComponentIssue(
            code="tts_component_runtime_probe_failed",
            component_id=health.component_id,
            subject="misaki.ja.JAG2P",
            message=(
                "日语配音组件已找到，但实际分词/音素转换测试失败："
                f"{type(error).__name__}: {error}"
            ),
            remediation=(
                "请更新 StoryForge 完整版；源码环境重新安装 requirements-ai.txt。"
            ),
        )
        return TTSComponentHealth(
            component_id=health.component_id,
            language_code=health.language_code,
            ready=False,
            issues=(issue,),
        )
    return health


@lru_cache(maxsize=8)
def _probe_default_kokoro_language_runtime(lang_code: str) -> TTSComponentHealth:
    return _probe_kokoro_language_runtime_uncached(lang_code)


def probe_kokoro_language_runtime(
    lang_code: str,
    *,
    module_finder: Callable[[str], object | None] | None = None,
    module_importer: Callable[[str], Any] | None = None,
) -> TTSComponentHealth:
    """Run and cache the real default probe once per worker process.

    Injected finders/importers deliberately bypass the cache so tests and
    deployment diagnostics can inspect a synthetic package layout reliably.
    """

    normalized = str(lang_code or "").strip().casefold()
    if module_finder is None and module_importer is None:
        return _probe_default_kokoro_language_runtime(normalized)
    return _probe_kokoro_language_runtime_uncached(
        normalized,
        module_finder=module_finder,
        module_importer=module_importer,
    )


def clear_kokoro_component_probe_cache() -> None:
    """Forget absence/readiness cached before a component was activated."""

    _probe_default_kokoro_language_runtime.cache_clear()


__all__ = [
    "KOKORO_LANGUAGE_COMPONENTS",
    "TTS_COMPONENT_MANIFEST_SCHEMA",
    "TTSComponentHealth",
    "TTSComponentIssue",
    "clear_kokoro_component_probe_cache",
    "kokoro_component_manifest",
    "kokoro_language_component_health",
    "probe_kokoro_language_runtime",
]
