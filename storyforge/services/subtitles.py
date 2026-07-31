"""Sentence-level ASS subtitle generation for vertical StoryForge videos."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import re
import tempfile
import textwrap
import unicodedata
from typing import Iterable, Mapping, Sequence

from ..models import INTRO_ANIMATIONS, SUBTITLE_ANIMATIONS


PathLike = str | os.PathLike[str]


# The TikTok action rail still needs breathing room, but the visual center of
# a portrait video must remain the physical frame center.  The previous
# asymmetric canvas moved every story-card element 58px left at 1080p (and
# 29px in the approval sample), which made an otherwise correct render look
# accidental.  A symmetric content rail keeps the whole composition centred
# while retaining the old 188px right-edge clearance on both sides.  That
# preserves the proven TikTok action-rail boundary instead of gaining visual
# width by drifting back under the platform controls.
_STORY_SAFE_HORIZONTAL = 188
_STORY_SAFE_TOP = 150
_STORY_SAFE_BOTTOM = 360
_STORY_REFERENCE_WIDTH = 1080
_STORY_REFERENCE_HEIGHT = 1920
_STORY_INTRO_PANEL_Y = 520
_STORY_INTRO_LOGO_SIZE = 64
_STORY_INTRO_LOGO_TOP = 550
_NEUTRAL_STORY_LABEL = "STORY BRIEF"
_NUMBERED_PART_RE = re.compile(r"\bpart\s+\d+(?:\s+of\s+\d+)?\b", re.IGNORECASE)
_NO_LINE_START = frozenset("，。！？；：、,.!?;:)]}》〉」』】”’")


@dataclass(frozen=True)
class NarrationSentence:
    """One sentence (or a silent chapter marker) in narration order."""

    text: str
    duration: float | None = None
    is_chapter: bool = False
    gap_after: float = 0.0

    def __post_init__(self) -> None:
        if self.duration is not None and self.duration <= 0:
            raise ValueError("duration must be positive when supplied")
        if self.gap_after < 0:
            raise ValueError("gap_after cannot be negative")


@dataclass(frozen=True)
class SubtitleCue:
    """One complete sentence shown for a bounded interval."""

    start: float
    end: float
    text: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.start) or self.start < 0:
            raise ValueError("cue start must be a non-negative finite number")
        if not math.isfinite(self.end) or self.end <= self.start:
            raise ValueError("cue end must be later than cue start")
        if not self.text.strip():
            raise ValueError("cue text cannot be empty")


@dataclass(frozen=True)
class SubtitleTimeline:
    cues: tuple[SubtitleCue, ...]
    total_duration: float
    chapter_pause_count: int = 0


@dataclass(frozen=True)
class AssStyleConfig:
    """User-adjustable style values with TikTok-safe defaults for 1080x1920."""

    play_res_x: int = 1080
    play_res_y: int = 1920
    font_name: str = "Arial"
    subtitle_font_size: int = 52
    subtitle_text_color: str = "#FFFFFF"
    subtitle_outline_color: str = "#000000"
    subtitle_outline: float = 3.5
    subtitle_shadow: float = 1.0
    subtitle_bold: bool = True
    subtitle_italic: bool = False
    subtitle_margin_left: int = 180
    subtitle_margin_right: int = 180
    subtitle_margin_bottom: int = 300
    subtitle_background_color: str = "#000000"
    subtitle_background_opacity: float = 0.0
    subtitle_alignment: str = "center"
    subtitle_position_x_percent: float = 50.0
    card_font_size: int = 50
    card_text_color: str = "#FFFFFF"
    card_background_color: str = "#111111"
    card_background_opacity: float = 0.72
    card_margin_left: int = 150
    card_margin_right: int = 150
    card_margin_top: int = 140
    card_bold: bool = True
    card_outline_color: str = "#FFFFFF"
    card_outline_width: float = 0.0
    card_alignment: str = "center"
    card_position_x_percent: float = 50.0
    card_position_y_percent: float = 7.8
    card_width_percent: float = 65.2
    card_padding: int = 14
    card_radius: int = 18
    intro_font_name: str = "Arial"
    intro_headline_font_size: int = 62
    intro_headline_color: str = "#FFE06A"
    intro_body_font_size: int = 36
    intro_body_color: str = "#172033"
    intro_label_font_size: int = 26
    intro_label_color: str = "#667085"
    intro_background_color: str = "#FFFFFF"
    intro_background_opacity: float = 1.0
    intro_border_color: str = "#FFFFFF"
    intro_border_width: int = 0
    intro_shadow_opacity: float = 0.34
    intro_width_percent: float = 65.2
    intro_position_x_percent: float = 50.0
    intro_position_y_percent: float = 27.1
    intro_padding: int = 56
    intro_radius: int = 32
    intro_text_alignment: str = "center"
    intro_max_lines: int = 5
    intro_animation: str = "fade_rise"
    outro_font_name: str = "Arial"
    outro_title_font_size: int = 58
    outro_title_color: str = "#111827"
    outro_body_font_size: int = 38
    outro_body_color: str = "#111827"
    outro_code_font_size: int = 56
    outro_code_color: str = "#3535E5"
    outro_background_color: str = "#FFFFFF"
    outro_background_opacity: float = 1.0
    outro_border_color: str = "#FFFFFF"
    outro_border_width: int = 0
    outro_width_percent: float = 65.2
    outro_height_percent: float = 46.9
    outro_position_x_percent: float = 50.0
    outro_position_y_percent: float = 21.9
    outro_padding: int = 56
    outro_radius: int = 32
    outro_text_alignment: str = "center"
    max_chars_per_line: int = 28
    max_subtitle_lines: int = 3
    semantic_short_phrases: bool = False
    subtitle_animation: str = "none"
    word_sync_enabled: bool = False
    # Empty preserves the legacy ``word_sync_enabled`` switch.  Production
    # settings pass an explicit off/cumulative/single value.
    word_display_mode: str = ""
    word_unread_color: str = "#D0D5DD"
    word_active_color: str = "#FFE06A"
    word_read_color: str = "#FFFFFF"
    word_pop_scale: int = 112
    word_pop_duration_ms: int = 140
    word_pop_intensity: float = 0.65

    def safe(self) -> "AssStyleConfig":
        """Clamp user values into the vertical-video safe region.

        Both sides reserve the same amount of room so centered text is centered
        on the actual video frame.  The bottom still reserves room for TikTok's
        caption/author UI, and the search card stays below the top UI.
        """

        width = max(360, int(self.play_res_x))
        height = max(640, int(self.play_res_y))
        # Safety margins are authored against the 1080x1920 delivery canvas.
        # Preview renders use a 540x960 canvas, so applying the full-resolution
        # pixel minimums there would leave only 220px for subtitles and force
        # otherwise short phrases into three narrow lines.
        layout_scale = min(
            width / _STORY_REFERENCE_WIDTH,
            height / _STORY_REFERENCE_HEIGHT,
        )
        # Captions use the same symmetric 188px interaction rail as the intro,
        # search and outro cards.  Keeping a separate 160px caption minimum
        # made the ASS render wider than both UI previews and the dotted safe
        # area.  At half-resolution this becomes 94px, preserving the exact
        # normalized layout used by the 1080x1920 delivery frame.
        subtitle_margin_min = max(
            1, round(_STORY_SAFE_HORIZONTAL * layout_scale)
        )
        card_margin_min = max(1, round(120 * layout_scale))
        subtitle_bottom_min = max(1, round(_STORY_SAFE_BOTTOM * layout_scale))
        card_top_min = max(1, round(100 * layout_scale))
        subtitle_horizontal_margin = _clamp_int(
            max(self.subtitle_margin_left, self.subtitle_margin_right),
            subtitle_margin_min,
            max(subtitle_margin_min, width // 3),
        )
        card_horizontal_margin = _clamp_int(
            max(self.card_margin_left, self.card_margin_right),
            card_margin_min,
            max(card_margin_min, width // 3),
        )
        animation = str(self.subtitle_animation or "none").strip().casefold()
        if animation not in SUBTITLE_ANIMATIONS:
            animation = "none"
        intro_animation = str(
            self.intro_animation or "fade_rise"
        ).strip().casefold()
        if intro_animation not in INTRO_ANIMATIONS:
            intro_animation = "fade_rise"
        word_display_mode = str(self.word_display_mode or "").strip().casefold()
        if word_display_mode not in {"off", "cumulative", "single"}:
            word_display_mode = "cumulative" if self.word_sync_enabled else "off"
        return replace(
            self,
            play_res_x=width,
            play_res_y=height,
            font_name=_safe_font_name(self.font_name),
            subtitle_font_size=_clamp_int(self.subtitle_font_size, 24, 96),
            subtitle_outline=_clamp_float(self.subtitle_outline, 0.0, 8.0),
            subtitle_shadow=_clamp_float(self.subtitle_shadow, 0.0, 6.0),
            subtitle_background_opacity=_clamp_float(
                self.subtitle_background_opacity, 0.0, 1.0
            ),
            subtitle_alignment=_safe_alignment(self.subtitle_alignment),
            subtitle_position_x_percent=_clamp_float(
                self.subtitle_position_x_percent, 10.0, 90.0
            ),
            subtitle_margin_left=subtitle_horizontal_margin,
            subtitle_margin_right=subtitle_horizontal_margin,
            subtitle_margin_bottom=_clamp_int(
                self.subtitle_margin_bottom,
                subtitle_bottom_min,
                max(subtitle_bottom_min, height // 2),
            ),
            card_font_size=_clamp_int(self.card_font_size, 24, 88),
            card_background_opacity=_clamp_float(
                self.card_background_opacity, 0.15, 1.0
            ),
            card_outline_width=_clamp_float(self.card_outline_width, 0.0, 8.0),
            card_alignment=_safe_alignment(self.card_alignment),
            card_position_x_percent=_clamp_float(
                self.card_position_x_percent, 10.0, 90.0
            ),
            card_position_y_percent=_clamp_float(
                self.card_position_y_percent, 5.0, 30.0
            ),
            card_width_percent=_clamp_float(self.card_width_percent, 28.0, 82.0),
            card_padding=_clamp_int(self.card_padding, 4, 48),
            card_radius=_clamp_int(self.card_radius, 0, 48),
            card_margin_left=card_horizontal_margin,
            card_margin_right=card_horizontal_margin,
            card_margin_top=_clamp_int(
                self.card_margin_top,
                card_top_min,
                max(card_top_min, height // 4),
            ),
            max_chars_per_line=_clamp_int(self.max_chars_per_line, 12, 60),
            max_subtitle_lines=_clamp_int(self.max_subtitle_lines, 2, 4),
            subtitle_animation=animation,
            word_sync_enabled=word_display_mode != "off",
            word_display_mode=word_display_mode,
            intro_headline_font_size=_clamp_int(self.intro_headline_font_size, 28, 96),
            intro_font_name=_safe_font_name(self.intro_font_name),
            intro_body_font_size=_clamp_int(self.intro_body_font_size, 20, 72),
            intro_label_font_size=_clamp_int(self.intro_label_font_size, 16, 52),
            intro_background_opacity=_clamp_float(
                self.intro_background_opacity, 0.15, 1.0
            ),
            intro_border_width=_clamp_int(self.intro_border_width, 0, 12),
            intro_shadow_opacity=_clamp_float(self.intro_shadow_opacity, 0.0, 0.8),
            intro_width_percent=_clamp_float(self.intro_width_percent, 40.0, 82.0),
            intro_position_x_percent=_clamp_float(
                self.intro_position_x_percent, 20.0, 80.0
            ),
            intro_position_y_percent=_clamp_float(
                self.intro_position_y_percent, 12.0, 58.0
            ),
            intro_padding=_clamp_int(self.intro_padding, 16, 120),
            intro_radius=_clamp_int(self.intro_radius, 0, 72),
            intro_text_alignment=_safe_alignment(self.intro_text_alignment),
            intro_max_lines=_clamp_int(self.intro_max_lines, 2, 8),
            intro_animation=intro_animation,
            outro_title_font_size=_clamp_int(self.outro_title_font_size, 28, 96),
            outro_font_name=_safe_font_name(self.outro_font_name),
            outro_body_font_size=_clamp_int(self.outro_body_font_size, 20, 72),
            outro_code_font_size=_clamp_int(self.outro_code_font_size, 24, 96),
            outro_background_opacity=_clamp_float(
                self.outro_background_opacity, 0.15, 1.0
            ),
            outro_border_width=_clamp_int(self.outro_border_width, 0, 12),
            outro_width_percent=_clamp_float(self.outro_width_percent, 40.0, 82.0),
            outro_height_percent=_clamp_float(self.outro_height_percent, 28.0, 62.0),
            outro_position_x_percent=_clamp_float(
                self.outro_position_x_percent, 20.0, 80.0
            ),
            outro_position_y_percent=_clamp_float(
                self.outro_position_y_percent, 12.0, 52.0
            ),
            outro_padding=_clamp_int(self.outro_padding, 16, 120),
            outro_radius=_clamp_int(self.outro_radius, 0, 72),
            outro_text_alignment=_safe_alignment(self.outro_text_alignment),
            word_pop_scale=_clamp_int(self.word_pop_scale, 100, 150),
            word_pop_duration_ms=_clamp_int(self.word_pop_duration_ms, 40, 500),
            word_pop_intensity=_clamp_float(self.word_pop_intensity, 0.0, 1.0),
        )


_CHAPTER_RE = re.compile(
    r"^\s*(?:(?:chapter|chap\.?|part)\s+"
    r"(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"(?:\s*[:\-–—].*)?|第\s*[一二三四五六七八九十百千零〇\d]+\s*[章节部])\s*$",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?。！？])\s+")
_SEMANTIC_CONJUNCTIONS = frozenset(
    {
        "although",
        "and",
        "because",
        "before",
        "but",
        "if",
        "once",
        "or",
        "since",
        "so",
        "though",
        "unless",
        "until",
        "when",
        "while",
        "yet",
    }
)
_SEMANTIC_AUXILIARIES = frozenset(
    {
        "am",
        "are",
        "can",
        "could",
        "did",
        "do",
        "does",
        "had",
        "has",
        "have",
        "is",
        "may",
        "might",
        "must",
        "shall",
        "should",
        "was",
        "were",
        "will",
        "would",
    }
)
_SEMANTIC_NEGATIONS = frozenset({"never", "no", "not"})
_SEMANTIC_HONORIFICS = frozenset(
    {"capt", "captain", "dr", "lady", "lord", "miss", "mr", "mrs", "ms", "prof", "sir"}
)
_SEMANTIC_PHRASAL_PAIRS = frozenset(
    {
        ("broke", "down"),
        ("came", "back"),
        ("come", "back"),
        ("figure", "out"),
        ("figured", "out"),
        ("find", "out"),
        ("found", "out"),
        ("gave", "in"),
        ("gave", "up"),
        ("get", "out"),
        ("got", "out"),
        ("hold", "on"),
        ("look", "up"),
        ("looked", "up"),
        ("picked", "up"),
        ("put", "down"),
        ("ran", "away"),
        ("turned", "around"),
        ("walked", "away"),
        ("went", "on"),
    }
)
_SEMANTIC_PARTICLES = frozenset(
    {"around", "away", "back", "down", "in", "off", "on", "out", "over", "through", "up"}
)


def _clamp_int(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, int(value)))


def _clamp_float(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _safe_alignment(value: str) -> str:
    normalized = str(value or "center").strip().casefold()
    return normalized if normalized in {"left", "center", "right"} else "center"


def _safe_font_name(value: str) -> str:
    normalized = " ".join(str(value or "").split())
    if (
        not normalized
        or len(normalized) > 80
        or any(ord(character) < 32 for character in normalized)
        or any(character in normalized for character in ",{}\\")
    ):
        return "Arial"
    return normalized


def is_chapter_heading(text: str) -> bool:
    """Return True for headings that should become silence, not subtitles."""

    return bool(_CHAPTER_RE.fullmatch(text.strip()))


def parse_narration_text(text: str) -> list[NarrationSentence]:
    """Split source text into sentence units while retaining chapter markers."""

    units: list[NarrationSentence] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw_line.split())
        if not line:
            continue
        if is_chapter_heading(line):
            units.append(NarrationSentence(line, is_chapter=True))
            continue
        for sentence in _SENTENCE_BOUNDARY_RE.split(line):
            sentence = sentence.strip()
            if sentence:
                units.append(NarrationSentence(sentence))
    return units


def estimate_sentence_duration(
    text: str,
    *,
    words_per_minute: float = 155.0,
    minimum_duration: float = 0.8,
) -> float:
    """Estimate English narration time when TTS timings are not yet available."""

    if words_per_minute <= 0 or minimum_duration <= 0:
        raise ValueError("words_per_minute and minimum_duration must be positive")
    words = re.findall(r"\b[\w'’.-]+\b", text, flags=re.UNICODE)
    duration = len(words) * 60.0 / words_per_minute
    # Give sentence-ending punctuation a small natural cadence allowance.  The
    # final TTS alignment can replace this estimate without changing ASS APIs.
    cadence = 0.16 if text.rstrip().endswith((".", "!", "?", "。", "！", "？")) else 0.0
    return max(minimum_duration, duration + cadence)


def _coerce_sentence(
    item: NarrationSentence | str | Mapping[str, object],
) -> NarrationSentence:
    if isinstance(item, NarrationSentence):
        return item
    if isinstance(item, str):
        return NarrationSentence(item, is_chapter=is_chapter_heading(item))
    if isinstance(item, Mapping):
        text = str(item.get("text", ""))
        raw_duration = item.get("duration")
        duration = float(raw_duration) if raw_duration is not None else None
        is_chapter = bool(item.get("is_chapter", is_chapter_heading(text)))
        gap_after = float(item.get("gap_after", 0.0))
        return NarrationSentence(text, duration, is_chapter, gap_after)
    raise TypeError(f"Unsupported narration sentence: {type(item)!r}")


def build_sentence_cues(
    sentences: Sequence[NarrationSentence | str | Mapping[str, object]],
    *,
    start_time: float = 0.0,
    chapter_pause: float = 0.8,
    words_per_minute: float = 155.0,
    minimum_duration: float = 0.8,
) -> SubtitleTimeline:
    """Build sentence cues; omitted chapter headings advance every later cue."""

    if not math.isfinite(start_time) or start_time < 0:
        raise ValueError("start_time must be a non-negative finite number")
    if not math.isfinite(chapter_pause) or chapter_pause < 0:
        raise ValueError("chapter_pause must be a non-negative finite number")

    current = start_time
    cues: list[SubtitleCue] = []
    chapter_count = 0
    for raw_item in sentences:
        item = _coerce_sentence(raw_item)
        clean_text = " ".join(item.text.split())
        if item.is_chapter or is_chapter_heading(clean_text):
            current += chapter_pause
            chapter_count += 1
            continue
        if not clean_text:
            continue
        duration = item.duration
        if duration is None:
            duration = estimate_sentence_duration(
                clean_text,
                words_per_minute=words_per_minute,
                minimum_duration=minimum_duration,
            )
        end = current + duration
        cues.append(SubtitleCue(current, end, clean_text))
        current = end + item.gap_after
    return SubtitleTimeline(tuple(cues), current, chapter_count)


def seconds_to_ass_time(seconds: float) -> str:
    """Convert seconds to ASS's h:mm:ss.cc format with proper carry."""

    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("timestamp must be a non-negative finite number")
    centiseconds = int(round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, hundredths = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{hundredths:02d}"


def colour_to_ass(colour: str, *, opacity: float = 1.0) -> str:
    """Convert #RRGGBB plus opacity to ASS &HAABBGGRR notation."""

    value = colour.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(character * 2 for character in value)
    if len(value) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError(f"Invalid RGB colour: {colour!r}")
    opacity = _clamp_float(opacity, 0.0, 1.0)
    red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    alpha = round((1.0 - opacity) * 255)
    return f"&H{alpha:02X}{blue:02X}{green:02X}{red:02X}"


def wrap_sentence(text: str, *, width: int = 38, max_lines: int = 3) -> str:
    """Strictly wrap text without widening a line beyond ``width``.

    The returned string can contain more than ``max_lines``.  Rendering uses
    :func:`paginate_sentence` to turn those lines into timed pages instead of
    squeezing an arbitrarily long sentence into one unsafe caption block.
    """

    return r"\N".join(
        line
        for page in paginate_sentence(text, width=width, max_lines=max_lines)
        for line in page.split(r"\N")
    )


def paginate_sentence(
    text: str,
    *,
    width: int = 38,
    max_lines: int = 3,
    break_long_words: bool = True,
) -> tuple[str, ...]:
    """Return safe caption pages with a strict width and line-count limit."""

    collapsed = " ".join(text.split())
    if not collapsed:
        return ()
    width = max(1, int(width))
    max_lines = max(1, int(max_lines))
    lines = textwrap.wrap(
        collapsed,
        width=width,
        break_long_words=break_long_words,
        break_on_hyphens=False,
    )
    page_count = math.ceil(len(lines) / max_lines)
    base_size, larger_pages = divmod(len(lines), page_count)
    pages: list[str] = []
    offset = 0
    for page_index in range(page_count):
        page_size = base_size + (1 if page_index < larger_pages else 0)
        pages.append(r"\N".join(lines[offset : offset + page_size]))
        offset += page_size
    return tuple(pages)


def _semantic_token(token: str) -> str:
    """Return the spoken core of one whitespace token for boundary heuristics."""

    return re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9'’-]+$", "", token)


def _looks_capitalized(token: str) -> bool:
    clean = _semantic_token(token)
    return bool(clean) and clean[0].isalpha() and clean[0].isupper()


def _protected_semantic_boundary(previous: str, following: str) -> bool:
    """Avoid the most disruptive English phrase breaks when another cut exists."""

    previous_clean = _semantic_token(previous)
    following_clean = _semantic_token(following)
    previous_lower = previous_clean.casefold()
    following_lower = following_clean.casefold()

    if not previous_clean or not following_clean:
        return False
    if previous_lower in _SEMANTIC_HONORIFICS and _looks_capitalized(following):
        return True
    if _looks_capitalized(previous) and _looks_capitalized(following):
        return True
    if following_lower in _SEMANTIC_NEGATIONS and previous_lower in _SEMANTIC_AUXILIARIES:
        return True
    if previous_lower in _SEMANTIC_NEGATIONS or previous_lower.endswith("n't"):
        return True
    if (previous_lower, following_lower) in _SEMANTIC_PHRASAL_PAIRS:
        return True
    if following_lower in _SEMANTIC_PARTICLES and previous_lower.endswith(("ed", "ing")):
        return True
    return False


def _semantic_boundary_score(previous: str, following: str) -> int:
    stripped = previous.rstrip('"\'”’)]}')
    if stripped.endswith((",", ";", ":", "—", "–")):
        return 4
    if _semantic_token(following).casefold() in _SEMANTIC_CONJUNCTIONS:
        return 3
    if stripped.endswith(("?", "!", ".")):
        return 2
    return 0


def split_semantic_phrases(
    text: str,
    *,
    min_words: int = 3,
    max_words: int = 8,
) -> tuple[str, ...]:
    """Split English text into compact, readable phrases.

    The splitter is deterministic and conservative. It favours clause
    punctuation and conjunctions, while treating names, negation and common
    phrasal verbs as protected boundaries whenever another 3-8 word cut exists.
    """

    if isinstance(min_words, bool) or isinstance(max_words, bool):
        raise TypeError("min_words and max_words must be integers")
    if not isinstance(min_words, int) or not isinstance(max_words, int):
        raise TypeError("min_words and max_words must be integers")
    if min_words < 1 or max_words < min_words:
        raise ValueError("word limits must satisfy 1 <= min_words <= max_words")

    collapsed = " ".join(text.split())
    if not collapsed:
        return ()
    tokens = collapsed.split(" ")
    if len(tokens) <= max_words:
        return (collapsed,)

    target_words = min(max_words, max(min_words, 6))
    phrases: list[str] = []
    start = 0
    token_count = len(tokens)
    while token_count - start > max_words:
        upper = min(token_count, start + max_words)
        candidates: list[tuple[bool, int, int, int]] = []
        for end in range(start + min_words, upper + 1):
            remaining = token_count - end
            if 0 < remaining < min_words:
                continue
            protected = end < token_count and _protected_semantic_boundary(
                tokens[end - 1], tokens[end]
            )
            score = (
                _semantic_boundary_score(tokens[end - 1], tokens[end])
                if end < token_count
                else 5
            )
            length = end - start
            candidates.append((not protected, score, -abs(length - target_words), end))

        if not candidates:
            end = upper
        else:
            unprotected = [candidate for candidate in candidates if candidate[0]]
            pool = unprotected or candidates
            _safe, _score, _distance, end = max(pool, key=lambda item: item[1:])
        phrases.append(" ".join(tokens[start:end]))
        start = end

    if start < token_count:
        phrases.append(" ".join(tokens[start:]))
    return tuple(phrases)


def _semantic_timing_weight(text: str) -> float:
    words = len(
        re.findall(r"\b[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*\b", text)
    )
    pause = 0.0
    stripped = text.rstrip('"\'”’)]}')
    if stripped.endswith(("?", "!", ".")):
        pause = 0.5
    elif stripped.endswith((",", ";", ":", "—", "–")):
        pause = 0.25
    return max(1.0, float(words) + pause)


def build_semantic_cues(
    cues: Sequence[SubtitleCue],
    *,
    min_words: int = 3,
    max_words: int = 8,
) -> tuple[SubtitleCue, ...]:
    """Allocate every real sentence interval continuously across short phrases."""

    semantic: list[SubtitleCue] = []
    for cue in cues:
        phrases = split_semantic_phrases(
            cue.text,
            min_words=min_words,
            max_words=max_words,
        )
        if not phrases:
            continue
        weights = [_semantic_timing_weight(phrase) for phrase in phrases]
        total_weight = sum(weights)
        elapsed_weight = 0.0
        duration = cue.end - cue.start
        for index, (phrase, weight) in enumerate(zip(phrases, weights, strict=True)):
            start = cue.start + duration * elapsed_weight / total_weight
            elapsed_weight += weight
            end = (
                cue.end
                if index == len(phrases) - 1
                else cue.start + duration * elapsed_weight / total_weight
            )
            semantic.append(SubtitleCue(start, end, phrase))
    return tuple(semantic)


def _effective_chars_per_line(config: AssStyleConfig) -> int:
    """Cap a character setting by the actual font size and safe pixel width."""

    available_pixels = max(
        1,
        config.play_res_x
        - config.subtitle_margin_left
        - config.subtitle_margin_right
        - math.ceil(2 * (config.subtitle_outline + config.subtitle_shadow)),
    )
    # English Arial/Segoe UI bold averages roughly half an em.  Using 0.55 is
    # deliberately conservative for capitals, quotes, and wider font choices.
    pixel_capacity = math.floor(
        available_pixels / max(1.0, config.subtitle_font_size * 0.55)
    )
    return max(10, min(config.max_chars_per_line, pixel_capacity))


def _subtitle_safe_bounds(config: AssStyleConfig) -> tuple[int, int]:
    """Return the hard symmetric horizontal caption rails for this canvas.

    ``AssStyleConfig.safe`` already normalizes both user margins to the same
    value.  Re-applying the reference rail here makes the event-level clip
    defensive when this helper is called from tests or future render paths
    with an un-normalized style object.
    """

    scale_x = config.play_res_x / _STORY_REFERENCE_WIDTH
    hard_margin = max(1, round(_STORY_SAFE_HORIZONTAL * scale_x))
    left = max(hard_margin, int(config.subtitle_margin_left))
    right = min(
        config.play_res_x - hard_margin,
        config.play_res_x - int(config.subtitle_margin_right),
    )
    if right <= left:
        center = round(config.play_res_x / 2)
        return center - 1, center + 1
    return left, right


def _caption_page_pixel_width(page: str, config: AssStyleConfig) -> int:
    """Conservatively estimate the widest explicit line in one caption page."""

    line_units = max(
        (_card_text_width(line) for line in page.split(r"\N")),
        default=1,
    )
    # Latin glyphs average about 0.55em; CJK characters count as two display
    # units in ``_card_text_width`` and therefore receive roughly 1.1em.
    glyph_width = line_units * config.subtitle_font_size * 0.55
    effect_guard = 2 * math.ceil(
        max(config.subtitle_outline, 0.0) + max(config.subtitle_shadow, 0.0) + 2
    )
    if config.subtitle_background_opacity > 0.001:
        effect_guard += max(8, round(config.subtitle_font_size * 0.18))
    return max(1, math.ceil(glyph_width + effect_guard))


@dataclass(frozen=True)
class _SubtitleEventLayout:
    safe_left: int
    safe_right: int
    block_left: int
    block_right: int
    anchor_x: int
    anchor_y: int
    alignment_number: int


def _subtitle_event_layout(
    config: AssStyleConfig,
    page: str,
) -> _SubtitleEventLayout:
    """Resolve the single hard-safe layout shared by every caption effect."""

    safe_left, safe_right = _subtitle_safe_bounds(config)
    safe_width = safe_right - safe_left
    block_width = min(safe_width, _caption_page_pixel_width(page, config))
    requested_x = round(
        config.play_res_x * config.subtitle_position_x_percent / 100.0
    )
    alignment = _safe_alignment(config.subtitle_alignment)
    if alignment == "left":
        minimum_x = safe_left
        maximum_x = max(minimum_x, safe_right - block_width)
    elif alignment == "right":
        minimum_x = min(safe_right, safe_left + block_width)
        maximum_x = safe_right
    else:
        half_width = math.ceil(block_width / 2)
        minimum_x = safe_left + half_width
        maximum_x = safe_right - half_width
        if maximum_x < minimum_x:
            minimum_x = maximum_x = round((safe_left + safe_right) / 2)
    anchor_x = max(minimum_x, min(maximum_x, requested_x))
    if alignment == "left":
        block_left = anchor_x
        block_right = anchor_x + block_width
    elif alignment == "right":
        block_left = anchor_x - block_width
        block_right = anchor_x
    else:
        block_left = anchor_x - math.ceil(block_width / 2)
        block_right = block_left + block_width
    block_left = max(safe_left, min(safe_right - 1, block_left))
    block_right = max(block_left + 1, min(safe_right, block_right))
    return _SubtitleEventLayout(
        safe_left=safe_left,
        safe_right=safe_right,
        block_left=block_left,
        block_right=block_right,
        anchor_x=anchor_x,
        anchor_y=config.play_res_y - config.subtitle_margin_bottom,
        alignment_number=_alignment_number(alignment, vertical="bottom"),
    )


def _subtitle_event_prefix(
    config: AssStyleConfig,
    page: str,
    *,
    animation: str = "none",
    event_duration_ms: int = 1000,
) -> str:
    """Build a normalized ASS anchor and optional seek-safe entrance effect.

    ``\\move`` and ``\\t`` use event-relative millisecond boundaries, so a
    frame rendered after a seek is derived only from its timestamp.  No effect
    depends on previous frames or mutable renderer state.
    """

    layout = _subtitle_event_layout(config, page)
    normalized_animation = (
        animation if animation in SUBTITLE_ANIMATIONS else "none"
    )
    duration_ms = max(1, int(event_duration_ms))
    common = f"\\an{layout.alignment_number}"
    hard_clip = (
        f"\\clip({layout.safe_left},0,{layout.safe_right},{config.play_res_y})"
    )
    if normalized_animation == "rise":
        motion_ms = max(1, min(180, duration_ms // 3 or 1))
        fade_ms = max(1, min(90, motion_ms))
        rise_pixels = _clamp_int(round(config.subtitle_font_size * 0.35), 8, 32)
        return (
            f"{{{common}\\move({layout.anchor_x},{layout.anchor_y + rise_pixels},"
            f"{layout.anchor_x},{layout.anchor_y},0,{motion_ms})\\q2{hard_clip}"
            f"\\fad({fade_ms},0)}}"
        )
    if normalized_animation == "mask_reveal":
        reveal_ms = max(1, min(260, duration_ms // 3 or 1))
        initial_right = min(layout.block_right, layout.block_left + 1)
        initial_clip = (
            f"\\clip({layout.block_left},0,{initial_right},{config.play_res_y})"
        )
        final_clip = (
            f"\\clip({layout.block_left},0,{layout.block_right},{config.play_res_y})"
        )
        return (
            f"{{{common}\\pos({layout.anchor_x},{layout.anchor_y})\\q2"
            f"{initial_clip}\\t(0,{reveal_ms},{final_clip})}}"
        )
    return (
        f"{{{common}\\pos({layout.anchor_x},{layout.anchor_y})\\q2{hard_clip}}}"
    )


def _timed_caption_pages(
    cue: SubtitleCue,
    *,
    width: int,
    max_lines: int,
    break_long_words: bool = True,
) -> tuple[tuple[float, float, str], ...]:
    pages = paginate_sentence(
        cue.text,
        width=width,
        max_lines=max_lines,
        break_long_words=break_long_words,
    )
    if not pages:
        return ()
    if len(pages) == 1:
        return ((cue.start, cue.end, pages[0]),)

    weights = [
        max(1, len(re.sub(r"\s+", "", page.replace(r"\N", " "))))
        for page in pages
    ]
    total_weight = sum(weights)
    duration = cue.end - cue.start
    elapsed_weight = 0
    timed: list[tuple[float, float, str]] = []
    for index, (page, weight) in enumerate(zip(pages, weights, strict=True)):
        start = cue.start + duration * elapsed_weight / total_weight
        elapsed_weight += weight
        end = (
            cue.end
            if index == len(pages) - 1
            else cue.start + duration * elapsed_weight / total_weight
        )
        timed.append((start, end, page))
    return tuple(timed)


def _escape_ass_text(text: str, *, preserve_line_breaks: bool = False) -> str:
    # Some Windows/libass font stacks map em/en dashes to a missing-glyph
    # question mark even in UTF-8 ASS files.  Use a neutral ASCII dash with
    # readable spacing so narration copy never appears corrupted in previews.
    normalized = text.replace("\u2014", " - ").replace("\u2013", "-").replace("\u2212", "-")
    escaped = normalized.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
    escaped = escaped.replace("\r\n", "\n").replace("\r", "\n")
    if preserve_line_breaks:
        escaped = escaped.replace("\n", r"\N")
    else:
        escaped = " ".join(escaped.splitlines())
    return escaped


def _escape_caption_page(text: str) -> str:
    """Escape caption copy while retaining explicit layout line breaks."""

    return _escape_ass_text(text).replace(r"\\N", r"\N")


def _caption_graphemes(text: str) -> tuple[str, ...]:
    """Split display copy without tearing combining marks or emoji ZWJ runs."""

    units: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith(r"\N", index):
            units.append(r"\N")
            index += 2
            continue
        unit = text[index]
        index += 1
        # Attach combining marks, variation selectors and complete zero-width
        # joiner sequences to their base glyph.  This is intentionally small
        # and dependency-free, but covers the novel languages and emoji used
        # by current templates.
        while index < len(text):
            character = text[index]
            codepoint = ord(character)
            if (
                unicodedata.combining(character)
                or unicodedata.category(character) in {"Mn", "Mc", "Me"}
                or 0xFE00 <= codepoint <= 0xFE0F
                or character == "\u200d"
                or unit.endswith("\u200d")
            ):
                unit += character
                index += 1
                continue
            # Regional-indicator pairs form one flag grapheme.
            if (
                len(unit) == 1
                and 0x1F1E6 <= ord(unit) <= 0x1F1FF
                and 0x1F1E6 <= codepoint <= 0x1F1FF
            ):
                unit += character
                index += 1
            break
        units.append(unit)
    return tuple(units)


def _typewriter_events(
    page: str,
    *,
    start: float,
    end: float,
    position_prefix: str,
) -> list[tuple[float, float, str]]:
    """Return mutually exclusive, seek-safe progressive caption states.

    Each state contains the complete line geometry; only its unrevealed suffix
    is transparent.  This prevents centring/wrapping jumps while seeking and
    avoids relying on karaoke state accumulated by earlier frames.
    """

    units = _caption_graphemes(page)
    if not units:
        return [(start, end, position_prefix)]
    event_ms = max(1, round((end - start) * 1000))
    reading_reserve_ms = min(250, max(1, event_ms // 4))
    reveal_budget_ms = max(0, event_ms - reading_reserve_ms)
    state_count = min(
        len(units),
        31,
        max(1, reveal_budget_ms // 50 + 1),
    )
    if state_count <= 1:
        return [(start, end, position_prefix + _escape_caption_page(page))]
    step_ms = max(
        10,
        min(50, reveal_budget_ms // max(1, state_count - 1)),
    )
    result: list[tuple[float, float, str]] = []
    for state_index in range(state_count):
        visible_count = math.ceil((state_index + 1) * len(units) / state_count)
        visible = _escape_caption_page("".join(units[:visible_count]))
        hidden = _escape_caption_page("".join(units[visible_count:]))
        state_start = start + state_index * step_ms / 1000.0
        state_end = (
            end
            if state_index == state_count - 1
            else min(end, start + (state_index + 1) * step_ms / 1000.0)
        )
        if seconds_to_ass_time(state_start) == seconds_to_ass_time(state_end):
            continue
        text = position_prefix + visible
        if hidden:
            text += r"{\alpha&HFF&}" + hidden
        result.append((state_start, state_end, text))
    if not result:
        return [(start, end, position_prefix + _escape_caption_page(page))]
    # Rounding safeguards above may skip an extremely short intermediate
    # state; the last event must still own the complete remaining interval.
    final_start, _final_end, final_text = result[-1]
    result[-1] = (final_start, end, final_text)
    return result


_INTRO_LAYER_DELAYS: dict[str, dict[str, float]] = {
    "fade_rise": {
        "shadow": 0.00,
        "panel": 0.00,
        "headline": 0.08,
        "brand_rule": 0.10,
        "divider": 0.14,
        "badge": 0.16,
        "platform": 0.18,
        "summary": 0.24,
        "footer": 0.32,
    },
    "soft_scale": {
        "shadow": 0.00,
        "panel": 0.00,
        "headline": 0.08,
        "brand_rule": 0.10,
        "divider": 0.14,
        "badge": 0.16,
        "platform": 0.18,
        "summary": 0.24,
        "footer": 0.32,
    },
    "side_reveal": {
        "shadow": 0.00,
        "panel": 0.00,
        "headline": 0.06,
        "brand_rule": 0.10,
        "divider": 0.14,
        "badge": 0.16,
        "platform": 0.18,
        "summary": 0.24,
        "footer": 0.32,
    },
    "layered_story": {
        "shadow": 0.00,
        "panel": 0.00,
        "headline": 0.08,
        "brand_rule": 0.12,
        "badge": 0.18,
        "divider": 0.20,
        "platform": 0.22,
        "summary": 0.32,
        "footer": 0.44,
    },
    "paper_drop": {
        "shadow": 0.00,
        "panel": 0.00,
        "brand_rule": 0.10,
        "headline": 0.18,
        "badge": 0.20,
        "platform": 0.22,
        "divider": 0.26,
        "summary": 0.34,
        "footer": 0.46,
    },
}


def _intro_layer_effect(
    style: AssStyleConfig,
    *,
    layer: str,
    x: int,
    y: int,
    panel_left: int,
    panel_right: int,
    intro_duration: float,
    layout_scale: float,
) -> tuple[float, str]:
    """Return layer start delay and a deterministic ASS motion override."""

    animation = style.intro_animation
    if animation not in INTRO_ANIMATIONS:
        animation = "fade_rise"
    delay = _INTRO_LAYER_DELAYS.get(animation, {}).get(layer, 0.0)
    delay = min(max(0.0, delay), max(0.0, intro_duration - 0.05))
    remaining_ms = max(1, round((intro_duration - delay) * 1000))
    position = f"\\pos({x},{y})"
    if layer == "platform":
        # This line carries the platform name and search code during the intro.
        # It bridges directly into the persistent SearchCard event, so it must
        # be present from frame zero and never inherit decorative motion.
        return 0.0, position
    if animation == "none":
        return delay, position
    if animation == "fade_rise":
        motion_ms = max(1, min(220, remaining_ms // 3 or 1))
        fade_ms = max(1, min(120, motion_ms))
        rise = max(6, round(18 * layout_scale))
        return (
            delay,
            f"\\move({x},{y + rise},{x},{y},0,{motion_ms})\\fad({fade_ms},180)",
        )
    if animation == "soft_scale":
        motion_ms = max(1, min(240, remaining_ms // 3 or 1))
        fade_ms = max(1, min(100, motion_ms))
        # Vertical-only scaling preserves ticket text's independently fitted X
        # scale and keeps every layer inside the same horizontal safe rail.
        return (
            delay,
            f"{position}\\fscy96\\t(0,{motion_ms},\\fscy100)"
            f"\\fad({fade_ms},180)",
        )
    if animation == "side_reveal":
        reveal_ms = max(1, min(300, remaining_ms // 3 or 1))
        left = max(0, min(style.play_res_x - 1, int(panel_left)))
        right = max(left + 1, min(style.play_res_x, int(panel_right)))
        return (
            delay,
            f"{position}\\clip({left},0,{left + 1},{style.play_res_y})"
            f"\\t(0,{reveal_ms},\\clip({left},0,{right},{style.play_res_y}))"
            r"\fad(60,180)",
        )
    if animation == "paper_drop":
        motion_ms = max(1, min(260, remaining_ms // 3 or 1))
        fade_ms = max(1, min(100, motion_ms))
        drop = max(8, round((30 if layer in {"shadow", "panel"} else 16) * layout_scale))
        return (
            delay,
            f"\\move({x},{y - drop},{x},{y},0,{motion_ms})\\fad({fade_ms},180)",
        )

    # ``layered_story`` deliberately varies the motion primitive per layer,
    # while every primitive remains a pure function of the event timestamp.
    motion_ms = max(1, min(260, remaining_ms // 3 or 1))
    fade_ms = max(1, min(110, motion_ms))
    if layer in {"shadow", "panel"}:
        return (
            delay,
            f"{position}\\fscy96\\t(0,{motion_ms},\\fscy100)"
            f"\\fad({fade_ms},180)",
        )
    if layer in {"brand_rule", "divider"}:
        left = max(0, min(style.play_res_x - 1, int(panel_left)))
        right = max(left + 1, min(style.play_res_x, int(panel_right)))
        return (
            delay,
            f"{position}\\clip({left},0,{left + 1},{style.play_res_y})"
            f"\\t(0,{motion_ms},\\clip({left},0,{right},{style.play_res_y}))"
            r"\fad(60,180)",
        )
    if layer in {"badge", "platform"}:
        slide = max(8, round(22 * layout_scale))
        return (
            delay,
            f"\\move({x + slide},{y},{x},{y},0,{motion_ms})\\fad({fade_ms},180)",
        )
    if layer in {"headline", "footer"}:
        rise = max(6, round(16 * layout_scale))
        return (
            delay,
            f"\\move({x},{y + rise},{x},{y},0,{motion_ms})\\fad({fade_ms},180)",
        )
    return delay, f"{position}\\fad({fade_ms},180)"


def _style_line(config: AssStyleConfig, name: str) -> str:
    bold = -1 if (
        config.subtitle_bold if name == "Subtitle" else config.card_bold
    ) else 0
    if name == "Subtitle":
        background_enabled = config.subtitle_background_opacity > 0.001
        return ",".join(
            [
                "Style: Subtitle",
                config.font_name,
                str(config.subtitle_font_size),
                colour_to_ass(config.subtitle_text_color),
                colour_to_ass(config.subtitle_text_color),
                colour_to_ass(
                    config.subtitle_background_color if background_enabled else config.subtitle_outline_color,
                    opacity=(config.subtitle_background_opacity if background_enabled else 1.0),
                ),
                colour_to_ass(
                    config.subtitle_background_color if background_enabled else "#000000",
                    opacity=(config.subtitle_background_opacity if background_enabled else 0.55),
                ),
                str(bold),
                "-1" if config.subtitle_italic else "0",
                "0",
                "0",
                "100",
                "100",
                "0",
                "0",
                "3" if background_enabled else "1",
                str(max(4.0, config.subtitle_outline) if background_enabled else config.subtitle_outline),
                str(config.subtitle_shadow),
                str(_alignment_number(config.subtitle_alignment, vertical="bottom")),
                str(config.subtitle_margin_left),
                str(config.subtitle_margin_right),
                str(config.subtitle_margin_bottom),
                "1",
            ]
        )
    return ",".join(
        [
            "Style: SearchCard",
            config.font_name,
            str(config.card_font_size),
            colour_to_ass(config.card_text_color),
            colour_to_ass(config.card_text_color),
            colour_to_ass(config.card_outline_color),
            colour_to_ass(config.card_background_color, opacity=config.card_background_opacity),
            str(bold),
            "0",
            "0",
            "0",
            "100",
            "100",
            "0",
            "0",
            "1",
            str(config.card_outline_width),
            "0",
            str(_alignment_number(config.card_alignment, vertical="middle")),
            str(config.card_margin_left),
            str(config.card_margin_right),
            str(config.card_margin_top),
            "1",
        ]
    )


def _end_card_style_line(config: AssStyleConfig, name: str) -> str:
    """Return one centered, high-contrast end-card typography layer."""

    layout_scale = max(
        0.25,
        min(config.play_res_x / 1080.0, config.play_res_y / 1920.0),
    )
    definitions = {
        "EndTitle": {
            "size": max(round(config.outro_title_font_size * layout_scale), round(config.subtitle_font_size * 1.08)),
            "primary": config.outro_title_color,
            "outline": config.outro_background_color,
            "back": "#000000",
            "border": 1,
            "outline_width": max(3.0, config.subtitle_outline),
            "shadow": 1.5,
        },
        "EndAction": {
            "size": max(round(config.outro_body_font_size * layout_scale), round(config.subtitle_font_size * 0.72)),
            "primary": config.outro_body_color,
            "outline": config.outro_background_color,
            "back": "#000000",
            "border": 1,
            "outline_width": 3.0,
            "shadow": 1.0,
        },
        "EndCode": {
            "size": max(round(config.outro_code_font_size * layout_scale), round(config.card_font_size * 1.12)),
            "primary": config.outro_code_color,
            "outline": config.outro_background_color,
            "back": config.outro_background_color,
            "border": 1,
            "outline_width": 0.0,
            "shadow": 0.0,
        },
    }
    if name not in definitions:
        raise ValueError(f"unknown end-card style: {name}")
    item = definitions[name]
    return ",".join(
        [
            f"Style: {name}",
            config.outro_font_name,
            str(item["size"]),
            colour_to_ass(str(item["primary"])),
            colour_to_ass(str(item["primary"])),
            colour_to_ass(str(item["outline"])),
            colour_to_ass(
                str(item["back"]),
                opacity=(config.outro_background_opacity if name == "EndCode" else 0.65),
            ),
            "-1",
            "0",
            "0",
            "0",
            "100",
            "100",
            "0",
            "0",
            str(item["border"]),
            str(item["outline_width"]),
            str(item["shadow"]),
            str(_alignment_number(config.outro_text_alignment, vertical="middle")),
            str(config.card_margin_left),
            str(config.card_margin_right),
            "0",
            "1",
        ]
    )


def _template_style_line(config: AssStyleConfig, name: str) -> str:
    """Return typography used by the optional platform/story-card template."""

    layout_scale = max(
        0.25,
        min(config.play_res_x / 1080.0, config.play_res_y / 1920.0),
    )
    definitions = {
        "TemplateShadow": (20, "#101828", "#101828", 0, 0.0, 0.0, 7),
        "TemplatePanel": (20, "#FFFFFF", "#FFFFFF", 0, 0.0, 0.0, 7),
        "TemplateAccent": (20, "#315BD8", "#315BD8", 0, 0.0, 0.0, 7),
        "IntroHeadline": (
            max(round(config.intro_headline_font_size * layout_scale), round(config.subtitle_font_size * 1.12)),
            config.intro_headline_color,
            "#080A0F",
            -1,
            max(3.0, min(4.0, config.subtitle_outline)),
            0.0,
            _alignment_number(config.intro_text_alignment, vertical="top"),
        ),
        "IntroBadge": (
            max(round(20 * layout_scale), round(config.card_font_size * 0.46)),
            "#FFFFFF",
            "#315BD8",
            -1,
            max(6.0, round(10 * layout_scale)),
            0.0,
            4,
        ),
        "IntroPlatform": (
            max(round(28 * layout_scale), round(config.card_font_size * 0.68)),
            config.intro_body_color,
            config.intro_background_color,
            -1,
            0.0,
            0.0,
            _alignment_number(config.intro_text_alignment, vertical="middle"),
        ),
        "IntroSummary": (
            max(round(config.intro_body_font_size * layout_scale), round(config.subtitle_font_size * 0.58)),
            config.intro_body_color,
            config.intro_background_color,
            0,
            0.0,
            0.0,
            _alignment_number(config.intro_text_alignment, vertical="top"),
        ),
        "IntroFooter": (
            max(round(config.intro_label_font_size * layout_scale), round(config.subtitle_font_size * 0.56)),
            config.intro_label_color,
            config.intro_background_color,
            -1,
            0.0,
            0.0,
            _alignment_number(config.intro_text_alignment, vertical="middle"),
        ),
    }
    if name not in definitions:
        raise ValueError(f"unknown template style: {name}")
    size, primary, outline, bold, border, shadow, alignment = definitions[name]
    border_style = 3 if name == "IntroBadge" else 1
    back_colour = outline if name == "IntroBadge" else "#FFFFFF"
    return ",".join(
        [
            f"Style: {name}",
            config.intro_font_name,
            str(size),
            colour_to_ass(primary),
            colour_to_ass(primary),
            colour_to_ass(outline),
            colour_to_ass(back_colour),
            str(bold),
            "0",
            "0",
            "0",
            "100",
            "100",
            "0",
            "0",
            str(border_style),
            str(border),
            str(shadow),
            str(alignment),
            "0",
            "0",
            "0",
            "1",
        ]
    )


def _rounded_rect_path(width: int, height: int, radius: int) -> str:
    """Return a deterministic ASS vector path for a rounded card panel."""

    width = max(2, int(width))
    height = max(2, int(height))
    radius = max(0, min(int(radius), width // 2, height // 2))
    return (
        f"m {radius} 0 l {width - radius} 0 "
        f"b {width} 0 {width} 0 {width} {radius} "
        f"l {width} {height - radius} "
        f"b {width} {height} {width} {height} {width - radius} {height} "
        f"l {radius} {height} b 0 {height} 0 {height} 0 {height - radius} "
        f"l 0 {radius} b 0 0 0 0 {radius} 0"
    )


def _alignment_number(value: str, *, vertical: str = "middle") -> int:
    horizontal = _safe_alignment(value)
    row = {
        "bottom": {"left": 1, "center": 2, "right": 3},
        "middle": {"left": 4, "center": 5, "right": 6},
        "top": {"left": 7, "center": 8, "right": 9},
    }.get(vertical, {"left": 4, "center": 5, "right": 6})
    return row[horizontal]


def _aligned_x(
    alignment: str,
    *,
    left: int,
    width: int,
    padding: int,
) -> int:
    normalized = _safe_alignment(alignment)
    if normalized == "left":
        return left + padding
    if normalized == "right":
        return left + width - padding
    return left + round(width / 2)


def _safe_panel_x(
    canvas_width: int,
    panel_width: int,
    requested_center_percent: float,
    safe_margin: int,
) -> int:
    """Clamp a requested panel centre into the symmetric interaction-safe rail."""

    requested_center = round(canvas_width * requested_center_percent / 100.0)
    minimum_center = safe_margin + round(panel_width / 2)
    maximum_center = canvas_width - safe_margin - round(panel_width / 2)
    center = max(minimum_center, min(maximum_center, requested_center))
    return center - round(panel_width / 2)


def _card_character_width(character: str) -> int:
    """Return a conservative display width for mixed Latin/CJK card copy."""

    if not character or unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _card_text_width(text: str) -> int:
    return sum(_card_character_width(character) for character in text)


def _wrap_card_copy(text: str, *, width: int) -> list[str]:
    """Wrap prose at words and East-Asian character boundaries.

    ``textwrap`` treats an entire Chinese or Japanese paragraph as one word.
    Truncating that single "word" previously produced an empty intro card.  A
    small display-width wrapper preserves normal English words while allowing
    CJK text to break naturally without requiring spaces.
    """

    remaining = " ".join(str(text or "").split())
    line_width = max(8, int(width))
    lines: list[str] = []
    while remaining:
        used = 0
        last_break = 0
        overflow_at = len(remaining)
        for index, character in enumerate(remaining):
            character_width = _card_character_width(character)
            if used + character_width > line_width:
                overflow_at = index
                break
            used += character_width
            if character.isspace() or unicodedata.east_asian_width(character) in {"W", "F"}:
                last_break = index + 1
        if overflow_at == len(remaining):
            lines.append(remaining.strip())
            break
        split_at = last_break if last_break > 0 else max(1, overflow_at)
        line = remaining[:split_at].rstrip()
        remaining = remaining[split_at:].lstrip()
        while remaining and remaining[0] in _NO_LINE_START:
            line += remaining[0]
            remaining = remaining[1:].lstrip()
        if line:
            lines.append(line)
    return lines


def _fit_card_lines(text: str, *, width: int, max_lines: int) -> str:
    """Fit user-provided prose into a bounded intro card without overflow."""

    lines = _wrap_card_copy(text, width=width)
    if not lines:
        return ""
    limit = max(1, int(max_lines))
    if len(lines) <= limit:
        return r"\N".join(lines)

    fitted = lines[:limit]
    final = fitted[-1].rstrip(" ,;:-.…，。！？；：、")
    width_limit = max(8, int(width))
    while final and _card_text_width(final + "…") > width_limit:
        final = final[:-1].rstrip()
    fitted[-1] = (final or fitted[-1][:1]) + "…"
    return r"\N".join(fitted)


def _word_tokens(text: str) -> tuple[tuple[str, bool], ...]:
    """Tokenize visible caption copy while retaining exact spaces/punctuation."""

    tokens: list[tuple[str, bool]] = []
    index = 0
    while index < len(text):
        if text.startswith(r"\N", index):
            tokens.append((r"\N", False))
            index += 2
            continue
        character = text[index]
        if character.isspace():
            end = index + 1
            while end < len(text) and text[end].isspace():
                end += 1
            tokens.append((text[index:end], False))
            index = end
            continue
        if unicodedata.east_asian_width(character) in {"W", "F"}:
            is_word = character.isalnum() or unicodedata.category(character).startswith("L")
            tokens.append((character, is_word))
            index += 1
            continue
        match = re.match(r"[\w’'-]+", text[index:], flags=re.UNICODE)
        if match:
            token = match.group(0)
            tokens.append((token, True))
            index += len(token)
            continue
        tokens.append((character, False))
        index += 1
    return tuple(tokens)


def _word_sync_events(
    page: str,
    *,
    start: float,
    end: float,
    style: AssStyleConfig,
    position_prefix: str,
) -> list[tuple[float, float, str]]:
    """Build deterministic, true word-time colour/pop states for one page.

    Current providers expose sentence/segment alignment.  Each page therefore
    allocates its measured duration across spoken tokens by display width.  The
    output consists of non-overlapping word windows, so later words cannot pop
    early and the state remains seek-safe in FFmpeg/libass.
    """

    tokens = _word_tokens(page)
    word_slots = [index for index, (_token, spoken) in enumerate(tokens) if spoken]
    if not word_slots:
        return [(start, end, position_prefix + _escape_ass_text(page).replace(r"\\N", r"\N"))]
    weights = [max(1, _card_text_width(tokens[index][0])) for index in word_slots]
    total_weight = sum(weights)
    elapsed = 0
    result: list[tuple[float, float, str]] = []
    effective_scale = 100 + round(
        (style.word_pop_scale - 100) * style.word_pop_intensity
    )
    read_color = colour_to_ass(style.word_read_color)
    active_color = colour_to_ass(style.word_active_color)
    unread_color = colour_to_ass(style.word_unread_color)
    for spoken_index, (token_index, weight) in enumerate(
        zip(word_slots, weights, strict=True)
    ):
        word_start = start + (end - start) * elapsed / total_weight
        elapsed += weight
        word_end = (
            end
            if spoken_index == len(word_slots) - 1
            else start + (end - start) * elapsed / total_weight
        )
        pop_ms = min(
            style.word_pop_duration_ms,
            max(1, round((word_end - word_start) * 1000)),
        )
        if style.word_display_mode == "single":
            active_token = tokens[token_index][0]
            result.append(
                (
                    word_start,
                    word_end,
                    position_prefix
                    + f"{{\\1c{active_color}\\fscx100\\fscy{effective_scale}"
                    + f"\\t(0,{pop_ms},\\fscy100)}}"
                    + _escape_ass_text(active_token),
                )
            )
            continue
        chunks = [position_prefix]
        current_word = -1
        for index, (token, spoken) in enumerate(tokens):
            if spoken:
                current_word += 1
                if index == token_index:
                    # Horizontal glyph scaling changes libass line metrics and
                    # made the complete sentence jump left/right on every word
                    # state.  A vertical pulse retains the requested "pop"
                    # while the line width and its safe centred anchor remain
                    # invariant for the whole spoken page.
                    chunks.append(
                        f"{{\\1c{active_color}\\fscx100"
                        f"\\fscy{effective_scale}\\t(0,{pop_ms},"
                        r"\fscy100)}"
                    )
                elif current_word < spoken_index:
                    chunks.append(f"{{\\1c{read_color}\\fscx100\\fscy100}}")
                else:
                    chunks.append(f"{{\\1c{unread_color}\\fscx100\\fscy100}}")
            if token == r"\N":
                chunks.append(r"\N")
            else:
                chunks.append(_escape_ass_text(token))
        result.append((word_start, word_end, "".join(chunks)))
    return result


def generate_ass(
    cues: Sequence[SubtitleCue],
    *,
    platform: str,
    code: str,
    search_text: str | None = None,
    video_duration: float | None = None,
    end_card_title: str = "",
    end_card_action: str = "",
    end_card_duration: float | None = None,
    video_template: str = "classic",
    intro_card_text: str = "",
    intro_headline: str = "",
    intro_card_duration: float = 5.5,
    final_label: str = "",
    platform_logo_present: bool = False,
    platform_brand_color: str = "",
    config: AssStyleConfig | None = None,
) -> str:
    """Generate stable sentence subtitles and a full-duration search card."""

    platform = " ".join(platform.split())
    code = " ".join(str(code).split())
    if not platform:
        raise ValueError("platform cannot be empty")
    if not code:
        raise ValueError("code cannot be empty")

    style = (config or AssStyleConfig()).safe()
    template = str(video_template or "classic").strip().casefold()
    if template not in {"classic", "platform_story_card"}:
        raise ValueError("video_template must be classic or platform_story_card")
    normalized_final_label = " ".join(str(final_label or "").split())
    if _NUMBERED_PART_RE.search(normalized_final_label):
        raise ValueError("final_label cannot contain a numbered Part label")
    try:
        brand_colour = colour_to_ass(platform_brand_color or "#315BD8")
    except ValueError:
        brand_colour = colour_to_ass("#315BD8")
    latest_cue_end = max((cue.end for cue in cues), default=0.0)
    duration = latest_cue_end if video_duration is None else float(video_duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("video_duration must be a positive finite number")
    if duration + 1e-6 < latest_cue_end:
        raise ValueError("video_duration cannot end before the final subtitle cue")
    end_card_enabled = bool(end_card_title.strip() or end_card_action.strip())
    end_duration = float(end_card_duration or 0.0)
    if end_card_enabled:
        if not math.isfinite(end_duration) or not 5.0 <= end_duration <= 7.0:
            raise ValueError("end_card_duration must be between 5 and 7 seconds")
        if end_duration > duration:
            raise ValueError("end_card_duration cannot exceed video_duration")
    intro_duration = float(intro_card_duration)
    if template == "platform_story_card":
        if not math.isfinite(intro_duration) or not 2.5 <= intro_duration <= 8.0:
            raise ValueError("intro_card_duration must be between 2.5 and 8 seconds")
        end_start = duration - end_duration if end_card_enabled else duration
        if intro_duration > end_start:
            raise ValueError("intro card must finish before the end card starts")

    style_lines = [
        _style_line(style, "Subtitle"),
        _style_line(style, "SearchCard"),
        _end_card_style_line(style, "EndTitle"),
        _end_card_style_line(style, "EndAction"),
        _end_card_style_line(style, "EndCode"),
    ]
    style_lines.extend(
        _template_style_line(style, name)
        for name in (
            "TemplateShadow",
            "TemplatePanel",
            "TemplateAccent",
            "IntroHeadline",
            "IntroBadge",
            "IntroPlatform",
            "IntroSummary",
            "IntroFooter",
        )
    )
    header = [
        "[Script Info]",
        "; Generated by StoryForge",
        "ScriptType: v4.00+",
        f"PlayResX: {style.play_res_x}",
        f"PlayResY: {style.play_res_y}",
        "ScaledBorderAndShadow: yes",
        # Smart wrapping is a final pixel-accurate safety net for unusually
        # wide glyphs; explicit page/line breaks remain authoritative.
        "WrapStyle: 0",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        *style_lines,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    search_copy = search_text or f"Search {platform}: {code}"
    rendered_search = _escape_ass_text(search_copy)
    scale_x = style.play_res_x / _STORY_REFERENCE_WIDTH
    scale_y = style.play_res_y / _STORY_REFERENCE_HEIGHT
    layout_scale = min(scale_x, scale_y)
    safe_left = round(_STORY_SAFE_HORIZONTAL * scale_x)
    safe_right = round(_STORY_SAFE_HORIZONTAL * scale_x)
    safe_top = round(_STORY_SAFE_TOP * scale_y)
    safe_width = style.play_res_x - safe_left - safe_right
    safe_center_x = round(style.play_res_x / 2)
    search_start = intro_duration if template == "platform_story_card" else 0.0
    search_end = duration - end_duration if end_card_enabled else duration
    events: list[str] = []
    if search_end > search_start:
        search_panel_width = min(
            safe_width,
            max(
                round(style.play_res_x * 0.28),
                round(style.play_res_x * style.card_width_percent / 100.0),
            ),
        )
        search_panel_x = _safe_panel_x(
            style.play_res_x,
            search_panel_width,
            style.card_position_x_percent,
            safe_left,
        )
        scaled_padding = max(4, round(style.card_padding * layout_scale))
        search_panel_height = max(
            round(54 * layout_scale),
            round(style.card_font_size * 1.35) + 2 * scaled_padding,
        )
        requested_search_y = round(
            style.play_res_y * style.card_position_y_percent / 100.0
        )
        search_panel_y = max(
            safe_top,
            min(
                style.play_res_y - round(_STORY_SAFE_BOTTOM * scale_y) - search_panel_height,
                requested_search_y,
            ),
        )
        search_radius = min(
            round(style.card_radius * layout_scale), search_panel_height // 2
        )
        search_path = _rounded_rect_path(
            search_panel_width, search_panel_height, search_radius
        )
        search_times = (
            f"{seconds_to_ass_time(search_start)},{seconds_to_ass_time(search_end)}"
        )
        events.append(
            "Dialogue: 5,"
            f"{search_times},TemplatePanel,,0,0,0,,"
            f"{{\\an7\\pos({search_panel_x},{search_panel_y})\\p1"
            f"\\1c{colour_to_ass(style.card_background_color, opacity=style.card_background_opacity)}"
            r"\bord0\shad0}"
            f"{search_path}{{\\p0}}"
        )
        if style.card_outline_width > 0:
            events.append(
                "Dialogue: 6,"
                f"{search_times},TemplateAccent,,0,0,0,,"
                f"{{\\an7\\pos({search_panel_x},{search_panel_y})\\p1"
                f"\\1c{colour_to_ass(style.card_outline_color)}\\1a&HFF&"
                f"\\3c{colour_to_ass(style.card_outline_color)}"
                f"\\bord{style.card_outline_width}\\shad0}}"
                f"{search_path}{{\\p0}}"
            )
        search_pixel_width = max(
            1.0, _card_text_width(search_copy) * style.card_font_size * 0.54
        )
        search_scale = max(
            62,
            min(
                100,
                round(
                    (search_panel_width - 2 * scaled_padding)
                    / search_pixel_width
                    * 100
                ),
            ),
        )
        search_text_x = _aligned_x(
            style.card_alignment,
            left=search_panel_x,
            width=search_panel_width,
            padding=scaled_padding,
        )
        events.append(
            "Dialogue: 7,"
            f"{search_times},SearchCard,,0,0,0,,"
            f"{{\\an{_alignment_number(style.card_alignment, vertical='top')}"
            f"\\pos({search_text_x},{search_panel_y})"
            f"\\q2\\fscx{search_scale}\\fscy{search_scale}\\shad0}}"
            f"{rendered_search}"
        )
    if template == "classic" and intro_duration > 0 and intro_headline.strip():
        # The compact approval reel promises a distinct opening phase.  Keep
        # the familiar classic layout, but show the generated hook below the
        # persistent search pill before narration and captions begin.
        headline_font_size = max(
            round(style.intro_headline_font_size * layout_scale),
            round(style.subtitle_font_size * 1.12),
        )
        headline_width = max(
            12,
            round(safe_width / max(1.0, headline_font_size * 0.53)),
        )
        classic_headline = _fit_card_lines(
            intro_headline,
            width=headline_width,
            max_lines=2,
        )
        classic_headline = _escape_ass_text(classic_headline).replace(
            r"\\N", r"\N"
        )
        classic_headline_y = max(
            safe_top + round(150 * scale_y),
            round(style.play_res_y * 0.16),
        )
        classic_duration = min(duration, intro_duration)
        classic_delay, classic_effect = _intro_layer_effect(
            style,
            layer="headline",
            x=safe_center_x,
            y=classic_headline_y,
            panel_left=safe_left,
            panel_right=style.play_res_x - safe_right,
            intro_duration=classic_duration,
            layout_scale=layout_scale,
        )
        events.append(
            f"Dialogue: 4,{seconds_to_ass_time(classic_delay)},"
            f"{seconds_to_ass_time(classic_duration)},"
            "IntroHeadline,,0,0,0,,"
            f"{{\\an8{classic_effect}\\q2}}"
            f"{classic_headline}"
        )
    if template == "platform_story_card":
        panel_width = min(
            safe_width,
            round(style.play_res_x * style.intro_width_percent / 100.0),
        )
        panel_x = _safe_panel_x(
            style.play_res_x,
            panel_width,
            style.intro_position_x_percent,
            safe_left,
        )
        panel_y = round(style.play_res_y * style.intro_position_y_percent / 100.0)
        intro_padding = max(8, round(style.intro_padding * layout_scale))
        summary_font_size = max(
            round(style.intro_body_font_size * layout_scale),
            round(style.subtitle_font_size * 0.64),
        )
        summary_width = max(
            18,
            round(
                (panel_width - 2 * intro_padding)
                / max(1.0, summary_font_size * 0.53)
            ),
        )
        fitted_summary = _fit_card_lines(
            intro_card_text,
            width=summary_width,
            max_lines=style.intro_max_lines,
        )
        summary_line_count = max(1, fitted_summary.count(r"\N") + 1)
        # Keep short briefs compact while reserving enough room for five
        # English or CJK lines plus a separate action/footer row.
        panel_height_reference = max(
            430,
            min(
                760,
                300
                + min(style.intro_max_lines, summary_line_count)
                * max(40, round(style.intro_body_font_size * 1.25)),
            ),
        )
        panel_height = round(panel_height_reference * scale_y)
        panel_y = max(
            safe_top,
            min(
                style.play_res_y - round(_STORY_SAFE_BOTTOM * scale_y) - panel_height,
                panel_y,
            ),
        )
        radius = round(style.intro_radius * layout_scale)
        panel_path = _rounded_rect_path(panel_width, panel_height, radius)
        shadow_path = _rounded_rect_path(panel_width, panel_height, radius)
        brand_rule_width = max(12, round(72 * scale_x))
        brand_rule_height = max(2, round(6 * scale_y))
        brand_rule_path = _rounded_rect_path(
            brand_rule_width,
            brand_rule_height,
            max(1, round(brand_rule_height / 2)),
        )
        divider_width = max(2, panel_width - 2 * intro_padding)
        divider_height = max(1, round(2 * scale_y))
        divider_path = (
            f"m 0 0 l {divider_width} 0 l {divider_width} {divider_height} "
            f"l 0 {divider_height}"
        )
        intro_end = seconds_to_ass_time(intro_duration)
        headline_font_size = max(
            round(style.intro_headline_font_size * layout_scale),
            round(style.subtitle_font_size * 1.12),
        )
        headline_width = max(
            12,
            round(
                (panel_width - 2 * intro_padding)
                / max(1.0, headline_font_size * 0.53)
            ),
        )
        headline = _fit_card_lines(
            intro_headline or end_card_title,
            width=headline_width,
            max_lines=2,
        )
        headline = _escape_ass_text(headline).replace(r"\\N", r"\N")
        summary = _escape_ass_text(fitted_summary).replace(r"\\N", r"\N")
        platform_ticket_copy = f'{platform}  ·  Search “{code}”'
        platform_ticket = _escape_ass_text(platform_ticket_copy)
        ticket_font_size = max(
            round(style.intro_label_font_size * layout_scale),
            round(style.card_font_size * 0.68),
        )
        ticket_width = max(1, panel_width - 2 * intro_padding)
        ticket_pixel_width = max(
            1.0,
            _card_text_width(platform_ticket_copy) * ticket_font_size * 0.54,
        )
        ticket_scale = max(62, min(100, round(ticket_width / ticket_pixel_width * 100)))
        footer_label = normalized_final_label or _NEUTRAL_STORY_LABEL
        footer = _escape_ass_text(footer_label)
        footer_colour = colour_to_ass("#EA3F38") if normalized_final_label else brand_colour
        intro_text_x = _aligned_x(
            style.intro_text_alignment,
            left=panel_x,
            width=panel_width,
            padding=intro_padding,
        )
        intro_middle_alignment = _alignment_number(
            style.intro_text_alignment, vertical="middle"
        )
        intro_top_alignment = _alignment_number(
            style.intro_text_alignment, vertical="top"
        )
        intro_panel_colour = colour_to_ass(
            style.intro_background_color,
            opacity=style.intro_background_opacity,
        )
        intro_shadow_colour = colour_to_ass(
            "#101828", opacity=style.intro_shadow_opacity
        )
        layer_points = {
            "shadow": (
                panel_x + round(8 * scale_x),
                panel_y + round(12 * scale_y),
            ),
            "panel": (panel_x, panel_y),
            "brand_rule": (
                panel_x + round(panel_width / 2) - round(brand_rule_width / 2),
                panel_y + round(22 * scale_y),
            ),
            "divider": (
                panel_x + intro_padding,
                panel_y + round(164 * scale_y),
            ),
            "headline": (
                intro_text_x,
                max(safe_top, panel_y - round(284 * scale_y)),
            ),
            "platform": (
                intro_text_x,
                panel_y + round(126 * scale_y),
            ),
            "summary": (
                intro_text_x,
                panel_y + round(204 * scale_y),
            ),
            "footer": (
                intro_text_x,
                panel_y + panel_height - round(62 * scale_y),
            ),
            "badge": (
                panel_x + round(panel_width / 2),
                round(_STORY_INTRO_LOGO_TOP * scale_y)
                + round(_STORY_INTRO_LOGO_SIZE * layout_scale / 2),
            ),
        }
        layer_effects = {
            layer: _intro_layer_effect(
                style,
                layer=layer,
                x=point[0],
                y=point[1],
                panel_left=panel_x,
                panel_right=panel_x + panel_width,
                intro_duration=intro_duration,
                layout_scale=layout_scale,
            )
            for layer, point in layer_points.items()
        }
        shadow_delay, shadow_effect = layer_effects["shadow"]
        panel_delay, panel_effect = layer_effects["panel"]
        brand_delay, brand_effect = layer_effects["brand_rule"]
        divider_delay, divider_effect = layer_effects["divider"]
        headline_delay, headline_effect = layer_effects["headline"]
        platform_delay, platform_effect = layer_effects["platform"]
        summary_delay, summary_effect = layer_effects["summary"]
        footer_delay, footer_effect = layer_effects["footer"]
        badge_delay, badge_effect = layer_effects["badge"]
        ticket_events = [
                f"Dialogue: 0,{seconds_to_ass_time(shadow_delay)},"
                f"{intro_end},TemplateShadow,,0,0,0,,"
                f"{{\\an7{shadow_effect}"
                f"\\p1\\1c{intro_shadow_colour}\\bord0\\shad0}}"
                f"{shadow_path}{{\\p0}}",
                f"Dialogue: 1,{seconds_to_ass_time(panel_delay)},"
                f"{intro_end},TemplatePanel,,0,0,0,,"
                f"{{\\an7{panel_effect}\\p1\\1c{intro_panel_colour}&"
                f"\\3c{colour_to_ass(style.intro_border_color)}"
                f"\\bord{style.intro_border_width}\\shad0}}"
                f"{panel_path}{{\\p0}}",
                f"Dialogue: 2,{seconds_to_ass_time(brand_delay)},"
                f"{intro_end},TemplateAccent,,0,0,0,,"
                f"{{\\an7{brand_effect}\\p1\\1c{brand_colour}"
                r"\bord0\shad0}"
                f"{brand_rule_path}{{\\p0}}",
                f"Dialogue: 2,{seconds_to_ass_time(divider_delay)},"
                f"{intro_end},TemplateAccent,,0,0,0,,"
                f"{{\\an7{divider_effect}"
                r"\p1\1c&H00E7D8D0&\bord0\shad0}"
                f"{divider_path}{{\\p0}}",
                f"Dialogue: 5,{seconds_to_ass_time(headline_delay)},"
                f"{intro_end},IntroHeadline,,0,0,0,,"
                f"{{\\an{intro_top_alignment}{headline_effect}}}{headline}",
                f"Dialogue: 3,{seconds_to_ass_time(platform_delay)},"
                f"{intro_end},IntroPlatform,,0,0,0,,"
                f"{{\\an{intro_middle_alignment}{platform_effect}"
                f"\\q2\\fscx{ticket_scale}}}{platform_ticket}",
                f"Dialogue: 3,{seconds_to_ass_time(summary_delay)},"
                f"{intro_end},IntroSummary,,0,0,0,,"
                f"{{\\an{intro_top_alignment}{summary_effect}\\q2}}{summary}",
                f"Dialogue: 4,{seconds_to_ass_time(footer_delay)},"
                f"{intro_end},IntroFooter,,0,0,0,,"
                f"{{\\an{intro_middle_alignment}{footer_effect}"
                f"\\1c{footer_colour}\\fsp1}}{footer}",
            ]
        if not platform_logo_present:
            # A compact fallback remains useful for legacy platform records.
            # When a real platform image is available FFmpeg composites it in
            # this reserved slot after ASS, so no coloured badge can bleed
            # through a transparent logo.
            ticket_events.insert(
                5,
                f"Dialogue: 3,{seconds_to_ass_time(badge_delay)},"
                f"{intro_end},IntroBadge,,0,0,0,,"
                f"{{\\an5{badge_effect}"
                f"\\3c{brand_colour}\\4c{brand_colour}}}STORY",
            )
        events.extend(ticket_events)
    if end_card_enabled:
        end_start = duration - end_duration
        center_x = round(style.play_res_x / 2)
        title_y = round(style.play_res_y * 0.27)
        action_y = round(style.play_res_y * 0.67)
        code_y = round(style.play_res_y * 0.79)
        end_time = seconds_to_ass_time(duration)
        title_copy = end_card_title or "Continue reading"
        action_copy = end_card_action or f"Open {platform} to continue."
        code_copy = search_text or f"Search {platform}: {code}"
        title = _escape_ass_text(title_copy)
        action = _escape_ass_text(action_copy)
        code_line = _escape_ass_text(code_copy)
        panel_width = min(
            safe_width,
            round(style.play_res_x * style.outro_width_percent / 100.0),
        )
        panel_height = min(
            style.play_res_y - safe_top - round(_STORY_SAFE_BOTTOM * scale_y),
            round(style.play_res_y * style.outro_height_percent / 100.0),
        )
        panel_x = _safe_panel_x(
            style.play_res_x,
            panel_width,
            style.outro_position_x_percent,
            safe_left,
        )
        panel_y = max(
            safe_top,
            min(
                style.play_res_y - round(_STORY_SAFE_BOTTOM * scale_y) - panel_height,
                round(style.play_res_y * style.outro_position_y_percent / 100.0),
            ),
        )
        panel_path = _rounded_rect_path(
            panel_width,
            panel_height,
            round(style.outro_radius * layout_scale),
        )
        brand_rule_width = max(12, round(72 * scale_x))
        brand_rule_height = max(2, round(6 * scale_y))
        brand_rule_path = _rounded_rect_path(
            brand_rule_width,
            brand_rule_height,
            max(1, round(brand_rule_height / 2)),
        )
        events.extend(
            [
                "Dialogue: 0,"
                f"{seconds_to_ass_time(end_start)},{end_time},TemplateShadow,,0,0,0,,"
                f"{{\\an7\\pos({panel_x + round(10 * scale_x)},{panel_y + round(14 * scale_y)})"
                r"\p1\1c&H00281810&\1a&H88&\bord0\shad0\fad(260,0)}"
                f"{panel_path}{{\\p0}}",
                "Dialogue: 1,"
                f"{seconds_to_ass_time(end_start)},{end_time},TemplatePanel,,0,0,0,,"
                f"{{\\an7\\pos({panel_x},{panel_y})\\p1"
                f"\\1c{colour_to_ass(style.outro_background_color, opacity=style.outro_background_opacity)}&"
                f"\\3c{colour_to_ass(style.outro_border_color)}"
                f"\\bord{style.outro_border_width}\\shad0\\fad(260,0)}}"
                f"{panel_path}{{\\p0}}",
                "Dialogue: 2,"
                f"{seconds_to_ass_time(end_start)},{end_time},TemplateAccent,,0,0,0,,"
                f"{{\\an7\\pos({panel_x + round(panel_width / 2) - round(brand_rule_width / 2)},{panel_y + round(28 * scale_y)})"
                f"\\p1\\1c{brand_colour}\\bord0\\shad0\\fad(260,0)}}"
                f"{brand_rule_path}{{\\p0}}",
            ]
        )
        outro_padding = max(8, round(style.outro_padding * layout_scale))
        center_x = _aligned_x(
            style.outro_text_alignment,
            left=panel_x,
            width=panel_width,
            padding=outro_padding,
        )
        title_y = panel_y + round(panel_height * 0.28)
        action_y = panel_y + round(panel_height * 0.622222)
        code_y = panel_y + round(panel_height * 0.824444)
        title_size = max(
            round(style.outro_title_font_size * layout_scale),
            round(style.subtitle_font_size * 1.08),
        )
        action_size = max(
            round(style.outro_body_font_size * layout_scale),
            round(style.subtitle_font_size * 0.72),
        )
        text_pixels = panel_width - 2 * outro_padding
        title_width = max(12, round(text_pixels / max(1.0, title_size * 0.53)))
        action_width = max(16, round(text_pixels / max(1.0, action_size * 0.53)))
        title = _escape_ass_text(
            _fit_card_lines(title_copy, width=title_width, max_lines=2)
        ).replace(r"\\N", r"\N")
        action = _escape_ass_text(
            _fit_card_lines(action_copy, width=action_width, max_lines=3)
        ).replace(r"\\N", r"\N")
        code_size = max(
            round(style.outro_code_font_size * layout_scale),
            round(style.card_font_size * 1.12),
        )
        code_pixel_width = max(
            1.0,
            _card_text_width(code_copy) * code_size * 0.54,
        )
        code_scale = max(62, min(100, round(text_pixels / code_pixel_width * 100)))
        title_animation = r"\bord0\shad0\fad(260,0)"
        action_animation = r"\bord0\shad0\fad(240,0)"
        code_animation = (
            r"\bord0\shad0"
            f"\\fscx{code_scale}\\fscy{code_scale}\\fad(220,0)"
        )
        event_specs = (
            (3, end_start + 0.15, "EndTitle", title_y, title_animation, title),
            (4, end_start + 0.85, "EndAction", action_y, action_animation, action),
            (
                5,
                end_start + 1.35,
                "EndCode",
                code_y,
                code_animation,
                code_line,
            ),
        )
        for layer, start, style_name, y, animation, text in event_specs:
            start = min(start, max(end_start, duration - 2.5))
            events.append(
                f"Dialogue: {layer},{seconds_to_ass_time(start)},{end_time},"
                f"{style_name},,0,0,0,,"
                f"{{\\an{_alignment_number(style.outro_text_alignment, vertical='middle')}"
                f"\\pos({center_x},{y}){animation}}}{text}"
            )
    line_width = _effective_chars_per_line(style)
    ordered_cues = sorted(cues, key=lambda item: (item.start, item.end))
    if end_card_enabled and end_card_action.strip():
        # The fixed closing sentence is already displayed as the larger
        # EndAction line.  Rendering the same spoken sentence again in the
        # normal subtitle position crowds the code card and creates a visible
        # duplicate on the final frame.
        action_key = " ".join(end_card_action.split()).casefold()
        ordered_cues = [
            cue
            for cue in ordered_cues
            if " ".join(cue.text.split()).casefold() != action_key
        ]
    rendered_cues: Sequence[SubtitleCue] = (
        build_semantic_cues(ordered_cues) if style.semantic_short_phrases else ordered_cues
    )
    subtitle_line_limit = 2 if style.semantic_short_phrases else style.max_subtitle_lines
    animation_prefix = {
        "none": "",
        "fade": r"{\fad(90,0)}",
        "soft_pop": r"{\fscx94\fscy94\t(0,120,\fscx100\fscy100)\fad(80,0)}",
        # rise and mask_reveal need the resolved caption anchor/clip and are
        # emitted by ``_subtitle_event_prefix`` below.  Typewriter creates
        # timestamped states instead of a single inline override.
        "rise": "",
        "mask_reveal": "",
        "typewriter": "",
    }[style.subtitle_animation]
    for cue in rendered_cues:
        if cue.end > duration + 1e-6:
            raise ValueError("subtitle cue ends after video_duration")
        for page_start, page_end, page in _timed_caption_pages(
            cue,
            width=line_width,
            max_lines=subtitle_line_limit,
            # Never split readable novel copy in the middle of a word.  A
            # rare overlong token may exceed the conservative width estimate
            # slightly, which is preferable to visibly broken subtitles.
            break_long_words=False,
        ):
            page_duration_ms = max(1, round((page_end - page_start) * 1000))
            # Word-synchronised captions already create one event per spoken
            # token.  Replaying an entrance animation for every token would
            # visibly jitter, while typewriter and active-word colouring are
            # conceptually conflicting modes.  Preserve stable word sync.
            position_animation = (
                "none" if style.word_sync_enabled else style.subtitle_animation
            )
            subtitle_position_prefix = _subtitle_event_prefix(
                style,
                page,
                animation=position_animation,
                event_duration_ms=page_duration_ms,
            )
            if style.word_sync_enabled:
                for word_start, word_end, word_text in _word_sync_events(
                    page,
                    start=page_start,
                    end=page_end,
                    style=style,
                    position_prefix=subtitle_position_prefix,
                ):
                    events.append(
                        "Dialogue: 0,"
                        f"{seconds_to_ass_time(word_start)},{seconds_to_ass_time(word_end)},"
                        f"Subtitle,,0,0,0,,{word_text}"
                    )
            elif style.subtitle_animation == "typewriter":
                for state_start, state_end, state_text in _typewriter_events(
                    page,
                    start=page_start,
                    end=page_end,
                    position_prefix=subtitle_position_prefix,
                ):
                    events.append(
                        "Dialogue: 0,"
                        f"{seconds_to_ass_time(state_start)},{seconds_to_ass_time(state_end)},"
                        f"Subtitle,,0,0,0,,{state_text}"
                    )
            else:
                escaped = _escape_caption_page(page)
                events.append(
                    "Dialogue: 0,"
                    f"{seconds_to_ass_time(page_start)},{seconds_to_ass_time(page_end)},"
                    f"Subtitle,,0,0,0,,{subtitle_position_prefix}{animation_prefix}{escaped}"
                )
    return "\n".join(header + events) + "\n"


def write_ass(
    output_path: PathLike,
    cues: Sequence[SubtitleCue],
    *,
    platform: str,
    code: str,
    search_text: str | None = None,
    video_duration: float | None = None,
    end_card_title: str = "",
    end_card_action: str = "",
    end_card_duration: float | None = None,
    video_template: str = "classic",
    intro_card_text: str = "",
    intro_headline: str = "",
    intro_card_duration: float = 5.5,
    final_label: str = "",
    platform_logo_present: bool = False,
    platform_brand_color: str = "",
    config: AssStyleConfig | None = None,
) -> Path:
    """Atomically write a UTF-8 ASS file and return its path."""

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    content = generate_ass(
        cues,
        platform=platform,
        code=code,
        search_text=search_text,
        video_duration=video_duration,
        end_card_title=end_card_title,
        end_card_action=end_card_action,
        end_card_duration=end_card_duration,
        video_template=video_template,
        intro_card_text=intro_card_text,
        intro_headline=intro_headline,
        intro_card_duration=intro_card_duration,
        final_label=final_label,
        platform_logo_present=platform_logo_present,
        platform_brand_color=platform_brand_color,
        config=config,
    )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
        os.replace(temporary_name, output)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    return output


__all__ = [
    "AssStyleConfig",
    "NarrationSentence",
    "SubtitleCue",
    "SubtitleTimeline",
    "build_semantic_cues",
    "build_sentence_cues",
    "colour_to_ass",
    "estimate_sentence_duration",
    "generate_ass",
    "is_chapter_heading",
    "parse_narration_text",
    "paginate_sentence",
    "seconds_to_ass_time",
    "split_semantic_phrases",
    "wrap_sentence",
    "write_ass",
]
