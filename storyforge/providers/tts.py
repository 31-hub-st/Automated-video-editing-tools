from __future__ import annotations

import asyncio
import base64
import gc
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import wave
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from ..cancellation import (
    JobCancelledError,
    raise_if_cancelled,
    run_cancellable_process,
)
from .base import (
    HTTPTransport,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderError,
    ProviderResponseError,
    UrllibTransport,
    coerce_provider_config,
    ensure_http_success,
    json_request_body,
    perform_request,
    require_api_key,
)


_SENTENCE_RE = re.compile(r"(?<=[.!?])(?:[\"'\N{RIGHT SINGLE QUOTATION MARK}]*)\s+")
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_TTS_CACHE_SCHEMA = "storyforge-tts-wav-v1"
_TTS_CACHE_LOCKS: dict[str, threading.RLock] = {}
_TTS_CACHE_LOCKS_GUARD = threading.Lock()
_TTS_CACHE_DEFAULT_MAX_BYTES = 5 * 1024 * 1024 * 1024
_TTS_CACHE_DEFAULT_MAX_AGE_DAYS = 30.0
_TTS_CACHE_PRUNE_INTERVAL_SECONDS = 15 * 60
_TTS_CACHE_PRUNE_LOCK = threading.Lock()
_TTS_CACHE_LAST_PRUNED: dict[str, float] = {}
_KOKORO_REPO_ID = "hexgrad/Kokoro-82M"
_KOKORO_MODEL_FILES = ("config.json", "kokoro-v1_0.pth")
_KOKORO_VOICE_IDS = ("af_heart", "af_bella", "af_nicole", "af_sarah")
_EDGE_PROVIDER_ALIASES = frozenset(
    {"edge", "edge_tts", "microsoft_edge", "microsoft_edge_tts"}
)
_EDGE_LOCALE_PREFERENCES: dict[str, tuple[str, ...]] = {
    "en": ("en-US",),
    "en-gb": ("en-GB",),
    "ja": ("ja-JP",),
    "es": ("es-ES",),
    "fr": ("fr-FR",),
    "de": ("de-DE",),
    "id": ("id-ID",),
    "ko": ("ko-KR",),
    "it": ("it-IT",),
    "pt-br": ("pt-BR",),
    "hi": ("hi-IN",),
    "zh": ("zh-CN",),
}
_EDGE_VOICE_CACHE: dict[str, tuple[float, tuple["TTSVoiceOption", ...]]] = {}
_EDGE_VOICE_CACHE_LOCK = threading.RLock()
_EDGE_VOICE_CACHE_SECONDS = 6 * 60 * 60
_EDGE_EMPTY_CACHE_SECONDS = 20


class _NullConsoleStream:
    """A tiny text stream for windowed/frozen processes without a console.

    PyInstaller's ``--windowed`` mode deliberately sets ``sys.stdout`` and
    ``sys.stderr`` to ``None``.  Kokoro 0.9.4 configures Loguru at import time
    with ``logger.add(sys.stderr)``; importing it in that environment therefore
    crashes before any model or voice is inspected.  Keep one process-lifetime
    sink because Loguru retains the object after the import completes.
    """

    encoding = "utf-8"

    @staticmethod
    def write(value: object) -> int:
        return len(str(value or ""))

    @staticmethod
    def flush() -> None:
        return None

    @staticmethod
    def isatty() -> bool:
        return False


_KOKORO_IMPORT_STREAM = _NullConsoleStream()
_KOKORO_IMPORT_LOCK = threading.RLock()


def _import_kokoro_runtime() -> tuple[Any, Any]:
    """Import Kokoro safely in both console and windowed frozen builds."""

    with _KOKORO_IMPORT_LOCK:
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        if original_stdout is None:
            sys.stdout = _KOKORO_IMPORT_STREAM
        if original_stderr is None:
            sys.stderr = _KOKORO_IMPORT_STREAM
        try:
            from kokoro import KModel, KPipeline
        finally:
            # Do not make StoryForge appear to have a console after the import.
            # Kokoro/Loguru already holds its safe sink object at this point.
            sys.stdout = original_stdout
            sys.stderr = original_stderr
    return KModel, KPipeline


@dataclass(frozen=True, slots=True)
class TTSVoiceOption:
    """One selectable female voice exposed by StoryForge.

    ``profile`` is a provider-neutral delivery hint.  It lets the production
    desk keep its existing mood workflow while using language-specific voice
    identifiers underneath.
    """

    voice_id: str
    label: str
    profile: str


_TTS_LANGUAGE_ALIASES = {
    "a": "en",
    "en": "en",
    "en-us": "en",
    "en_us": "en",
    "english": "en",
    "american english": "en",
    "b": "en-gb",
    "en-gb": "en-gb",
    "en_uk": "en-gb",
    "british english": "en-gb",
    "e": "es",
    "es": "es",
    "es-es": "es",
    "spanish": "es",
    "f": "fr",
    "fr": "fr",
    "fr-fr": "fr",
    "french": "fr",
    "d": "de",
    "de": "de",
    "de-de": "de",
    "german": "de",
    "id": "id",
    "id-id": "id",
    "indonesian": "id",
    "bahasa indonesia": "id",
    "ko": "ko",
    "ko-kr": "ko",
    "korean": "ko",
    "h": "hi",
    "hi": "hi",
    "hi-in": "hi",
    "hindi": "hi",
    "i": "it",
    "it": "it",
    "it-it": "it",
    "italian": "it",
    "j": "ja",
    "ja": "ja",
    "ja-jp": "ja",
    "japanese": "ja",
    "p": "pt-br",
    "pt": "pt-br",
    "pt-br": "pt-br",
    "pt_br": "pt-br",
    "portuguese": "pt-br",
    "brazilian portuguese": "pt-br",
    "z": "zh",
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh_cn": "zh",
    "zh-tw": "zh",
    "zh-hant": "zh",
    "chinese": "zh",
    "mandarin": "zh",
}

KOKORO_LANGUAGE_CODES: dict[str, str] = {
    "en": "a",
    "en-gb": "b",
    "es": "e",
    "fr": "f",
    "hi": "h",
    "it": "i",
    "ja": "j",
    "pt-br": "p",
    "zh": "z",
}

# Voice ids are from the official Kokoro-82M voice collection.  Some
# languages currently have fewer than three female identities; do not invent
# extra ids just to fill the UI.  The preview service returns every distinct
# identity that is actually available, up to three.
KOKORO_FEMALE_VOICES: dict[str, tuple[TTSVoiceOption, ...]] = {
    "en": (
        TTSVoiceOption("af_heart", "Heart", "dramatic"),
        TTSVoiceOption("af_bella", "Bella", "warm"),
        TTSVoiceOption("af_nicole", "Nicole", "calm"),
        TTSVoiceOption("af_sarah", "Sarah", "confident"),
    ),
    "en-gb": (
        TTSVoiceOption("bf_alice", "Alice", "dramatic"),
        TTSVoiceOption("bf_emma", "Emma", "warm"),
        TTSVoiceOption("bf_isabella", "Isabella", "calm"),
        TTSVoiceOption("bf_lily", "Lily", "confident"),
    ),
    "es": (TTSVoiceOption("ef_dora", "Dora", "warm"),),
    "fr": (TTSVoiceOption("ff_siwis", "Siwis", "calm"),),
    "hi": (
        TTSVoiceOption("hf_alpha", "Alpha", "dramatic"),
        TTSVoiceOption("hf_beta", "Beta", "warm"),
    ),
    "it": (TTSVoiceOption("if_sara", "Sara", "warm"),),
    "ja": (
        TTSVoiceOption("jf_alpha", "Alpha", "dramatic"),
        TTSVoiceOption("jf_gongitsune", "Gongitsune", "calm"),
        TTSVoiceOption("jf_tebukuro", "Tebukuro", "warm"),
        TTSVoiceOption("jf_nezumi", "Nezumi", "confident"),
    ),
    "pt-br": (TTSVoiceOption("pf_dora", "Dora", "warm"),),
    "zh": (
        TTSVoiceOption("zf_xiaoxiao", "Xiaoxiao", "dramatic"),
        TTSVoiceOption("zf_xiaobei", "Xiaobei", "warm"),
        TTSVoiceOption("zf_xiaoyi", "Xiaoyi", "calm"),
        TTSVoiceOption("zf_xiaoni", "Xiaoni", "confident"),
    ),
}

DEEPGRAM_FEMALE_VOICES: dict[str, tuple[TTSVoiceOption, ...]] = {
    "en": (
        TTSVoiceOption("aura-2-andromeda-en", "Andromeda", "dramatic"),
        TTSVoiceOption("aura-2-cordelia-en", "Cordelia", "warm"),
        TTSVoiceOption("aura-2-athena-en", "Athena", "calm"),
        TTSVoiceOption("aura-2-thalia-en", "Thalia", "confident"),
    ),
    "ja": (
        TTSVoiceOption("aura-2-izanami-ja", "Izanami", "dramatic"),
        TTSVoiceOption("aura-2-uzume-ja", "Uzume", "warm"),
        TTSVoiceOption("aura-2-ama-ja", "Ama", "calm"),
    ),
}


def edge_tts_runtime_available() -> bool:
    """Return whether this Python runtime contains the optional Edge client.

    This deliberately reports only the local component state.  Network and
    upstream voice availability are verified separately when candidates are
    requested, so the UI never presents a cached/static voice as available
    merely because the package was installed.
    """

    try:
        return "edge_tts" in sys.modules or importlib.util.find_spec("edge_tts") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _run_edge_async(factory: Callable[[], Any]) -> Any:
    """Run one Edge coroutine from synchronous desktop/server code.

    StoryForge currently calls providers synchronously, but embedders may invoke
    them from a thread which already owns an asyncio loop.  In that uncommon
    case use a short-lived helper thread rather than nesting event loops.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: list[Any] = []
    failure: list[BaseException] = []

    def target() -> None:
        try:
            result.append(asyncio.run(factory()))
        except BaseException as error:  # propagate the original provider error
            failure.append(error)

    thread = threading.Thread(target=target, name="storyforge-edge-tts", daemon=True)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0] if result else None


def _query_edge_voices(*, proxy: str = "", timeout_seconds: float = 15.0) -> list[dict[str, Any]]:
    if not edge_tts_runtime_available():
        return []
    import edge_tts

    async def query() -> list[dict[str, Any]]:
        rows = await asyncio.wait_for(
            edge_tts.list_voices(proxy=proxy or None),
            timeout=max(3.0, float(timeout_seconds)),
        )
        return [dict(item) for item in rows if isinstance(item, dict)]

    return list(_run_edge_async(query) or [])


def clear_edge_voice_cache() -> None:
    """Clear discovery results after a network/provider configuration change."""

    with _EDGE_VOICE_CACHE_LOCK:
        _EDGE_VOICE_CACHE.clear()


def edge_female_voice_candidates(
    language: object = "en",
    *,
    proxy: str = "",
    refresh: bool = False,
) -> tuple[TTSVoiceOption, ...]:
    """Discover up to three female Edge voices that exist upstream right now.

    No voice identifiers are invented or assumed.  An unavailable module,
    failed network probe, or upstream language with no female voices returns an
    empty tuple.  Successful results are cached locally for six hours; a failed
    probe is cached for only twenty seconds so recovery does not require an app
    restart.
    """

    try:
        normalized_language = normalize_tts_language(language)
    except ValueError:
        return ()
    cache_key = f"{normalized_language}|{proxy.strip()}"
    now = time.monotonic()
    if not refresh:
        with _EDGE_VOICE_CACHE_LOCK:
            cached = _EDGE_VOICE_CACHE.get(cache_key)
        if cached is not None:
            cached_at, options = cached
            ttl = _EDGE_VOICE_CACHE_SECONDS if options else _EDGE_EMPTY_CACHE_SECONDS
            if now - cached_at <= ttl:
                return options

    if not edge_tts_runtime_available():
        options: tuple[TTSVoiceOption, ...] = ()
    else:
        try:
            rows = _query_edge_voices(proxy=proxy)
        except Exception:
            rows = []
        preferred_locales = tuple(
            item.casefold() for item in _EDGE_LOCALE_PREFERENCES[normalized_language]
        )
        language_prefixes = tuple(
            dict.fromkeys(item.split("-", 1)[0] for item in preferred_locales)
        )
        allow_regional_fallback = normalized_language not in {"en-gb", "pt-br"}

        def locale_rank(row: dict[str, Any]) -> tuple[int, str]:
            locale = str(row.get("Locale") or "").strip().casefold()
            short_name = str(row.get("ShortName") or "").strip()
            if locale in preferred_locales:
                rank = preferred_locales.index(locale)
            elif allow_regional_fallback and any(
                locale.startswith(prefix + "-") for prefix in language_prefixes
            ):
                rank = len(preferred_locales)
            else:
                rank = 999
            return rank, short_name.casefold()

        matching = [
            row
            for row in rows
            if str(row.get("Gender") or "").strip().casefold() == "female"
            and locale_rank(row)[0] < 999
            and str(row.get("ShortName") or "").strip()
        ]
        matching.sort(key=locale_rank)
        profiles = ("dramatic", "warm", "calm")
        selected: list[TTSVoiceOption] = []
        used: set[str] = set()
        for row in matching:
            voice_id = str(row.get("ShortName") or "").strip()
            if not voice_id or voice_id.casefold() in used:
                continue
            used.add(voice_id.casefold())
            local_name = str(row.get("LocalName") or "").strip()
            if not local_name:
                suffix = voice_id.rsplit("-", 1)[-1]
                local_name = re.sub(r"Neural$", "", suffix, flags=re.IGNORECASE) or voice_id
            selected.append(
                TTSVoiceOption(
                    voice_id=voice_id,
                    label=local_name,
                    profile=profiles[len(selected)],
                )
            )
            if len(selected) == 3:
                break
        options = tuple(selected)

    with _EDGE_VOICE_CACHE_LOCK:
        _EDGE_VOICE_CACHE[cache_key] = (now, options)
    return options

_KOKORO_LANGUAGE_DEPENDENCIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "j": (
        "pyopenjtalk-plus==0.4.1.post8",
        ("pyopenjtalk", "fugashi", "jaconv", "mojimoji", "unidic_lite"),
    ),
    "z": (
        "misaki[zh]",
        ("jieba", "ordered_set", "pypinyin", "cn2an", "pypinyin_dict"),
    ),
}


def normalize_tts_language(value: object = "en") -> str:
    """Normalize an app/BCP-47 language value to the TTS catalog key."""

    raw = str(value or "en").strip().casefold().replace("_", "-")
    normalized = _TTS_LANGUAGE_ALIASES.get(raw)
    if normalized is None:
        raise ValueError(f"Unsupported TTS language: {value!r}")
    return normalized


def female_voice_candidates(
    provider: object,
    language: object = "en",
) -> tuple[TTSVoiceOption, ...]:
    """Return real, distinct female voices for one provider and language."""

    try:
        normalized_language = normalize_tts_language(language)
    except ValueError:
        return ()
    normalized_provider = str(provider or "").strip().casefold().replace("-", "_")
    if normalized_provider in {
        "kokoro",
        "local",
        "local_kokoro",
        "kokoro_local",
        "kokoro_http",
        "kokoro_cli",
    }:
        return KOKORO_FEMALE_VOICES.get(normalized_language, ())
    if normalized_provider in {"deepgram", "deepgram_aura", "aura", "aura_2"}:
        return DEEPGRAM_FEMALE_VOICES.get(normalized_language, ())
    if normalized_provider in _EDGE_PROVIDER_ALIASES:
        return edge_female_voice_candidates(normalized_language)
    return ()


def _voice_kokoro_language_codes(voice: object) -> set[str]:
    # Kokoro ids start with language + gender (for example jf_alpha).  Also
    # support its comma/plus voice-mixing syntax without treating arbitrary
    # text as a language declaration.
    return set(
        re.findall(
            r"(?:^|[+,\s])([abefhijpz])[fm]_",
            str(voice or "").strip().casefold(),
        )
    )


def kokoro_language_code(language: object = "", voice: object = "") -> str:
    """Resolve Kokoro's one-letter code and reject voice/language mismatches."""

    raw_language = str(language or "").strip()
    if raw_language:
        try:
            configured = KOKORO_LANGUAGE_CODES[normalize_tts_language(raw_language)]
        except (KeyError, ValueError) as error:
            raise ProviderConfigurationError(
                f"Kokoro 不支持语种 {raw_language!r}。支持英语、日语、西班牙语、法语、"
                "印地语、意大利语、巴西葡萄牙语和中文。",
                provider="local_kokoro",
            ) from error
    else:
        configured = ""
    inferred = _voice_kokoro_language_codes(voice)
    if len(inferred) > 1:
        raise ProviderConfigurationError(
            "一次 Kokoro 配音不能混用不同语种的声线。请只选择同一语种的女声。",
            provider="local_kokoro",
        )
    inferred_code = next(iter(inferred), "")
    if configured and inferred_code and configured != inferred_code:
        raise ProviderConfigurationError(
            f"所选 Kokoro 声线 {voice!s} 与当前语种不匹配。请重新生成该语种的女声候选。",
            provider="local_kokoro",
        )
    return configured or inferred_code or "a"


def _missing_kokoro_language_modules(lang_code: str) -> tuple[str, ...]:
    dependency = _KOKORO_LANGUAGE_DEPENDENCIES.get(lang_code)
    if dependency is None:
        return ()
    missing: list[str] = []
    for module in dependency[1]:
        try:
            available = importlib.util.find_spec(module) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            available = False
        if not available:
            missing.append(module)
    return tuple(missing)


def _ensure_kokoro_language_dependencies(lang_code: str, provider: str) -> None:
    missing = _missing_kokoro_language_modules(lang_code)
    if not missing:
        return
    language_name = "日语" if lang_code == "j" else "中文"
    raise ProviderConfigurationError(
        f"Kokoro {language_name}组件未安装（缺少 {', '.join(missing)}）。"
        "源码版请在 StoryForge 项目目录运行 "
        "python -m pip install -r requirements-ai.txt 后重启；"
        "其他电脑请安装包含对应语种组件的 StoryForge 版本。",
        provider=provider,
    )


def _kokoro_runtime_roots() -> tuple[Path, ...]:
    """Return stable locations where an offline Kokoro bundle may live.

    The first entry can be supplied by deployment tooling.  A copied release
    works without environment variables when it keeps ``local-ai/kokoro`` next
    to the EXE.  Source checkouts use the same relative folder at project root.
    """

    roots: list[Path] = []
    configured = str(os.environ.get("STORYFORGE_KOKORO_ASSETS") or "").strip()
    if configured:
        roots.append(Path(configured).expanduser())
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent / "local-ai" / "kokoro")
    roots.append(Path(__file__).resolve().parents[2] / "local-ai" / "kokoro")
    roots.append(Path.cwd() / "local-ai" / "kokoro")
    unique: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def _offline_kokoro_assets() -> Path | None:
    """Find a complete local model bundle, avoiding first-run downloads."""

    for root in _kokoro_runtime_roots():
        required = [root / name for name in _KOKORO_MODEL_FILES]
        required.extend(root / "voices" / f"{voice}.pt" for voice in _KOKORO_VOICE_IDS)
        try:
            if all(path.is_file() and path.stat().st_size > 0 for path in required):
                return root
        except OSError:
            continue
    return None


def _prepare_huggingface_cache() -> Path:
    """Keep downloaded model files outside a one-file EXE's temporary folder."""

    configured = str(os.environ.get("HF_HOME") or "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        base = (
            str(os.environ.get("LOCALAPPDATA") or "").strip()
            or str(os.environ.get("PUBLIC") or "").strip()
            or tempfile.gettempdir()
        )
        root = (Path(base) / "StoryForge" / "ai-cache" / "huggingface").resolve()
        # huggingface_hub reads this variable when it is first imported.
        os.environ["HF_HOME"] = str(root)
    root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    return root


def _closed_http_client_error(error: BaseException) -> bool:
    """Recognize httpx/huggingface_hub's stale shared-client failure."""

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).casefold()
        if "client has been closed" in message or "client is closed" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


def _reset_huggingface_session() -> None:
    """Discard huggingface_hub's global httpx client after a failed request."""

    try:
        from huggingface_hub import close_session
    except (ImportError, AttributeError):
        return
    close_session()


def _kokoro_network_error(error: BaseException) -> ProviderResponseError:
    cache = _prepare_huggingface_cache()
    expected = _kokoro_runtime_roots()[0]
    return ProviderResponseError(
        "Kokoro 首次运行需要读取本地模型资源，但当前电脑没有完整离线模型，"
        "并且联网下载失败。请把发布包中的 local-ai\\kokoro 文件夹与 EXE 放在一起，"
        "或检查当前电脑能否访问 Hugging Face 后重试。"
        f"模型缓存位置：{cache}；离线模型位置：{expected}",
        provider="local_kokoro",
        retryable=True,
        response_excerpt=str(error)[:500],
    )


def split_sentences(text: str) -> list[str]:
    """Conservative English sentence splitter used only for TTS file boundaries."""

    compact = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n").replace("\r", "\n"))
    sentences: list[str] = []
    for paragraph in re.split(r"\n+", compact):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        sentences.extend(item.strip() for item in _SENTENCE_RE.split(paragraph) if item.strip())
    return sentences


@dataclass(frozen=True, slots=True)
class TTSRequest:
    sentences: Sequence[str]
    output_dir: str | Path
    voice: str = ""
    speed: float = 1.0
    file_stem: str = "sentence"

    def __post_init__(self) -> None:
        if not 0.5 <= self.speed <= 2.0:
            raise ValueError("TTS speed must be between 0.5 and 2.0.")
        if not any(str(sentence).strip() for sentence in self.sentences):
            raise ValueError("At least one non-empty sentence is required.")


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    index: int
    text: str
    path: str
    duration_seconds: float
    voice: str
    provider: str

    def to_dict(self) -> dict[str, str | int | float]:
        return {
            "index": self.index,
            "text": self.text,
            "path": self.path,
            "duration_seconds": self.duration_seconds,
            "voice": self.voice,
            "provider": self.provider,
        }


@dataclass(frozen=True, slots=True)
class TTSResult:
    segments: tuple[SpeechSegment, ...]
    provider: str
    model: str = ""

    @property
    def duration_seconds(self) -> float:
        return sum(item.duration_seconds for item in self.segments)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.segments)

    @property
    def path(self) -> str:
        """Convenience path for callers synthesizing a single sentence."""

        if len(self.segments) != 1:
            raise AttributeError("A multi-sentence TTS result has paths, not one path.")
        return self.segments[0].path

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [item.to_dict() for item in self.segments],
            "provider": self.provider,
            "model": self.model,
            "duration_seconds": self.duration_seconds,
        }


SpeechResult = TTSResult


def wav_duration(audio: bytes | str | Path) -> float:
    try:
        source: Any = io.BytesIO(audio) if isinstance(audio, bytes) else str(audio)
        with wave.open(source, "rb") as stream:
            rate = stream.getframerate()
            frames = stream.getnframes()
            if rate <= 0 or frames <= 0:
                raise ValueError("WAV contains no audio frames.")
            return frames / float(rate)
    except (wave.Error, EOFError, OSError, ValueError) as error:
        raise ValueError(f"Invalid WAV audio: {error}") from error


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _default_tts_cache_dir() -> Path:
    """Return the per-user cache without importing the application config layer."""

    override = os.environ.get("STORYFORGE_TTS_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "StoryForgeStudio" / "cache" / "tts"
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "StoryForgeStudio" / "cache" / "tts"
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return base / "StoryForgeStudio" / "tts"


def prune_tts_cache(
    cache_dir: str | Path | None = None,
    *,
    max_bytes: int = _TTS_CACHE_DEFAULT_MAX_BYTES,
    max_age_days: float = _TTS_CACHE_DEFAULT_MAX_AGE_DAYS,
) -> int:
    """Best-effort removal of old or excess sentence WAV cache files.

    Age pruning runs first, then the oldest remaining files are removed until
    the cache fits ``max_bytes``.  The cache is only an optimization, so an
    inaccessible directory or a file that another process currently owns is
    skipped instead of turning synthesis or rendering into a failure.

    The return value is the number of files successfully removed.
    """

    deleted = 0
    try:
        root = (
            Path(cache_dir).expanduser().resolve()
            if cache_dir is not None
            else _default_tts_cache_dir()
        )
        byte_limit = max(0, int(max_bytes))
        age_seconds = max(0.0, float(max_age_days)) * 24 * 60 * 60
        if not root.is_dir():
            return 0
    except (OSError, TypeError, ValueError, OverflowError):
        return 0

    entries: list[tuple[float, int, Path]] = []
    try:
        for path in root.rglob("*.wav"):
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
                entries.append((float(stat.st_mtime), int(stat.st_size), path))
            except OSError:
                continue
    except OSError:
        # Keep any entries discovered before an unreadable directory.  They
        # can still be pruned safely without making the whole pass fail.
        pass

    cutoff = time.time() - age_seconds
    retained: list[tuple[float, int, Path]] = []
    for modified_at, size, path in entries:
        if modified_at < cutoff:
            try:
                path.unlink()
            except OSError:
                retained.append((modified_at, size, path))
            else:
                deleted += 1
        else:
            retained.append((modified_at, size, path))

    total_bytes = sum(size for _modified_at, size, _path in retained)
    if total_bytes <= byte_limit:
        return deleted
    for _modified_at, size, path in sorted(retained, key=lambda item: item[0]):
        if total_bytes <= byte_limit:
            break
        try:
            path.unlink()
        except OSError:
            continue
        deleted += 1
        total_bytes = max(0, total_bytes - size)
    return deleted


def _maybe_prune_tts_cache(cache_path: Path) -> None:
    """Throttle automatic cache maintenance after successful cache writes."""

    try:
        cache_root = cache_path.parents[2]
        cache_key = os.path.normcase(str(cache_root.resolve()))
        now = time.monotonic()
        with _TTS_CACHE_PRUNE_LOCK:
            last_pruned = _TTS_CACHE_LAST_PRUNED.get(cache_key, 0.0)
            if now - last_pruned < _TTS_CACHE_PRUNE_INTERVAL_SECONDS:
                return
            _TTS_CACHE_LAST_PRUNED[cache_key] = now
            # A long-running Hub can see removable drives and per-user paths.
            # Bound this bookkeeping independently from the disk cache itself.
            if len(_TTS_CACHE_LAST_PRUNED) > 128:
                oldest = sorted(
                    _TTS_CACHE_LAST_PRUNED.items(), key=lambda item: item[1]
                )[:64]
                for old_key, _timestamp in oldest:
                    if old_key != cache_key:
                        _TTS_CACHE_LAST_PRUNED.pop(old_key, None)
        prune_tts_cache(cache_root)
    except (OSError, IndexError):
        pass


def _cache_lock(cache_key: str) -> threading.RLock:
    """Serialize identical synthesis within this process.

    Cache files are still written atomically, so separate StoryForge processes
    can safely race; this lock merely avoids doing the same expensive inference
    twice in one batch.
    """

    with _TTS_CACHE_LOCKS_GUARD:
        return _TTS_CACHE_LOCKS.setdefault(cache_key, threading.RLock())


class TTSProvider(ABC):
    def __init__(
        self, config: ProviderConfig, transport: HTTPTransport | None = None
    ) -> None:
        self.config = config
        self.transport = transport or UrllibTransport()

    def synthesize(
        self,
        request: TTSRequest | str | Sequence[str],
        output_dir: str | Path | None = None,
        *,
        voice: str = "",
        speed: float = 1.0,
        file_stem: str = "sentence",
    ) -> TTSResult:
        """Synthesize one independent WAV for every sentence.

        Passing a string invokes the conservative sentence splitter. Passing a
        sequence preserves the caller's sentence boundaries. The return value is
        always a batch result; for a single sentence ``result.path`` is available.
        """

        exact_output: Path | None = None
        if isinstance(request, TTSRequest):
            if output_dir is not None or voice or speed != 1.0 or file_stem != "sentence":
                raise TypeError("Extra synthesis options cannot accompany a TTSRequest.")
            normalized = request
        else:
            if output_dir is None:
                raise TypeError("output_dir is required unless a TTSRequest is supplied.")
            sentences = split_sentences(request) if isinstance(request, str) else [
                str(item).strip() for item in request if str(item).strip()
            ]
            target = Path(output_dir)
            if target.suffix.casefold() == ".wav" and len(sentences) == 1:
                exact_output = target
                target = target.parent
                file_stem = exact_output.stem
            normalized = TTSRequest(
                sentences=sentences,
                output_dir=target,
                voice=voice,
                speed=speed,
                file_stem=file_stem,
            )

        directory = Path(normalized.output_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        stem = _SAFE_STEM_RE.sub("_", normalized.file_stem).strip("._") or "sentence"
        selected_voice = normalized.voice or self.default_voice
        results: list[SpeechSegment] = []
        sentences = [str(item).strip() for item in normalized.sentences if str(item).strip()]
        for index, sentence in enumerate(sentences, start=1):
            raise_if_cancelled()
            path = (
                exact_output.expanduser().resolve()
                if exact_output is not None
                else directory / f"{stem}-{index:04d}.wav"
            )
            results.append(
                self.synthesize_sentence(
                    sentence,
                    path,
                    index=index,
                    voice=selected_voice,
                    speed=normalized.speed,
                )
            )
        raise_if_cancelled()
        return TTSResult(
            segments=tuple(results), provider=self.config.name, model=self.config.model
        )

    @property
    def default_voice(self) -> str:
        return self.config.model

    @property
    def language(self) -> str:
        """Language identity used by the sentence cache.

        Providers may use any of the common option names.  An empty language is
        still part of the cache identity, and concrete providers can override a
        default that is implicit in their engine.
        """

        for key in ("lang_code", "language", "lang"):
            value = self.config.options.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def _cache_root(self) -> Path | None:
        enabled = self.config.options.get("cache_enabled", True)
        if enabled is False or (
            isinstance(enabled, str)
            and enabled.strip().casefold() in {"0", "false", "no", "off"}
        ):
            return None
        configured = self.config.options.get("cache_dir")
        if configured is None:
            configured = self.config.options.get("tts_cache_dir")
        root = Path(configured).expanduser() if configured else _default_tts_cache_dir()
        return root.resolve()

    def _cache_key(self, text: str, voice: str, speed: float) -> str:
        command = self.config.options.get("command", "")
        if isinstance(command, Sequence) and not isinstance(
            command, (str, bytes, bytearray)
        ):
            command_identity = [str(item) for item in command]
        else:
            command_identity = str(command or "")
        identity = {
            "schema": _TTS_CACHE_SCHEMA,
            "provider": self.config.name.strip().casefold(),
            "engine": f"{type(self).__module__}.{type(self).__qualname__}",
            "model": self.config.model,
            "voice": voice,
            "speed": float(speed).hex(),
            "text": text,
            "lang": self.language,
            # These settings can change the returned samples without changing
            # the required provider/model/voice/text identity.
            "sample_rate": str(self.config.options.get("sample_rate", "")),
            "endpoint": self.config.endpoint,
            "command": command_identity,
        }
        encoded = json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _cache_path(self, cache_key: str) -> Path | None:
        root = self._cache_root()
        if root is None:
            return None
        return root / _TTS_CACHE_SCHEMA / cache_key[:2] / f"{cache_key}.wav"

    @staticmethod
    def _read_cached_wav(path: Path) -> tuple[bytes, float] | None:
        try:
            audio = path.read_bytes()
            return audio, wav_duration(audio)
        except (OSError, ValueError):
            # A crash from an older build or manual modification must never be
            # mistaken for a cache hit.  Best-effort removal lets the next
            # successful synthesis repair it.
            try:
                path.unlink()
            except OSError:
                pass
            return None

    @staticmethod
    def _write_cache(path: Path, audio: bytes) -> None:
        try:
            _atomic_write(path, audio)
        except OSError:
            # Caching is an optimization.  A read-only or full cache directory
            # must not turn a successful voice generation into a failed job.
            pass
        else:
            _maybe_prune_tts_cache(path)

    def _generate_sentence_atomically(
        self,
        sentence: str,
        path: Path,
        *,
        voice: str,
        speed: float,
    ) -> tuple[bytes, float]:
        """Generate through a temporary path and publish only valid WAV audio."""

        handle, temp_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp.wav", dir=path.parent
        )
        os.close(handle)
        temporary = Path(temp_name)
        # Some CLI adapters distinguish between "write this path" and stdout
        # by checking whether the path exists after the command.  Keep the
        # randomized name but present a genuinely absent output to the engine.
        temporary.unlink()
        try:
            raise_if_cancelled()
            audio = self._generate_audio(sentence, voice, speed, temporary)
            raise_if_cancelled()
            if audio is not None:
                try:
                    duration = wav_duration(audio)
                except ValueError as error:
                    raise ProviderResponseError(
                        f"{self.config.name} returned invalid WAV audio: {error}",
                        provider=self.config.name,
                    ) from error
                _atomic_write(path, audio)
                return audio, duration

            if not temporary.is_file():
                raise ProviderResponseError(
                    f"{self.config.name} completed without creating {path}.",
                    provider=self.config.name,
                )
            try:
                duration = wav_duration(temporary)
                audio = temporary.read_bytes()
            except (OSError, ValueError) as error:
                raise ProviderResponseError(
                    f"{self.config.name} created invalid WAV audio: {error}",
                    provider=self.config.name,
                ) from error
            os.replace(temporary, path)
            return audio, duration
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def synthesize_sentence(
        self,
        text: str,
        output_path: str | Path,
        *,
        index: int = 1,
        voice: str = "",
        speed: float = 1.0,
    ) -> SpeechSegment:
        sentence = text.strip()
        if not sentence:
            raise ValueError("Cannot synthesize an empty sentence.")
        if not 0.5 <= speed <= 2.0:
            raise ValueError("TTS speed must be between 0.5 and 2.0.")
        path = Path(output_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        selected_voice = voice or self.default_voice
        cache_key = self._cache_key(sentence, selected_voice, speed)
        cache_path = self._cache_path(cache_key)
        lock = _cache_lock(cache_key) if cache_path is not None else threading.RLock()
        with lock:
            raise_if_cancelled()
            cached = self._read_cached_wav(cache_path) if cache_path is not None else None
            if cached is not None:
                audio, duration = cached
                _atomic_write(path, audio)
            else:
                audio, duration = self._generate_sentence_atomically(
                    sentence,
                    path,
                    voice=selected_voice,
                    speed=speed,
                )
                if cache_path is not None:
                    self._write_cache(cache_path, audio)
        raise_if_cancelled()
        return SpeechSegment(
            index=index,
            text=sentence,
            path=str(path),
            duration_seconds=duration,
            voice=selected_voice,
            provider=self.config.name,
        )

    @abstractmethod
    def _generate_audio(
        self, text: str, voice: str, speed: float, output_path: Path
    ) -> bytes | None:
        raise NotImplementedError


class DeepgramAuraProvider(TTSProvider):
    DEFAULT_ENDPOINT = "https://api.deepgram.com/v1/speak"
    DEFAULT_MODEL = "aura-2-thalia-en"

    def __init__(
        self, config: ProviderConfig, transport: HTTPTransport | None = None
    ) -> None:
        if not config.model:
            config = ProviderConfig(
                name=config.name,
                model=self.DEFAULT_MODEL,
                endpoint=config.endpoint,
                api_key=config.api_key,
                timeout_seconds=config.timeout_seconds,
                options=config.options,
            )
        require_api_key(config)
        super().__init__(config, transport)

    def _generate_audio(
        self, text: str, voice: str, speed: float, output_path: Path
    ) -> bytes | None:
        api_speed = min(float(speed), 1.5)
        parameters: dict[str, str | int] = {
            "model": voice or self.config.model,
            "encoding": "linear16",
            "container": "wav",
        }
        sample_rate = self.config.options.get("sample_rate")
        if sample_rate:
            parameters["sample_rate"] = int(sample_rate)
        # Aura-2 accepts a 0.7–1.5 query speed while preserving natural prosody.
        if api_speed != 1.0:
            parameters["speed"] = str(api_speed)
        base = self.config.endpoint or self.DEFAULT_ENDPOINT
        endpoint = base + ("&" if "?" in base else "?") + urlencode(parameters)
        response = perform_request(
            self.transport,
            provider=self.config.name,
            method="POST",
            url=endpoint,
            headers={
                "Authorization": f"Token {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "audio/wav",
            },
            body=json_request_body({"text": text}),
            timeout=self.config.timeout_seconds,
        )
        ensure_http_success(self.config.name, response)
        if not response.body:
            raise ProviderResponseError(
                "Deepgram returned an empty audio response.",
                provider=self.config.name,
            )
        if speed <= 1.5:
            return response.body

        # StoryForge exposes up to 280 WPM.  Apply only the residual factor
        # beyond Aura's 1.5 query limit locally, without changing pitch.  The
        # resulting WAV flows through the normal validation and cache path.
        ffmpeg = _edge_ffmpeg_executable()
        if ffmpeg is None:
            raise ProviderConfigurationError(
                "Deepgram 极快语速需要本机 FFmpeg 完成最后一段无变调加速。",
                provider=self.config.name,
            )
        handle, source_name = tempfile.mkstemp(
            prefix=f".{output_path.stem}-deepgram-",
            suffix=".wav",
            dir=output_path.parent,
        )
        os.close(handle)
        source_path = Path(source_name)
        try:
            source_path.write_bytes(response.body)
            residual = float(speed) / 1.5
            command = [
                str(ffmpeg),
                "-hide_banner",
                "-nostdin",
                "-y",
                "-i",
                str(source_path),
                "-vn",
                "-filter:a",
                f"atempo={residual:.8f}",
                "-c:a",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "1",
                str(output_path),
            ]
            completed = run_cancellable_process(
                command,
                runner=subprocess.run,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if completed.returncode != 0 or not output_path.is_file():
                detail = (completed.stderr or completed.stdout or "未知 FFmpeg 错误")[-1200:]
                raise ProviderResponseError(
                    f"Deepgram 极快语速处理失败：{detail}",
                    provider=self.config.name,
                )
            return None
        finally:
            try:
                source_path.unlink()
            except OSError:
                pass


DeepgramTTSProvider = DeepgramAuraProvider


ProcessRunner = Callable[..., subprocess.CompletedProcess[bytes]]


def _edge_ffmpeg_executable() -> Path | None:
    configured = str(os.environ.get("STORYFORGE_FFMPEG") or "").strip()
    if configured and Path(configured).is_file():
        return Path(configured).resolve()
    discovered = shutil.which("ffmpeg")
    if discovered:
        return Path(discovered).resolve()
    try:
        import imageio_ffmpeg

        bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
        return bundled.resolve() if bundled.is_file() else None
    except (ImportError, RuntimeError, OSError):
        return None


def _edge_cli_command() -> list[str] | None:
    executable = shutil.which("edge-tts")
    if executable:
        return [str(Path(executable).resolve())]
    # Source/venv installs always support ``python -m``.  A frozen desktop EXE
    # is not a Python launcher, so packaged builds use the in-process fallback
    # unless their installer also ships the console script.
    if not getattr(sys, "frozen", False) and edge_tts_runtime_available():
        return [sys.executable, "-m", "edge_tts"]
    return None


class EdgeTTSProvider(TTSProvider):
    """No-key online Edge speech client running on the production computer.

    ``edge-tts`` returns compressed audio.  StoryForge converts it to a stable
    mono PCM WAV before publishing it to the normal narration pipeline, keeping
    cache validation, subtitle timing and FFmpeg composition provider-neutral.
    """

    DEFAULT_MODEL = "edge-neural"

    def __init__(
        self,
        config: ProviderConfig,
        transport: HTTPTransport | None = None,
        *,
        runner: ProcessRunner | None = None,
        ffmpeg_executable: str | Path | None = None,
    ) -> None:
        if not config.model:
            config = ProviderConfig(
                name=config.name,
                model=self.DEFAULT_MODEL,
                endpoint=config.endpoint,
                api_key="",
                timeout_seconds=config.timeout_seconds,
                options=config.options,
            )
        if not edge_tts_runtime_available():
            raise ProviderConfigurationError(
                "当前电脑未安装 Edge TTS 组件。请运行 "
                "pip install \"edge-tts>=7.2,<8\" 后重启 StoryForge。",
                provider=config.name,
            )
        super().__init__(config, transport)
        self.runner = runner or subprocess.run
        explicit = Path(ffmpeg_executable).expanduser() if ffmpeg_executable else None
        self.ffmpeg_executable = (
            explicit.resolve() if explicit and explicit.is_file() else _edge_ffmpeg_executable()
        )
        self.edge_cli_command = (
            _edge_cli_command()
            if self.config.options.get("use_cli", True) is not False
            else None
        )

    @property
    def default_voice(self) -> str:
        language = self.config.options.get("language") or "en"
        candidates = edge_female_voice_candidates(
            language,
            proxy=str(self.config.options.get("proxy") or ""),
        )
        if not candidates:
            raise ProviderConfigurationError(
                "Edge TTS 未能从上游取得该语种的可用女声，请检查网络后重新生成候选。",
                provider=self.config.name,
            )
        return candidates[0].voice_id

    def _generate_audio(
        self, text: str, voice: str, speed: float, output_path: Path
    ) -> bytes | None:
        if self.ffmpeg_executable is None:
            raise ProviderConfigurationError(
                "Edge TTS 需要 FFmpeg 将在线语音转换为制作所需的 WAV，"
                "但当前电脑未检测到 FFmpeg。",
                provider=self.config.name,
            )
        import edge_tts

        rate_percent = max(-50, min(100, round((float(speed) - 1.0) * 100)))
        rate = f"{rate_percent:+d}%"
        proxy = str(self.config.options.get("proxy") or "").strip()
        handle, mp3_name = tempfile.mkstemp(
            prefix=f".{output_path.stem}-edge-", suffix=".mp3", dir=output_path.parent
        )
        os.close(handle)
        mp3_path = Path(mp3_name)
        text_path: Path | None = None
        try:
            async def synthesize() -> None:
                kwargs: dict[str, Any] = {
                    "text": text,
                    "voice": voice,
                    "rate": rate,
                    "volume": "+0%",
                    "pitch": "+0Hz",
                }
                if proxy:
                    kwargs["proxy"] = proxy
                communicate = edge_tts.Communicate(**kwargs)
                await asyncio.wait_for(
                    communicate.save(str(mp3_path)),
                    timeout=max(5.0, float(self.config.timeout_seconds)),
                )

            try:
                if self.edge_cli_command:
                    text_handle, text_name = tempfile.mkstemp(
                        prefix=f".{output_path.stem}-edge-",
                        suffix=".txt",
                        dir=output_path.parent,
                    )
                    os.close(text_handle)
                    text_path = Path(text_name)
                    text_path.write_text(text, encoding="utf-8")
                    command = [
                        *self.edge_cli_command,
                        "--file",
                        str(text_path),
                        "--voice",
                        voice,
                        "--rate",
                        rate,
                        "--volume",
                        "+0%",
                        "--pitch",
                        "+0Hz",
                        "--write-media",
                        str(mp3_path),
                    ]
                    if proxy:
                        command.extend(("--proxy", proxy))
                    completed = run_cancellable_process(
                        command,
                        runner=self.runner,
                        capture_output=True,
                        check=False,
                        timeout=self.config.timeout_seconds,
                    )
                    if completed.returncode != 0:
                        stderr = (completed.stderr or b"").decode(
                            "utf-8", errors="replace"
                        )[:500]
                        raise ProviderResponseError(
                            f"Edge TTS 在线配音失败：{stderr or '客户端未返回原因'}",
                            provider=self.config.name,
                            retryable=True,
                            response_excerpt=stderr,
                        )
                else:
                    _run_edge_async(synthesize)
            except JobCancelledError:
                raise
            except ProviderResponseError:
                raise
            except Exception as error:
                raise ProviderResponseError(
                    "Edge TTS 在线配音失败。请检查当前制作电脑的网络连接后重试："
                    f"{error}",
                    provider=self.config.name,
                    retryable=True,
                    response_excerpt=str(error)[:500],
                ) from error
            if not mp3_path.is_file() or mp3_path.stat().st_size <= 0:
                raise ProviderResponseError(
                    "Edge TTS 没有返回有效音频。",
                    provider=self.config.name,
                    retryable=True,
                )
            completed = run_cancellable_process(
                [
                    str(self.ffmpeg_executable),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(mp3_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "24000",
                    "-c:a",
                    "pcm_s16le",
                    str(output_path),
                ],
                runner=self.runner,
                capture_output=True,
                check=False,
                timeout=self.config.timeout_seconds,
            )
            if completed.returncode != 0:
                stderr = (completed.stderr or b"").decode(
                    "utf-8", errors="replace"
                )[:500]
                raise ProviderResponseError(
                    f"Edge TTS 音频转换失败：{stderr or 'FFmpeg 未返回原因'}",
                    provider=self.config.name,
                    response_excerpt=stderr,
                )
            if not output_path.is_file():
                raise ProviderResponseError(
                    "Edge TTS 音频转换结束但没有生成 WAV。",
                    provider=self.config.name,
                )
            return None
        finally:
            try:
                mp3_path.unlink()
            except OSError:
                pass
            if text_path is not None:
                try:
                    text_path.unlink()
                except OSError:
                    pass


class KokoroProvider(TTSProvider):
    """Adapter for a local Kokoro HTTP server or configurable CLI.

    HTTP endpoints use the OpenAI-compatible ``/v1/audio/speech`` schema. A CLI
    command is supplied in ``options.command`` as a list/string with optional
    ``{text}``, ``{output}``, ``{voice}``, ``{model}``, and ``{speed}``
    placeholders. It can also be placed in ``endpoint`` with a ``cli:`` prefix.
    No Python Kokoro package is imported, so a missing engine produces a clear
    configuration error instead of an opaque ImportError.
    """

    DEFAULT_MODEL = "kokoro"
    DEFAULT_VOICE = "af_heart"

    def __init__(
        self,
        config: ProviderConfig,
        transport: HTTPTransport | None = None,
        *,
        runner: ProcessRunner | None = None,
    ) -> None:
        if not config.model:
            config = ProviderConfig(
                name=config.name,
                model=self.DEFAULT_MODEL,
                endpoint=config.endpoint,
                api_key=config.api_key,
                timeout_seconds=config.timeout_seconds,
                options=config.options,
            )
        super().__init__(config, transport)
        self.runner = runner or subprocess.run

    @property
    def default_voice(self) -> str:
        return str(self.config.options.get("voice") or self.DEFAULT_VOICE)

    def _http_endpoint(self) -> str:
        endpoint = self.config.endpoint.rstrip("/")
        if endpoint.endswith("/v1/audio/speech"):
            return endpoint
        path = urlsplit(endpoint).path.rstrip("/")
        if path in {"", "/v1"}:
            return endpoint + (
                "/audio/speech" if path == "/v1" else "/v1/audio/speech"
            )
        # A configured non-root path is treated as an exact custom endpoint.
        return endpoint

    def _command_template(self) -> list[str] | None:
        raw = self.config.options.get("command")
        if raw is None and self.config.endpoint.casefold().startswith("cli:"):
            raw = self.config.endpoint[4:].strip()
        if raw is None:
            return None
        if isinstance(raw, str):
            command = shlex.split(raw, posix=os.name != "nt")
        elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
            command = [str(item) for item in raw]
        else:
            raise ProviderConfigurationError(
                "Kokoro options.command must be a command string or argument list.",
                provider=self.config.name,
            )
        if not command:
            raise ProviderConfigurationError(
                "Kokoro CLI command cannot be empty.", provider=self.config.name
            )
        return command

    def _generate_audio(
        self, text: str, voice: str, speed: float, output_path: Path
    ) -> bytes | None:
        if self.config.endpoint.casefold().startswith(("http://", "https://")):
            return self._http_audio(text, voice, speed)
        command = self._command_template()
        if command is None:
            raise ProviderConfigurationError(
                "No Kokoro engine is configured. Start a local Kokoro HTTP server and "
                "set its endpoint, or set options.command (or a cli: endpoint) to a "
                "working Kokoro CLI command.",
                provider=self.config.name,
            )
        return self._cli_audio(command, text, voice, speed, output_path)

    def _http_audio(self, text: str, voice: str, speed: float) -> bytes:
        headers = {
            "Content-Type": "application/json",
            "Accept": "audio/wav",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        response = perform_request(
            self.transport,
            provider=self.config.name,
            method="POST",
            url=self._http_endpoint(),
            headers=headers,
            body=json_request_body(
                {
                    "model": self.config.model,
                    "input": text,
                    "voice": voice,
                    "response_format": "wav",
                    "speed": speed,
                }
            ),
            timeout=self.config.timeout_seconds,
        )
        ensure_http_success(self.config.name, response)
        content_type = str(response.headers.get("content-type", "")).casefold()
        if "json" not in content_type:
            if not response.body:
                raise ProviderResponseError(
                    "Kokoro returned an empty audio response.",
                    provider=self.config.name,
                )
            return response.body
        try:
            payload = response.json()
            encoded = (
                payload.get("audio")
                or payload.get("audio_base64")
                or payload.get("data")
            )
            if isinstance(encoded, dict):
                encoded = encoded.get("audio") or encoded.get("data")
            if not isinstance(encoded, str):
                raise KeyError("audio")
            return base64.b64decode(encoded, validate=True)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ProviderResponseError(
                "Kokoro returned JSON without valid base64 WAV audio.",
                provider=self.config.name,
                response_excerpt=response.text[:500],
            ) from error

    def _cli_audio(
        self,
        template: list[str],
        text: str,
        voice: str,
        speed: float,
        output_path: Path,
    ) -> bytes | None:
        values = {
            "text": text,
            "output": str(output_path),
            "voice": voice,
            "model": self.config.model,
            "speed": str(speed),
        }
        try:
            command = [item.format(**values) for item in template]
        except (KeyError, ValueError) as error:
            raise ProviderConfigurationError(
                f"Invalid Kokoro CLI placeholder: {error}", provider=self.config.name
            ) from error
        executable = command[0]
        if not (Path(executable).is_file() or shutil.which(executable)):
            raise ProviderConfigurationError(
                f"Kokoro CLI executable was not found: {executable}",
                provider=self.config.name,
            )
        stdin = text.encode("utf-8") if not any("{text}" in item for item in template) else None
        try:
            completed = run_cancellable_process(
                command,
                runner=self.runner,
                input=stdin,
                capture_output=True,
                check=False,
                timeout=self.config.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ProviderConfigurationError(
                f"Could not run the configured Kokoro CLI: {error}",
                provider=self.config.name,
            ) from error
        if completed.returncode != 0:
            stderr = (completed.stderr or b"").decode("utf-8", errors="replace")[:500]
            try:
                output_path.unlink()
            except OSError:
                pass
            raise ProviderResponseError(
                f"Kokoro CLI exited with code {completed.returncode}. {stderr}".strip(),
                provider=self.config.name,
                response_excerpt=stderr,
            )
        if output_path.is_file():
            return None
        stdout = completed.stdout or b""
        if stdout:
            return stdout
        raise ProviderResponseError(
            "Kokoro CLI succeeded but produced neither the configured output file nor WAV stdout.",
            provider=self.config.name,
        )


KokoroTTSProvider = KokoroProvider


def _prepare_windows_espeak_loader() -> None:
    """Relocate eSpeak resources when their Windows path is not ASCII-safe.

    The Windows eSpeak DLL used by ``espeakng-loader`` can fall back to the
    build-time data directory when its configured path contains non-ASCII
    characters.  StoryForge projects commonly live in Chinese-named folders,
    so cache the small DLL/data bundle under LocalAppData before Misaki imports
    and configures the phonemizer.
    """

    if os.name != "nt":
        return

    try:
        import espeakng_loader
    except ImportError:
        return

    library_source = Path(espeakng_loader.get_library_path()).resolve()
    data_source = Path(espeakng_loader.get_data_path()).resolve()
    try:
        f"{library_source}{data_source}".encode("ascii")
        return
    except UnicodeEncodeError:
        pass

    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            loader_version = version("espeakng-loader")
        except PackageNotFoundError:
            loader_version = "bundled"

        cache_bases = [
            os.environ.get("STORYFORGE_ESPEAK_CACHE", ""),
            os.environ.get("LOCALAPPDATA", ""),
            str(Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Documents"),
            tempfile.gettempdir(),
        ]
        cache_error: OSError | None = None
        cache_paths: tuple[Path, Path] | None = None
        for raw_base in dict.fromkeys(item for item in cache_bases if item):
            cache_root = (
                Path(raw_base)
                / "StoryForge"
                / "runtime"
                / f"espeakng-loader-{loader_version}"
            )
            if not str(cache_root).isascii():
                continue

            library_target = cache_root / library_source.name
            data_target = cache_root / data_source.name
            required_data = tuple(
                data_target / name
                for name in ("phontab", "phondata", "phonindex")
            )
            try:
                cache_root.mkdir(parents=True, exist_ok=True)
                if not library_target.is_file() or library_target.stat().st_size == 0:
                    shutil.copy2(library_source, library_target)
                if any(not item.is_file() or item.stat().st_size == 0 for item in required_data):
                    shutil.copytree(data_source, data_target, dirs_exist_ok=True)
                if any(not item.is_file() or item.stat().st_size == 0 for item in required_data):
                    raise OSError("the cached eSpeak pronunciation data is incomplete")
                cache_paths = (library_target, data_target)
                break
            except OSError as error:
                cache_error = error

        if cache_paths is None:
            detail = cache_error or OSError("no writable ASCII-only cache path was found")
            raise detail
        library_target, data_target = cache_paths

        # Misaki calls these functions while importing ``kokoro.KPipeline``.
        # Point it at the ASCII-safe cache without changing global installation.
        espeakng_loader.get_library_path = lambda: str(library_target)
        espeakng_loader.get_data_path = lambda: str(data_target)
        os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = str(library_target)
        os.environ["PHONEMIZER_ESPEAK_DATA_PATH"] = str(data_target)
    except OSError as error:
        raise ProviderConfigurationError(
            f"Could not prepare the Windows eSpeak runtime: {error}",
            provider="local_kokoro",
        ) from error


class EmbeddedKokoroProvider(TTSProvider):
    """Use the official Kokoro Python package in-process and cache its model."""

    DEFAULT_MODEL = "kokoro"
    DEFAULT_VOICE = "af_heart"
    _pipelines: dict[str, Any] = {}
    _pipeline_lock = threading.RLock()

    @classmethod
    def release_cached_resources(cls) -> int:
        """Release in-process Kokoro models before memory-heavy rendering.

        Only StoryForge's class-level ownership is removed.  A synthesis that
        is already using a pipeline keeps its own live reference and remains
        safe; once it finishes, normal garbage collection can reclaim it too.
        CUDA allocator cleanup is deliberately best-effort because CPU-only
        installs and partially available PyTorch runtimes are both supported.
        """

        with cls._pipeline_lock:
            released = len(cls._pipelines)
            cached_pipelines = tuple(cls._pipelines.values())
            cls._pipelines.clear()
            # Drop every temporary strong reference before collecting cycles.
            del cached_pipelines
            loaded_torch = sys.modules.get("torch")
            if released == 0 and loaded_torch is None:
                # Edge/Deepgram/HTTP Kokoro jobs must not import the heavyweight
                # local ML runtime merely to release a cache that never existed.
                return released
            try:
                gc.collect()
            except Exception:
                pass
            if loaded_torch is None:
                try:
                    import torch
                except (ImportError, OSError):
                    return released
            else:
                torch = loaded_torch
            cuda = getattr(torch, "cuda", None)
            if cuda is None:
                return released
            is_available = getattr(cuda, "is_available", None)
            if callable(is_available):
                try:
                    if not is_available():
                        return released
                except Exception:
                    return released
            for operation_name in ("empty_cache", "ipc_collect"):
                operation = getattr(cuda, operation_name, None)
                if not callable(operation):
                    continue
                try:
                    operation()
                except Exception:
                    pass
            return released

    def __init__(
        self, config: ProviderConfig, transport: HTTPTransport | None = None
    ) -> None:
        if not config.model:
            config = ProviderConfig(
                name=config.name,
                model=self.DEFAULT_MODEL,
                endpoint=config.endpoint,
                api_key=config.api_key,
                timeout_seconds=config.timeout_seconds,
                options=config.options,
            )
        super().__init__(config, transport)

    @property
    def default_voice(self) -> str:
        return str(self.config.options.get("voice") or self.DEFAULT_VOICE)

    @property
    def language(self) -> str:
        configured = (
            self.config.options.get("lang_code")
            or self.config.options.get("language")
            or self.config.options.get("lang")
            or ""
        )
        return kokoro_language_code(
            configured,
            "" if configured else self.default_voice,
        )

    def _pipeline(self, lang_code: str | None = None) -> Any:
        raise_if_cancelled()
        selected_code = lang_code or self.language
        if selected_code not in set(KOKORO_LANGUAGE_CODES.values()):
            raise ProviderConfigurationError(
                f"Unsupported Kokoro language code: {selected_code!r}",
                provider=self.config.name,
            )
        _ensure_kokoro_language_dependencies(selected_code, self.config.name)
        with self._pipeline_lock:
            if selected_code in self._pipelines:
                return self._pipelines[selected_code]
            assets = _offline_kokoro_assets()
            try:
                _prepare_windows_espeak_loader()
                _prepare_huggingface_cache()
                KModel, KPipeline = _import_kokoro_runtime()
            except ImportError as error:
                raise ProviderConfigurationError(
                    "No Kokoro engine is installed. Kokoro 本地组件尚未安装。"
                    "请运行 scripts\\setup_local_ai.ps1，"
                    "安装完成后重新启动软件。",
                    provider=self.config.name,
                ) from error
            try:
                if assets is not None:
                    # Match Kokoro's normal online initialization path.  Passing
                    # an already constructed KModel to KPipeline skips its
                    # automatic ``to(device).eval()`` call; leaving dropout in
                    # training mode makes repeated previews sound inconsistent.
                    import torch

                    device = "cuda" if torch.cuda.is_available() else "cpu"
                    model = KModel(
                        repo_id=_KOKORO_REPO_ID,
                        config=str(assets / "config.json"),
                        model=str(assets / "kokoro-v1_0.pth"),
                    ).to(device).eval()
                    pipeline = KPipeline(
                        lang_code=selected_code,
                        repo_id=_KOKORO_REPO_ID,
                        model=model,
                    )
                    setattr(pipeline, "_storyforge_asset_dir", str(assets))
                else:
                    pipeline = None
                    last_error: BaseException | None = None
                    # huggingface_hub 1.x can close its global httpx client after
                    # a transient connection error and then accidentally reuse
                    # that closed instance inside its own retry loop.  Re-entering
                    # KPipeline with a fresh global client recovers safely.
                    for attempt in range(2):
                        try:
                            pipeline = KPipeline(
                                lang_code=selected_code,
                                repo_id=_KOKORO_REPO_ID,
                            )
                            break
                        except JobCancelledError:
                            raise
                        except BaseException as error:
                            last_error = error
                            if not _closed_http_client_error(error) or attempt:
                                raise
                            _reset_huggingface_session()
                    if pipeline is None:
                        raise last_error or RuntimeError("Kokoro pipeline was not created")
            except ProviderError:
                raise
            except JobCancelledError:
                raise
            except BaseException as error:
                if assets is None and _closed_http_client_error(error):
                    raise _kokoro_network_error(error) from error
                raise ProviderResponseError(
                    f"Kokoro 本地模型加载失败：{error}",
                    provider=self.config.name,
                ) from error
            self._pipelines[selected_code] = pipeline
            raise_if_cancelled()
            return pipeline

    @staticmethod
    def _load_offline_voice(pipeline: Any, voice: str) -> None:
        asset_value = str(getattr(pipeline, "_storyforge_asset_dir", "") or "")
        if not asset_value:
            return
        asset_dir = Path(asset_value)
        requested = [item.strip() for item in str(voice or "").split(",") if item.strip()]
        missing = [item for item in requested if item not in getattr(pipeline, "voices", {})]
        if not missing:
            return
        voice_paths = {
            voice_id: asset_dir / "voices" / f"{voice_id}.pt"
            for voice_id in missing
        }
        for voice_id, voice_path in voice_paths.items():
            if not voice_path.is_file():
                raise ProviderConfigurationError(
                    f"Kokoro 离线声线缺失：{voice_path.name}。请把该声线文件放到 "
                    f"{voice_path.parent} 后重启，或在联网环境运行一次以完成声线下载。",
                    provider="local_kokoro",
                )
        try:
            import torch
        except ImportError as error:
            raise ProviderConfigurationError(
                "Kokoro 本地模型缺少 PyTorch 运行组件。",
                provider="local_kokoro",
            ) from error
        for voice_id, voice_path in voice_paths.items():
            pipeline.voices[voice_id] = torch.load(
                voice_path,
                map_location="cpu",
                weights_only=True,
            )

    def _generate_audio(
        self, text: str, voice: str, speed: float, output_path: Path
    ) -> bytes:
        del output_path
        try:
            import numpy as np
            import soundfile as sf
        except ImportError as error:
            raise ProviderConfigurationError(
                "No Kokoro engine audio dependencies are installed. "
                "请运行 scripts\\setup_local_ai.ps1。",
                provider=self.config.name,
            ) from error
        selected_voice = voice or self.default_voice
        configured_language = (
            self.config.options.get("lang_code")
            or self.config.options.get("language")
            or self.config.options.get("lang")
            or ""
        )
        lang_code = kokoro_language_code(configured_language, selected_voice)
        chunks: list[Any] = []
        last_error: BaseException | None = None
        for attempt in range(2):
            chunks = []
            try:
                pipeline = self._pipeline(lang_code)
                self._load_offline_voice(pipeline, selected_voice)
                for _graphemes, _phonemes, audio in pipeline(
                    text, voice=selected_voice, speed=speed
                ):
                    raise_if_cancelled()
                    if hasattr(audio, "detach"):
                        audio = audio.detach().cpu().numpy()
                    chunks.append(np.asarray(audio, dtype=np.float32).reshape(-1))
                raise_if_cancelled()
                last_error = None
                break
            except ProviderError:
                raise
            except JobCancelledError:
                raise
            except BaseException as error:
                last_error = error
                if not _closed_http_client_error(error) or attempt:
                    break
                _reset_huggingface_session()
        if last_error is not None:
            if _closed_http_client_error(last_error):
                raise _kokoro_network_error(last_error) from last_error
            raise ProviderResponseError(
                f"Kokoro 本地配音失败：{last_error}", provider=self.config.name
            ) from last_error
        if not chunks:
            raise ProviderResponseError(
                "Kokoro 没有返回音频。", provider=self.config.name
            )
        combined = np.concatenate(chunks)
        buffer = io.BytesIO()
        sf.write(buffer, combined, 24000, format="WAV", subtype="PCM_16")
        return buffer.getvalue()


def release_embedded_kokoro_runtime() -> int:
    """Public rendering-boundary hook for releasing cached Kokoro models."""

    return EmbeddedKokoroProvider.release_cached_resources()


def create_tts_provider(
    config: ProviderConfig | Any = None,
    *,
    transport: HTTPTransport | None = None,
    runner: ProcessRunner | None = None,
) -> TTSProvider:
    normalized = coerce_provider_config(config, kind="tts")
    name = normalized.name.casefold().replace("-", "_").strip()
    if name in {"deepgram", "deepgram_aura", "aura", "aura_2"}:
        return DeepgramAuraProvider(normalized, transport)
    if name in _EDGE_PROVIDER_ALIASES:
        return EdgeTTSProvider(normalized, transport, runner=runner)
    if name in {
        "kokoro",
        "local",
        "local_kokoro",
        "kokoro_local",
        "kokoro_http",
        "kokoro_cli",
    }:
        if not normalized.endpoint and not normalized.options.get("command"):
            return EmbeddedKokoroProvider(normalized, transport)
        return KokoroProvider(normalized, transport, runner=runner)
    raise ProviderConfigurationError(
        f"Unsupported TTS provider {normalized.name!r}. Supported providers are "
        "Edge TTS, Deepgram Aura and local Kokoro HTTP/CLI.",
        provider=normalized.name,
    )


__all__ = [
    "DEEPGRAM_FEMALE_VOICES",
    "DeepgramAuraProvider",
    "DeepgramTTSProvider",
    "EdgeTTSProvider",
    "KOKORO_FEMALE_VOICES",
    "KOKORO_LANGUAGE_CODES",
    "KokoroProvider",
    "KokoroTTSProvider",
    "EmbeddedKokoroProvider",
    "SpeechResult",
    "SpeechSegment",
    "TTSProvider",
    "TTSRequest",
    "TTSResult",
    "TTSVoiceOption",
    "create_tts_provider",
    "clear_edge_voice_cache",
    "prune_tts_cache",
    "release_embedded_kokoro_runtime",
    "edge_female_voice_candidates",
    "edge_tts_runtime_available",
    "female_voice_candidates",
    "kokoro_language_code",
    "normalize_tts_language",
    "split_sentences",
    "wav_duration",
]
