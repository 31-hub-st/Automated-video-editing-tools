from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import shutil
import unicodedata
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterator
from typing import Any, Callable, Mapping
from uuid import uuid4

from .catalog import CatalogRepository
from .models import (
    COLOR_GRADES,
    COVER_ANIMATIONS,
    INTRO_ANIMATIONS,
    SUBTITLE_ANIMATIONS,
    AppSettings,
    JobStatus,
    PlatformProfile,
    RenderJob,
    normalize_retired_subtitle_settings,
)
from .pipeline import safe_component
from .providers.base import ProviderConfig, ProviderError
from .providers.text import TextRequest, create_text_provider
from .providers.tts import (
    available_female_voice_candidates,
    ensure_kokoro_language_available,
    kokoro_language_code,
)
from .style_options import preset_names, validate_style_patch
from .services.manuscript_import import (
    ImportedManuscript,
    prepare_manuscript,
    prepare_manuscript_file,
)
from .services.media import MediaError, VIDEO_EXTENSIONS, canonical_mood
from .services.text_processing import count_words
from .services.voice_preview import VoicePreviewService


def _file_uri(value: str) -> str:
    if not value:
        return ""
    try:
        path = Path(value).expanduser().resolve()
        return path.as_uri() if path.is_file() else ""
    except (OSError, ValueError):
        return ""


def _cover_tone(identifier: str) -> str:
    tones = ("cobalt", "ember", "violet", "noir")
    return tones[sum(identifier.encode("utf-8")) % len(tones)]


_STORY_MOOD_LABELS = {
    "suspense": "悬念",
    "romance": "浪漫",
    "sad": "悲伤",
    "revenge": "复仇 / 爽文",
}


_LOCAL_KOKORO_PROVIDER_ALIASES = frozenset(
    {
        "kokoro",
        "local",
        "local_kokoro",
        "kokoro_local",
        "kokoro_http",
        "kokoro_cli",
    }
)


# A current StoryForge regular render keeps its narration/caption alignment in
# the employee workstation's private narration index.  That makes the finished
# video a valid reuse source on the machine that rendered it, without restoring
# the legacy extra MP3 output.  Portable audio-only exports remain MP3 files.
_REUSABLE_NARRATION_EXTENSIONS = frozenset({".mp3"}) | VIDEO_EXTENSIONS


class _RenderJobSpool:
    """Disk-backed append/iterate buffer for arbitrarily large render plans.

    Planning still validates every requested variant before queueing starts,
    but only one ``RenderJob`` is resident while the plan is written or read.
    The temporary spool is removed after a complete read.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("w", encoding="utf-8", newline="\n")
        self._count = 0

    def append(self, job: RenderJob) -> None:
        self._stream.write(
            json.dumps(
                job.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        self._count += 1

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[RenderJob]:
        self._stream.flush()
        self._stream.close()
        try:
            with self.path.open("r", encoding="utf-8") as source:
                for line in source:
                    if line.strip():
                        yield RenderJob.from_dict(json.loads(line))
        finally:
            self.discard()

    def discard(self) -> None:
        stream = getattr(self, "_stream", None)
        if stream is not None and not stream.closed:
            stream.close()
        path = getattr(self, "path", None)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def __del__(self) -> None:
        self.discard()


_DEEPGRAM_PROVIDER_ALIASES = frozenset(
    {"deepgram", "deepgram_aura", "aura", "aura_2"}
)
_EDGE_PROVIDER_ALIASES = frozenset(
    {"edge", "edge_tts", "microsoft_edge", "microsoft_edge_tts"}
)


def _canonical_voice_provider(value: object) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    if normalized in _LOCAL_KOKORO_PROVIDER_ALIASES:
        return "local_kokoro"
    if normalized in _DEEPGRAM_PROVIDER_ALIASES:
        return "deepgram"
    if normalized in _EDGE_PROVIDER_ALIASES:
        return "edge_tts"
    return normalized


def _classification_sample(text: str, synopsis: str = "", maximum: int = 6000) -> str:
    """Sample the beginning, middle and ending instead of judging only the hook."""

    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    summary = re.sub(r"\s+", " ", str(synopsis or "")).strip()
    if len(compact) <= maximum:
        story = compact
    else:
        chunk = max(800, maximum // 3)
        middle_start = max(0, len(compact) // 2 - chunk // 2)
        story = "\n\n".join(
            (
                compact[:chunk],
                compact[middle_start : middle_start + chunk],
                compact[-chunk:],
            )
        )
    combined = f"Story synopsis: {summary}\n\nStory excerpts:\n{story}" if summary else story
    if not combined.strip():
        raise ValueError("小说正文为空，无法判断故事类型。")
    return combined.strip()


_LOCAL_TEXT_PROVIDERS = {
    "local",
    "local_rules",
    "local_rule",
    "local_passthrough",
    "passthrough",
}
_INTRO_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "another",
    "because",
    "before",
    "being",
    "could",
    "every",
    "from",
    "have",
    "into",
    "must",
    "their",
    "there",
    "these",
    "they",
    "this",
    "through",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "would",
}


def _intro_sentences(text: str) -> list[str]:
    """Split Latin and CJK prose without requiring spaces after CJK punctuation."""

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return []
    sentences: list[str] = []
    start = 0
    closing = {'"', "'", "’", "”", "」", "』"}
    for index, character in enumerate(value):
        if character not in ".!?。！？":
            continue
        next_index = index + 1
        while next_index < len(value) and value[next_index] in closing:
            next_index += 1
        if character == "." and next_index < len(value) and not value[next_index].isspace():
            continue
        sentence = value[start:next_index].strip()
        if sentence:
            sentences.append(sentence)
        start = next_index
    tail = value[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _intro_wrapped_line_count(text: str, *, width: int = 35) -> int:
    """Mirror the renderer's mixed Latin/CJK wrapping for budget validation."""

    remaining = " ".join(str(text or "").split())
    if not remaining:
        return 0
    line_width = max(8, int(width))
    lines = 0
    while remaining:
        used = 0
        last_break = 0
        overflow_at = len(remaining)
        for index, character in enumerate(remaining):
            character_width = (
                0
                if unicodedata.combining(character)
                else 2
                if unicodedata.east_asian_width(character) in {"W", "F"}
                else 1
            )
            if used + character_width > line_width:
                overflow_at = index
                break
            used += character_width
            if character.isspace() or unicodedata.east_asian_width(character) in {
                "W",
                "F",
            }:
                last_break = index + 1
        lines += 1
        if overflow_at == len(remaining):
            break
        split_at = last_break if last_break > 0 else max(1, overflow_at)
        remaining = remaining[split_at:].lstrip()
    return lines


def _fit_intro_card_text(
    text: str,
    *,
    maximum_words: int = 28,
    maximum_latin_characters: int = 155,
    maximum_cjk_characters: int = 70,
    maximum_sentences: int = 2,
) -> str:
    """Fit factual prose into the visible five-line story-card budget."""

    sentences = _intro_sentences(text)
    value = " ".join(sentences[:maximum_sentences]).strip()
    if not value:
        return ""
    cjk_characters = re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", value)
    compact_length = len(re.sub(r"\s+", "", value))
    if compact_length and len(cjk_characters) / compact_length >= 0.25:
        truncated = len(value) > maximum_cjk_characters
        core_limit = maximum_cjk_characters - (1 if truncated else 0)
        fitted = value[:core_limit].rstrip(" ,;:，、；：。–—")
        while fitted and _intro_wrapped_line_count(
            fitted + ("…" if truncated else "")
        ) > 5:
            fitted = fitted[:-1].rstrip(" ,;:，、；：。–—")
            truncated = True
        return fitted + ("…" if truncated else "")
    words = value.split()
    fitted_words = words[:maximum_words]
    truncated = len(fitted_words) < len(words)
    fitted = " ".join(fitted_words).rstrip(" ,;:–—-")
    while fitted_words and len(fitted) + (1 if truncated else 0) > maximum_latin_characters:
        fitted_words.pop()
        truncated = True
        fitted = " ".join(fitted_words).rstrip(" ,;:–—-")
    if not fitted:
        fitted = value[: max(1, maximum_latin_characters - 1)].rstrip()
        truncated = len(fitted) < len(value)
    while fitted_words and _intro_wrapped_line_count(
        fitted + ("…" if truncated else "")
    ) > 5:
        fitted_words.pop()
        truncated = True
        fitted = " ".join(fitted_words).rstrip(" ,;:–—-")
    return fitted + ("…" if truncated else "")


def _intro_card_excerpt(
    synopsis: str,
    episode_text: str,
    *,
    maximum_words: int = 28,
) -> tuple[str, str]:
    """Freeze a compact factual fallback, preferring the catalog synopsis."""

    summary = re.sub(r"\s+", " ", str(synopsis or "")).strip()
    body = re.sub(r"\s+", " ", str(episode_text or "")).strip()
    source_text = summary or body
    source = "novel_synopsis" if summary else "episode_excerpt"
    return _fit_intro_card_text(source_text, maximum_words=maximum_words), source


def _intro_candidate_is_grounded(candidate: str, source_text: str) -> bool:
    """Reject likely model invention before generated copy reaches the renderer."""

    candidate_value = re.sub(r"\s+", " ", str(candidate or "")).strip()
    source_value = re.sub(r"\s+", " ", str(source_text or "")).strip()
    if not candidate_value or not source_value:
        return False
    if re.search(
        r"\b(?:download|install|search\s+(?:for\s+)?(?:code|on)|continue\s+reading)\b",
        candidate_value,
        flags=re.IGNORECASE,
    ):
        return False
    if not set(re.findall(r"\d+(?:[.,]\d+)?", candidate_value)).issubset(
        set(re.findall(r"\d+(?:[.,]\d+)?", source_value))
    ):
        return False

    candidate_cjk = "".join(
        re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", candidate_value)
    )
    source_cjk = "".join(
        re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", source_value)
    )
    if len(candidate_cjk) >= 4:
        candidate_pairs = {
            candidate_cjk[index : index + 2]
            for index in range(len(candidate_cjk) - 1)
        }
        source_pairs = {
            source_cjk[index : index + 2] for index in range(len(source_cjk) - 1)
        }
        return bool(candidate_pairs) and (
            len(candidate_pairs & source_pairs) / len(candidate_pairs) >= 0.45
        )

    def terms(value: str) -> set[str]:
        return {
            item.casefold()
            for item in re.findall(r"[^\W\d_]{4,}", value, flags=re.UNICODE)
            if item.casefold() not in _INTRO_STOPWORDS
        }

    candidate_terms = terms(candidate_value)
    source_terms = terms(source_value)
    if not candidate_terms:
        return False
    return len(candidate_terms & source_terms) / len(candidate_terms) >= 0.45


class LibraryService:
    """Translate the normalized catalog into production-focused UI objects."""

    def __init__(
        self,
        catalog: CatalogRepository,
        settings_getter: Callable[[], AppSettings],
        data_dir: str | Path,
        *,
        text_provider_factory: Callable[[Any], Any] = create_text_provider,
        remote_text_provider: bool = False,
    ) -> None:
        self.catalog = catalog
        self._settings_getter = settings_getter
        self.data_dir = Path(data_dir).resolve()
        self.voice_previews = VoicePreviewService(
            settings_getter,
            cache_root=self.data_dir / "voice-previews",
        )
        self._text_provider_factory = text_provider_factory
        self._remote_text_provider = bool(remote_text_provider)

    def _intro_card_copy(
        self,
        synopsis: str,
        episode_text: str,
        *,
        title: str,
        language: str,
    ) -> tuple[str, str]:
        """Create optional AI-enhanced card copy with a factual local fallback."""

        fallback, source = _intro_card_excerpt(synopsis, episode_text)
        raw_source = re.sub(
            r"\s+", " ", str(synopsis or episode_text or "")
        ).strip()
        if not fallback or not raw_source:
            return fallback, source

        settings = self._settings_getter()
        selected = str(
            settings.providers.text_provider or "local"
        ).strip().casefold().replace("-", "_")
        if selected in _LOCAL_TEXT_PROVIDERS and not self._remote_text_provider:
            return fallback, source

        # A synopsis is normally short. For the episode-body fallback, give the
        # model a bounded opening excerpt instead of an entire long episode.
        source_sentences = _intro_sentences(raw_source)
        prompt_source = " ".join(source_sentences[:8]).strip() or raw_source
        if len(prompt_source) > 2400:
            prompt_source = prompt_source[:2400].rstrip(" ,;:–—-")
        request = TextRequest(
            text=prompt_source,
            title=str(title or ""),
            adult_mode=settings.adult_mode,
            retention_min=settings.retention_min,
            retention_max=settings.retention_max,
            language=str(language or settings.language or "English"),
            enforce_retention=False,
            purpose="intro_card",
        )
        try:
            provider = self._text_provider_factory(settings.providers)
            result = provider.polish(request)
        except ProviderError:
            return fallback, source

        candidate = str(result.polished_text or "").strip(" \t\r\n\"'“”")
        if not _intro_candidate_is_grounded(candidate, prompt_source):
            return fallback, source
        fitted = _fit_intro_card_text(candidate)
        if not fitted:
            return fallback, source
        if fitted.casefold() == fallback.casefold():
            return fallback, source
        return fitted, f"{source}_ai"

    def generate_intro_card_copy(
        self,
        novel_id: str,
        episode_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, str]:
        """Generate one reviewable card text before a batch is queued.

        This is intentionally separate from rendering: the user sees and saves
        the exact text here, and every later job consumes that frozen value.
        """

        novel = self.catalog.get_novel(str(novel_id or ""))
        revision = dict(novel.get("current_revision") or {})
        episodes = sorted(
            list(revision.get("episodes") or []),
            key=lambda item: int(item.get("ordinal") or 0),
        )
        selected = {str(item) for item in list(episode_ids or []) if str(item)}
        selected_episodes = [
            item
            for item in episodes
            if str(item.get("id") or "") in selected
        ]
        if not selected_episodes and episodes:
            selected_episodes = [episodes[0]]
        if not selected_episodes:
            raise ValueError("小说正文没有可用于简介卡的分集。")
        episode_text = "\n\n".join(
            str((episode.get("metadata") or {}).get("text") or "").strip()
            for episode in selected_episodes
            if str((episode.get("metadata") or {}).get("text") or "").strip()
        )
        text, source = self._intro_card_copy(
            str(novel.get("synopsis") or ""),
            episode_text,
            title=str(novel.get("title") or ""),
            language=str(
                novel.get("language_name")
                or novel.get("language_code")
                or novel.get("language")
                or "English"
            ),
        )
        if not text:
            raise ValueError("小说简介和所选分集没有可用于简介卡的正文。")
        return {"text": text, "source": source}

    def sync_platforms(self, profiles: list[PlatformProfile]) -> None:
        for profile in profiles:
            self.catalog.save_platform(profile.to_dict())

    @staticmethod
    def _ui_account(value: Mapping[str, Any]) -> dict[str, Any]:
        metadata = dict(value.get("metadata") or {})
        handle = "" if metadata.get("handle_pending") else str(value.get("handle") or "")
        return {
            "id": str(value["id"]),
            "platform_id": str(metadata.get("promotion_platform_id") or ""),
            "network": str(value.get("network") or "TikTok"),
            "name": str(value.get("display_name") or handle or "未命名账号"),
            "handle": handle,
            "region": str(metadata.get("region") or ""),
            "positioning": str(metadata.get("positioning") or ""),
            "notes": str(metadata.get("notes") or ""),
            "published_episode_count": int(metadata.get("published_episode_count") or 0),
            "active": str(value.get("status") or "active") == "active",
            "status": str(value.get("status") or "active"),
            "record_count": int(value.get("record_count") or 0),
            "row_version": int(value.get("row_version") or 0),
        }

    @staticmethod
    def _ui_binding(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": str(value["id"]),
            "platform_id": str(value["platform_id"]),
            "platform_name": str(value.get("platform_name") or ""),
            "platform_title": str(value.get("platform_title") or ""),
            "language": str(value.get("language") or "en-US"),
            "codes": [
                {
                    "id": str(code["id"]),
                    "value": str(code["code"]),
                    "slot_no": int(code.get("slot_no") or 0),
                    "active": str(code.get("status") or "active") == "active",
                    "status": str(code.get("status") or "active"),
                    "notes": str(code.get("notes") or ""),
                }
                for code in value.get("promo_codes", [])
            ],
            "historical_count": len(value.get("promo_codes", [])),
            "slots_remaining": int(value.get("promo_code_slots_remaining") or 0),
        }

    @staticmethod
    def _source_ordinals(source_map: list[Any]) -> list[int]:
        ordinals: list[int] = []
        for item in source_map:
            if not isinstance(item, Mapping):
                continue
            raw = item.get("chapter_ordinals") or item.get("chapters") or []
            if isinstance(raw, (list, tuple)):
                for value in raw:
                    try:
                        ordinal = int(value)
                    except (TypeError, ValueError):
                        continue
                    if ordinal not in ordinals:
                        ordinals.append(ordinal)
        return ordinals

    @classmethod
    def _source_label(
        cls,
        source_map: list[Any],
        *,
        chapter_titles: Mapping[int, str] | None = None,
        episode_metadata: Mapping[str, Any] | None = None,
        fallback: str = "",
    ) -> str:
        metadata = dict(episode_metadata or {})
        source_heading = str(
            metadata.get("source_heading")
            or metadata.get("original_title")
            or ""
        ).strip()
        if not source_heading:
            resolved_titles: list[str] = []
            title_index = chapter_titles or {}
            for ordinal in cls._source_ordinals(source_map):
                title = str(title_index.get(ordinal) or "").strip()
                if title and title not in resolved_titles:
                    resolved_titles.append(title)
            source_heading = " / ".join(resolved_titles)

        try:
            part_index = max(1, int(metadata.get("source_part_index") or 1))
            part_count = max(1, int(metadata.get("source_part_count") or 1))
        except (TypeError, ValueError):
            part_index, part_count = 1, 1
        base = source_heading or str(fallback or "").strip() or "自动分集"
        if part_count > 1:
            return f"{base} · 长集拆段 {min(part_index, part_count)}/{part_count}"
        return base

    def _ui_draft(self, value: Mapping[str, Any] | None) -> dict[str, Any]:
        if not value:
            return {
                "id": "",
                "platform_id": "",
                "binding_id": "",
                "promo_code_id": "",
                "publishing_account_id": "",
                "episode_ids": [],
                "variant_count": 1,
                "target_video_count": 10,
                "story_mood": "",
                "story_mood_source": "auto",
                "approvals": {"main": "pending", "variants": {}},
                "status": "draft",
                "row_version": 0,
                "warnings": [],
                "source_narration_audio": "",
            }
        metadata = dict(value.get("metadata") or {})
        production_settings = (
            dict(metadata.get("production_settings") or {})
            if isinstance(metadata.get("production_settings"), Mapping)
            else {}
        )
        if (
            "subtitle_preset" not in production_settings
            and str(value.get("subtitle_style_id") or "").strip()
        ):
            production_settings["subtitle_preset"] = str(
                value.get("subtitle_style_id") or ""
            ).strip()
        production_settings = normalize_retired_subtitle_settings(
            production_settings
        )
        voice = (
            dict(metadata.get("voice") or {})
            if isinstance(metadata.get("voice"), Mapping)
            else {}
        )
        return {
            "id": str(value.get("id") or ""),
            "platform_id": str(metadata.get("platform_id") or ""),
            "binding_id": str(value.get("binding_id") or ""),
            "promo_code_id": str(value.get("promo_code_id") or ""),
            "publishing_account_id": str(value.get("publishing_account_id") or ""),
            "episode_ids": list(value.get("episode_ids") or []),
            "variant_count": int(value.get("creative_line_count") or 1),
            "target_video_count": int(
                metadata.get("target_video_count")
                or max(1, int(value.get("creative_line_count") or 1))
            ),
            "story_mood": str(metadata.get("story_mood") or ""),
            "story_mood_source": str(metadata.get("story_mood_source") or "auto"),
            "approvals": metadata.get("approvals")
            if isinstance(metadata.get("approvals"), dict)
            else {"main": "pending", "variants": {}},
            "voice_profile": str(value.get("voice_profile") or ""),
            "voice": voice,
            "production_settings": production_settings,
            "source_narration_audio": str(
                production_settings.get("source_narration_audio") or ""
            ),
            "subtitle_style_id": str(
                production_settings.get("subtitle_preset")
                or value.get("subtitle_style_id")
                or ""
            ),
            "outro_style_id": str(value.get("outro_style_id") or ""),
            "status": str(value.get("status") or "draft"),
            "row_version": int(value.get("row_version") or 0),
            "video_folder": str(metadata.get("video_folder") or ""),
            "music_folder": str(metadata.get("music_folder") or ""),
            "output_folder": str(metadata.get("output_folder") or ""),
            "intro_card_text": str(metadata.get("intro_card_text") or ""),
            "intro_card_source": str(metadata.get("intro_card_source") or ""),
            "intro_card_copies": dict(metadata.get("intro_card_copies") or {})
            if isinstance(metadata.get("intro_card_copies"), Mapping)
            else {},
            "applied_production_preset_id": str(
                metadata.get("production_preset_id") or ""
            ),
            "applied_production_preset_revision": int(
                metadata.get("production_preset_revision") or 0
            ),
            "applied_production_preset_hash": str(
                metadata.get("production_preset_hash") or ""
            ),
            "production_preset_dirty": bool(
                metadata.get("production_preset_dirty", False)
            ),
            "warnings": [
                str(item)
                for item in list(metadata.get("warnings") or [])
                if str(item).strip()
            ],
        }

    def _validated_production_settings(
        self,
        value: Mapping[str, Any] | None,
        *,
        base: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a complete, non-secret recipe safe to freeze on a draft."""

        # Older saved drafts may not contain the V9 visual-style mappings.
        # Layer them over today's non-secret defaults so reopening an old draft
        # remains renderable and gains editable cards without a data migration.
        result = normalize_retired_subtitle_settings(
            {
                **self.production_settings_snapshot(self._settings_getter()),
                **dict(base or {}),
            }
        )
        incoming = normalize_retired_subtitle_settings(
            dict(value or {}) if isinstance(value, Mapping) else {}
        )

        for boolean_key in ("export_narration_audio", "cover_outro_enabled"):
            if boolean_key not in incoming:
                continue
            raw_boolean = incoming[boolean_key]
            if not isinstance(raw_boolean, bool):
                raise ValueError(f"{boolean_key} must be true or false")
            result[boolean_key] = raw_boolean

        def clamp_int(key: str, minimum: int, maximum: int) -> None:
            if key not in incoming:
                return
            try:
                result[key] = max(minimum, min(maximum, int(incoming[key])))
            except (TypeError, ValueError):
                raise ValueError(f"{key} 不是有效数字。") from None

        def clamp_float(key: str, minimum: float, maximum: float) -> None:
            if key not in incoming:
                return
            try:
                result[key] = max(minimum, min(maximum, float(incoming[key])))
            except (TypeError, ValueError):
                raise ValueError(f"{key} 不是有效数字。") from None

        for key, allowed in {
            "output_mode": {"video_and_mp3", "audio_only", "reuse_audio"},
            "video_transition": {"cut", "fade"},
            "subtitle_word_mode": {"off", "cumulative", "single"},
            "bgm_mode": {"auto", "manual", "none"},
            "adult_mode": {"direct", "engaging"},
            "caption_mode": {"semantic", "sentence"},
            "subtitle_preset": {
                *preset_names("subtitle"),
            },
            "intro_card_preset": {*preset_names("intro_card")},
            "code_card_preset": {*preset_names("code_card")},
            "outro_card_preset": {*preset_names("outro_card")},
            "subtitle_animation": SUBTITLE_ANIMATIONS,
            "intro_animation": INTRO_ANIMATIONS,
            "render_mode": {"speed", "quality", "compatibility"},
            "cover_animation": COVER_ANIMATIONS,
            "color_grade": COLOR_GRADES,
            "video_template": {"classic", "platform_story_card"},
        }.items():
            if key not in incoming:
                continue
            normalized = str(incoming[key] or "").strip()
            if normalized not in allowed:
                raise ValueError(f"{key} 选项无效。")
            result[key] = normalized

        if "narration_wpm" in incoming:
            try:
                parsed_wpm = float(incoming["narration_wpm"])
            except (TypeError, ValueError):
                raise ValueError("narration_wpm 不是有效整数。") from None
            if (
                not math.isfinite(parsed_wpm)
                or not parsed_wpm.is_integer()
                or not 200 <= parsed_wpm <= 280
            ):
                raise ValueError("narration_wpm 必须在 200 到 280 WPM 之间。")
            result["narration_wpm"] = int(parsed_wpm)
        if "video_playback_speed" in incoming:
            try:
                playback_speed = float(incoming["video_playback_speed"])
            except (TypeError, ValueError):
                raise ValueError("video_playback_speed 不是有效数字。") from None
            if not math.isfinite(playback_speed) or not 0.8 <= playback_speed <= 3.0:
                raise ValueError("video_playback_speed 必须在 0.8 到 3.0 之间。")
            result["video_playback_speed"] = playback_speed
        clamp_int("preview_seconds", 12, 60)
        clamp_float("intro_card_duration_seconds", 2.5, 8.0)
        if "output_fps" in incoming:
            try:
                output_fps = int(incoming["output_fps"])
            except (TypeError, ValueError):
                raise ValueError("output_fps 不是有效数字。") from None
            if output_fps not in {30, 60}:
                raise ValueError("output_fps 只支持 30 或 60 FPS。")
            result["output_fps"] = output_fps
        clamp_float("bgm_volume", 0.05, 0.50)
        clamp_float("chapter_pause_seconds", 0.0, 3.0)
        # The cover-led CTA is intentionally short and consistent across every
        # entry point.  Keep draft validation aligned with AppSettings and the
        # renderer instead of silently accepting values the pipeline later
        # rewrites.
        clamp_float("end_card_seconds", 5.0, 7.0)
        for path_key in ("source_narration_audio", "bgm_file"):
            if path_key in incoming:
                result[path_key] = str(incoming[path_key] or "").strip()
        if (
            str(result.get("output_mode") or "") == "reuse_audio"
            and not str(result.get("source_narration_audio") or "").strip()
        ):
            raise ValueError("reuse_audio 模式必须选择 source_narration_audio。")
        if (
            str(result.get("bgm_mode") or "auto") == "manual"
            and not str(result.get("bgm_file") or "").strip()
        ):
            raise ValueError("manual 背景音乐模式必须选择 bgm_file。")

        for kind in ("subtitle", "intro_card", "code_card", "outro_card"):
            preset_key = "subtitle_preset" if kind == "subtitle" else f"{kind}_preset"
            selected_preset = str(result.get(preset_key) or "").strip().casefold()
            patch = incoming.get(kind)
            if patch is not None and not isinstance(patch, Mapping):
                raise ValueError(f"{kind} settings must be an object")
            preset_changed = preset_key in incoming and (
                selected_preset
                != str((base or {}).get(preset_key) or "").strip().casefold()
            )
            style_base = validate_style_patch(
                kind,
                {},
                base=result.get(kind) if isinstance(result.get(kind), Mapping) else {},
                preset=selected_preset,
                reset_to_preset=preset_changed,
            )
            result[kind] = validate_style_patch(
                kind,
                patch if isinstance(patch, Mapping) else {},
                base=style_base,
                preset=selected_preset,
            )
        return result

    def novel_for_ui(self, novel_id: str) -> dict[str, Any]:
        value = self.catalog.get_novel(novel_id)
        revision = value.get("current_revision") or {}
        episodes_raw = list(revision.get("episodes") or [])
        metadata = dict(value.get("metadata") or {})
        chapter_titles = {
            int(item.get("ordinal") or index): str(item.get("title") or "").strip()
            for index, item in enumerate(revision.get("chapters") or [], start=1)
        }
        episodes: list[dict[str, Any]] = []
        current_wpm = float(self._settings_getter().narration_wpm)
        for index, item in enumerate(episodes_raw, start=1):
            episode_metadata = dict(item.get("metadata") or {})
            source_map = list(item.get("source_map") or [])
            stored_title = str(item.get("title") or f"自动分集 {index}").strip()
            source_heading = str(
                episode_metadata.get("source_heading")
                or episode_metadata.get("original_title")
                or ""
            ).strip()
            if not source_heading:
                resolved_titles = [
                    chapter_titles.get(ordinal, "")
                    for ordinal in self._source_ordinals(source_map)
                ]
                source_heading = " / ".join(
                    dict.fromkeys(title for title in resolved_titles if title)
                )
            try:
                source_part_index = max(
                    1, int(episode_metadata.get("source_part_index") or 1)
                )
                source_part_count = max(
                    1, int(episode_metadata.get("source_part_count") or 1)
                )
            except (TypeError, ValueError):
                source_part_index, source_part_count = 1, 1
            source_part_index = min(source_part_index, source_part_count)
            is_source_split = source_part_count > 1
            display_title = stored_title
            if (
                source_heading
                and not is_source_split
                and re.fullmatch(r"(?:E\s*)?\d{1,6}|自动分集\s*\d*", stored_title, re.I)
            ):
                display_title = source_heading
            split_label = (
                f"长集拆段 {source_part_index}/{source_part_count}"
                if is_source_split
                else ""
            )
            try:
                spoken_units = int(episode_metadata.get("word_count") or 0)
            except (TypeError, ValueError):
                spoken_units = 0
            if spoken_units <= 0:
                spoken_units = count_words(str(episode_metadata.get("text") or ""))
            episodes.append(
                {
                    "id": str(item["id"]),
                    "number": int(item.get("ordinal") or index),
                    "title": display_title,
                    "display_title": display_title,
                    "planned_title": stored_title,
                    "original_title": source_heading or display_title,
                    "source_title": source_heading or display_title,
                    "source_label": self._source_label(
                        source_map,
                        chapter_titles=chapter_titles,
                        episode_metadata=episode_metadata,
                        fallback=display_title,
                    ),
                    "source_part_index": source_part_index,
                    "source_part_count": source_part_count,
                    "is_source_split": is_source_split,
                    "split_label": split_label,
                    "explicit_source_boundary": bool(
                        episode_metadata.get("explicit_source_boundary")
                    ),
                    "spoken_units": spoken_units,
                    "duration_seconds": spoken_units * 60.0 / current_wpm,
                    "status": str(item.get("status") or "ready").replace(
                        "planned", "ready"
                    ),
                    "recap_text": str(item.get("recap_text") or ""),
                    "source_map": source_map,
                    "text": str(episode_metadata.get("text") or ""),
                    "metadata": episode_metadata,
                }
            )
        records = [
            item
            for item in self.catalog.list_records(novel_id=novel_id, limit=500)["items"]
            if not bool((item.get("metadata") or {}).get("lease_gate"))
        ]
        material_index: dict[str, dict[str, Any]] = {}
        for record in records:
            record_metadata = dict(record.get("metadata") or {})
            for raw_material in record_metadata.get("materials") or []:
                if not isinstance(raw_material, Mapping):
                    continue
                fingerprint = str(raw_material.get("fingerprint") or "").strip()
                fallback_key = (
                    f"{raw_material.get('type') or 'media'}:"
                    f"{raw_material.get('name') or ''}"
                )
                key = fingerprint or fallback_key
                current = material_index.get(key)
                candidate = {
                    "name": str(raw_material.get("name") or "未命名素材"),
                    "type": str(raw_material.get("type") or "video"),
                    "usage_count": int(raw_material.get("usage_count") or 0),
                    "fingerprint": fingerprint,
                    "selection_mode": str(
                        raw_material.get("selection_mode") or ""
                    ),
                    "generic_fallback": bool(
                        raw_material.get("generic_fallback")
                    ),
                    "requested_category": str(
                        raw_material.get("requested_category") or ""
                    ),
                }
                if current is None or candidate["usage_count"] > current["usage_count"]:
                    material_index[key] = candidate
        drafts = self.catalog.list_drafts(novel_id=novel_id, limit=100)["items"]
        queued_draft_ids = {
            str(item.get("draft_id") or "")
            for item in records
            if str(item.get("draft_id") or "")
        }
        # A queued batch is an immutable production snapshot, not the user's
        # next editable form.  Return only the newest draft that has never
        # created a production record; otherwise the UI starts a fresh,
        # unsaved batch.  This also repairs legacy drafts whose status stayed
        # "draft" after queueing.
        draft = next(
            (
                item
                for item in drafts
                if str(item.get("status") or "draft") == "draft"
                and str(item.get("id") or "") not in queued_draft_ids
            ),
            None,
        )
        bindings = [self._ui_binding(item) for item in value.get("bindings", [])]
        total_seconds = sum(float(item["duration_seconds"]) for item in episodes)
        cover_path = str(value.get("cover_path") or "")
        tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else []
        # Voice history is a convenience default, not a prerequisite for
        # opening the novel library.  During a rolling workstation/Hub update
        # an older Hub may not expose this RPC yet; degrading to the novel's
        # stored preference keeps the library usable until the Hub restarts on
        # the same version instead of failing the entire bootstrap request.
        try:
            last_successful_voice = self.catalog.last_successful_voice(novel_id)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            last_successful_voice = {}
        voice_id = str(
            last_successful_voice.get("voice_id")
            or metadata.get("preferred_voice_id")
            or metadata.get("locked_voice_id")
            or ""
        )
        voice_provider = str(
            last_successful_voice.get("provider")
            or metadata.get("preferred_voice_provider")
            or metadata.get("locked_voice_provider")
            or ""
        )
        voice_label = str(
            last_successful_voice.get("label")
            or metadata.get("preferred_voice_label")
            or metadata.get("locked_voice_label")
            or voice_id
            or "待试听"
        )
        return {
            "id": str(value["id"]),
            "title": str(value.get("title") or "Untitled story"),
            "language": str(value.get("language_code") or value.get("language") or "unknown"),
            "language_code": str(
                value.get("language_code") or value.get("language") or "unknown"
            ),
            "language_name": str(value.get("language_name") or "未识别"),
            "language_confidence": float(value.get("language_confidence") or 0.0),
            "language_source": str(value.get("language_source") or "auto"),
            "language_detection": dict(value.get("language_detection") or {}),
            "synopsis": str(value.get("synopsis") or ""),
            "tags": [str(item) for item in tags],
            "cover_path": cover_path,
            "cover_uri": _file_uri(cover_path),
            "cover_tone": str(metadata.get("cover_tone") or _cover_tone(str(value["id"]))),
            "source_type": str(revision.get("source_format") or "text"),
            "source_chapters": len(revision.get("chapters") or []),
            "estimated_duration_seconds": total_seconds,
            "default_voice": voice_label,
            "preferred_voice_provider": voice_provider,
            "preferred_voice_id": voice_id,
            # Compatibility aliases; these are defaults only and no longer
            # prevent a producer from choosing another voice for a later batch.
            "locked_voice_provider": voice_provider,
            "locked_voice_id": voice_id,
            "locked_voice_profile": str(
                last_successful_voice.get("profile")
                or metadata.get("preferred_voice_profile")
                or metadata.get("locked_voice_profile")
                or ""
            ),
            "voice_candidates": list(metadata.get("voice_candidates") or []),
            "story_classification": (
                dict(metadata.get("story_classification") or {})
                if isinstance(metadata.get("story_classification"), Mapping)
                else {}
            ),
            "statistics": {
                "successful_video_count": int(
                    value.get("successful_video_count") or 0
                ),
                "last_production_at": str(value.get("last_production_at") or ""),
            },
            "platform_bindings": bindings,
            "episodes": episodes,
            "materials": sorted(
                material_index.values(),
                key=lambda item: (-int(item["usage_count"]), str(item["name"]).casefold()),
            ),
            "draft": self._ui_draft(draft),
            "current_revision_id": str(value.get("current_revision_id") or ""),
            "content_hash": str(revision.get("content_hash") or ""),
            "updated_at": str(value.get("updated_at") or ""),
        }

    def _record_for_ui(
        self,
        value: Mapping[str, Any],
        account_names: Mapping[str, str],
    ) -> dict[str, Any]:
        metadata = dict(value.get("metadata") or {})
        raw_status = str(value.get("status") or "queued")
        status = (
            "active"
            if raw_status
            in {"queued", "preflight", "sample_ready", "awaiting_approval", "running"}
            else raw_status
        )
        output_path = str(value.get("output_path") or "")
        output_folder = str(Path(output_path).parent) if output_path else str(metadata.get("output_folder") or "")
        account_id = str(value.get("publishing_account_id") or "")
        return {
            "id": str(value["id"]),
            "batch_id": str(value.get("batch_id") or ""),
            "job_id": str(value.get("job_id") or ""),
            "novel_id": str(value.get("novel_id") or ""),
            "title": str(value.get("novel_title_snapshot") or ""),
            "episode_label": str(metadata.get("episode_label") or ""),
            "episode_ids": [
                str(item)
                for item in list(metadata.get("episode_ids") or [])
                if str(item)
            ],
            "creative_line": int(value.get("variant_index") or 1),
            "logical_task_key": str(value.get("logical_task_key") or ""),
            "current_attempt": int(value.get("current_attempt") or 1),
            "status": status,
            "raw_status": raw_status,
            "platform_id": str(metadata.get("platform_id") or ""),
            "promo_code": str(value.get("promo_code_snapshot") or ""),
            "publishing_account_name": account_names.get(account_id, "待分配"),
            "stage_label": str(metadata.get("stage_label") or raw_status),
            "progress": float(value.get("progress") or 0),
            "error": str(value.get("error_message") or ""),
            "failure_diagnostics": dict(metadata.get("failure_diagnostics") or {}),
            "output_folder": output_folder,
            "output_path": output_path,
            "preview_path": str(metadata.get("preview_file") or ""),
            "preview_uri": str(metadata.get("preview_uri") or ""),
            "device_id": str(value.get("device_id") or ""),
            "cancel_requested_at": value.get("cancel_requested_at"),
            "cancelled_at": value.get("cancelled_at"),
            "cancellation_reason": str(value.get("cancellation_reason") or ""),
            "archived": bool(value.get("archived")),
            "trashed": bool(value.get("trashed")),
            "trashed_at": value.get("trashed_at"),
            "artifact_count": int(value.get("artifact_count") or 0),
            "materials": list(metadata.get("materials") or []),
            "media_selection": dict(metadata.get("media_selection") or {}),
            "created_at": str(value.get("created_at") or ""),
        }

    def library_bootstrap(self) -> dict[str, Any]:
        summaries = self.catalog.list_novels(limit=500)["items"]
        novels = [self.novel_for_ui(str(item["id"])) for item in summaries]
        raw_accounts = self.catalog.list_publishing_accounts(include_archived=False)["items"]
        accounts = [self._ui_account(item) for item in raw_accounts]
        account_names = {item["id"]: item["name"] for item in accounts}
        records = [
            self._record_for_ui(item, account_names)
            for item in self.catalog.list_records(limit=500)["items"]
            if not bool((item.get("metadata") or {}).get("lease_gate"))
        ]
        try:
            users = self.catalog.list_users(include_inactive=True)["items"]
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            # Producers intentionally lack users.manage.  Account management
            # must disappear for them without preventing the rest of the
            # novel library and their own records from loading.
            users = []
        return {
            "summary": self.catalog.bootstrap_summary(),
            "novels": novels,
            "publishing_accounts": accounts,
            "production_records": records,
            "media_usage": self.catalog.list_media_usage(limit=500)["items"],
            "users": users,
        }

    def production_record_groups(self, filters: Mapping[str, Any]) -> dict[str, Any]:
        """UI projection of the hierarchical production ledger."""

        raw_accounts = self.catalog.list_publishing_accounts(
            include_archived=True
        )["items"]
        accounts = [self._ui_account(item) for item in raw_accounts]
        account_names = {item["id"]: item["name"] for item in accounts}
        payload = self.catalog.list_record_groups(**dict(filters))
        result = dict(payload)
        novels: list[dict[str, Any]] = []
        for novel in payload.get("items") or []:
            novel_value = dict(novel)
            batches: list[dict[str, Any]] = []
            for batch in novel.get("batches") or []:
                batch_value = dict(batch)
                batch_value["tasks"] = [
                    {
                        **self._record_for_ui(task, account_names),
                        "attempts": list(task.get("attempts") or []),
                        "member_name": str(task.get("member_name") or ""),
                        "device_name": str(task.get("device_name") or ""),
                        "logical_task_key": str(task.get("logical_task_key") or ""),
                        "current_attempt": int(task.get("current_attempt") or 1),
                        "cancel_requested_at": task.get("cancel_requested_at"),
                        "cancelled_at": task.get("cancelled_at"),
                        "cancellation_reason": str(task.get("cancellation_reason") or ""),
                        "archived": bool(task.get("archived")),
                        "trashed": bool(task.get("trashed")),
                    }
                    for task in batch.get("tasks") or []
                ]
                batches.append(batch_value)
            novel_value["batches"] = batches
            novels.append(novel_value)
        result["items"] = novels
        return result

    @staticmethod
    def _catalog_import_payload(
        prepared: ImportedManuscript,
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        estimate_wpm = (
            prepared.word_count * 60.0 / prepared.estimated_seconds
            if prepared.word_count > 0 and prepared.estimated_seconds > 0
            else 210.0
        )
        metadata: dict[str, Any] = {
            "estimated_duration_seconds": prepared.estimated_seconds,
            "source_word_count": prepared.word_count,
        }
        if "tags" in value:
            metadata["tags"] = list(value.get("tags") or [])
        payload: dict[str, Any] = {
            "novel_id": str(value.get("novel_id") or "") or None,
            "title": prepared.title,
            "body": prepared.normalized_text,
            "language": str(value.get("language") or "auto"),
            "source_format": str(value.get("source_type") or Path(prepared.source_name).suffix.lstrip(".") or "text"),
            "source_name": prepared.source_name,
            "metadata": metadata,
            "revision_metadata": {
                "estimated_duration_seconds": prepared.estimated_seconds,
                "source_word_count": prepared.word_count,
                "planner_version": 4,
                "estimator_version": 2,
                "estimate_wpm": estimate_wpm,
            },
            "chapters": [
                {
                    "ordinal": item.ordinal,
                    "title": item.heading,
                    "body": item.text,
                    "metadata": {
                        "word_count": item.word_count,
                        "estimator_version": 2,
                        "is_explicit_boundary": item.is_explicit_boundary,
                    },
                }
                for item in prepared.chapters
            ],
            "episodes": [
                {
                    "ordinal": item.ordinal,
                    "title": item.title,
                    "estimated_duration_seconds": item.estimated_seconds,
                    "status": "planned",
                    "source_map": [
                        {
                            "chapter_ordinals": list(item.source_chapter_ordinals),
                            "start_word": item.source_start_word,
                            "end_word": item.source_end_word,
                        }
                    ],
                    "metadata": {
                        "text": item.text,
                        "word_count": item.word_count,
                        "estimator_version": 2,
                        "boundary_reason": item.boundary_reason,
                        "duration_warning": item.duration_warning,
                        "source_heading": item.source_heading,
                        "original_title": item.source_heading or item.title,
                        "source_part_index": item.source_part_index,
                        "source_part_count": item.source_part_count,
                        "explicit_source_boundary": item.explicit_source_boundary,
                    },
                }
                for item in prepared.episodes
            ],
        }
        if "synopsis" in value:
            payload["synopsis"] = str(value.get("synopsis") or "").strip()
        if "cover_path" in value:
            payload["cover_path"] = str(value.get("cover_path") or "")
        return payload

    def import_text(self, value: Mapping[str, Any]) -> dict[str, Any]:
        prepared = prepare_manuscript(
            str(value.get("text") or ""),
            title=str(value.get("title") or ""),
            source_name="pasted-story.txt",
            wpm=self._settings_getter().narration_wpm,
        )
        result = self.catalog.import_novel(self._catalog_import_payload(prepared, value))
        return {
            **result,
            "novel": self.novel_for_ui(str(result["novel"]["id"])),
        }

    def import_file(self, value: Mapping[str, Any]) -> dict[str, Any]:
        path = str(value.get("file_path") or "").strip()
        prepared = prepare_manuscript_file(
            path,
            title=str(value.get("title") or ""),
            wpm=self._settings_getter().narration_wpm,
        )
        payload = {**dict(value), "source_type": Path(path).suffix.lstrip(".")}
        result = self.catalog.import_novel(self._catalog_import_payload(prepared, payload))
        return {
            **result,
            "novel": self.novel_for_ui(str(result["novel"]["id"])),
        }

    def save_novel(self, value: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(value)
        if "cover_path" in value and str(value.get("cover_path") or "").strip():
            cover = Path(str(value.get("cover_path"))).expanduser()
            if not cover.is_file() or cover.suffix.casefold() not in {
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".bmp",
            }:
                raise ValueError("小说封面必须是可读取的 JPG、PNG、WEBP 或 BMP 图片。")
            novel_id = str(value.get("id") or "").strip()
            if not novel_id:
                raise ValueError("保存封面前需要先创建小说。")
            cover_root = self.data_dir / "covers"
            cover_root.mkdir(parents=True, exist_ok=True)
            target = cover_root / f"{safe_component(novel_id)}{cover.suffix.casefold()}"
            if cover.resolve() != target.resolve():
                shutil.copy2(cover, target)
            payload["cover_path"] = str(target.resolve())
        updated = self.catalog.save_novel(payload)
        return self.novel_for_ui(str(updated["id"]))

    def save_binding(self, value: Mapping[str, Any]) -> dict[str, Any]:
        novel_id = str(value.get("novel_id") or "")
        self.catalog.save_novel_binding(value)
        return self.novel_for_ui(novel_id)

    def _binding(self, novel_id: str, platform_id: str) -> dict[str, Any]:
        novel = self.catalog.get_novel(novel_id)
        binding = next(
            (
                item
                for item in novel.get("bindings", [])
                if str(item.get("platform_id")) == platform_id
            ),
            None,
        )
        if binding is None:
            raise ValueError("请先把小说绑定到所选推广平台。")
        if bool(binding.get("archived")):
            raise ValueError("该小说的平台绑定已归档，请先在资料库恢复后再制作。")
        return binding

    def add_promo_code(self, value: Mapping[str, Any]) -> dict[str, Any]:
        novel_id = str(value.get("novel_id") or "")
        platform_id = str(value.get("platform_id") or "")
        binding = self._binding(novel_id, platform_id)
        raw_code = str(value.get("code") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]+", raw_code):
            raise ValueError("口令只允许英文字母和数字。")
        code = self.catalog.add_promo_code(
            {
                "binding_id": binding["id"],
                "code": raw_code,
                "status": "active",
            }
        )
        return {
            "novel": self.novel_for_ui(novel_id),
            "promo_code": {
                "id": code["id"],
                "value": code["code"],
                "active": code["status"] == "active",
            },
        }

    def update_promo_code(self, value: Mapping[str, Any]) -> dict[str, Any]:
        novel_id = str(value.get("novel_id") or "")
        code_id = str(value.get("promo_code_id") or "")
        code = self.catalog.update_promo_code(
            code_id,
            {"status": "active" if bool(value.get("active")) else "inactive"},
        )
        return {
            "novel": self.novel_for_ui(novel_id),
            "promo_code": {
                "id": code["id"],
                "value": code["code"],
                "active": code["status"] == "active",
            },
        }

    def save_publishing_account(self, value: Mapping[str, Any]) -> dict[str, Any]:
        name = str(value.get("name") or value.get("display_name") or "").strip()
        if not name:
            raise ValueError("发布账号名称不能为空。")
        handle = str(value.get("handle") or "").strip()
        pending = not handle
        if pending:
            handle = f"pending-{uuid4().hex[:12]}"
        result = self.catalog.save_publishing_account(
            {
                "id": value.get("id"),
                "network": str(value.get("network") or "TikTok"),
                "handle": handle,
                "display_name": name,
                "status": "active" if value.get("active", True) else "inactive",
                "expected_version": value.get("expected_version"),
                "metadata": {
                    "promotion_platform_id": str(value.get("platform_id") or ""),
                    "region": str(value.get("region") or ""),
                    "positioning": str(value.get("positioning") or ""),
                    "notes": str(value.get("notes") or ""),
                    "published_episode_count": int(value.get("published_episode_count") or 0),
                    "handle_pending": pending,
                },
            }
        )
        return self._ui_account(result)

    def save_draft(self, value: Mapping[str, Any]) -> dict[str, Any]:
        novel_id = str(value.get("novel_id") or "")
        platform_id = str(value.get("platform_id") or "")
        binding = self._binding(novel_id, platform_id)
        promo_code_id = str(value.get("promo_code_id") or "")
        promo_code = next(
            (
                item
                for item in binding.get("promo_codes", [])
                if str(item.get("id")) == promo_code_id
            ),
            None,
        )
        if promo_code is None or str(promo_code.get("status") or "active") != "active":
            raise ValueError("请选择该小说在当前平台下的有效口令。")
        existing_id = str(value.get("id") or "")
        existing: dict[str, Any] | None = None
        if existing_id:
            existing = self.catalog.get_draft(existing_id)
            if (
                str(existing.get("binding_id")) != str(binding["id"])
                or str(existing.get("promo_code_id")) != promo_code_id
            ):
                existing_id = ""
                existing = None
        novel = self.catalog.get_novel(novel_id)
        novel_metadata = dict(novel.get("metadata") or {})
        existing_metadata = dict((existing or {}).get("metadata") or {})
        incoming_recipe = value.get("production_settings")
        if not isinstance(incoming_recipe, Mapping):
            incoming_recipe = value.get("generation_settings")
        incoming_recipe = (
            dict(incoming_recipe) if isinstance(incoming_recipe, Mapping) else {}
        )
        if "source_narration_audio" in value:
            incoming_recipe["source_narration_audio"] = str(
                value.get("source_narration_audio") or ""
            ).strip()
        if "bgm_file" in value:
            incoming_recipe["bgm_file"] = str(value.get("bgm_file") or "").strip()
        recipe_base = existing_metadata.get("production_settings")
        production_settings = self._validated_production_settings(
            incoming_recipe,
            base=(
                recipe_base
                if isinstance(recipe_base, Mapping)
                else self.production_settings_snapshot(self._settings_getter())
            ),
        )
        # A saved batch is reproducible even if the novel is reclassified later.
        # Existing drafts keep their frozen language; only a new draft snapshots
        # the novel's current effective classification.
        if not isinstance(recipe_base, Mapping) or "language" not in recipe_base:
            production_settings["language"] = str(
                novel.get("language_code") or novel.get("language") or "unknown"
            )
            production_settings["language_confidence"] = float(
                novel.get("language_confidence") or 0.0
            )
            production_settings["language_source"] = str(
                novel.get("language_source") or "auto"
            )
        existing_voice = (
            dict(existing_metadata.get("voice") or {})
            if isinstance(existing_metadata.get("voice"), Mapping)
            else {}
        )
        last_successful_voice = self.catalog.last_successful_voice(novel_id)
        raw_incoming_voice = value.get("voice")
        has_incoming_voice = isinstance(raw_incoming_voice, Mapping) or any(
            key in value
            for key in ("voice_provider", "voice_id", "voice_label", "voice_profile")
        )
        incoming_voice = raw_incoming_voice
        if not isinstance(incoming_voice, Mapping):
            incoming_voice = {}
        voice = {
            "provider": str(
                incoming_voice.get("provider")
                or value.get("voice_provider")
                or existing_voice.get("provider")
                or last_successful_voice.get("provider")
                or novel_metadata.get("preferred_voice_provider")
                or novel_metadata.get("locked_voice_provider")
                or ""
            ).strip(),
            "voice_id": str(
                incoming_voice.get("voice_id")
                or value.get("voice_id")
                or existing_voice.get("voice_id")
                or last_successful_voice.get("voice_id")
                or novel_metadata.get("preferred_voice_id")
                or novel_metadata.get("locked_voice_id")
                or ""
            ).strip(),
            "label": str(
                incoming_voice.get("label")
                or value.get("voice_label")
                or existing_voice.get("label")
                or last_successful_voice.get("label")
                or novel_metadata.get("preferred_voice_label")
                or novel_metadata.get("locked_voice_label")
                or ""
            ).strip(),
            "profile": str(
                incoming_voice.get("profile")
                or value.get("voice_profile")
                or existing_voice.get("profile")
                or last_successful_voice.get("profile")
                or novel_metadata.get("preferred_voice_profile")
                or novel_metadata.get("locked_voice_profile")
                or ""
            ).strip(),
        }
        if bool(voice["provider"]) != bool(voice["voice_id"]):
            raise ValueError("请选择一个完整的本批女声配置。")
        if has_incoming_voice and voice["voice_id"]:
            candidates = [
                item
                for item in list(novel_metadata.get("voice_candidates") or [])
                if isinstance(item, Mapping)
            ]
            matching_candidate = next(
                (
                    item
                    for item in candidates
                    if str(item.get("provider") or "").strip()
                    == voice["provider"]
                    and str(item.get("voice_id") or "").strip()
                    == voice["voice_id"]
                ),
                None,
            )
            if candidates and matching_candidate is None:
                raise ValueError("所选声音不在当前的3个试听候选中，请重新试听。")
            # Voice identity and speaking speed are independent.  A preview is
            # still cached per WPM because its audio bytes differ, but changing
            # WPM must never invalidate or silently replace the selected actor.
        episode_ids = [str(item) for item in list(value.get("episode_ids") or []) if str(item)]
        uses_total_count = (
            "target_video_count" in value
            or existing_metadata.get("video_count_mode") == "total"
        )
        if uses_total_count:
            target_video_count = int(
                value.get("target_video_count")
                or existing_metadata.get("target_video_count")
                or 10
            )
            target_video_count = max(1, target_video_count)
        else:
            target_video_count = max(
                1,
                int(
                    value.get("variant_count")
                    or (existing or {}).get("creative_line_count")
                    or 1
                ),
            )
        # One draft now represents one ordered multi-episode story unit.  The
        # requested count is the number of variants for that complete unit.
        variant_count = target_video_count
        classification = (
            dict(novel_metadata.get("story_classification") or {})
            if isinstance(novel_metadata.get("story_classification"), Mapping)
            else {}
        )
        raw_story_mood = str(
            value.get("story_mood")
            or existing_metadata.get("story_mood")
            or classification.get("mood")
            or "suspense"
        )
        try:
            story_mood = canonical_mood(raw_story_mood)
        except MediaError as error:
            raise ValueError(
                "故事类型无效，只能选择悬念、浪漫、悲伤或复仇 / 爽文。"
            ) from error
        suggested_mood = str(classification.get("mood") or "")
        requested_source = str(value.get("story_mood_source") or "").strip().casefold()
        story_mood_source = (
            "manual"
            if requested_source == "manual"
            or (suggested_mood and story_mood != suggested_mood)
            else "auto"
        )
        revision = dict(novel.get("current_revision") or {})
        episode_by_id = {
            str(item.get("id") or ""): item
            for item in list(revision.get("episodes") or [])
            if str(item.get("id") or "")
        }
        first_episode_id = next(
            (item for item in episode_ids if item in episode_by_id),
            "",
        )
        first_episode_text = str(
            ((episode_by_id.get(first_episode_id) or {}).get("metadata") or {}).get(
                "text"
            )
            or ""
        )
        novel_synopsis = str(novel.get("synopsis") or "")
        fallback_intro, fallback_source = _intro_card_excerpt(
            novel_synopsis,
            first_episode_text,
        )
        incoming_intro = _fit_intro_card_text(
            str(value.get("intro_card_text") or "").strip()
        )
        intro_card_text = incoming_intro or fallback_intro
        intro_card_source = str(value.get("intro_card_source") or "").strip()
        if not intro_card_source:
            intro_card_source = fallback_source
        intro_card_copies: dict[str, dict[str, str]] = {}
        for episode_id in episode_ids:
            episode = episode_by_id.get(episode_id)
            if episode is None:
                continue
            episode_text = str((episode.get("metadata") or {}).get("text") or "")
            if novel_synopsis or episode_id == first_episode_id:
                text_value, source_value = intro_card_text, intro_card_source
            else:
                text_value, source_value = _intro_card_excerpt("", episode_text)
            intro_card_copies[episode_id] = {
                "text": text_value,
                "source": source_value,
            }

        production_preset_id = str(
            value.get("applied_production_preset_id") or ""
        ).strip()
        production_preset_revision = 0
        production_preset_hash = ""
        production_preset_dirty = bool(
            value.get("production_preset_dirty", False)
        )
        detached_preset_warning = ""
        if production_preset_id:
            requested_preset_id = production_preset_id
            preset_actor_user_id = str(
                value.get("created_by_user_id")
                or (existing or {}).get("created_by_user_id")
                or ""
            )
            presets = self.catalog.list_production_presets(
                actor_user_id=preset_actor_user_id or None
            ).get("items", [])
            selected_preset = next(
                (
                    item
                    for item in presets
                    if str(item.get("id") or "") == production_preset_id
                ),
                None,
            )
            if selected_preset is None:
                # A production preset is only a convenient source for the
                # complete settings already frozen in this draft.  Deleting
                # that source must not strand an employee's pending batch.
                # Drop provenance and keep the validated settings snapshot as
                # an ordinary per-batch customization.
                production_preset_id = ""
                production_preset_dirty = True
                detached_preset_warning = (
                    f"原制作方案 {requested_preset_id} 已删除；"
                    "已保留当前制作参数并转为本批自定义。"
                )
            else:
                production_preset_revision = int(
                    selected_preset.get("revision") or 1
                )
                production_preset_hash = str(
                    selected_preset.get("content_hash") or ""
                )
                expected_revision = int(
                    value.get("applied_production_preset_revision") or 0
                )
                expected_hash = str(
                    value.get("applied_production_preset_hash") or ""
                ).strip()
                if (
                    expected_revision
                    and expected_revision != production_preset_revision
                ):
                    raise ValueError("制作方案已更新，请重新套用后再生成。")
                if expected_hash and expected_hash != production_preset_hash:
                    raise ValueError("制作方案内容已更新，请重新套用后再生成。")
        metadata = {
            "platform_id": platform_id,
            "approvals": value.get("approvals")
            if isinstance(value.get("approvals"), dict)
            else {"main": "pending", "variants": {}},
            "video_folder": str(
                value.get("video_folder") or existing_metadata.get("video_folder") or ""
            ),
            "music_folder": str(
                value.get("music_folder") or existing_metadata.get("music_folder") or ""
            ),
            "output_folder": str(
                value.get("output_folder") or existing_metadata.get("output_folder") or ""
            ),
            "recipe_version": 1,
            "production_settings": production_settings,
            "voice": voice,
            "video_count_mode": "total",
            "episode_composition_mode": "merged_batch",
            "target_video_count": target_video_count,
            "story_mood": story_mood,
            "story_mood_source": story_mood_source,
            "intro_card_text": intro_card_text,
            "intro_card_source": intro_card_source,
            "intro_card_copies": intro_card_copies,
            "production_preset_id": production_preset_id,
            "production_preset_revision": production_preset_revision,
            "production_preset_hash": production_preset_hash,
            "production_preset_dirty": production_preset_dirty,
        }
        publishing_account_id = str(value.get("publishing_account_id") or "").strip()
        if publishing_account_id:
            accounts = self.catalog.list_publishing_accounts(include_archived=True)[
                "items"
            ]
            account = next(
                (
                    item
                    for item in accounts
                    if str(item.get("id")) == publishing_account_id
                ),
                None,
            )
            if account is None or str(account.get("status") or "") != "active":
                raise ValueError("所选发布账号已停用，请重新选择或保留待分配。")
            account_platform_id = str(
                (account.get("metadata") or {}).get("promotion_platform_id") or ""
            )
            if account_platform_id and account_platform_id != platform_id:
                raise ValueError("所选发布账号不属于当前小说平台。")
        fingerprint_payload = {
            "novel_id": novel_id,
            "binding_id": str(binding["id"]),
            "promo_code_id": promo_code_id,
            "publishing_account_id": publishing_account_id,
            "episode_ids": episode_ids,
            "target_video_count": target_video_count,
            "story_mood": story_mood,
            "voice": voice,
            "production_settings": production_settings,
            "video_folder": metadata["video_folder"],
            "music_folder": metadata["music_folder"],
            "output_folder": metadata["output_folder"],
            "intro_card_text": intro_card_text,
            "intro_card_source": intro_card_source,
            "subtitle_style_id": str(
                production_settings.get("subtitle_preset") or "clear_outline"
            ),
            "outro_style_id": str(value.get("outro_style_id") or "cover_focus"),
            "production_preset_id": production_preset_id,
            "production_preset_revision": production_preset_revision,
            "production_preset_hash": production_preset_hash,
        }
        configuration_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        duplicate = self.catalog.find_duplicate_draft_configuration(
            novel_id,
            configuration_fingerprint,
            exclude_draft_id=existing_id,
        )
        warnings: list[str] = []
        if detached_preset_warning:
            warnings.append(detached_preset_warning)
        if duplicate:
            warnings.append(
                "该小说已有一批完全相同的制作配置；本次仍已保存，请确认是否需要重复制作。"
            )
        metadata["configuration_fingerprint"] = configuration_fingerprint
        metadata["warnings"] = warnings
        draft = self.catalog.save_draft(
            {
                "id": existing_id or None,
                "novel_id": novel_id,
                "binding_id": binding["id"],
                "promo_code_id": promo_code_id,
                "publishing_account_id": publishing_account_id or None,
                "episode_ids": episode_ids,
                "creative_line_count": variant_count,
                "voice_profile": str(voice.get("profile") or ""),
                "subtitle_style_id": str(
                    production_settings.get("subtitle_preset") or "clear_outline"
                ),
                "outro_style_id": str(value.get("outro_style_id") or "cover_focus"),
                "status": "draft",
                "created_by_user_id": str(value.get("created_by_user_id") or "") or None,
                "metadata": metadata,
                "expected_version": (
                    value.get("expected_version")
                    if value.get("expected_version") is not None
                    else value.get("row_version")
                ),
            }
        )
        return {
            "novel": self.novel_for_ui(novel_id),
            "draft": self._ui_draft(draft),
            "warnings": warnings,
            "warning": warnings[0] if warnings else "",
        }

    def classify_novel(
        self,
        novel_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Classify one novel once per revision and share the result via Catalog.

        A configured cloud/local LLM is preferred.  The deterministic local
        provider remains a safe fallback, so choosing a novel never becomes
        impossible merely because an external text service is unavailable.
        """

        novel = self.catalog.get_novel(novel_id)
        revision = dict(novel.get("current_revision") or {})
        content_hash = str(revision.get("content_hash") or "")
        metadata = dict(novel.get("metadata") or {})
        existing = (
            dict(metadata.get("story_classification") or {})
            if isinstance(metadata.get("story_classification"), Mapping)
            else {}
        )
        if (
            not force
            and existing.get("mood") in _STORY_MOOD_LABELS
            and str(existing.get("content_hash") or "") == content_hash
        ):
            return {
                "classification": existing,
                "novel": self.novel_for_ui(novel_id),
                "cached": True,
            }

        body = str(revision.get("body") or "")
        sample = _classification_sample(body, str(novel.get("synopsis") or ""))
        settings = self._settings_getter()
        request = TextRequest(
            text=sample,
            title=str(novel.get("title") or ""),
            adult_mode=settings.adult_mode,
            retention_min=settings.retention_min,
            retention_max=settings.retention_max,
            language=str(novel.get("language_name") or settings.language or "English"),
            enforce_retention=False,
        )
        selected = str(settings.providers.text_provider or "local").strip().casefold()
        classification_source = "ai" if self._remote_text_provider or selected not in {
            "local",
            "local_rules",
            "local_rule",
            "local_passthrough",
            "passthrough",
        } else "local_rules"
        warning = ""
        try:
            # Provider construction itself can fail (for example a selected
            # cloud service with no API key).  Keep it inside the fallback
            # boundary so automatic classification never blocks novel choice.
            provider = self._text_provider_factory(settings.providers)
            result = provider.polish(request)
        except ProviderError as error:
            if not settings.providers.allow_provider_fallback or classification_source == "local_rules":
                raise
            warning = str(error) or type(error).__name__
            provider = (
                create_text_provider(ProviderConfig(name="local"))
                if self._remote_text_provider
                else self._text_provider_factory(ProviderConfig(name="local"))
            )
            result = provider.polish(request)
            classification_source = "local_fallback"
        if self._remote_text_provider and str(result.provider or "").casefold() in {
            "local",
            "local_rules",
            "local_rule",
            "local_passthrough",
            "passthrough",
        }:
            classification_source = "local_fallback"
            warning = warning or "Hub 文本服务不可用，已使用本机确定性规则判断。"
        try:
            mood = canonical_mood(result.mood)
        except MediaError:
            local_provider = (
                create_text_provider(ProviderConfig(name="local"))
                if self._remote_text_provider
                else self._text_provider_factory(ProviderConfig(name="local"))
            )
            local_result = local_provider.polish(request)
            mood = canonical_mood(local_result.mood)
            result = local_result
            classification_source = "local_fallback"
            warning = warning or "AI 返回了不支持的题材，已使用本地判断。"

        classification = {
            "mood": mood,
            "label": _STORY_MOOD_LABELS[mood],
            "source": classification_source,
            "provider": str(result.provider or selected or "local"),
            "model": str(result.model or ""),
            "content_hash": content_hash,
            "revision_id": str(revision.get("id") or ""),
            "classified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "warning": warning,
        }
        saved_novel = self.catalog.save_novel_classification(
            novel_id,
            classification,
        )
        saved_metadata = dict(saved_novel.get("metadata") or {})
        if isinstance(saved_metadata.get("story_classification"), Mapping):
            classification = dict(saved_metadata["story_classification"])
        return {
            "classification": classification,
            "novel": self.novel_for_ui(novel_id),
            "cached": False,
        }

    def generate_voice_candidates(
        self,
        novel_id: str,
        mood: str = "suspense",
        *,
        narration_wpm: int | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        novel = self.catalog.get_novel(novel_id)
        language_code = str(
            novel.get("language_code") or novel.get("language") or "unknown"
        )
        language_confidence = float(novel.get("language_confidence") or 0.0)
        if language_code in {"unknown", "mixed", "other"} or language_confidence < 0.64:
            raise ValueError(
                "小说语种尚未可靠确认，请先在小说库手动确认语种，"
                "再生成女声候选。"
            )
        settings = self._settings_getter()
        tts_provider = str(settings.providers.tts_provider or "").strip().casefold()
        if tts_provider in _LOCAL_KOKORO_PROVIDER_ALIASES:
            ensure_kokoro_language_available(
                language_code,
                provider=tts_provider or "local_kokoro",
                endpoint=settings.providers.kokoro_endpoint,
                command=settings.providers.kokoro_command,
            )
        if not available_female_voice_candidates(
            tts_provider,
            language_code,
            endpoint=settings.providers.kokoro_endpoint,
            command=settings.providers.kokoro_command,
        ):
            language_name = str(novel.get("language_name") or language_code)
            raise ValueError(
                f"当前配音服务 {tts_provider or '未配置'} 暂无{language_name}女声。"
                "请切换支持该语种的配音服务，或由管理员补充对应声线。"
            )
        revision = novel.get("current_revision") or {}
        preview_kwargs: dict[str, Any] = {"language": language_code}
        if (
            narration_wpm is not None
            and "narration_wpm"
            in inspect.signature(self.voice_previews.generate).parameters
        ):
            preview_kwargs["narration_wpm"] = narration_wpm
        candidates = self.voice_previews.generate(
            str(revision.get("body") or ""),
            mood,
            self.data_dir / "voice-previews" / novel_id,
            **preview_kwargs,
        )
        if persist:
            self.catalog.save_novel_voice_state(
                novel_id,
                {"voice_candidates": candidates},
            )
        return {"candidates": candidates, "novel": self.novel_for_ui(novel_id)}

    def lock_voice(self, novel_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        """Remember a convenient default voice without locking future batches."""

        novel = self.catalog.get_novel(novel_id)
        metadata = dict(novel.get("metadata") or {})
        provider = str(value.get("provider") or "").strip()
        voice_id = str(value.get("voice_id") or "").strip()
        if not provider or not voice_id:
            raise ValueError("请选择一个有效的候选女声。")
        candidates = [
            candidate
            for candidate in list(metadata.get("voice_candidates") or [])
            if isinstance(candidate, Mapping)
        ]
        if candidates and not any(
            str(candidate.get("provider") or "").strip() == provider
            and str(candidate.get("voice_id") or "").strip() == voice_id
            for candidate in candidates
        ):
            raise ValueError("所选声音不在本小说当前的 3 个试听候选中，请重新试听。")
        self.catalog.save_novel_voice_state(
            novel_id,
            {
                "preferred_voice_provider": provider,
                "preferred_voice_id": voice_id,
                "preferred_voice_label": str(value.get("label") or voice_id),
                "preferred_voice_profile": str(value.get("profile") or ""),
            },
        )
        return self.novel_for_ui(novel_id)

    def _preflight_draft_voice(
        self,
        novel: Mapping[str, Any],
        draft_metadata: dict[str, Any],
        *,
        language: str,
    ) -> tuple[str, str, dict[str, Any]]:
        """Validate the voice frozen on this draft before queueing.

        Older drafts can contain a provider alias or a voice which no longer
        exists in the selected provider/language catalog.  A provider alias is
        normalized without changing the actor.  A removed voice is migrated
        only to an already-previewed voice with the same delivery profile; if
        that is impossible, candidates are regenerated once and the user is
        asked to select again. Every automatic repair is retained in this
        draft; later batches remain free to choose another voice.
        """

        novel_metadata = dict(novel.get("metadata") or {})
        voice = (
            dict(draft_metadata.get("voice") or {})
            if isinstance(draft_metadata.get("voice"), Mapping)
            else {}
        )
        raw_provider = str(voice.get("provider") or "").strip()
        raw_voice_id = str(voice.get("voice_id") or "").strip()
        if not raw_provider or not raw_voice_id:
            raise ValueError("请在制作台试听并选择本批女声。")

        provider = _canonical_voice_provider(raw_provider)
        settings = self._settings_getter()
        if provider in _LOCAL_KOKORO_PROVIDER_ALIASES:
            ensure_kokoro_language_available(
                language,
                provider=provider,
                endpoint=settings.providers.kokoro_endpoint,
                command=settings.providers.kokoro_command,
            )
        available = list(
            available_female_voice_candidates(
                provider,
                language,
                endpoint=settings.providers.kokoro_endpoint,
                command=settings.providers.kokoro_command,
            )
        )
        if not available:
            language_name = str(novel.get("language_name") or language)
            raise ValueError(
                f"当前配音服务 {raw_provider} 暂无{language_name}女声，"
                "请在制作台更换配音服务后重新试听。"
            )

        option_by_id = {item.voice_id: item for item in available}
        selected = option_by_id.get(raw_voice_id)
        candidate_rows = [
            dict(item)
            for item in list(novel_metadata.get("voice_candidates") or [])
            if isinstance(item, Mapping)
        ]
        old_candidate = next(
            (
                item
                for item in candidate_rows
                if _canonical_voice_provider(item.get("provider")) == provider
                and str(item.get("voice_id") or "").strip() == raw_voice_id
            ),
            None,
        )
        desired_profile = str(
            voice.get("profile")
            or (old_candidate or {}).get("profile")
            or ""
        ).strip()
        regenerated = False

        if selected is None:
            selected = next(
                (
                    option
                    for option in available
                    if desired_profile and option.profile == desired_profile
                    and any(
                        _canonical_voice_provider(candidate.get("provider")) == provider
                        and str(candidate.get("voice_id") or "").strip()
                        == option.voice_id
                        for candidate in candidate_rows
                    )
                ),
                None,
            )
            if selected is None:
                # Rebuild the shortlist once on this device.  This both probes
                # the configured engine and replaces stale candidate ids before
                # any task is queued.
                configured_provider = _canonical_voice_provider(
                    self._settings_getter().providers.tts_provider
                )
                if configured_provider != provider:
                    raise ValueError(
                        "该批次的旧配音服务与本机当前服务不同。"
                        "请在制作台重新生成候选配音并选择一次。"
                    )
                try:
                    rebuilt = self.generate_voice_candidates(
                        str(novel["id"]),
                        str(draft_metadata.get("story_mood") or "suspense"),
                        narration_wpm=int(
                            (draft_metadata.get("production_settings") or {}).get(
                                "narration_wpm",
                                self._settings_getter().narration_wpm,
                            )
                        ),
                        persist=False,
                    )
                except (ProviderError, ValueError, OSError) as error:
                    raise ValueError(
                        "旧草稿锁定的女声已不可用，且本机无法重建候选配音："
                        f"{error}"
                    ) from error
                candidate_rows = [
                    dict(item)
                    for item in list(rebuilt.get("candidates") or [])
                    if isinstance(item, Mapping)
                ]
                regenerated = True
                selected = next(
                    (
                        option_by_id.get(str(candidate.get("voice_id") or "").strip())
                        for candidate in candidate_rows
                        if _canonical_voice_provider(candidate.get("provider")) == provider
                        and str(candidate.get("profile") or "").strip()
                        == desired_profile
                        and option_by_id.get(
                            str(candidate.get("voice_id") or "").strip()
                        )
                        is not None
                    ),
                    None,
                )

            if selected is None:
                suffix = "，已重新生成候选声音" if regenerated else ""
                raise ValueError(
                    "旧草稿锁定的女声已不可用"
                    f"{suffix}；请在制作台为本批重新选择一次。"
                )

        matching_candidate = next(
            (
                item
                for item in candidate_rows
                if _canonical_voice_provider(item.get("provider")) == provider
                and str(item.get("voice_id") or "").strip() == selected.voice_id
            ),
            {},
        )
        resolved_voice = {
            "provider": provider,
            "voice_id": selected.voice_id,
            "label": str(matching_candidate.get("label") or selected.label),
            "profile": str(matching_candidate.get("profile") or selected.profile),
        }
        changed = raw_provider != provider or raw_voice_id != selected.voice_id
        if not changed:
            return provider, selected.voice_id, resolved_voice

        reason = (
            "provider_alias_normalized"
            if raw_voice_id == selected.voice_id
            else "legacy_voice_replaced_same_profile"
        )
        migration = {
            "migrated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reason": reason,
            "language": str(language),
            "from": {
                "provider": raw_provider,
                "voice_id": raw_voice_id,
                "label": str(voice.get("label") or raw_voice_id),
                "profile": desired_profile,
            },
            "to": dict(resolved_voice),
        }
        migrations = [
            dict(item)
            for item in list(draft_metadata.get("voice_migrations") or [])
            if isinstance(item, Mapping)
        ]
        migrations.append(migration)
        draft_metadata["voice_migrations"] = migrations[-50:]
        draft_metadata["voice"] = dict(resolved_voice)

        # Automatic preflight repair is deliberately draft-scoped.  The
        # ``novel`` argument is a snapshot which may already be stale when a
        # worker reaches this point; writing the migrated voice/candidates
        # back through it could overwrite an administrator's newer shared
        # lock.  Novel-level voice state is changed only by the explicit voice
        # candidate/lock operations above.

        return provider, selected.voice_id, resolved_voice

    @staticmethod
    def _episode_text(episode: Mapping[str, Any]) -> str:
        metadata = dict(episode.get("metadata") or {})
        return str(metadata.get("text") or "").strip()

    def _recap_from_previous(self, previous_text: str) -> str:
        """Create an 8–12 second factual recap from the preceding episode."""

        normalized = re.sub(r"\s+", " ", previous_text).strip()
        if not normalized:
            return ""
        sentences = [
            item.strip()
            for item in re.split(r"(?<=[.!?])[\"'’]*\s+", normalized)
            if item.strip()
        ]
        # Ten seconds at the currently selected WPM leaves room for the spoken
        # “Previously” cue.  Prefer complete sentences; only crop when the most
        # recent sentence alone is longer than the target.
        wpm = max(200, min(280, int(self._settings_getter().narration_wpm)))
        max_words = max(30, min(46, round(wpm * 10 / 60) - 1))
        selected: list[str] = []
        selected_words = 0
        for sentence in reversed(sentences):
            count = len(sentence.split())
            if selected and selected_words + count > max_words:
                break
            selected.insert(0, sentence)
            selected_words += count
            if selected_words >= round(max_words * 0.72):
                break
        tail = " ".join(selected or sentences[-1:])
        words = tail.split()
        if len(words) > max_words:
            tail = " ".join(words[-max_words:])
            if tail and tail[0].islower():
                tail = tail[0].upper() + tail[1:]
        tail = tail.strip()
        if not tail:
            return ""
        return f"Previously, {tail}"

    @staticmethod
    def _job_seed(draft_id: str, episode_id: str, variant_index: int) -> int:
        digest = hashlib.sha256(
            f"{draft_id}:{episode_id}:{variant_index}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:8], "big") & 0x7FFFFFFF

    @staticmethod
    def _episode_group_label(episodes: list[Mapping[str, Any]]) -> str:
        """Return a compact, sortable label for one merged episode group."""

        ordinals = [
            max(1, int(episode.get("ordinal") or index))
            for index, episode in enumerate(episodes, start=1)
        ]
        if len(ordinals) == 1:
            return f"E{ordinals[0]:03d}"
        contiguous = ordinals == list(range(ordinals[0], ordinals[-1] + 1))
        if contiguous:
            return f"E{ordinals[0]:03d}-E{ordinals[-1]:03d}"
        if len(ordinals) <= 6:
            return "_".join(f"E{ordinal:03d}" for ordinal in ordinals)
        return f"E{ordinals[0]:03d}-E{ordinals[-1]:03d}_X{len(ordinals)}"

    @staticmethod
    def production_settings_snapshot(settings: AppSettings) -> dict[str, Any]:
        """Freeze approved creative settings without copying API secrets."""

        return {
            "output_mode": settings.output_mode,
            "source_narration_audio": "",
            "video_playback_speed": settings.video_playback_speed,
            "video_transition": settings.video_transition,
            "subtitle_word_mode": settings.subtitle_word_mode,
            "language": settings.language,
            "retention_min": settings.retention_min,
            "retention_max": settings.retention_max,
            "adult_mode": settings.adult_mode,
            "narration_wpm": settings.narration_wpm,
            "chapter_pause_seconds": settings.chapter_pause_seconds,
            "output_width": settings.output_width,
            "output_height": settings.output_height,
            "output_fps": settings.output_fps,
            "export_narration_audio": settings.export_narration_audio,
            "bgm_volume": settings.bgm_volume,
            "bgm_mode": settings.bgm_mode,
            "bgm_file": settings.bgm_file,
            "voice_by_mood": dict(settings.voice_by_mood),
            "subtitle": asdict(settings.subtitle),
            "intro_card": asdict(settings.intro_card),
            "code_card": asdict(settings.code_card),
            "outro_card": asdict(settings.outro_card),
            "caption_mode": settings.caption_mode,
            "subtitle_preset": settings.subtitle_preset,
            "intro_card_preset": settings.intro_card_preset,
            "code_card_preset": settings.code_card_preset,
            "outro_card_preset": settings.outro_card_preset,
            "subtitle_animation": settings.subtitle_animation,
            "intro_animation": settings.intro_animation,
            "preview_seconds": settings.preview_seconds,
            "intro_card_duration_seconds": settings.intro_card_duration_seconds,
            "max_episode_minutes": settings.max_episode_minutes,
            "cover_animation": settings.cover_animation,
            "cover_outro_enabled": settings.cover_outro_enabled,
            "color_grade": settings.color_grade,
            "end_card_seconds": settings.end_card_seconds,
            "render_mode": settings.render_mode,
            "video_template": settings.video_template,
            "video_encoder": settings.video_encoder,
            "providers": {
                "text_provider": settings.providers.text_provider,
                "text_model": settings.providers.text_model,
                "text_endpoint": settings.providers.text_endpoint,
                "tts_provider": settings.providers.tts_provider,
                "tts_endpoint": settings.providers.tts_endpoint,
                "kokoro_endpoint": settings.providers.kokoro_endpoint,
                "kokoro_command": settings.providers.kokoro_command,
                "allow_provider_fallback": settings.providers.allow_provider_fallback,
            },
        }

    def build_render_jobs(
        self,
        value: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str, list[RenderJob]]:
        """Materialize a render plan for legacy/internal callers.

        The production API uses :meth:`build_render_job_plan` so an unlimited
        target count never requires one giant in-memory list.  Keeping this
        wrapper preserves the existing library-service contract used by
        focused tooling and older integrations.
        """

        draft, platform_id, _total_count, jobs = self.build_render_job_plan(value)
        return draft, platform_id, list(jobs)

    def build_render_job_plan(
        self,
        value: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str, int, Iterator[RenderJob]]:
        """Build a lazy, directly runnable full-video plan from a draft.

        Historical preview jobs remain readable by the queue and API, but new
        production runs no longer create a rendered sample or wait for manual
        sample approval.  The browser's live visual/voice preview is the only
        preview step in the current workflow.  Job objects are yielded one at
        a time so target counts are constrained only by available disk/time,
        not by a fixed application or memory cap.
        """

        draft_id = str(value.get("draft_id") or "").strip()
        if not draft_id:
            raise ValueError("请先保存生产草稿。")
        draft = self.catalog.get_draft(draft_id)
        novel = self.catalog.get_novel(str(draft["novel_id"]))
        binding = next(
            (
                item
                for item in novel.get("bindings", [])
                if str(item.get("id")) == str(draft["binding_id"])
            ),
            None,
        )
        if binding is None:
            raise ValueError("生产草稿对应的平台绑定已不存在。")
        platform_id = str(binding["platform_id"])

        draft_metadata = dict(draft.get("metadata") or {})
        saved_recipe = draft_metadata.get("production_settings")
        frozen_recipe = (
            dict(saved_recipe) if isinstance(saved_recipe, Mapping) else {}
        )
        output_mode = str(
            frozen_recipe.get("output_mode") or "video_and_mp3"
        ).strip().casefold()
        effective_language = str(
            frozen_recipe.get("language")
            or novel.get("language_code")
            or novel.get("language")
            or "unknown"
        )
        language_confidence = float(
            frozen_recipe.get("language_confidence")
            if frozen_recipe.get("language_confidence") is not None
            else novel.get("language_confidence") or 0.0
        )
        if (
            (
                effective_language in {"unknown", "mixed", "other"}
                or language_confidence < 0.64
            )
            and value.get("confirm_language") is not True
        ):
            raise ValueError(
                "小说语种尚未可靠确认。请先在小说库确认语种，"
                "或明确确认后再生成完整视频。"
            )
        if output_mode == "reuse_audio":
            draft_voice = (
                dict(draft_metadata.get("voice") or {})
                if isinstance(draft_metadata.get("voice"), Mapping)
                else {}
            )
            locked_provider = str(draft_voice.get("provider") or "").strip()
            locked_voice = str(draft_voice.get("voice_id") or "").strip()
        else:
            locked_provider, locked_voice, draft_voice = self._preflight_draft_voice(
                novel,
                draft_metadata,
                language=effective_language,
            )
        if locked_provider.casefold() == "local_kokoro" and locked_voice:
            try:
                kokoro_language_code(effective_language, locked_voice)
            except ProviderError as error:
                raise ValueError(str(error)) from error
        folders = {
            "video_folder": str(
                value.get("video_folder") or draft_metadata.get("video_folder") or ""
            ).strip(),
            "music_folder": str(
                value.get("music_folder") or draft_metadata.get("music_folder") or ""
            ).strip(),
            "output_folder": str(
                value.get("output_folder") or draft_metadata.get("output_folder") or ""
            ).strip(),
        }
        source_narration_audio = str(
            value.get("source_narration_audio")
            or frozen_recipe.get("source_narration_audio")
            or ""
        ).strip()
        source_narration_suffix = Path(source_narration_audio).suffix.casefold()
        source_is_storyforge_video = (
            output_mode == "reuse_audio" and source_narration_suffix in VIDEO_EXTENSIONS
        )
        bgm_mode = str(frozen_recipe.get("bgm_mode") or "auto").strip().casefold()
        if source_is_storyforge_video:
            bgm_mode = "none"
        bgm_file = str(
            value.get("bgm_file") or frozen_recipe.get("bgm_file") or ""
        ).strip()
        if output_mode != "audio_only":
            if not folders["video_folder"] or not Path(
                folders["video_folder"]
            ).is_dir():
                raise ValueError("视频素材文件夹不存在，请重新选择。")
            if bgm_mode == "auto" and (
                not folders["music_folder"]
                or not Path(folders["music_folder"]).is_dir()
            ):
                raise ValueError("背景音乐文件夹不存在，请重新选择。")
            if bgm_mode == "manual" and (
                not bgm_file or not Path(bgm_file).expanduser().is_file()
            ):
                raise ValueError("手动背景音乐文件不存在，请重新选择。")
        if output_mode == "reuse_audio" and (
            not source_narration_audio
            or not Path(source_narration_audio).expanduser().is_file()
        ):
            raise ValueError("复用的旁白音频不存在，请重新选择。")
        if (
            output_mode == "reuse_audio"
            and source_narration_suffix not in _REUSABLE_NARRATION_EXTENSIONS
        ):
            raise ValueError(
                "复用旁白只接受 StoryForge 输出的 MP3 配音或 MP4/MOV/MKV/WEBM 成品视频。"
            )
        if not folders["output_folder"]:
            raise ValueError("请选择输出文件夹。")
        Path(folders["output_folder"]).expanduser().mkdir(parents=True, exist_ok=True)

        episodes = list(draft.get("episodes") or [])
        if not episodes:
            raise ValueError("当前草稿没有选择任何分集。")
        revision_numbers = {
            str(item.get("id") or ""): int(item.get("revision_number") or 0)
            for item in novel.get("revisions") or []
        }
        # A draft stores the order in which boxes were clicked. Production order
        # must instead follow the manuscript, so a reversed or non-contiguous
        # selection still renders in the author's episode order.
        episodes.sort(
            key=lambda item: (
                revision_numbers.get(str(item.get("revision_id") or ""), 0),
                int(item.get("ordinal") or 0),
                int(item.get("selection_ordinal") or 0),
            )
        )
        selected_revision_ids = {
            str(item.get("revision_id") or "").strip()
            for item in episodes
            if str(item.get("revision_id") or "").strip()
        }
        if len(selected_revision_ids) != 1:
            raise ValueError("同一批次选择的分集必须来自同一个小说版本。")
        selected_revision_id = next(iter(selected_revision_ids))
        legacy_variant_count = max(1, int(draft.get("creative_line_count") or 1))
        target_video_count = max(
            1,
            int(draft_metadata.get("target_video_count") or legacy_variant_count),
        )
        # "仅生成配音" produces one reusable narration for the merged episode
        # selection.  Repeating the same TTS job according to the video variant
        # count wastes time, disk and any metered provider quota.
        batch_total_count = 1 if output_mode == "audio_only" else target_video_count
        account_label = "待分配"
        account_id = str(draft.get("publishing_account_id") or "")
        if account_id:
            accounts = self.catalog.list_publishing_accounts(include_archived=True)[
                "items"
            ]
            account = next(
                (item for item in accounts if str(item.get("id")) == account_id),
                None,
            )
            if account is not None:
                account_label = str(
                    account.get("display_name") or account.get("handle") or "待分配"
                )

        source_root = self.data_dir / "production-inputs" / safe_component(draft_id)
        source_root.mkdir(parents=True, exist_ok=True)
        episode_texts = [self._episode_text(item) for item in episodes]
        if any(not item for item in episode_texts):
            raise ValueError("所选分集缺少正文，请重新导入小说后再生成。")

        # Build context from every episode in the current revision, but create
        # only one recap for the first episode of the complete selected group.
        # Thus a group starting at E004 recaps E003 once; later selected
        # episodes never insert another recap. Older frozen drafts keep their
        # stored recap when their full revision is no longer exposed.
        previous_text_by_episode_id: dict[str, str] = {}
        current_revision = dict(novel.get("current_revision") or {})
        current_revision_id = str(current_revision.get("id") or "")
        ordered_revision_episodes = sorted(
            list(current_revision.get("episodes") or []),
            key=lambda item: int(item.get("ordinal") or 0),
        )
        episode_count = len(ordered_revision_episodes)
        final_episode_id = (
            str(ordered_revision_episodes[-1].get("id") or "")
            if ordered_revision_episodes
            else ""
        )
        previous_text = ""
        for context_episode in ordered_revision_episodes:
            context_id = str(context_episode.get("id") or "")
            if context_id:
                previous_text_by_episode_id[context_id] = previous_text
            previous_text = self._episode_text(context_episode)

        cover_path = str(novel.get("cover_path") or "")
        production_run_id = uuid4().hex[:12]
        jobs = _RenderJobSpool(
            self.data_dir / "job-spool" / f"{production_run_id}.jsonl"
        )
        live_snapshot = self.production_settings_snapshot(self._settings_getter())
        settings_snapshot = self._validated_production_settings(
            saved_recipe if isinstance(saved_recipe, Mapping) else None,
            base=(saved_recipe if isinstance(saved_recipe, Mapping) else live_snapshot),
        )
        settings_snapshot["source_narration_audio"] = source_narration_audio
        settings_snapshot["bgm_file"] = bgm_file
        if source_is_storyforge_video:
            # The selected StoryForge video already contains its complete audio
            # mix.  Adding another BGM track would duplicate the music.  The
            # pipeline still requires the private StoryForge narration index,
            # so an arbitrary external video cannot pass as a reuse source.
            settings_snapshot["bgm_mode"] = "none"
        provider_snapshot = dict(settings_snapshot.get("providers") or {})
        provider_snapshot["tts_provider"] = locked_provider
        settings_snapshot["providers"] = provider_snapshot
        settings_snapshot["source_language"] = str(
            novel.get("language_code") or novel.get("language") or "en"
        )
        try:
            story_mood = canonical_mood(
                str(draft_metadata.get("story_mood") or "suspense")
            )
        except MediaError as error:
            raise ValueError("生产草稿中的故事类型无效，请重新保存本批设置。") from error
        story_mood_source = str(
            draft_metadata.get("story_mood_source") or "auto"
        )
        voice_profile = str(
            draft_voice.get("profile") or draft.get("voice_profile") or ""
        ).strip()
        if voice_profile in {"dramatic", "warm", "calm", "confident"}:
            settings_snapshot["voice_by_mood"] = {
                mood: voice_profile for mood in ("suspense", "romance", "sad", "revenge")
            }
        saved_intro_copies = (
            dict(draft_metadata.get("intro_card_copies") or {})
            if isinstance(draft_metadata.get("intro_card_copies"), Mapping)
            else {}
        )
        saved_intro_text = str(draft_metadata.get("intro_card_text") or "").strip()
        saved_intro_source = str(
            draft_metadata.get("intro_card_source") or ""
        ).strip()
        novel_synopsis = str(novel.get("synopsis") or "")
        first_episode = episodes[0]
        first_episode_id = str(first_episode.get("id") or "")
        first_episode_number = max(1, int(first_episode.get("ordinal") or 1))
        if first_episode_number <= 1:
            recap = ""
        elif selected_revision_id == current_revision_id:
            recap = self._recap_from_previous(
                previous_text_by_episode_id.get(first_episode_id, "")
            )
        else:
            recap = str(first_episode.get("recap_text") or "").strip()

        frozen_intro = saved_intro_copies.get(first_episode_id)
        if isinstance(frozen_intro, Mapping):
            intro_card_text = str(frozen_intro.get("text") or "").strip()
            intro_card_source = str(frozen_intro.get("source") or "").strip()
        elif saved_intro_text:
            intro_card_text = saved_intro_text
            intro_card_source = saved_intro_source or "saved_draft"
        else:
            # Compatibility for drafts saved before frozen intro copy was
            # introduced. Never call AI while materializing render jobs.
            intro_card_text, intro_card_source = _intro_card_excerpt(
                novel_synopsis,
                episode_texts[0],
            )

        ordered_episode_ids = tuple(
            str(episode.get("id") or "") for episode in episodes
        )
        episode_label = self._episode_group_label(episodes)
        narration_parts = [recap] if recap else []
        for selection_index, (episode, episode_text) in enumerate(
            zip(episodes, episode_texts, strict=True), start=1
        ):
            episode_number = max(
                1, int(episode.get("ordinal") or selection_index)
            )
            # The pipeline strips this standalone marker from both narration
            # and captions while retaining the configured 0.8 second pause.
            narration_parts.append(f"Chapter {episode_number}\n{episode_text}")
        narration_text = "\n\n".join(narration_parts)
        source_episode_code = re.sub(r"[^A-Za-z0-9]", "", episode_label) or "E001"
        source_name = safe_component(
            f"{source_episode_code}_{draft['promo_code_snapshot']}_{draft['novel_title_snapshot']}",
            fallback=f"{source_episode_code}_story",
        )
        source_file = source_root / f"{source_name}.txt"
        source_file.write_text(narration_text, encoding="utf-8", newline="\n")
        fingerprint = hashlib.sha256(narration_text.encode("utf-8")).hexdigest()
        group_seed_key = "|".join(ordered_episode_ids)
        selected_is_final = bool(
            selected_revision_id == current_revision_id
            and final_episode_id
            and final_episode_id in ordered_episode_ids
        )

        for variant_index in range(1, batch_total_count + 1):
            jobs.append(
                RenderJob(
                    batch_id=draft_id,
                    platform_id=platform_id,
                    source_file=str(source_file),
                    title=str(draft["novel_title_snapshot"]),
                    code=str(draft["promo_code_snapshot"]),
                    video_folder=folders["video_folder"],
                    music_folder=folders["music_folder"],
                    output_folder=folders["output_folder"],
                    status=JobStatus.QUEUED,
                    stage_label=(
                        "等待生成纯旁白配音"
                        if output_mode == "audio_only"
                        else (
                            "等待使用现有旁白生成视频"
                            if output_mode == "reuse_audio"
                            else "等待生成完整视频与配音"
                        )
                    ),
                    novel_id=str(draft["novel_id"]),
                    revision_id=selected_revision_id,
                    episode_id=first_episode_id,
                    episode_ids=ordered_episode_ids,
                    episode_label=episode_label,
                    listing_id=str(draft["binding_id"]),
                    promo_code_id=str(draft["promo_code_id"]),
                    promo_code_snapshot=str(draft["promo_code_snapshot"]),
                    production_draft_id=draft_id,
                    production_run_id=production_run_id,
                    publishing_account_id=account_id,
                    publishing_account_label=account_label,
                    batch_total_count=batch_total_count,
                    batch_ordinal=variant_index,
                    episode_number=first_episode_number,
                    episode_count=episode_count,
                    is_final_episode=selected_is_final,
                    variant_index=variant_index,
                    variant_count=batch_total_count,
                    variant_seed=self._job_seed(
                        draft_id, group_seed_key, variant_index
                    ),
                    job_kind="full",
                    preview_approved=False,
                    locked_voice_provider=locked_provider,
                    locked_voice_id=locked_voice,
                    story_mood=story_mood,
                    story_mood_source=story_mood_source,
                    content_fingerprint=fingerprint,
                    cover_path=cover_path,
                    cover_outro_enabled=bool(
                        settings_snapshot.get("cover_outro_enabled", True)
                    ),
                    intro_card_text=intro_card_text,
                    intro_card_source=intro_card_source,
                    production_preset_id=str(
                        draft_metadata.get("production_preset_id") or ""
                    ),
                    production_preset_revision=int(
                        draft_metadata.get("production_preset_revision") or 0
                    ),
                    production_preset_hash=str(
                        draft_metadata.get("production_preset_hash") or ""
                    ),
                    production_preset_dirty=bool(
                        draft_metadata.get("production_preset_dirty", False)
                    ),
                    settings_snapshot=settings_snapshot,
                )
            )

        draft_metadata.update(
            {
                "video_folder": (
                    "worker://local/videos"
                    if self._remote_text_provider
                    else folders["video_folder"]
                ),
                "music_folder": (
                    "worker://local/music"
                    if self._remote_text_provider
                    else folders["music_folder"]
                ),
                "output_folder": (
                    "worker://local/output"
                    if self._remote_text_provider
                    else folders["output_folder"]
                ),
            }
        )
        draft_metadata.update(
            {
                "platform_id": platform_id,
                "revision_id": selected_revision_id,
                "content_hash": str(
                    (novel.get("current_revision") or {}).get("content_hash") or ""
                ),
                "job_count": batch_total_count,
                "episode_count_snapshot": episode_count,
                "episode_ids": list(ordered_episode_ids),
                "episode_label": episode_label,
                "episode_composition_mode": "merged_batch",
                "last_production_run_id": production_run_id,
            }
        )
        draft = self.catalog.save_draft(
            {
                "id": draft_id,
                "metadata": draft_metadata,
                "status": "ready",
            }
        )
        return draft, platform_id, batch_total_count, iter(jobs)
