from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..models import AppSettings
from ..maintenance import prune_voice_preview_cache
from ..pipeline import UsageLedger, narration_speed_for_wpm
from ..providers.base import ProviderConfig, ProviderConfigurationError
from ..providers.tts import (
    TTSVoiceOption,
    create_tts_provider,
    edge_tts_runtime_available,
    female_voice_candidates,
    kokoro_language_code,
    normalize_tts_language,
)
from .media import canonical_mood
from .text_processing import extract_chapters, normalize_manuscript_text


TTSProviderFactory = Callable[[Any], Any]
_CJK_LANGUAGE_CODES = {"ja", "zh"}
_CJK_CHAPTER_HEADING_RE = re.compile(
    r"^\s*第\s*[0-9０-９一二三四五六七八九十百千]+\s*[章話话回節节]"
    r"(?:\s*[:：]\s*.*|\s+.*)?\s*$"
)
_SENTENCE_ENDINGS = frozenset(".!?。！？।")
_CLOSING_MARKS = frozenset("\"'’”」』）)]】")
_PROFILE_LABELS = {
    "dramatic": "戏剧张力",
    "warm": "温暖亲密",
    "calm": "冷静克制",
    "confident": "清晰强势",
}
_MOOD_CANDIDATES = {
    "suspense": ("dramatic", "calm", "confident"),
    "romance": ("warm", "dramatic", "calm"),
    "sad": ("calm", "warm", "dramatic"),
    "revenge": ("confident", "dramatic", "warm"),
}
_EDGE_PROVIDER_NAMES = {
    "edge",
    "edge_tts",
    "microsoft_edge",
    "microsoft_edge_tts",
}


def _audio_duration(path: Path, fallback: Any = 0.0) -> float:
    """Measure PCM/WAV previews from disk and use provider data otherwise."""

    try:
        with wave.open(str(path), "rb") as stream:
            frame_rate = int(stream.getframerate())
            if frame_rate > 0:
                return float(stream.getnframes()) / frame_rate
    except (OSError, EOFError, wave.Error):
        pass
    try:
        duration = float(fallback or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return duration if math.isfinite(duration) and duration >= 0 else 0.0


def _touch_cache_pair(*paths: Path) -> None:
    """Refresh a complete cached audition without creating missing files."""

    for path in paths:
        try:
            os.utime(path, None)
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class VoiceCandidate:
    profile: str
    label: str
    provider: str
    voice_id: str
    audio_path: str
    audio_uri: str
    duration_seconds: float
    excerpt: str
    language: str
    voice_name: str
    narration_wpm: int
    cached: bool
    cache_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _split_multilingual_sentences(text: str) -> tuple[str, ...]:
    prose = re.sub(r"\s+", " ", text).strip()
    if not prose:
        return ()
    sentences: list[str] = []
    start = 0
    index = 0
    while index < len(prose):
        if prose[index] not in _SENTENCE_ENDINGS:
            index += 1
            continue
        end = index + 1
        while end < len(prose) and prose[end] in _SENTENCE_ENDINGS:
            end += 1
        while end < len(prose) and prose[end] in _CLOSING_MARKS:
            end += 1
        sentence = prose[start:end].strip()
        if sentence:
            sentences.append(sentence)
        while end < len(prose) and prose[end].isspace():
            end += 1
        start = end
        index = end
    remainder = prose[start:].strip()
    if remainder:
        sentences.append(remainder)
    return tuple(sentences)


def _spoken_word_spans(text: str) -> tuple[re.Match[str], ...]:
    r"""Return whitespace-delimited spoken tokens without splitting graphemes.

    Python's ``\w`` does not keep Indic combining marks attached to their base
    letter.  Cutting at a regex-word boundary could therefore turn ``छिपा``
    into ``छ…``.  Supported non-CJK narration languages use whitespace between
    words, so preserving each complete non-space token is both safer and closer
    to the WPM limit shown in the UI.
    """

    return tuple(
        match
        for match in re.finditer(r"\S+", text)
        if any(
            unicodedata.category(character)[0] in {"L", "M", "N"}
            for character in match.group()
        )
    )


def _without_cjk_chapter_headings(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not _CJK_CHAPTER_HEADING_RE.fullmatch(line)
    )


def audition_excerpt(
    text: str,
    *,
    language: str = "en",
    maximum_words: int = 42,
    maximum_characters: int = 180,
) -> str:
    normalized = normalize_manuscript_text(text)
    cleaned = extract_chapters(normalized, pause_seconds=0).narration_text
    try:
        normalized_language = normalize_tts_language(language)
    except ValueError:
        normalized_language = str(language or "").strip().casefold()
    cleaned = _without_cjk_chapter_headings(cleaned)
    # The same splitter handles Latin punctuation, CJK marks and the Hindi
    # danda. English-only splitting treated an entire Hindi manuscript as one
    # sentence and could send hundreds of words to every audition request.
    sentences = _split_multilingual_sentences(cleaned)
    selected: list[str] = []
    words = 0
    characters = 0
    for sentence in sentences:
        sentence_words = _spoken_word_spans(sentence)
        if normalized_language not in _CJK_LANGUAGE_CODES:
            remaining_words = max(1, int(maximum_words)) - words
            if remaining_words <= 0:
                break
            if len(sentence_words) > remaining_words:
                cutoff = sentence_words[remaining_words - 1].end()
                fragment = sentence[:cutoff].rstrip()
                selected.append(fragment + "…")
                words += remaining_words
                break
        selected.append(sentence)
        words += len(sentence_words)
        characters += len(re.sub(r"\s+", "", sentence))
        if (
            normalized_language in _CJK_LANGUAGE_CODES
            and characters >= maximum_characters
        ) or (
            normalized_language not in _CJK_LANGUAGE_CODES
            and words >= maximum_words
        ):
            break
    separator = "" if normalized_language in _CJK_LANGUAGE_CODES else " "
    excerpt = separator.join(selected).strip()
    if normalized_language in _CJK_LANGUAGE_CODES and len(excerpt) > maximum_characters:
        excerpt = excerpt[:maximum_characters].rstrip() + "…"
    if not excerpt:
        raise ValueError("小说正文没有可用于试听的句子。")
    return excerpt


def _ordered_voice_options(
    catalog: tuple[TTSVoiceOption, ...],
    profiles: tuple[str, ...],
) -> tuple[tuple[str, TTSVoiceOption], ...]:
    """Match mood profiles first, then fill with another real female voice."""

    selected: list[tuple[str, TTSVoiceOption]] = []
    used: set[str] = set()
    for profile in profiles:
        option = next(
            (
                item
                for item in catalog
                if item.profile == profile and item.voice_id not in used
            ),
            None,
        )
        if option is None:
            option = next((item for item in catalog if item.voice_id not in used), None)
        if option is None:
            break
        used.add(option.voice_id)
        selected.append((profile, option))
    return tuple(selected)


class VoicePreviewService:
    def __init__(
        self,
        settings_getter: Callable[[], AppSettings],
        *,
        tts_provider_factory: TTSProviderFactory = create_tts_provider,
        usage_ledger: UsageLedger | None = None,
        cache_root: str | Path | None = None,
    ) -> None:
        self._settings_getter = settings_getter
        self._tts_provider_factory = tts_provider_factory
        self._usage_ledger = usage_ledger or UsageLedger()
        self._cache_root = (
            Path(cache_root).expanduser().resolve()
            if cache_root is not None
            else None
        )

    @staticmethod
    def _provider_config(settings: AppSettings, language: str = "en") -> Any:
        providers = settings.providers
        if providers.tts_provider == "local_kokoro":
            options: dict[str, Any] = {
                "lang_code": kokoro_language_code(language),
            }
            if providers.kokoro_command:
                options["command"] = providers.kokoro_command
            return ProviderConfig(
                name="local_kokoro",
                endpoint=providers.kokoro_endpoint,
                options=options,
            )
        normalized = str(providers.tts_provider or "").strip().casefold().replace("-", "_")
        if normalized in _EDGE_PROVIDER_NAMES:
            return ProviderConfig(
                name="edge_tts",
                options={"language": language},
            )
        return providers

    def generate(
        self,
        text: str,
        mood: str,
        output_dir: str | Path,
        *,
        language: str = "en",
        narration_wpm: int | None = None,
    ) -> list[dict[str, Any]]:
        settings = self._settings_getter()
        raw_wpm = settings.narration_wpm if narration_wpm is None else narration_wpm
        if isinstance(raw_wpm, bool):
            raise ValueError("narration_wpm must be an integer between 200 and 280")
        try:
            parsed_wpm = float(raw_wpm)
        except (TypeError, ValueError):
            raise ValueError(
                "narration_wpm must be an integer between 200 and 280"
            ) from None
        if (
            not math.isfinite(parsed_wpm)
            or not parsed_wpm.is_integer()
            or not 200 <= parsed_wpm <= 280
        ):
            raise ValueError("narration_wpm must be an integer between 200 and 280")
        requested_wpm = int(parsed_wpm)
        normalized_mood = canonical_mood(mood)
        try:
            normalized_language = normalize_tts_language(language)
        except ValueError as error:
            raise ProviderConfigurationError(
                f"当前语音服务尚未配置语种 {language!r}。请切换支持该语种的配音服务。",
                provider=settings.providers.tts_provider,
            ) from error
        profiles = _MOOD_CANDIDATES[normalized_mood]
        output = Path(output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        provider_name = settings.providers.tts_provider
        normalized_provider = str(provider_name or "").strip().casefold().replace("-", "_")
        catalog = female_voice_candidates(provider_name, normalized_language)
        if not catalog:
            if normalized_provider in _EDGE_PROVIDER_NAMES:
                reason = (
                    "当前电脑未安装 Edge TTS 组件"
                    if not edge_tts_runtime_available()
                    else "未能联网读取该语种的真实女声列表"
                )
                raise ProviderConfigurationError(
                    f"{reason}。请检查组件和网络后重试；软件不会用虚构声线代替。",
                    provider=provider_name,
                )
            raise ProviderConfigurationError(
                f"当前配音服务 {provider_name} 尚未配置语种 {language} 的可用女声。"
                "请切换服务，或由管理员补充该语种声线。",
                provider=provider_name,
            )
        selected_options = _ordered_voice_options(catalog, profiles)
        speed = narration_speed_for_wpm(requested_wpm, provider_name)
        metered_provider = (
            normalized_provider not in _EDGE_PROVIDER_NAMES
            and normalized_provider != "local_kokoro"
        )
        provider: Any | None = None
        pending_character_count = 0
        candidates: list[VoiceCandidate] = []
        protected_cache_paths: set[Path] = set()
        normalized_source = normalize_manuscript_text(text)
        source_hash = hashlib.sha256(
            normalized_source.encode("utf-8")
        ).hexdigest()
        initial_units = max(1, round(requested_wpm * 10.0 / 60.0))
        target_excerpt = audition_excerpt(
            text,
            language=normalized_language,
            maximum_words=initial_units,
            maximum_characters=initial_units,
        )
        target_units = (
            len(re.sub(r"\s+", "", target_excerpt).removesuffix("…"))
            if normalized_language in _CJK_LANGUAGE_CODES
            else len(_spoken_word_spans(target_excerpt.removesuffix("…")))
        )
        source_can_fill_preview = target_units >= max(
            8, round(initial_units * 0.8)
        )
        try:
            for index, (profile, option) in enumerate(selected_options, start=1):
                voice_id = option.voice_id
                cache_key = hashlib.sha256(
                    json.dumps(
                        {
                            "schema": 1,
                            "source": source_hash,
                            "mood": normalized_mood,
                            "language": normalized_language,
                            "provider": normalized_provider,
                            "voice_id": voice_id,
                            "profile": profile,
                            "narration_wpm": requested_wpm,
                            "speed": round(float(speed), 6),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                candidate_dir = output / "cache" / cache_key
                sidecar = candidate_dir / "preview.json"
                cached_payload: dict[str, Any] = {}
                if sidecar.is_file():
                    try:
                        loaded = json.loads(sidecar.read_text(encoding="utf-8"))
                        if isinstance(loaded, dict):
                            cached_payload = loaded
                    except (OSError, ValueError, json.JSONDecodeError):
                        cached_payload = {}
                cached_audio_file = str(cached_payload.get("audio_file") or "")
                cached_path = candidate_dir / cached_audio_file
                try:
                    cached_duration = float(
                        cached_payload.get("duration_seconds") or 0.0
                    )
                except (TypeError, ValueError):
                    cached_duration = 0.0
                if (
                    cached_payload.get("cache_key") == cache_key
                    and Path(cached_audio_file).name == cached_audio_file
                    and cached_path.is_file()
                    and 8.0
                    <= _audio_duration(cached_path, cached_duration)
                    <= 12.0
                ):
                    cached_duration = _audio_duration(cached_path, cached_duration)
                    path = cached_path.resolve()
                    _touch_cache_pair(path, sidecar)
                    protected_cache_paths.update((path, sidecar))
                    candidates.append(
                        VoiceCandidate(
                            profile=profile,
                            label=_PROFILE_LABELS[profile],
                            provider=provider_name,
                            voice_id=voice_id,
                            audio_path=str(path),
                            audio_uri=path.as_uri(),
                            duration_seconds=cached_duration,
                            excerpt=str(cached_payload.get("excerpt") or ""),
                            language=normalized_language,
                            voice_name=option.label,
                            narration_wpm=requested_wpm,
                            cached=True,
                            cache_key=cache_key,
                        )
                    )
                    continue

                if provider is None:
                    provider = self._tts_provider_factory(
                        self._provider_config(settings, normalized_language)
                    )
                candidate_dir.mkdir(parents=True, exist_ok=True)
                units = initial_units
                synthesis_token = uuid4().hex[:12]
                last_excerpt = ""
                result: Any | None = None
                excerpt = ""
                duration = 0.0
                for attempt in range(4):
                    excerpt = audition_excerpt(
                        text,
                        language=normalized_language,
                        maximum_words=min(140, units),
                        maximum_characters=min(420, units),
                    )
                    if excerpt == last_excerpt and result is not None:
                        break
                    prospective_characters = pending_character_count + len(excerpt)
                    if metered_provider:
                        self._usage_ledger.check(
                            provider_name,
                            prospective_characters,
                            settings.providers.monthly_character_limit,
                        )
                    result = provider.synthesize(
                        [excerpt],
                        candidate_dir,
                        voice=voice_id,
                        speed=speed,
                        file_stem=f"candidate-{index}-{synthesis_token}",
                    )
                    pending_character_count = prospective_characters
                    duration = _audio_duration(
                        Path(result.path),
                        getattr(result, "duration_seconds", 0.0),
                    )
                    if math.isfinite(duration) and 8.0 <= duration <= 12.0:
                        break
                    last_excerpt = excerpt
                    if not math.isfinite(duration) or duration <= 0:
                        units = min(420, units * 2)
                        continue
                    proposed = max(1, round(units * 10.0 / duration))
                    if proposed == units:
                        proposed += 1 if duration < 8.0 else -1
                    units = max(1, min(420, proposed))
                if result is None:
                    raise RuntimeError("voice preview synthesis returned no result")
                path = Path(result.path).resolve()
                if not path.is_file():
                    raise OSError(f"voice preview file was not created: {path}")
                protected_cache_paths.add(path)
                if source_can_fill_preview and not 8.0 <= duration <= 12.0:
                    raise ValueError(
                        "配音服务未能生成 8–12 秒的真实试听片段，请检查该声线后重试。"
                    )
                if 8.0 <= duration <= 12.0:
                    payload = {
                        "cache_key": cache_key,
                        "audio_file": path.name,
                        "duration_seconds": duration,
                        "excerpt": excerpt,
                        "narration_wpm": requested_wpm,
                    }
                    temporary_sidecar = sidecar.with_name(
                        f".{sidecar.name}.{uuid4().hex}.tmp"
                    )
                    try:
                        temporary_sidecar.write_text(
                            json.dumps(payload, ensure_ascii=False, sort_keys=True),
                            encoding="utf-8",
                        )
                        temporary_sidecar.replace(sidecar)
                        protected_cache_paths.add(sidecar)
                    finally:
                        try:
                            temporary_sidecar.unlink()
                        except OSError:
                            pass
                candidates.append(
                    VoiceCandidate(
                        profile=profile,
                        label=_PROFILE_LABELS[profile],
                        provider=provider_name,
                        voice_id=voice_id,
                        audio_path=str(path),
                        audio_uri=path.as_uri(),
                        duration_seconds=duration,
                        excerpt=excerpt,
                        language=normalized_language,
                        voice_name=option.label,
                        narration_wpm=requested_wpm,
                        cached=False,
                        cache_key=cache_key,
                    )
                )
        finally:
            if metered_provider and pending_character_count:
                self._usage_ledger.commit(provider_name, pending_character_count)
            # A long-running employee worker can generate previews for many
            # novels without restarting.  Keep that cache bounded after every
            # audition; locked/open files are skipped by best-effort cleanup.
            prune_voice_preview_cache(
                self._cache_root or output,
                protected_paths=protected_cache_paths,
            )
        return [item.to_dict() for item in candidates]
