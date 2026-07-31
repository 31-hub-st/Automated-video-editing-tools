"""Text preparation primitives for English StoryForge manuscripts.

The functions in this module deliberately use only the Python standard library.
They are kept independent from providers and rendering so the preflight stage can
be deterministic and inexpensive.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
import re
import unicodedata


_EXPECTED_FILENAME = "B73165_Story Title.txt"
_FILENAME_RE = re.compile(
    r"^(?P<code>[A-Za-z0-9]+)_(?P<title>.+)\.txt$", re.IGNORECASE
)
_CHAPTER_RE = re.compile(r"^\s*chapter\s+(?P<number>[0-9]+)\s*$", re.IGNORECASE)

# A Latin word may be a contraction, a hyphenated term, an acronym, or a
# number.  Include the full Latin Unicode blocks so words such as ``corazón``
# and ``Français`` are each counted once instead of being split around the
# accent.  CJK and Hangul are estimated separately below because they normally
# do not use whitespace-delimited words.
_LATIN_LETTERS = "A-Za-z\u00c0-\u024f\u1e00-\u1eff"
_WORD_RE = re.compile(
    rf"""
    (?<![{_LATIN_LETTERS}0-9_])
    (?:
        (?:[{_LATIN_LETTERS}]\.){{2,}}
        |
        [{_LATIN_LETTERS}][{_LATIN_LETTERS}0-9]*(?:['\u2019][{_LATIN_LETTERS}0-9]+)*(?:-[{_LATIN_LETTERS}0-9]+(?:['\u2019][{_LATIN_LETTERS}0-9]+)*)*
        |
        [0-9]+(?:[.,][0-9]+)*
    )
    (?![{_LATIN_LETTERS}0-9_])
    """,
    re.VERBOSE,
)

_CLOSING_PUNCTUATION = '\"\'\u2019\u201d)]}\u3009\u300b\u300d\u300f\u3011\u3015\u3017\u3019\u301b'
_CJK_SENTENCE_PUNCTUATION = "\u3002\uff01\uff1f"


def _script_character_counts(text: str) -> tuple[int, int, int, int]:
    """Return Han, Kana, Hangul and Thai spoken-character counts."""

    han = kana = hangul = thai = 0
    for character in text:
        point = ord(character)
        if (
            0x3400 <= point <= 0x4DBF
            or 0x4E00 <= point <= 0x9FFF
            or 0xF900 <= point <= 0xFAFF
        ):
            han += 1
        elif 0x3040 <= point <= 0x30FF or 0x31F0 <= point <= 0x31FF:
            kana += 1
        elif (
            0x1100 <= point <= 0x11FF
            or 0x3130 <= point <= 0x318F
            or 0xAC00 <= point <= 0xD7AF
        ):
            hangul += 1
        elif 0x0E00 <= point <= 0x0E7F and character.isalpha():
            thai += 1
    return han, kana, hangul, thai


def _weighted_script_units(text: str) -> int:
    """Estimate whitespace-free scripts as English-WPM-equivalent units.

    StoryForge keeps WPM as the user-facing speed control.  These conservative
    divisors translate typical narrated characters/syllables into equivalent
    planning units; actual audio duration still wins after TTS generation.
    """

    han, kana, hangul, thai = _script_character_counts(text)
    units = 0
    if kana:
        # Japanese prose mixes kana and Han. Roughly 1.5 spoken characters map
        # to one English-WPM planning unit at ordinary story-reading cadence.
        units += max(1, round((han + kana) / 1.5))
        han = 0
    if han:
        # Mandarin is denser than Japanese, so Han-only prose uses a smaller
        # divisor and therefore a slightly longer duration per character.
        units += max(1, round(han / 1.2))
    if hangul:
        units += max(1, round(hangul / 1.45))
    if thai:
        units += max(1, round(thai / 2.8))
    return units

# These titles almost never end a sentence when another name follows.
_TITLE_ABBREVIATIONS = {
    "capt",
    "col",
    "dr",
    "gen",
    "gov",
    "hon",
    "jr",
    "lt",
    "mr",
    "mrs",
    "ms",
    "pres",
    "prof",
    "rep",
    "rev",
    "sen",
    "sgt",
    "sr",
    "st",
}

_CONTEXTUAL_ABBREVIATIONS = {
    "approx",
    "ch",
    "co",
    "corp",
    "dept",
    "e.g",
    "etc",
    "fig",
    "i.e",
    "inc",
    "ltd",
    "mt",
    "no",
    "p.m",
    "a.m",
    "vs",
}

# An acronym followed by one of these words is likely at a real sentence end:
# "She lives in the U.S. It is a long trip."  Otherwise, protect constructions
# such as "the U.S. Embassy".
_LIKELY_SENTENCE_STARTERS = {
    "a",
    "after",
    "although",
    "and",
    "as",
    "at",
    "before",
    "but",
    "despite",
    "finally",
    "he",
    "her",
    "here",
    "however",
    "i",
    "if",
    "in",
    "it",
    "meanwhile",
    "my",
    "nevertheless",
    "next",
    "now",
    "on",
    "she",
    "since",
    "so",
    "still",
    "suddenly",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "though",
    "to",
    "we",
    "when",
    "while",
    "yet",
    "you",
}

# Common UTF-8 bytes interpreted as Windows-1252.  Escape notation keeps this
# source reliable even when a Windows console is using a legacy code page.
_WINDOWS_MOJIBAKE_REPLACEMENTS = {
    "\u00e2\u20ac\u2122": "\u2019",  # right single quote
    "\u00e2\u20ac\u02dc": "\u2018",  # left single quote
    "\u00e2\u20ac\u0153": "\u201c",  # left double quote
    "\u00e2\u20ac\u009d": "\u201d",  # right double quote (Latin-1 path)
    "\u00e2\u20ac\u201c": "\u2013",  # en dash
    "\u00e2\u20ac\u201d": "\u2014",  # em dash
    "\u00e2\u20ac\u00a6": "\u2026",  # ellipsis
    "\u00c2\u00a0": "\u00a0",  # non-breaking space
}

_TYPOGRAPHY_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '\"',
        "\u201d": '\"',
        "\u201e": '\"',
        "\u201f": '\"',
        "\u2033": '\"',
        "\u2010": "\u2014",
        "\u2011": "\u2014",
        "\u2012": "\u2014",
        "\u2013": "\u2014",
        "\u2015": "\u2014",
        "\u2026": "...",
        "\u00a0": " ",
        "\u200b": None,
        "\u200c": None,
        "\u200d": None,
        "\ufeff": None,
    }
)


class StoryFilenameError(ValueError):
    """Raised when a story filename does not contain a code and title."""


@dataclass(frozen=True, slots=True)
class StoryFilename:
    """The business identifiers encoded in a story TXT filename."""

    code: str
    title: str
    filename: str


@dataclass(frozen=True, slots=True)
class ChapterBoundary:
    """A removed chapter heading and its position in narration text.

    ``character_offset`` points at the first character of the chapter in the
    cleaned narration. ``sentence_index`` points at its first sentence.
    ``pause_before_seconds`` is zero for an opening heading and normally nonzero
    for subsequent chapters.
    """

    number: int
    heading: str
    source_line: int
    character_offset: int
    sentence_index: int
    pause_before_seconds: float


@dataclass(frozen=True, slots=True)
class ChapterExtraction:
    """Narration/subtitle text with standalone chapter headings removed."""

    narration_text: str
    subtitle_text: str
    boundaries: tuple[ChapterBoundary, ...]


@dataclass(frozen=True, slots=True)
class TextStatistics:
    """Word and duration estimates for one cleaned manuscript."""

    word_count: int
    words_per_minute: float
    estimated_speech_seconds: float
    chapter_pause_seconds: float
    estimated_total_seconds: float

    @property
    def estimated_total_minutes(self) -> float:
        return self.estimated_total_seconds / 60.0


@dataclass(frozen=True, slots=True)
class ManuscriptAnalysis:
    """Complete deterministic preflight analysis of a story manuscript."""

    story: StoryFilename
    original_text: str
    normalized_text: str
    narration_text: str
    subtitle_text: str
    chapters: tuple[ChapterBoundary, ...]
    sentences: tuple[str, ...]
    statistics: TextStatistics

    @property
    def code(self) -> str:
        return self.story.code

    @property
    def title(self) -> str:
        return self.story.title

    @property
    def word_count(self) -> int:
        return self.statistics.word_count

    @property
    def estimated_duration_seconds(self) -> float:
        return self.statistics.estimated_total_seconds


def parse_story_filename(filename: str | Path) -> StoryFilename:
    """Parse ``B73165_Story Title.txt`` into its code and title.

    Only the first underscore is structural, so underscores may still appear in
    the title.  A dedicated exception provides a useful preflight error instead
    of allowing a malformed job to fail much later in rendering.
    """

    if not isinstance(filename, (str, Path)):
        raise TypeError("filename must be a string or pathlib.Path")

    basename = Path(filename).name
    match = _FILENAME_RE.fullmatch(basename)
    if match is None:
        raise StoryFilenameError(
            f"Invalid story filename {basename!r}; expected a code containing "
            f"letters and/or numbers, an underscore, a non-empty title, and "
            f".txt (for example "
            f"{_EXPECTED_FILENAME!r})."
        )

    code = match.group("code")
    title = match.group("title").strip()
    if not title:
        raise StoryFilenameError(
            f"Invalid story filename {basename!r}; the title after '{code}_' "
            f"cannot be empty (for example {_EXPECTED_FILENAME!r})."
        )

    return StoryFilename(code=code, title=title, filename=basename)


def _repair_gbk_punctuation_pairs(text: str) -> str:
    """Repair GBK-rendered UTF-8 punctuation such as U+9225/U+6A9A.

    UTF-8 smart punctuation begins with bytes E2 80.  Interpreting those bytes
    as GBK produces U+9225 (the character commonly displayed as ``鈥``), while
    the final punctuation byte and the next ASCII byte become a second CJK
    character.  Re-encoding each suspicious pair recovers both characters.
    """

    output: list[str] = []
    index = 0
    punctuation = {"\u2018", "\u2019", "\u201c", "\u201d", "\u2013", "\u2014", "\u2026"}

    while index < len(text):
        if text[index] == "\u9225" and index + 1 < len(text):
            pair = text[index : index + 2]
            try:
                candidate = pair.encode("gbk").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                candidate = ""
            if candidate and any(mark in candidate for mark in punctuation):
                output.append(candidate)
                index += 2
                continue

        output.append(text[index])
        index += 1

    return "".join(output)


def _mojibake_score(text: str) -> int:
    markers = ("\u00c2", "\u00c3", "\u00e2\u20ac", "\u00f0\u0178", "\u9225", "\ufffd")
    return sum(text.count(marker) for marker in markers)


def repair_mojibake(text: str) -> str:
    """Conservatively recover common UTF-8/Windows-1252/GBK mojibake.

    The whole-string transcode is accepted only when it reduces known mojibake
    markers.  Targeted replacements then handle mixed text containing both
    already-correct Unicode and broken fragments.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")

    repaired = text
    if _mojibake_score(repaired):
        for encoding in ("cp1252", "latin-1"):
            try:
                candidate = repaired.encode(encoding).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if _mojibake_score(candidate) < _mojibake_score(repaired):
                repaired = candidate
                break

    for broken, correct in _WINDOWS_MOJIBAKE_REPLACEMENTS.items():
        repaired = repaired.replace(broken, correct)

    return _repair_gbk_punctuation_pairs(repaired)


def normalize_typography(text: str) -> str:
    """Use straight quotes and one canonical em dash in English narration."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")

    normalized = unicodedata.normalize("NFC", text).translate(_TYPOGRAPHY_TRANSLATION)
    # English em dashes conventionally have no surrounding spaces.  Restrict
    # this cleanup to horizontal whitespace so paragraph breaks are preserved.
    normalized = re.sub(r"[ \t]*\u2014[ \t]*", "\u2014", normalized)
    return normalized


def normalize_manuscript_text(text: str) -> str:
    """Repair encoding damage, typography, newlines, and trailing whitespace."""

    normalized = normalize_typography(repair_mojibake(text))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]

    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()

    return "\n".join(lines)


def split_english_sentences(text: str) -> tuple[str, ...]:
    """Split narrated prose across Latin and CJK sentence punctuation.

    The historical public name is retained for compatibility. English title,
    acronym and dialogue-tag protections still apply, while ``。！？`` may end
    a sentence without the following whitespace used by Latin prose.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")

    # A blank line is an intentional narration boundary even when the previous
    # paragraph ends in a quote without terminal punctuation.  Preserve it so
    # unrelated paragraphs do not become one very long TTS/subtitle cue.
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n+", text)
        if paragraph.strip()
    ]
    if len(paragraphs) > 1:
        return tuple(
            sentence
            for paragraph in paragraphs
            for sentence in split_english_sentences(paragraph)
        )

    prose = re.sub(r"\s+", " ", text).strip()
    if not prose:
        return ()

    sentences: list[str] = []
    start = 0
    index = 0

    while index < len(prose):
        if prose[index] not in f".!?{_CJK_SENTENCE_PUNCTUATION}":
            index += 1
            continue

        punctuation_start = index
        while (
            index + 1 < len(prose)
            and prose[index + 1] in f".!?{_CJK_SENTENCE_PUNCTUATION}"
        ):
            index += 1
        punctuation_end = index

        close_end = punctuation_end + 1
        while close_end < len(prose) and prose[close_end] in _CLOSING_PUNCTUATION:
            close_end += 1

        is_cjk_boundary = prose[punctuation_start] in _CJK_SENTENCE_PUNCTUATION
        if (
            not is_cjk_boundary
            and close_end < len(prose)
            and not prose[close_end].isspace()
        ):
            index += 1
            continue

        next_nonspace = close_end
        while next_nonspace < len(prose) and prose[next_nonspace].isspace():
            next_nonspace += 1

        # A quoted question followed by a lowercase dialogue tag is one sentence:
        # '"Are you ready?" she asked.'
        had_closing_quote = any(
            char in "\"'\u2019\u201d" for char in prose[punctuation_end + 1 : close_end]
        )
        if (
            not is_cjk_boundary
            and had_closing_quote
            and next_nonspace < len(prose)
            and prose[next_nonspace].islower()
        ):
            index += 1
            continue

        if (
            prose[punctuation_start] == "."
            and punctuation_start == punctuation_end
            and _is_protected_period(prose, punctuation_start, next_nonspace)
        ):
            index += 1
            continue

        sentence = prose[start:close_end].strip()
        if sentence:
            sentences.append(sentence)
        start = next_nonspace
        index = next_nonspace

    remainder = prose[start:].strip()
    if remainder:
        sentences.append(remainder)

    return tuple(sentences)


def _is_protected_period(text: str, period_index: int, next_index: int) -> bool:
    if (
        period_index > 0
        and period_index + 1 < len(text)
        and text[period_index - 1].isdigit()
        and text[period_index + 1].isdigit()
    ):
        return True

    prefix = text[: period_index + 1]
    next_match = re.match(r"[A-Za-z]+", text[next_index:]) if next_index < len(text) else None
    next_word = next_match.group(0) if next_match else ""

    acronym_match = re.search(r"(?:[A-Za-z]\.){2,}$", prefix)
    if acronym_match:
        if not next_word:
            return False
        return next_word.lower() not in _LIKELY_SENTENCE_STARTERS

    word_match = re.search(r"([A-Za-z]+)\.$", prefix)
    if word_match is None:
        return False

    abbreviation = word_match.group(1).lower()
    if abbreviation in _TITLE_ABBREVIATIONS:
        return bool(next_word)
    if abbreviation in _CONTEXTUAL_ABBREVIATIONS:
        if abbreviation == "no":
            return next_index < len(text) and text[next_index].isdigit()
        return bool(next_word) and next_word.lower() not in _LIKELY_SENTENCE_STARTERS

    # Protect initials in names such as "J. K. Rowling".
    if len(word_match.group(1)) == 1 and word_match.group(1).isupper():
        return bool(next_word) and next_word[0].isupper()

    return False


def extract_chapters(
    text: str,
    *,
    pause_seconds: float = 0.8,
) -> ChapterExtraction:
    """Remove standalone ``Chapter N`` lines and retain their timing metadata."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    _validate_nonnegative_finite(pause_seconds, "pause_seconds")

    lines = text.splitlines()
    content_lines: list[str] = []
    pending: list[int] = []
    boundaries_data: list[dict[str, object]] = []
    has_content = False

    for source_line, raw_line in enumerate(lines, start=1):
        chapter_match = _CHAPTER_RE.fullmatch(raw_line)
        if chapter_match:
            while content_lines and not content_lines[-1]:
                content_lines.pop()
            boundary_index = len(boundaries_data)
            boundaries_data.append(
                {
                    "number": int(chapter_match.group("number")),
                    "heading": raw_line.strip(),
                    "source_line": source_line,
                    "target_line": None,
                    "pause_before_seconds": pause_seconds if has_content else 0.0,
                }
            )
            pending.append(boundary_index)
            continue

        line = raw_line.rstrip()
        if not line:
            if pending:
                continue
            if content_lines and content_lines[-1]:
                content_lines.append("")
            continue

        if pending:
            if content_lines and content_lines[-1]:
                content_lines.append("")
            target_line = len(content_lines)
            for boundary_index in pending:
                boundaries_data[boundary_index]["target_line"] = target_line
            pending.clear()

        content_lines.append(line)
        has_content = True

    while content_lines and not content_lines[-1]:
        content_lines.pop()

    cleaned_text = "\n".join(content_lines)
    line_offsets: list[int] = []
    offset = 0
    for line_index, line in enumerate(content_lines):
        line_offsets.append(offset)
        offset += len(line)
        if line_index < len(content_lines) - 1:
            offset += 1

    boundaries: list[ChapterBoundary] = []
    for data in boundaries_data:
        target_line = data["target_line"]
        if isinstance(target_line, int) and target_line < len(line_offsets):
            character_offset = line_offsets[target_line]
        else:
            character_offset = len(cleaned_text)
        sentence_index = len(split_english_sentences(cleaned_text[:character_offset]))
        boundaries.append(
            ChapterBoundary(
                number=int(data["number"]),
                heading=str(data["heading"]),
                source_line=int(data["source_line"]),
                character_offset=character_offset,
                sentence_index=sentence_index,
                pause_before_seconds=float(data["pause_before_seconds"]),
            )
        )

    boundary_tuple = tuple(boundaries)
    return ChapterExtraction(
        narration_text=cleaned_text,
        subtitle_text=cleaned_text,
        boundaries=boundary_tuple,
    )


def count_words(text: str) -> int:
    """Count language-aware narration planning units.

    Latin-script languages retain ordinary word counting (including accented
    letters), while whitespace-free CJK/Hangul/Thai prose is converted to
    English-WPM-equivalent units. The name is kept because it is part of the
    existing import and manifest schema.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return sum(1 for _ in _WORD_RE.finditer(text)) + _weighted_script_units(text)


def estimate_duration_seconds(word_count: int, *, wpm: float = 155.0) -> float:
    """Estimate narration duration for ``word_count`` at the requested WPM."""

    if isinstance(word_count, bool) or not isinstance(word_count, int):
        raise TypeError("word_count must be an integer")
    if word_count < 0:
        raise ValueError("word_count cannot be negative")
    _validate_positive_finite(wpm, "wpm")
    return word_count * 60.0 / float(wpm)


def calculate_text_statistics(
    text: str,
    *,
    wpm: float = 155.0,
    chapter_pause_seconds: float = 0.0,
) -> TextStatistics:
    """Return word and duration statistics for narration-ready text."""

    _validate_positive_finite(wpm, "wpm")
    _validate_nonnegative_finite(chapter_pause_seconds, "chapter_pause_seconds")
    word_count = count_words(text)
    speech_seconds = estimate_duration_seconds(word_count, wpm=wpm)
    return TextStatistics(
        word_count=word_count,
        words_per_minute=float(wpm),
        estimated_speech_seconds=speech_seconds,
        chapter_pause_seconds=float(chapter_pause_seconds),
        estimated_total_seconds=speech_seconds + float(chapter_pause_seconds),
    )


def analyze_manuscript(
    text: str,
    filename: str | Path,
    *,
    wpm: float = 155.0,
    chapter_pause_seconds: float = 0.8,
) -> ManuscriptAnalysis:
    """Build a complete structured analysis for one English TXT manuscript."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not text.strip():
        raise ValueError("manuscript text is empty")

    story = parse_story_filename(filename)
    normalized = normalize_manuscript_text(text)
    chapters = extract_chapters(normalized, pause_seconds=chapter_pause_seconds)
    sentences = split_english_sentences(chapters.narration_text)
    total_pause = sum(boundary.pause_before_seconds for boundary in chapters.boundaries)
    statistics = calculate_text_statistics(
        chapters.narration_text,
        wpm=wpm,
        chapter_pause_seconds=total_pause,
    )

    # Recalculate sentence indices from the final sentence segmentation.  The
    # replacement is normally identical to extraction-time data, but keeping it
    # here makes the relationship explicit for future segmenter improvements.
    final_boundaries = tuple(
        replace(
            boundary,
            sentence_index=len(
                split_english_sentences(
                    chapters.narration_text[: boundary.character_offset]
                )
            ),
        )
        for boundary in chapters.boundaries
    )

    return ManuscriptAnalysis(
        story=story,
        original_text=text,
        normalized_text=normalized,
        narration_text=chapters.narration_text,
        subtitle_text=chapters.subtitle_text,
        chapters=final_boundaries,
        sentences=sentences,
        statistics=statistics,
    )


def _validate_positive_finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise ValueError(f"{name} must be a positive finite number")


def _validate_nonnegative_finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise ValueError(f"{name} must be a non-negative finite number")


__all__ = [
    "ChapterBoundary",
    "ChapterExtraction",
    "ManuscriptAnalysis",
    "StoryFilename",
    "StoryFilenameError",
    "TextStatistics",
    "analyze_manuscript",
    "calculate_text_statistics",
    "count_words",
    "estimate_duration_seconds",
    "extract_chapters",
    "normalize_manuscript_text",
    "normalize_typography",
    "parse_story_filename",
    "repair_mojibake",
    "split_english_sentences",
]
