from __future__ import annotations

import codecs
import hashlib
import math
import re
import unicodedata
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

from .text_processing import (
    count_words,
    normalize_manuscript_text,
    split_english_sentences,
)


# Source manuscripts may call their authored units chapters, episodes, parts,
# or use a localized equivalent. A heading must own the complete line so an
# in-prose reference such as "chapter 2 explains why" is not split.
_SOURCE_UNIT_LINE_RE = re.compile(
    r"^\s*(?:"
    r"(?:chapter|episode|ep\.?|part|book|cap[ií]tulo|epis[oó]dio|chapitre|"
    r"kapitel|bab|glava|глава)\s*[-_#:]?\s*"
    r"(?:\d{1,6}|[ivxlcdm]+)"
    r"(?:\s*[:.\-\u2013\u2014\uff1a]\s*.*|\s+[^\d\s].*)?"
    r"|"
    r"\u7b2c\s*(?:\d{1,6}|[\u4e00-\u9fff]{1,10})\s*"
    r"(?:\u96c6|\u7ae0|\u56de|\u8bdd|\u8a71|\u8282|\u7bc0|\u90e8)"
    r"(?:\s*[:.\-\u2013\u2014\uff1a]\s*.*|\s+.*)?"
    r"|"
    r"(?:\uc81c\s*)?\d{1,6}\s*(?:\ud654|\uc7a5)"
    r"(?:\s*[:.\-\u2013\u2014\uff1a]\s*.*|\s+.*)?"
    r")\s*$",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(r"\?")
_REVELATION_RE = re.compile(
    r"\b(?:"
    r"realized|discovered|learned|revealed|confessed|admitted|uncovered|"
    r"found\s+out|turned\s+out|the\s+truth|the\s+secret|"
    r"was\s+actually|were\s+actually|wasn't|weren't"
    r")\b",
    re.IGNORECASE,
)
_TURN_RE = re.compile(
    r"(?:"
    r"^\s*(?:but|however|yet|instead|suddenly|only\s+then|without\s+warning)\b|"
    r"\b(?:but\s+then|until\s+that\s+moment|only\s+to|to\s+my\s+surprise)\b"
    r")",
    re.IGNORECASE,
)
_SUSPENSE_RE = re.compile(
    r"\b(?:"
    r"froze|gasped|wasn't\s+alone|not\s+alone|phone\s+rang|"
    r"door\s+(?:opened|creaked)|footsteps|"
    r"voice\s+(?:said|whispered)|knock\s+at\s+the\s+door|"
    r"someone\s+was\s+watching|vanished|disappeared|went\s+missing"
    r")\b",
    re.IGNORECASE,
)
_DECISION_RE = re.compile(
    r"\b(?:decided|chose|swore|vowed|refused|agreed|"
    r"made\s+up\s+(?:his|her|my|their)\s+mind)\b",
    re.IGNORECASE,
)
_CONFLICT_RE = re.compile(
    r"\b(?:confronted|accused|threatened|attacked|slapped|punched|fought)\b",
    re.IGNORECASE,
)
_DRAMATIC_END_RE = re.compile(r"(?:!+|\.{3})[\"')\]}]*$")
_DOCX_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_BINARY_SIGNATURES = (
    b"MZ",
    b"PK\x03\x04",
    b"%PDF-",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"\xff\xd8\xff",
    b"RIFF",
)
_TEXT_WHITESPACE = {"\t", "\n", "\r", "\f"}
_CJK_PUNCTUATION = set("，。！？；：、…“”‘’（）《》【】—")


@dataclass(frozen=True, slots=True)
class ImportedChapter:
    ordinal: int
    heading: str
    text: str
    word_count: int
    is_explicit_boundary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EpisodeProposal:
    ordinal: int
    title: str
    text: str
    source_chapter_ordinals: tuple[int, ...]
    source_start_word: int
    source_end_word: int
    word_count: int
    estimated_seconds: float
    boundary_reason: str
    duration_warning: bool
    source_heading: str = ""
    source_part_index: int = 1
    source_part_count: int = 1
    explicit_source_boundary: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_chapter_ordinals"] = list(self.source_chapter_ordinals)
        return data


@dataclass(frozen=True, slots=True)
class ImportedManuscript:
    title: str
    source_name: str
    normalized_text: str
    content_sha256: str
    word_count: int
    estimated_seconds: float
    chapters: tuple[ImportedChapter, ...]
    episodes: tuple[EpisodeProposal, ...]

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        data = {
            "title": self.title,
            "source_name": self.source_name,
            "content_sha256": self.content_sha256,
            "word_count": self.word_count,
            "estimated_seconds": self.estimated_seconds,
            "chapters": [item.to_dict() for item in self.chapters],
            "episodes": [item.to_dict() for item in self.episodes],
        }
        if include_text:
            data["normalized_text"] = self.normalized_text
        return data


def _is_han(character: str) -> bool:
    value = ord(character)
    return (
        0x3400 <= value <= 0x4DBF
        or 0x4E00 <= value <= 0x9FFF
        or 0xF900 <= value <= 0xFAFF
        or 0x20000 <= value <= 0x3134F
    )


def _is_kana(character: str) -> bool:
    value = ord(character)
    return (
        0x3040 <= value <= 0x30FF
        or 0x31F0 <= value <= 0x31FF
        or 0xFF66 <= value <= 0xFF9D
    )


def _is_latin(character: str) -> bool:
    return "LATIN" in unicodedata.name(character, "")


def _text_quality(text: str, encoding: str) -> float | None:
    """Score one strict legacy decode without mistaking arbitrary bytes for prose.

    GB18030, Shift-JIS, and cp1252 overlap enough that simply trying them in
    sequence is unsafe.  Script evidence makes the choice deterministic while
    the control, symbol, and repetition checks keep binary data out.
    """

    if not text or not text.strip():
        return None
    if "\x00" in text or "\ufffd" in text:
        return None

    controls = [
        character
        for character in text
        if unicodedata.category(character) == "Cc"
        and character not in _TEXT_WHITESPACE
    ]
    if controls:
        return None

    visible = [character for character in text if not character.isspace()]
    if not visible:
        return None
    categories = [unicodedata.category(character) for character in visible]
    suspicious = sum(
        category in {"Cn", "Co", "Cs"} for category in categories
    )
    if suspicious:
        return None

    meaningful = sum(
        category[0] in {"L", "N"} for category in categories
    )
    if meaningful < max(1, round(len(visible) * 0.28)):
        return None

    if len(visible) >= 16:
        frequency = Counter(visible)
        most_common = frequency.most_common(1)[0][1]
        if most_common / len(visible) > 0.72:
            return None
        if len(visible) >= 32 and len(frequency) / len(visible) < 0.16:
            return None

    printable = sum(
        character.isprintable() or character in _TEXT_WHITESPACE
        for character in text
    ) / len(text)
    if printable < 0.96:
        return None

    han = sum(_is_han(character) for character in visible)
    kana = sum(_is_kana(character) for character in visible)
    latin = sum(_is_latin(character) for character in visible)
    ascii_letters = sum(character.isascii() and character.isalpha() for character in visible)
    punctuation = sum(
        category[0] == "P" or character in _CJK_PUNCTUATION
        for character, category in zip(visible, categories)
    )
    whitespace = sum(character.isspace() for character in text)
    visible_count = len(visible)

    score = printable * 30.0
    score += meaningful / visible_count * 40.0
    score += min(15.0, whitespace / max(1, len(text)) * 180.0)
    score += min(10.0, punctuation / visible_count * 100.0)

    if encoding == "gb18030":
        score += han / visible_count * 80.0
        score -= kana / visible_count * 80.0
    elif encoding == "shift_jis":
        score += han / visible_count * 25.0
        score += kana / visible_count * 120.0
        if kana:
            score += 35.0
    elif encoding == "cp1252":
        score += latin / visible_count * 25.0
        score += ascii_letters / max(1, meaningful) * 65.0
        if not ascii_letters and len(visible) >= 8:
            score -= 50.0

    if len(visible) >= 16 and not whitespace and not punctuation:
        score -= 30.0
    return score


def _validated_text(text: str, filename: str) -> str:
    if _text_quality(text, "utf") is None:
        raise ValueError(
            f"无法读取 {filename}；文件不像可读小说文本，请使用 UTF-8、"
            "UTF-16、GB18030 或 Shift-JIS 编码。"
        )
    return text


def _decode_txt(raw: bytes, filename: str) -> str:
    if not raw:
        raise ValueError(f"无法读取 {filename}；TXT 文件为空。")
    if any(raw.startswith(signature) for signature in _BINARY_SIGNATURES):
        raise ValueError(f"无法读取 {filename}；文件内容不是 TXT 文本。")

    # A BOM is authoritative.  Do not fall through to a permissive legacy
    # codec when the explicitly declared Unicode payload is damaged.
    if raw.startswith(codecs.BOM_UTF8):
        try:
            return _validated_text(raw.decode("utf-8-sig", errors="strict"), filename)
        except UnicodeDecodeError as error:
            raise ValueError(f"无法读取 {filename}；UTF-8 文本已损坏。") from error
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return _validated_text(raw.decode("utf-16", errors="strict"), filename)
        except UnicodeDecodeError as error:
            raise ValueError(f"无法读取 {filename}；UTF-16 文本已损坏。") from error

    try:
        decoded_utf8 = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        decoded_utf8 = ""
    if decoded_utf8:
        return _validated_text(decoded_utf8, filename)

    candidates: list[tuple[float, int, str]] = []
    for priority, encoding in enumerate(("gb18030", "shift_jis", "cp1252")):
        try:
            candidate = raw.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
        score = _text_quality(candidate, encoding)
        if score is not None:
            candidates.append((score, -priority, candidate))

    if candidates:
        score, _priority, text = max(candidates, key=lambda item: (item[0], item[1]))
        if score >= 100.0:
            return text
    raise ValueError(
        f"无法读取 {filename}；请将 TXT 保存为 UTF-8、UTF-16、GB18030 "
        "或 Shift-JIS，且不要上传二进制文件。"
    )


def _read_docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            document = archive.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise ValueError(f"无法读取 Word 文件 {path.name}；请使用有效的 .docx 文件。") from error

    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as error:
        raise ValueError(f"Word 文件 {path.name} 的正文结构损坏。") from error

    namespace = {"w": _DOCX_NAMESPACE}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:body/w:p", namespace):
        fragments: list[str] = []
        for node in paragraph.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "t" and node.text:
                fragments.append(node.text)
            elif tag == "tab":
                fragments.append("\t")
            elif tag in {"br", "cr"}:
                fragments.append("\n")
        paragraphs.append("".join(fragments).strip())
    text = "\n\n".join(paragraphs).strip()
    if not text:
        raise ValueError(f"Word 文件 {path.name} 没有可读取的正文。")
    return text


def read_manuscript_file(path: str | Path) -> str:
    source = Path(path).expanduser()
    if not source.is_file():
        raise ValueError("小说文件不存在。")
    suffix = source.suffix.casefold()
    if suffix == ".txt":
        return _decode_txt(source.read_bytes(), source.name)
    if suffix == ".docx":
        return _read_docx(source)
    if suffix == ".doc":
        raise ValueError("暂不支持旧版 .doc；请在 Word 中另存为 .docx。")
    raise ValueError("只支持 TXT 或 DOCX 小说文件。")


def _title_from_source(source_name: str) -> str:
    stem = Path(source_name).stem.strip()
    if "_" in stem:
        prefix, candidate = stem.split("_", 1)
        if prefix.isalnum() and candidate.strip():
            return candidate.strip()
    return stem or "Untitled story"


def split_source_chapters(text: str) -> tuple[ImportedChapter, ...]:
    normalized = normalize_manuscript_text(text)
    if not normalized.strip():
        raise ValueError("小说正文不能为空。")

    chapters: list[ImportedChapter] = []
    current_heading = ""
    current_is_explicit = False
    current_lines: list[str] = []

    def commit() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if not body:
            current_lines = []
            return
        ordinal = len(chapters) + 1
        chapters.append(
            ImportedChapter(
                ordinal=ordinal,
                heading=current_heading or f"Chapter {ordinal}",
                text=body,
                word_count=count_words(body),
                is_explicit_boundary=current_is_explicit,
            )
        )
        current_lines = []

    for line in normalized.splitlines():
        if _SOURCE_UNIT_LINE_RE.fullmatch(line):
            commit()
            current_heading = line.strip()
            current_is_explicit = True
            continue
        current_lines.append(line.rstrip())
    commit()

    # Promotional manuscripts often put a short hook before ``Bab 1``,
    # ``Chapter 1``, or ``第1話``.  It belongs to the opening authored unit;
    # presenting it as a separate E001 would give the operator a false extra
    # episode and detach the hook from the story it introduces.
    first_explicit_index = next(
        (
            index
            for index, chapter in enumerate(chapters)
            if chapter.is_explicit_boundary
        ),
        -1,
    )
    if first_explicit_index > 0:
        first_explicit = chapters[first_explicit_index]
        prefix = "\n\n".join(
            chapter.text.strip()
            for chapter in chapters[:first_explicit_index]
            if chapter.text.strip()
        )
        merged_text = f"{prefix}\n\n{first_explicit.text}".strip()
        chapters = [
            ImportedChapter(
                ordinal=1,
                heading=first_explicit.heading,
                text=merged_text,
                word_count=count_words(merged_text),
                is_explicit_boundary=True,
            ),
            *[
                ImportedChapter(
                    ordinal=index,
                    heading=chapter.heading,
                    text=chapter.text,
                    word_count=chapter.word_count,
                    is_explicit_boundary=chapter.is_explicit_boundary,
                )
                for index, chapter in enumerate(
                    chapters[first_explicit_index + 1 :],
                    start=2,
                )
            ],
        ]

    if not chapters:
        chapters.append(
            ImportedChapter(
                ordinal=1,
                heading="Chapter 1",
                text=normalized,
                word_count=count_words(normalized),
                is_explicit_boundary=False,
            )
        )
    return tuple(chapters)


def _boundary_quality(sentence: str) -> tuple[int, str]:
    """Rank an episode ending by narrative value, not mere punctuation.

    Every sentence is a mechanically safe split point, but a question, reveal,
    turn, or suspense beat is much more likely to make the next video feel
    intentional.  Equal-quality candidates are still resolved by proximity to
    the requested duration in ``_sentence_chunks``.
    """

    if _QUESTION_RE.search(sentence):
        return (3, "suspense question")
    if _REVELATION_RE.search(sentence):
        return (3, "revelation boundary")
    if _TURN_RE.search(sentence):
        return (3, "turning-point boundary")
    if _SUSPENSE_RE.search(sentence):
        return (3, "suspense boundary")
    if _DECISION_RE.search(sentence):
        return (2, "decision boundary")
    if _CONFLICT_RE.search(sentence):
        return (2, "conflict boundary")
    if _DRAMATIC_END_RE.search(sentence):
        return (1, "dramatic beat")
    return (0, "sentence boundary")


def _canonical_content(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _sentence_chunks(
    text: str,
    *,
    target_words: int,
    maximum_words: int,
) -> list[tuple[str, str]]:
    sentences = list(split_english_sentences(text))
    if not sentences:
        return []

    chunks: list[tuple[str, str]] = []
    start = 0
    while start < len(sentences):
        words = 0
        candidates: list[tuple[int, int, int, str]] = []
        end = start
        while end < len(sentences):
            sentence_words = count_words(sentences[end])
            if words and words + sentence_words > maximum_words:
                break
            words += sentence_words
            score, reason = _boundary_quality(sentences[end])
            distance = abs(words - target_words)
            if words >= max(1, int(target_words * 0.68)):
                candidates.append((score, -distance, end + 1, reason))
            end += 1
            if words >= maximum_words:
                break

        if end >= len(sentences):
            chosen = len(sentences)
            reason = "story ending"
        elif candidates:
            _, _, chosen, reason = max(
                candidates,
                key=lambda candidate: (candidate[0], candidate[1]),
            )
        else:
            chosen = max(start + 1, end)
            reason = "duration boundary"
        chunks.append((" ".join(sentences[start:chosen]).strip(), reason))
        start = chosen
    return chunks


def plan_video_episodes(
    chapters: Iterable[ImportedChapter],
    *,
    wpm: float = 210.0,
    target_minutes: float = 7.5,
    maximum_minutes: float = 10.0,
) -> tuple[EpisodeProposal, ...]:
    if not math.isfinite(float(wpm)) or wpm <= 0:
        raise ValueError("wpm must be a positive finite number")
    if not math.isfinite(float(target_minutes)) or target_minutes <= 0:
        raise ValueError("target_minutes must be positive")
    if not math.isfinite(float(maximum_minutes)) or maximum_minutes < target_minutes:
        raise ValueError("maximum_minutes must be at least target_minutes")

    chapter_list = [item for item in chapters if item.text.strip()]
    if not chapter_list:
        raise ValueError("至少需要一个非空章节。")

    target_words = max(1, round(wpm * target_minutes))
    maximum_words = max(target_words, round(wpm * maximum_minutes))
    units: list[
        tuple[str, tuple[int, ...], str, str, int, int, bool]
    ] = []

    for chapter in chapter_list:
        if chapter.word_count > maximum_words:
            chunks = _sentence_chunks(
                chapter.text,
                target_words=target_words,
                maximum_words=maximum_words,
            )
            part_count = len(chunks)
            for part_index, (chunk, reason) in enumerate(chunks, start=1):
                if chunk:
                    units.append(
                        (
                            chunk,
                            (chapter.ordinal,),
                            reason,
                            chapter.heading if chapter.is_explicit_boundary else "",
                            part_index,
                            part_count,
                            chapter.is_explicit_boundary,
                        )
                    )
        else:
            units.append(
                (
                    chapter.text.strip(),
                    (chapter.ordinal,),
                    (
                        "explicit source boundary"
                        if chapter.is_explicit_boundary
                        else "chapter boundary"
                    ),
                    chapter.heading if chapter.is_explicit_boundary else "",
                    1,
                    1,
                    chapter.is_explicit_boundary,
                )
            )

    merged: list[
        tuple[str, tuple[int, ...], str, str, int, int, bool]
    ] = []
    buffer_text: list[str] = []
    buffer_chapters: list[int] = []
    buffer_words = 0
    buffer_reason = "duration boundary"

    def flush_automatic_buffer(reason: str | None = None) -> None:
        nonlocal buffer_text, buffer_chapters, buffer_words, buffer_reason
        if not buffer_text:
            return
        merged.append(
            (
                "\n\n".join(buffer_text),
                tuple(dict.fromkeys(buffer_chapters)),
                reason or buffer_reason,
                "",
                1,
                1,
                False,
            )
        )
        buffer_text = []
        buffer_chapters = []
        buffer_words = 0
        buffer_reason = "duration boundary"

    for (
        text,
        source_ordinals,
        reason,
        source_heading,
        source_part_index,
        source_part_count,
        explicit_source_boundary,
    ) in units:
        # Authored headings are user-visible production choices.  Never merge
        # across them, even when a unit is very short.  A long authored unit
        # may be divided, but every part remains inside that source boundary.
        if explicit_source_boundary:
            flush_automatic_buffer()
            merged.append(
                (
                    text,
                    source_ordinals,
                    reason,
                    source_heading,
                    source_part_index,
                    source_part_count,
                    True,
                )
            )
            continue

        words = count_words(text)
        if buffer_text and buffer_words + words > maximum_words:
            flush_automatic_buffer()
        buffer_text.append(text)
        buffer_chapters.extend(source_ordinals)
        buffer_words += words
        buffer_reason = reason
        if buffer_words >= target_words:
            flush_automatic_buffer()
    if buffer_text:
        if (
            merged
            and not merged[-1][6]
            and buffer_words < int(target_words * 0.35)
        ):
            (
                previous_text,
                previous_chapters,
                _previous_reason,
                _previous_heading,
                _previous_part_index,
                _previous_part_count,
                _previous_explicit,
            ) = merged[-1]
            combined = f"{previous_text}\n\n{' '.join(buffer_text)}".strip()
            if count_words(combined) <= maximum_words:
                merged[-1] = (
                    combined,
                    tuple(dict.fromkeys((*previous_chapters, *buffer_chapters))),
                    "story ending",
                    "",
                    1,
                    1,
                    False,
                )
            else:
                flush_automatic_buffer("story ending")
        else:
            flush_automatic_buffer("story ending")

    episodes: list[EpisodeProposal] = []
    source_word_cursor = 0
    for ordinal, (
        text,
        source_ordinals,
        reason,
        source_heading,
        source_part_index,
        source_part_count,
        explicit_source_boundary,
    ) in enumerate(merged, start=1):
        words = count_words(text)
        seconds = words * 60.0 / float(wpm)
        title = f"E{ordinal:03d}"
        if explicit_source_boundary:
            title = source_heading
            if source_part_count > 1:
                title = f"{source_heading} ({source_part_index}/{source_part_count})"
        episodes.append(
            EpisodeProposal(
                ordinal=ordinal,
                title=title,
                text=text,
                source_chapter_ordinals=source_ordinals,
                source_start_word=source_word_cursor,
                source_end_word=source_word_cursor + words,
                word_count=words,
                estimated_seconds=seconds,
                boundary_reason=reason,
                duration_warning=seconds > maximum_minutes * 60.0,
                source_heading=source_heading,
                source_part_index=source_part_index,
                source_part_count=source_part_count,
                explicit_source_boundary=explicit_source_boundary,
            )
        )
        source_word_cursor += words

    source_content = _canonical_content(
        "\n\n".join(chapter.text.strip() for chapter in chapter_list)
    )
    planned_content = _canonical_content(
        "\n\n".join(episode.text for episode in episodes)
    )
    if planned_content != source_content:
        raise RuntimeError(
            "episode planning changed manuscript content or order"
        )
    if source_word_cursor != sum(chapter.word_count for chapter in chapter_list):
        raise RuntimeError("episode planning lost or duplicated manuscript words")
    return tuple(episodes)


def prepare_manuscript(
    text: str,
    *,
    title: str = "",
    source_name: str = "pasted-story.txt",
    wpm: float = 210.0,
) -> ImportedManuscript:
    normalized = normalize_manuscript_text(text)
    if not normalized.strip():
        raise ValueError("小说正文不能为空。")
    chapters = split_source_chapters(normalized)
    episodes = plan_video_episodes(chapters, wpm=wpm)
    words = count_words("\n\n".join(item.text for item in chapters))
    fingerprint_text = re.sub(r"\s+", " ", normalized).strip().casefold()
    digest = hashlib.sha256(fingerprint_text.encode("utf-8")).hexdigest()
    return ImportedManuscript(
        title=title.strip() or _title_from_source(source_name),
        source_name=source_name,
        normalized_text=normalized,
        content_sha256=digest,
        word_count=words,
        estimated_seconds=words * 60.0 / float(wpm),
        chapters=chapters,
        episodes=episodes,
    )


def prepare_manuscript_file(
    path: str | Path,
    *,
    title: str = "",
    wpm: float = 210.0,
) -> ImportedManuscript:
    source = Path(path).expanduser()
    text = read_manuscript_file(source)
    return prepare_manuscript(text, title=title, source_name=source.name, wpm=wpm)
