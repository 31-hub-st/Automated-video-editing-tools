from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    COLOR_GRADES,
    COVER_ANIMATIONS,
    INTRO_ANIMATIONS,
    SUBTITLE_ANIMATIONS,
    VISUAL_STYLE_PRESETS,
)
from .services.language_detection import (
    LANGUAGE_NAMES_ZH,
    LanguageDetection,
    detect_language,
    normalize_language_code,
)
from .services.manuscript_import import prepare_manuscript
from .services.text_processing import count_words
from .production_presets import ProductionPresetStore


SCHEMA_VERSION = 12
SQLITE_MAX_INTEGER = (1 << 63) - 1

ROLE_ADMIN = "admin"
ROLE_PRODUCER = "producer"
USER_ROLES = frozenset({ROLE_ADMIN, ROLE_PRODUCER})

KNOWN_PERMISSIONS = frozenset(
    {
        "library.view",
        "library.edit",
        "platforms.manage",
        "promo_codes.use",
        "promo_codes.manage",
        "publishing_accounts.manage",
        "drafts.create",
        "drafts.manage_all",
        "samples.approve_own",
        "samples.approve_all",
        "records.view_own",
        "records.view_all",
        "records.export",
        "jobs.retry_own",
        "jobs.retry_all",
        "production.execute",
        "voice.preview",
        "text.assist",
        "presets.manage_own",
        "updates.manage_own",
        "users.manage",
        "permissions.manage",
        "hub.manage",
    }
)

ROLE_DEFAULTS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: KNOWN_PERMISSIONS,
    ROLE_PRODUCER: frozenset(
        {
            "library.view",
            "promo_codes.use",
            "drafts.create",
            "samples.approve_own",
            "records.view_own",
            "jobs.retry_own",
            "production.execute",
            "voice.preview",
            "text.assist",
            "presets.manage_own",
            "updates.manage_own",
        }
    ),
}

# Schema version 3 exposed a middle-tier supervisor role. During the version 4
# migration those users become producers, while these former defaults are
# written as account-level overrides so their effective access neither grows
# nor shrinks. Keep this migration-only policy separate from the current roles.
LEGACY_SUPERVISOR_DEFAULTS = frozenset(
    {
        "library.view",
        "library.edit",
        "platforms.manage",
        "promo_codes.use",
        "promo_codes.manage",
        "publishing_accounts.manage",
        "drafts.create",
        "drafts.manage_all",
        "samples.approve_own",
        "samples.approve_all",
        "records.view_own",
        "records.view_all",
        "records.export",
        "jobs.retry_own",
        "jobs.retry_all",
    }
)

SUPER_ADMIN_PERMISSIONS = frozenset(
    {"users.manage", "permissions.manage", "hub.manage"}
)

# Voice previews are production data, not editable novel content.  Keep their
# schema deliberately narrow so a producer can generate and select narration
# without gaining access to titles, manuscripts, covers, synopses or arbitrary
# metadata keys.
NOVEL_VOICE_STATE_FIELDS = frozenset(
    {
        "voice_candidates",
        "preferred_voice_provider",
        "preferred_voice_id",
        "preferred_voice_label",
        "preferred_voice_profile",
        "locked_voice_provider",
        "locked_voice_id",
        "locked_voice_label",
        "locked_voice_profile",
        "voice_lock_history",
    }
)
VOICE_CANDIDATE_FIELDS = frozenset(
    {
        "profile",
        "label",
        "provider",
        "voice_id",
        "audio_path",
        "audio_uri",
        "duration_seconds",
        "excerpt",
        "language",
        "voice_name",
        "narration_wpm",
        "cached",
        "cache_key",
    }
)
VOICE_LOCK_HISTORY_FIELDS = frozenset({"provider", "voice_id", "label"})

PROMO_CODE_STATUSES = frozenset({"active", "inactive", "expired", "revoked"})
PUBLISHING_ACCOUNT_STATUSES = frozenset({"active", "inactive", "archived"})
DRAFT_STATUSES = frozenset({"draft", "ready", "archived"})
RECORD_STATUSES = frozenset(
    {
        "queued",
        "preflight",
        "sample_ready",
        "awaiting_approval",
        "running",
        "completed",
        "failed",
        "skipped",
        "interrupted",
        "cancelled",
    }
)


# Only settings that are meaningful and safe on every render workstation may
# be distributed by the Hub.  In particular this contract intentionally
# excludes providers, credentials, network addresses, local paths, binaries,
# encoders and commands.  Keep the portable payload small and versioned; the
# desktop applies it on top of its own machine-local settings.
PORTABLE_CONFIG_SCALAR_ENUMS: dict[str, frozenset[str]] = {
    "output_mode": frozenset({"video_and_mp3", "audio_only", "reuse_audio"}),
    "video_transition": frozenset({"cut", "fade"}),
    "subtitle_word_mode": frozenset({"off", "cumulative", "single"}),
    "bgm_mode": frozenset({"auto", "none"}),
    "adult_mode": frozenset({"direct", "engaging"}),
    "caption_mode": frozenset({"sentence", "semantic"}),
    "subtitle_preset": frozenset(VISUAL_STYLE_PRESETS["subtitle"]),
    "intro_card_preset": frozenset(VISUAL_STYLE_PRESETS["intro_card"]),
    "code_card_preset": frozenset(VISUAL_STYLE_PRESETS["code_card"]),
    "outro_card_preset": frozenset(VISUAL_STYLE_PRESETS["outro_card"]),
    "subtitle_animation": SUBTITLE_ANIMATIONS,
    "intro_animation": INTRO_ANIMATIONS,
    "cover_animation": COVER_ANIMATIONS,
    "color_grade": COLOR_GRADES,
    "render_mode": frozenset({"speed", "quality", "compatibility"}),
    "video_template": frozenset({"classic", "platform_story_card"}),
}
PORTABLE_CONFIG_NUMBER_RANGES: dict[str, tuple[float, float, bool]] = {
    "retention_min": (0.50, 1.0, False),
    "retention_max": (0.50, 1.0, False),
    "narration_wpm": (200, 280, True),
    "video_playback_speed": (0.8, 3.0, False),
    "chapter_pause_seconds": (0.0, 10.0, False),
    "output_width": (480, 3840, True),
    "output_height": (480, 3840, True),
    "bgm_volume": (0.0, 1.0, False),
    "preview_seconds": (5, 60, True),
    "max_episode_minutes": (1.0, 60.0, False),
    "end_card_seconds": (5.0, 7.0, False),
}
PORTABLE_CONFIG_BOOLEAN_FIELDS = frozenset({"cover_outro_enabled"})
PORTABLE_STYLE_FIELDS: dict[str, dict[str, tuple[str, Any]]] = {
    "subtitle": {
        "font_family": ("text", 120),
        "font_size": ("int", (16, 120)),
        "text_color": ("color", None),
        "outline_color": ("color", None),
        "outline_width": ("int", (0, 12)),
        "bottom_margin": ("int", (0, 1200)),
        "horizontal_margin": ("int", (0, 500)),
        "max_chars_per_line": ("int", (8, 80)),
        "bold": ("bool", None),
        "italic": ("bool", None),
        "shadow_width": ("number", (0.0, 12.0)),
        "background_color": ("color", None),
        "background_opacity": ("number", (0.0, 1.0)),
        "alignment": ("enum", frozenset({"left", "center", "right"})),
        "position_x_percent": ("number", (5.0, 95.0)),
        "max_lines": ("int", (1, 5)),
        "word_sync_enabled": ("bool", None),
        "unread_color": ("color", None),
        "active_color": ("color", None),
        "read_color": ("color", None),
        "pop_scale": ("int", (100, 160)),
        "pop_duration_ms": ("int", (40, 600)),
        "pop_intensity": ("number", (0.0, 1.0)),
    },
    "intro_card": {
        "font_family": ("text", 120),
        "headline_font_size": ("int", (16, 120)),
        "headline_color": ("color", None),
        "body_font_size": ("int", (14, 90)),
        "body_color": ("color", None),
        "label_font_size": ("int", (12, 72)),
        "label_color": ("color", None),
        "background_color": ("color", None),
        "background_opacity": ("number", (0.0, 1.0)),
        "border_color": ("color", None),
        "border_width": ("int", (0, 12)),
        "shadow_opacity": ("number", (0.0, 1.0)),
        "width_percent": ("number", (25.0, 95.0)),
        "position_x_percent": ("number", (5.0, 95.0)),
        "position_y_percent": ("number", (5.0, 95.0)),
        "padding": ("int", (0, 160)),
        "radius": ("int", (0, 100)),
        "text_alignment": ("enum", frozenset({"left", "center", "right"})),
        "max_lines": ("int", (1, 12)),
    },
    "code_card": {
        "font_family": ("text", 120),
        "font_size": ("int", (16, 100)),
        "text_color": ("color", None),
        "background_color": ("color", None),
        "opacity": ("number", (0.0, 1.0)),
        "top_margin": ("int", (0, 1000)),
        "horizontal_margin": ("int", (0, 500)),
        "bold": ("bool", None),
        "outline_color": ("color", None),
        "outline_width": ("number", (0.0, 12.0)),
        "alignment": ("enum", frozenset({"left", "center", "right"})),
        "position_x_percent": ("number", (5.0, 95.0)),
        "position_y_percent": ("number", (2.0, 95.0)),
        "width_percent": ("number", (20.0, 95.0)),
        "padding": ("int", (0, 100)),
        "radius": ("int", (0, 100)),
    },
    "outro_card": {
        "font_family": ("text", 120),
        "title_font_size": ("int", (16, 120)),
        "title_color": ("color", None),
        "body_font_size": ("int", (14, 90)),
        "body_color": ("color", None),
        "code_font_size": ("int", (16, 120)),
        "code_color": ("color", None),
        "background_color": ("color", None),
        "background_opacity": ("number", (0.0, 1.0)),
        "border_color": ("color", None),
        "border_width": ("int", (0, 12)),
        "width_percent": ("number", (25.0, 95.0)),
        "height_percent": ("number", (20.0, 90.0)),
        "position_x_percent": ("number", (5.0, 95.0)),
        "position_y_percent": ("number", (5.0, 95.0)),
        "padding": ("int", (0, 160)),
        "radius": ("int", (0, 100)),
        "text_alignment": ("enum", frozenset({"left", "center", "right"})),
    },
}
PORTABLE_VOICE_MOODS = frozenset({"suspense", "romance", "sad", "revenge"})
PORTABLE_VOICE_PROFILES = frozenset({"dramatic", "warm", "calm", "confident"})
_PORTABLE_FORBIDDEN_KEY_PARTS = (
    "path",
    "endpoint",
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "command",
    "executable",
    "binary",
    "encoder",
)


class CatalogError(RuntimeError):
    """Base exception for catalog operations."""


class CatalogValidationError(CatalogError, ValueError):
    """Raised when a public API payload is invalid."""


class CatalogNotFoundError(CatalogError, LookupError):
    """Raised when a referenced catalog entity does not exist."""


class CatalogConflictError(CatalogError):
    """Raised when a unique or optimistic-concurrency rule is violated."""


class CatalogPermissionError(CatalogError, PermissionError):
    """Raised when an authenticated actor does not own a scoped entity."""


class DuplicateContentError(CatalogConflictError):
    """Raised when a revision is attached to the wrong duplicate novel."""

    def __init__(self, novel_id: str, revision_id: str, content_hash: str) -> None:
        self.novel_id = novel_id
        self.revision_id = revision_id
        self.content_hash = content_hash
        super().__init__(
            f"content already belongs to novel {novel_id} (revision {revision_id})"
        )


class PromoCodeLimitError(CatalogConflictError):
    """Raised after five historical codes have been claimed for one binding."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_manuscript_for_hash(value: str) -> str:
    """Return a stable representation for exact-body de-duplication.

    The normalization deliberately ignores BOMs, newline style, trailing spaces,
    repeated horizontal whitespace and surplus blank lines.  Letter case and
    punctuation remain significant so materially different prose is not merged.
    """

    if not isinstance(value, str):
        raise CatalogValidationError("body must be text")
    text = unicodedata.normalize("NFC", value.replace("\ufeff", ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Line wrapping and paragraph spacing are file-layout details, not story
    # identity.  Collapsing all Unicode whitespace also catches copies exported
    # from TXT, pasted text and DOCX with different visual wrapping.
    return re.sub(r"\s+", " ", text).strip()


def manuscript_sha256(value: str) -> str:
    normalized = normalize_manuscript_for_hash(value)
    if not normalized:
        raise CatalogValidationError("body cannot be empty")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _new_id() -> str:
    return uuid4().hex


def _json_dump(value: Any) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_load(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        loaded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return loaded


def _required_text(value: Any, label: str, *, maximum: int = 500) -> str:
    text = str(value or "").strip()
    if not text:
        raise CatalogValidationError(f"{label} is required")
    if len(text) > maximum:
        raise CatalogValidationError(f"{label} is too long")
    return text


def _optional_text(value: Any, *, maximum: int = 10000) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise CatalogValidationError("text value is too long")
    return text


def _normalized_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _positive_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise CatalogValidationError(f"{label} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise CatalogValidationError(f"{label} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise CatalogValidationError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return parsed


def _metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CatalogValidationError("metadata must be an object")
    return {str(key): item for key, item in value.items()}


def installation_id_sha256(value: Any) -> str:
    """Hash a private installation identifier before catalog persistence."""

    identifier = _required_text(value, "installation_id", maximum=2000)
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()


def _installation_id_hash(value: Any) -> str:
    fingerprint = str(value or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise CatalogValidationError(
            "installation_id_hash must be a lowercase SHA-256 hex digest"
        )
    return fingerprint


def _bounded_json_object(
    value: Any,
    *,
    label: str,
    maximum_bytes: int = 32_768,
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CatalogValidationError(f"{label} must be an object")
    normalized = {str(key): item for key, item in value.items()}
    try:
        encoded = _json_dump(normalized).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CatalogValidationError(f"{label} must contain JSON values") from error
    if len(encoded) > maximum_bytes:
        raise CatalogValidationError(f"{label} is too large")
    return normalized


def _portable_number(
    value: Any,
    *,
    label: str,
    minimum: float,
    maximum: float,
    integer: bool,
) -> int | float:
    if isinstance(value, bool):
        raise CatalogValidationError(f"{label} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise CatalogValidationError(f"{label} must be a number") from error
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise CatalogValidationError(
            f"{label} must be between {minimum:g} and {maximum:g}"
        )
    if integer:
        if not number.is_integer():
            raise CatalogValidationError(f"{label} must be an integer")
        return int(number)
    return number


def _portable_text(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise CatalogValidationError(f"{label} must be text")
    text = value.strip()
    if not text or len(text) > maximum:
        raise CatalogValidationError(f"{label} is invalid")
    if (
        "://" in text
        or re.match(r"^[A-Za-z]:[\\/]", text)
        or text.startswith(("/", "\\\\"))
        or any(character in text for character in ("\x00", "\r", "\n", ";", "|"))
    ):
        raise CatalogValidationError(f"{label} cannot contain paths or commands")
    return text


def _reject_forbidden_portable_keys(value: Mapping[str, Any], *, prefix: str = "") -> None:
    for raw_key, item in value.items():
        key = str(raw_key).strip()
        normalized = key.casefold().replace("-", "_")
        label = f"{prefix}.{key}" if prefix else key
        if any(part in normalized for part in _PORTABLE_FORBIDDEN_KEY_PARTS):
            raise CatalogValidationError(
                f"portable config field {label} is machine-local or sensitive"
            )
        if isinstance(item, Mapping):
            _reject_forbidden_portable_keys(item, prefix=label)


def _portable_style(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogValidationError(f"{name} must be an object")
    schema = PORTABLE_STYLE_FIELDS[name]
    unsupported = sorted(set(map(str, value)) - set(schema))
    if unsupported:
        raise CatalogValidationError(
            f"{name} contains unsupported fields: {', '.join(unsupported)}"
        )
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        kind, constraint = schema[key]
        label = f"{name}.{key}"
        if kind == "bool":
            if not isinstance(raw_value, bool):
                raise CatalogValidationError(f"{label} must be true or false")
            result[key] = raw_value
        elif kind == "text":
            result[key] = _portable_text(
                raw_value, label=label, maximum=int(constraint)
            )
        elif kind == "color":
            color = str(raw_value or "").strip().upper()
            if not re.fullmatch(r"#[0-9A-F]{6}", color):
                raise CatalogValidationError(f"{label} must be a #RRGGBB color")
            result[key] = color
        elif kind == "enum":
            selected = str(raw_value or "").strip().casefold()
            if selected not in constraint:
                raise CatalogValidationError(f"{label} is unsupported")
            result[key] = selected
        else:
            minimum, maximum = constraint
            result[key] = _portable_number(
                raw_value,
                label=label,
                minimum=float(minimum),
                maximum=float(maximum),
                integer=kind == "int",
            )
    return result


def normalize_portable_device_config(value: Any) -> dict[str, Any]:
    """Validate and detach the only settings a Hub may push to workstations."""

    if not isinstance(value, Mapping):
        raise CatalogValidationError("portable config must be an object")
    incoming = {str(key): item for key, item in value.items()}
    _reject_forbidden_portable_keys(incoming)
    allowed = (
        set(PORTABLE_CONFIG_SCALAR_ENUMS)
        | set(PORTABLE_CONFIG_NUMBER_RANGES)
        | set(PORTABLE_CONFIG_BOOLEAN_FIELDS)
        | {
            "language",
            "output_fps",
            "voice_by_mood",
            "subtitle",
            "intro_card",
            "code_card",
            "outro_card",
        }
    )
    unsupported = sorted(set(incoming) - allowed)
    if unsupported:
        raise CatalogValidationError(
            f"portable config contains unsupported fields: {', '.join(unsupported)}"
        )
    if not incoming:
        raise CatalogValidationError("portable config cannot be empty")

    result: dict[str, Any] = {}
    for key, choices in PORTABLE_CONFIG_SCALAR_ENUMS.items():
        if key not in incoming:
            continue
        selected = str(incoming[key] or "").strip().casefold()
        if selected not in choices:
            raise CatalogValidationError(f"{key} is unsupported")
        result[key] = selected
    for key, (minimum, maximum, integer) in PORTABLE_CONFIG_NUMBER_RANGES.items():
        if key in incoming:
            result[key] = _portable_number(
                incoming[key],
                label=key,
                minimum=minimum,
                maximum=maximum,
                integer=integer,
            )
    for key in PORTABLE_CONFIG_BOOLEAN_FIELDS:
        if key not in incoming:
            continue
        if not isinstance(incoming[key], bool):
            raise CatalogValidationError(f"{key} must be a boolean")
        result[key] = incoming[key]
    if "language" in incoming:
        language = _portable_text(
            incoming["language"], label="language", maximum=24
        )
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", language):
            raise CatalogValidationError("language must be a language tag")
        result["language"] = language
    if "output_fps" in incoming:
        fps = _portable_number(
            incoming["output_fps"],
            label="output_fps",
            minimum=30,
            maximum=60,
            integer=True,
        )
        if fps not in {30, 60}:
            raise CatalogValidationError("output_fps must be 30 or 60")
        result["output_fps"] = fps
    if "voice_by_mood" in incoming:
        voices = incoming["voice_by_mood"]
        if not isinstance(voices, Mapping):
            raise CatalogValidationError("voice_by_mood must be an object")
        unsupported_moods = sorted(set(map(str, voices)) - PORTABLE_VOICE_MOODS)
        if unsupported_moods:
            raise CatalogValidationError(
                "voice_by_mood contains unsupported moods: "
                + ", ".join(unsupported_moods)
            )
        normalized_voices: dict[str, str] = {}
        for raw_mood, raw_profile in voices.items():
            mood = str(raw_mood)
            profile = str(raw_profile or "").strip().casefold()
            if profile not in PORTABLE_VOICE_PROFILES:
                raise CatalogValidationError(
                    f"voice_by_mood.{mood} is unsupported"
                )
            normalized_voices[mood] = profile
        result["voice_by_mood"] = normalized_voices
    for style_name in PORTABLE_STYLE_FIELDS:
        if style_name in incoming:
            result[style_name] = _portable_style(style_name, incoming[style_name])

    if (
        "retention_min" in result
        and "retention_max" in result
        and float(result["retention_min"]) > float(result["retention_max"])
    ):
        raise CatalogValidationError("retention_min cannot exceed retention_max")
    encoded = _json_dump(result).encode("utf-8")
    if len(encoded) > 65_536:
        raise CatalogValidationError("portable config is too large")
    # JSON round-tripping guarantees callers never retain a mutable reference.
    return json.loads(encoded.decode("utf-8"))


def _voice_state_mapping(
    value: Any,
    *,
    label: str,
    allowed_fields: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogValidationError(f"{label} must be an object")
    normalized = {str(key): item for key, item in value.items()}
    unsupported = sorted(set(normalized) - allowed_fields)
    if unsupported:
        raise CatalogValidationError(
            f"{label} contains unsupported fields: {', '.join(unsupported)}"
        )
    return normalized


def _voice_candidate(value: Any, *, index: int) -> dict[str, Any]:
    candidate = _voice_state_mapping(
        value,
        label=f"voice candidate {index}",
        allowed_fields=VOICE_CANDIDATE_FIELDS,
    )
    duration_raw = candidate.get("duration_seconds", 0.0)
    try:
        duration = float(duration_raw or 0.0)
    except (TypeError, ValueError) as error:
        raise CatalogValidationError(
            f"voice candidate {index} duration_seconds must be a number"
        ) from error
    if not 0.0 <= duration <= 3600.0:
        raise CatalogValidationError(
            f"voice candidate {index} duration_seconds is out of range"
        )
    try:
        narration_wpm = int(candidate.get("narration_wpm") or 0)
    except (TypeError, ValueError) as error:
        raise CatalogValidationError(
            f"voice candidate {index} narration_wpm must be an integer"
        ) from error
    if narration_wpm and not 200 <= narration_wpm <= 280:
        raise CatalogValidationError(
            f"voice candidate {index} narration_wpm is out of range"
        )
    cached = candidate.get("cached", False)
    if not isinstance(cached, bool):
        raise CatalogValidationError(
            f"voice candidate {index} cached must be a boolean"
        )
    return {
        "profile": _optional_text(candidate.get("profile"), maximum=120),
        "label": _optional_text(candidate.get("label"), maximum=300),
        "provider": _required_text(
            candidate.get("provider"),
            f"voice candidate {index} provider",
            maximum=120,
        ),
        "voice_id": _required_text(
            candidate.get("voice_id"),
            f"voice candidate {index} voice_id",
            maximum=300,
        ),
        "audio_path": _optional_text(candidate.get("audio_path"), maximum=2000),
        "audio_uri": _optional_text(candidate.get("audio_uri"), maximum=4000),
        "duration_seconds": duration,
        "excerpt": _optional_text(candidate.get("excerpt"), maximum=4000),
        "language": _optional_text(candidate.get("language"), maximum=60),
        "voice_name": _optional_text(candidate.get("voice_name"), maximum=300),
        "narration_wpm": narration_wpm,
        "cached": cached,
        "cache_key": _optional_text(candidate.get("cache_key"), maximum=128),
    }


def _voice_candidates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise CatalogValidationError("voice_candidates must be an array")
    if len(value) > 3:
        raise CatalogValidationError("voice_candidates cannot contain more than 3 voices")
    return [
        _voice_candidate(candidate, index=index)
        for index, candidate in enumerate(value, start=1)
    ]


def _voice_lock_history(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise CatalogValidationError("voice_lock_history must be an array")
    if len(value) > 100:
        raise CatalogValidationError("voice_lock_history is too long")
    history: list[dict[str, str]] = []
    for index, raw in enumerate(value, start=1):
        item = _voice_state_mapping(
            raw,
            label=f"voice lock history item {index}",
            allowed_fields=VOICE_LOCK_HISTORY_FIELDS,
        )
        history.append(
            {
                "provider": _optional_text(item.get("provider"), maximum=120),
                "voice_id": _required_text(
                    item.get("voice_id"),
                    f"voice lock history item {index} voice_id",
                    maximum=300,
                ),
                "label": _optional_text(item.get("label"), maximum=300),
            }
        )
    return history


def _requested_language(value: Any) -> str | None:
    """Return a canonical manual language, or ``None`` for automatic mode."""

    raw = str(value or "").strip()
    if not raw or raw.casefold() == "auto":
        return None
    try:
        return normalize_language_code(raw)
    except ValueError as error:
        raise CatalogValidationError(str(error)) from error


def _language_values(
    detection: LanguageDetection,
    *,
    manual_code: str | None = None,
) -> dict[str, Any]:
    effective_code = manual_code or detection.code
    return {
        "language": effective_code,
        "language_code": effective_code,
        "language_name": LANGUAGE_NAMES_ZH[effective_code],
        "language_confidence": 1.0 if manual_code else detection.confidence,
        "language_source": "manual" if manual_code else "auto",
        "detected_language_code": detection.code,
        "detected_language_name": detection.display_name,
        "detected_language_confidence": detection.confidence,
        "language_detected_at": utc_now(),
    }


_SCHEMA = r"""
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS software_users (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    username TEXT NOT NULL,
    normalized_username TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('admin', 'producer')),
    password_hash TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(site_id, normalized_username)
);

CREATE TABLE IF NOT EXISTS user_permissions (
    user_id TEXT NOT NULL REFERENCES software_users(id) ON DELETE CASCADE,
    permission TEXT NOT NULL,
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, permission)
);

CREATE TABLE IF NOT EXISTS hub_devices (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    installation_id_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    hostname TEXT NOT NULL DEFAULT '',
    app_version TEXT NOT NULL DEFAULT '',
    os_name TEXT NOT NULL DEFAULT '',
    architecture TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    last_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(site_id, installation_id_hash)
);

CREATE INDEX IF NOT EXISTS idx_hub_devices_site_seen
    ON hub_devices(site_id, active, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS hub_access_tokens (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES software_users(id) ON DELETE CASCADE,
    device_id TEXT REFERENCES hub_devices(id) ON DELETE SET NULL,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    revoked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hub_tokens_user
    ON hub_access_tokens(site_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS device_config_revisions (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    config_schema_version INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    target_mode TEXT NOT NULL CHECK (target_mode IN ('single', 'multiple', 'all')),
    note TEXT NOT NULL DEFAULT '',
    created_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, revision_number)
);

CREATE INDEX IF NOT EXISTS idx_device_config_revisions_site
    ON device_config_revisions(site_id, revision_number DESC);

CREATE TABLE IF NOT EXISTS device_config_targets (
    revision_id TEXT NOT NULL REFERENCES device_config_revisions(id) ON DELETE CASCADE,
    device_id TEXT NOT NULL REFERENCES hub_devices(id) ON DELETE CASCADE,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    assigned_at TEXT NOT NULL,
    acknowledged_at TEXT,
    ack_status TEXT NOT NULL DEFAULT '' CHECK (ack_status IN ('', 'applied', 'failed')),
    ack_message TEXT NOT NULL DEFAULT '',
    reported_config_hash TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(revision_id, device_id)
);

CREATE INDEX IF NOT EXISTS idx_device_config_targets_device
    ON device_config_targets(site_id, device_id, assigned_at DESC);

CREATE TABLE IF NOT EXISTS platforms (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    search_template TEXT NOT NULL DEFAULT 'Search {platform}: {code}',
    ending_template TEXT NOT NULL DEFAULT 'Download {platform} and search code {code} to continue reading.',
    logo_path TEXT NOT NULL DEFAULT '',
    brand_color TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(site_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS novels (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    synopsis TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'unknown',
    language_code TEXT NOT NULL DEFAULT 'unknown',
    language_name TEXT NOT NULL DEFAULT '未识别',
    language_confidence REAL NOT NULL DEFAULT 0,
    language_source TEXT NOT NULL DEFAULT 'auto',
    detected_language_code TEXT NOT NULL DEFAULT 'unknown',
    detected_language_name TEXT NOT NULL DEFAULT '未识别',
    detected_language_confidence REAL NOT NULL DEFAULT 0,
    language_detected_at TEXT,
    cover_path TEXT NOT NULL DEFAULT '',
    current_revision_id TEXT,
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_novels_site_title
    ON novels(site_id, normalized_title);

CREATE TABLE IF NOT EXISTS content_revisions (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE RESTRICT,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    body TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_format TEXT NOT NULL DEFAULT 'text',
    source_name TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(novel_id, revision_number),
    UNIQUE(site_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_revisions_novel
    ON content_revisions(novel_id, revision_number);

CREATE TABLE IF NOT EXISTS chapters (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES content_revisions(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(revision_id, ordinal)
);

CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES content_revisions(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    title TEXT NOT NULL DEFAULT '',
    source_map_json TEXT NOT NULL DEFAULT '[]',
    recap_text TEXT NOT NULL DEFAULT '',
    estimated_duration_seconds REAL,
    status TEXT NOT NULL DEFAULT 'planned',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(revision_id, ordinal)
);

CREATE TABLE IF NOT EXISTS novel_platform_bindings (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE RESTRICT,
    platform_id TEXT NOT NULL REFERENCES platforms(id) ON DELETE RESTRICT,
    external_book_id TEXT NOT NULL DEFAULT '',
    platform_title TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    commission_rate REAL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(novel_id, platform_id)
);

CREATE TABLE IF NOT EXISTS promo_codes (
    id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES novel_platform_bindings(id) ON DELETE RESTRICT,
    slot_no INTEGER NOT NULL CHECK (slot_no BETWEEN 1 AND 5),
    code TEXT NOT NULL,
    normalized_code TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive', 'expired', 'revoked')),
    label TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(binding_id, slot_no),
    UNIQUE(binding_id, normalized_code)
);

CREATE TRIGGER IF NOT EXISTS promo_codes_max_five
BEFORE INSERT ON promo_codes
WHEN (SELECT COUNT(*) FROM promo_codes WHERE binding_id = NEW.binding_id) >= 5
BEGIN
    SELECT RAISE(ABORT, 'promo code historical limit reached');
END;

CREATE TRIGGER IF NOT EXISTS promo_codes_no_delete
BEFORE DELETE ON promo_codes
BEGIN
    SELECT RAISE(ABORT, 'promo codes cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS publishing_accounts (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    network TEXT NOT NULL,
    normalized_network TEXT NOT NULL,
    handle TEXT NOT NULL,
    normalized_handle TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive', 'archived')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(site_id, normalized_network, normalized_handle)
);

CREATE TABLE IF NOT EXISTS production_drafts (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE RESTRICT,
    binding_id TEXT NOT NULL REFERENCES novel_platform_bindings(id) ON DELETE RESTRICT,
    promo_code_id TEXT NOT NULL REFERENCES promo_codes(id) ON DELETE RESTRICT,
    publishing_account_id TEXT REFERENCES publishing_accounts(id) ON DELETE SET NULL,
    creative_line_count INTEGER NOT NULL CHECK (creative_line_count > 0),
    novel_title_snapshot TEXT NOT NULL,
    platform_name_snapshot TEXT NOT NULL,
    promo_code_snapshot TEXT NOT NULL,
    voice_profile TEXT NOT NULL DEFAULT '',
    subtitle_style_id TEXT NOT NULL DEFAULT '',
    outro_style_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('draft', 'ready', 'archived')),
    created_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_episodes (
    draft_id TEXT NOT NULL REFERENCES production_drafts(id) ON DELETE CASCADE,
    episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY(draft_id, episode_id),
    UNIQUE(draft_id, ordinal)
);

CREATE TABLE IF NOT EXISTS production_batches (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    external_run_id TEXT NOT NULL DEFAULT '',
    draft_id TEXT REFERENCES production_drafts(id) ON DELETE SET NULL,
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE RESTRICT,
    binding_id TEXT NOT NULL REFERENCES novel_platform_bindings(id) ON DELETE RESTRICT,
    publishing_account_id TEXT REFERENCES publishing_accounts(id) ON DELETE SET NULL,
    created_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
    device_id TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    archived_at TEXT,
    trashed_at TEXT,
    trashed_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(site_id, external_run_id)
);

CREATE INDEX IF NOT EXISTS idx_batches_site_created
    ON production_batches(site_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_batches_novel_created
    ON production_batches(novel_id, created_at DESC);

CREATE TABLE IF NOT EXISTS production_records (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    batch_id TEXT REFERENCES production_batches(id) ON DELETE SET NULL,
    draft_id TEXT REFERENCES production_drafts(id) ON DELETE SET NULL,
    job_id TEXT,
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE RESTRICT,
    binding_id TEXT NOT NULL REFERENCES novel_platform_bindings(id) ON DELETE RESTRICT,
    episode_id TEXT REFERENCES episodes(id) ON DELETE SET NULL,
    publishing_account_id TEXT REFERENCES publishing_accounts(id) ON DELETE SET NULL,
    created_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
    device_id TEXT NOT NULL DEFAULT '',
    variant_index INTEGER NOT NULL CHECK (variant_index > 0),
    logical_task_key TEXT NOT NULL DEFAULT '',
    current_attempt INTEGER NOT NULL DEFAULT 1 CHECK (current_attempt > 0),
    novel_title_snapshot TEXT NOT NULL,
    platform_name_snapshot TEXT NOT NULL,
    promo_code_snapshot TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'preflight', 'sample_ready', 'awaiting_approval', 'running',
        'completed', 'failed', 'skipped', 'interrupted', 'cancelled'
    )),
    progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
    output_path TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    completed_at TEXT,
    lease_owner_device TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    cancel_requested_at TEXT,
    cancelled_at TEXT,
    cancel_requested_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
    cancellation_reason TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    archived_at TEXT,
    archived_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
    archive_snapshot_json TEXT NOT NULL DEFAULT '{}',
    trashed_at TEXT,
    trashed_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(site_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_records_site_created
    ON production_records(site_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_records_novel
    ON production_records(novel_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_records_publishing_account
    ON production_records(publishing_account_id, created_at DESC);

-- Version-11 indexes that reference new ledger columns are created by
-- _migrate_production_ledger_schema().  Keeping them out of this bootstrap
-- script is essential: SQLite executes the bootstrap before migrations, and
-- a real version-10 database does not have batch_id / trashed_at yet.

CREATE TABLE IF NOT EXISTS production_record_attempts (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    record_id TEXT NOT NULL REFERENCES production_records(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    job_id TEXT NOT NULL DEFAULT '',
    device_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
    output_path TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    completed_at TEXT,
    cancel_requested_at TEXT,
    cancelled_at TEXT,
    cancellation_reason TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(record_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS idx_record_attempts_record
    ON production_record_attempts(record_id, attempt_no DESC);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES production_records(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    device_id TEXT NOT NULL DEFAULT '',
    local_path TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    duration_seconds REAL CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_record ON artifacts(record_id);

CREATE TABLE IF NOT EXISTS media_usage_events (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    fingerprint TEXT NOT NULL,
    media_type TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    local_path TEXT NOT NULL DEFAULT '',
    record_id TEXT REFERENCES production_records(id) ON DELETE SET NULL,
    device_id TEXT NOT NULL DEFAULT '',
    use_count INTEGER NOT NULL DEFAULT 1 CHECK (use_count > 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    used_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_usage_fingerprint
    ON media_usage_events(site_id, fingerprint, used_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    actor_user_id TEXT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_site_created
    ON audit_events(site_id, created_at DESC);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
"""


class CatalogRepository:
    """SQLite-backed catalog suitable for a local app or one Hub process.

    The repository never retains a SQLite connection.  Every public operation
    opens and closes its own connection, which keeps background renderer threads
    safe and lets a future HTTP Hub use the same repository implementation.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        site_id: str = "local",
        site_name: str = "StoryForge",
        busy_timeout_ms: int = 5000,
    ) -> None:
        raw_path = str(database_path)
        if raw_path == ":memory:":
            raise CatalogValidationError(
                "a file-backed database is required because every operation uses a new connection"
            )
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.site_id = _required_text(site_id, "site_id", maximum=120)
        self.site_name = _required_text(site_name, "site_name", maximum=200)
        self.busy_timeout_ms = _positive_int(
            busy_timeout_ms,
            "busy_timeout_ms",
            minimum=100,
            maximum=120_000,
        )
        # Complete production recipes are shared beside the Hub catalog.  They
        # intentionally stay outside settings.json so applying a recipe can
        # never copy provider credentials, device paths or Hub secrets.
        self._production_presets = ProductionPresetStore(
            self.database_path.parent / "production-presets.json"
        )
        self._transaction_local = threading.local()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
            if mode.casefold() != "wal":
                raise CatalogError(f"could not enable SQLite WAL mode (got {mode})")
            connection.executescript(_SCHEMA)
            database_version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            )
            if database_version > SCHEMA_VERSION:
                raise CatalogError(
                    f"catalog schema {database_version} is newer than supported schema {SCHEMA_VERSION}"
                )
            self._migrate_schema(connection, database_version)
            now = utc_now()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO sites(id, name, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
                    """,
                    (self.site_id, self.site_name, now, now),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()

    @staticmethod
    def _migrate_schema(
        connection: sqlite3.Connection, database_version: int
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(production_records)")
        }
        lease_columns = {
            "lease_owner_device": "TEXT NOT NULL DEFAULT ''",
            "lease_expires_at": "TEXT",
            "heartbeat_at": "TEXT",
        }
        missing = [name for name in lease_columns if name not in columns]
        if database_version < 2 or missing:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for name in missing:
                    connection.execute(
                        f"ALTER TABLE production_records ADD COLUMN {name} {lease_columns[name]}"
                    )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_records_lease_expiry
                    ON production_records(site_id, lease_expires_at)
                    """
                )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, utc_now()),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

        if database_version >= 4:
            # Inspect individual rows rather than trusting MAX(version). A
            # restored or partially repaired database can contain a newer row
            # while an earlier idempotent migration marker is missing.
            applied = {
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations WHERE version >= 5"
                ).fetchall()
            }
            if 5 not in applied:
                CatalogRepository._migrate_language_schema(connection)
            if 6 not in applied:
                CatalogRepository._migrate_episode_planner_schema(connection)
            if 7 not in applied:
                CatalogRepository._migrate_multilingual_timing_schema(connection)
            if 8 not in applied:
                CatalogRepository._migrate_platform_branding_schema(connection)
            if 9 not in applied:
                CatalogRepository._migrate_job_archive_schema(connection)
            # Always run the idempotent structural check.  It repairs a
            # partially restored catalog even when its migration marker was
            # copied without all version-10 tables or columns.
            CatalogRepository._migrate_device_management_schema(connection)
            CatalogRepository._migrate_production_ledger_schema(connection)
            if 12 not in applied:
                CatalogRepository._migrate_authored_episode_choices(connection)
            return

        # Version 3 temporarily introduced a supervisor role. Version 4 folds
        # that role into producer and narrows the CHECK constraint back to the
        # two roles exposed by the product. Before changing the role, freeze
        # only the defaults that differ between supervisor and producer as
        # per-user overrides. Existing explicit overrides win via INSERT OR
        # IGNORE, so effective access is exactly the same after migration, the
        # account is not elevated to admin, and the settings page stays concise.
        # SQLite cannot alter a CHECK in place, so rebuild only the parent user
        # table while foreign-key enforcement is paused. Child rows keep the
        # same user IDs and remain attached.
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            supervisor_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM software_users WHERE role = 'supervisor'"
                ).fetchall()
            ]
            generated_at = utc_now()
            changed_defaults = sorted(
                LEGACY_SUPERVISOR_DEFAULTS.symmetric_difference(
                    ROLE_DEFAULTS[ROLE_PRODUCER]
                )
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO user_permissions(
                    user_id, permission, allowed, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        user_id,
                        permission,
                        int(permission in LEGACY_SUPERVISOR_DEFAULTS),
                        generated_at,
                    )
                    for user_id in supervisor_ids
                    for permission in changed_defaults
                ],
            )
            connection.execute(
                """
                CREATE TABLE software_users_v4 (
                    id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
                    username TEXT NOT NULL,
                    normalized_username TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL CHECK (role IN ('admin', 'producer')),
                    password_hash TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    row_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(site_id, normalized_username)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO software_users_v4(
                    id, site_id, username, normalized_username, display_name,
                    role, password_hash, active, metadata_json, row_version,
                    created_at, updated_at
                )
                SELECT id, site_id, username, normalized_username, display_name,
                    CASE WHEN role = 'supervisor' THEN 'producer' ELSE role END,
                    password_hash, active, metadata_json, row_version,
                    created_at, updated_at
                FROM software_users
                """
            )
            connection.execute("DROP TABLE software_users")
            connection.execute("ALTER TABLE software_users_v4 RENAME TO software_users")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise CatalogError(
                    "catalog role migration failed foreign-key validation"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (3, utc_now()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (4, utc_now()),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

        CatalogRepository._migrate_language_schema(connection)
        CatalogRepository._migrate_episode_planner_schema(connection)
        CatalogRepository._migrate_multilingual_timing_schema(connection)
        CatalogRepository._migrate_platform_branding_schema(connection)
        CatalogRepository._migrate_job_archive_schema(connection)
        CatalogRepository._migrate_device_management_schema(connection)
        CatalogRepository._migrate_production_ledger_schema(connection)
        CatalogRepository._migrate_authored_episode_choices(connection)

    @staticmethod
    def _migrate_language_schema(connection: sqlite3.Connection) -> None:
        """Add deterministic manuscript-language classification and backfill it."""

        definitions = {
            "language_code": "TEXT NOT NULL DEFAULT 'unknown'",
            "language_name": "TEXT NOT NULL DEFAULT '未识别'",
            "language_confidence": "REAL NOT NULL DEFAULT 0",
            "language_source": "TEXT NOT NULL DEFAULT 'auto'",
            "detected_language_code": "TEXT NOT NULL DEFAULT 'unknown'",
            "detected_language_name": "TEXT NOT NULL DEFAULT '未识别'",
            "detected_language_confidence": "REAL NOT NULL DEFAULT 0",
            "language_detected_at": "TEXT",
        }
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(novels)").fetchall()
        }
        connection.execute("BEGIN IMMEDIATE")
        try:
            for name, definition in definitions.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE novels ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_novels_site_language
                ON novels(site_id, language_code)
                """
            )
            detected_at = utc_now()
            rows = connection.execute(
                """
                SELECT n.id, COALESCE(r.body, '') AS body
                FROM novels n
                LEFT JOIN content_revisions r ON r.id = n.current_revision_id
                """
            ).fetchall()
            for row in rows:
                detection = detect_language(str(row["body"] or ""))
                connection.execute(
                    """
                    UPDATE novels SET
                        language = ?, language_code = ?, language_name = ?,
                        language_confidence = ?, language_source = 'auto',
                        detected_language_code = ?, detected_language_name = ?,
                        detected_language_confidence = ?, language_detected_at = ?
                    WHERE id = ?
                    """,
                    (
                        detection.code,
                        detection.code,
                        detection.display_name,
                        detection.confidence,
                        detection.code,
                        detection.display_name,
                        detection.confidence,
                        detected_at,
                        row["id"],
                    ),
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (5, detected_at),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_episode_planner_schema(connection: sqlite3.Connection) -> None:
        """Safely rebuild unreferenced current revisions with planner v2.

        Authored chapter/episode headings became first-class production choices
        in planner v2.  Existing episode IDs may already be frozen into drafts
        or production history, so those revisions must remain byte-for-byte
        untouched.  ``BEGIN IMMEDIATE`` prevents a writer from creating a new
        reference between the safety check and replacement.

        The per-revision metadata marker makes this migration idempotent even
        if the global migration row is removed while recovering a database.
        """

        connection.execute("BEGIN IMMEDIATE")
        try:
            current_revisions = connection.execute(
                """
                SELECT r.id, r.body, r.source_name, r.metadata_json, n.title
                FROM content_revisions r
                JOIN novels n ON n.current_revision_id = r.id
                ORDER BY r.created_at, r.id
                """
            ).fetchall()
            migrated_at = utc_now()
            for revision in current_revisions:
                revision_id = str(revision["id"])
                revision_metadata = _json_load(revision["metadata_json"], {})
                if not isinstance(revision_metadata, dict):
                    revision_metadata = {}
                try:
                    planner_version = int(
                        revision_metadata.get("planner_version") or 0
                    )
                except (TypeError, ValueError):
                    planner_version = 0
                if planner_version >= 2:
                    continue

                referenced = connection.execute(
                    """
                    SELECT 1
                    FROM episodes e
                    WHERE e.revision_id = ?
                      AND (
                        EXISTS (
                            SELECT 1 FROM draft_episodes de
                            WHERE de.episode_id = e.id
                        )
                        OR EXISTS (
                            SELECT 1 FROM production_records pr
                            WHERE pr.episode_id = e.id
                        )
                      )
                    LIMIT 1
                    """,
                    (revision_id,),
                ).fetchone()
                if referenced is not None:
                    # Do not even mark a referenced revision: its episode plan
                    # and metadata are an immutable historical snapshot.
                    continue

                prepared = prepare_manuscript(
                    str(revision["body"]),
                    title=str(revision["title"]),
                    source_name=str(revision["source_name"] or "pasted-story.txt"),
                )

                connection.execute(
                    "DELETE FROM episodes WHERE revision_id = ?", (revision_id,)
                )
                connection.execute(
                    "DELETE FROM chapters WHERE revision_id = ?", (revision_id,)
                )

                for chapter in prepared.chapters:
                    connection.execute(
                        """
                        INSERT INTO chapters(
                            id, revision_id, ordinal, title, body, content_hash,
                            metadata_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _new_id(),
                            revision_id,
                            chapter.ordinal,
                            chapter.heading,
                            chapter.text,
                            manuscript_sha256(chapter.text),
                            _json_dump(
                                {
                                    "word_count": chapter.word_count,
                                    "explicit_source_boundary": bool(
                                        getattr(chapter, "is_explicit_boundary", False)
                                    ),
                                }
                            ),
                            migrated_at,
                        ),
                    )

                for episode in prepared.episodes:
                    episode_metadata = {
                        "text": episode.text,
                        "word_count": episode.word_count,
                        "boundary_reason": episode.boundary_reason,
                        "duration_warning": episode.duration_warning,
                        "source_heading": str(
                            getattr(episode, "source_heading", "") or ""
                        ),
                        "source_part_index": int(
                            getattr(episode, "source_part_index", 1) or 1
                        ),
                        "source_part_count": int(
                            getattr(episode, "source_part_count", 1) or 1
                        ),
                        "explicit_source_boundary": bool(
                            getattr(episode, "explicit_source_boundary", False)
                        ),
                    }
                    connection.execute(
                        """
                        INSERT INTO episodes(
                            id, revision_id, ordinal, title, source_map_json,
                            recap_text, estimated_duration_seconds, status,
                            metadata_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, '', ?, 'planned', ?, ?, ?)
                        """,
                        (
                            _new_id(),
                            revision_id,
                            episode.ordinal,
                            episode.title,
                            _json_dump(
                                [
                                    {
                                        "chapter_ordinals": list(
                                            episode.source_chapter_ordinals
                                        ),
                                        "start_word": episode.source_start_word,
                                        "end_word": episode.source_end_word,
                                    }
                                ]
                            ),
                            episode.estimated_seconds,
                            _json_dump(episode_metadata),
                            migrated_at,
                            migrated_at,
                        ),
                    )

                revision_metadata.update(
                    {
                        "planner_version": 2,
                        "source_word_count": prepared.word_count,
                        "estimated_duration_seconds": prepared.estimated_seconds,
                    }
                )
                connection.execute(
                    "UPDATE content_revisions SET metadata_json = ? WHERE id = ?",
                    (_json_dump(revision_metadata), revision_id),
                )

            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (6, migrated_at),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_multilingual_timing_schema(connection: sqlite3.Connection) -> None:
        """Recalculate stored estimates with Unicode-aware narration units.

        Planner v2 counted only ASCII words, which reduced Japanese, Chinese
        and Korean chapters to the handful of digits they happened to contain.
        This migration deliberately preserves episode IDs, text, ordering and
        references; only derived counts, source cursors and estimates change.
        """

        connection.execute("BEGIN IMMEDIATE")
        try:
            migrated_at = utc_now()
            revisions = connection.execute(
                """
                SELECT r.id, r.metadata_json
                FROM content_revisions r
                JOIN novels n ON n.current_revision_id = r.id
                ORDER BY r.created_at, r.id
                """
            ).fetchall()
            for revision in revisions:
                revision_id = str(revision["id"])
                revision_metadata = _json_load(revision["metadata_json"], {})
                if not isinstance(revision_metadata, dict):
                    revision_metadata = {}
                try:
                    estimator_version = int(
                        revision_metadata.get("estimator_version") or 0
                    )
                except (TypeError, ValueError):
                    estimator_version = 0
                if estimator_version >= 2:
                    continue

                episode_rows = connection.execute(
                    "SELECT * FROM episodes WHERE revision_id = ? ORDER BY ordinal, id",
                    (revision_id,),
                ).fetchall()
                inferred_wpm: list[float] = []
                for row in episode_rows:
                    metadata = _json_load(row["metadata_json"], {})
                    if not isinstance(metadata, dict):
                        continue
                    try:
                        old_units = int(metadata.get("word_count") or 0)
                        old_seconds = float(row["estimated_duration_seconds"] or 0)
                    except (TypeError, ValueError):
                        continue
                    if old_units > 0 and old_seconds > 0:
                        candidate = old_units * 60.0 / old_seconds
                        if 60.0 <= candidate <= 500.0:
                            inferred_wpm.append(candidate)
                inferred_wpm.sort()
                wpm = (
                    inferred_wpm[len(inferred_wpm) // 2]
                    if inferred_wpm
                    else 210.0
                )

                source_cursor = 0
                total_units = 0
                total_seconds = 0.0
                for row in episode_rows:
                    metadata = _json_load(row["metadata_json"], {})
                    if not isinstance(metadata, dict):
                        metadata = {}
                    text = str(metadata.get("text") or "")
                    units = count_words(text)
                    seconds = units * 60.0 / wpm
                    metadata.update(
                        {
                            "word_count": units,
                            "estimator_version": 2,
                            "duration_warning": seconds > 600.0,
                        }
                    )
                    source_map = _json_load(row["source_map_json"], [])
                    if not isinstance(source_map, list):
                        source_map = []
                    if source_map and isinstance(source_map[0], dict):
                        source_map[0] = {
                            **source_map[0],
                            "start_word": source_cursor,
                            "end_word": source_cursor + units,
                        }
                    connection.execute(
                        """
                        UPDATE episodes
                        SET source_map_json = ?, estimated_duration_seconds = ?,
                            metadata_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            _json_dump(source_map),
                            seconds,
                            _json_dump(metadata),
                            migrated_at,
                            row["id"],
                        ),
                    )
                    source_cursor += units
                    total_units += units
                    total_seconds += seconds

                chapter_rows = connection.execute(
                    "SELECT id, body, metadata_json FROM chapters WHERE revision_id = ?",
                    (revision_id,),
                ).fetchall()
                for row in chapter_rows:
                    metadata = _json_load(row["metadata_json"], {})
                    if not isinstance(metadata, dict):
                        metadata = {}
                    metadata.update(
                        {
                            "word_count": count_words(str(row["body"] or "")),
                            "estimator_version": 2,
                        }
                    )
                    connection.execute(
                        "UPDATE chapters SET metadata_json = ? WHERE id = ?",
                        (_json_dump(metadata), row["id"]),
                    )

                revision_metadata.update(
                    {
                        "planner_version": max(
                            3, int(revision_metadata.get("planner_version") or 0)
                        ),
                        "estimator_version": 2,
                        "source_word_count": total_units,
                        "estimated_duration_seconds": total_seconds,
                        "estimate_wpm": wpm,
                    }
                )
                connection.execute(
                    "UPDATE content_revisions SET metadata_json = ? WHERE id = ?",
                    (_json_dump(revision_metadata), revision_id),
                )

            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (7, migrated_at),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_authored_episode_choices(connection: sqlite3.Connection) -> None:
        """Restore one independently selectable choice per authored chapter.

        Early planners could merge two short, explicitly headed chapters into
        one episode.  That made the production desk display values such as
        ``Chapter 1 / Chapter 2`` and prevented staff from choosing the two
        chapters independently.

        Draft-only references are safe to repair: their selections are mapped
        from the old source-chapter coverage to the new episode IDs.  A
        revision already referenced by production history remains immutable so
        completed records never silently point at different story content.
        """

        connection.execute("BEGIN IMMEDIATE")
        try:
            migrated_at = utc_now()
            revisions = connection.execute(
                """
                SELECT r.id, r.body, r.source_name, r.metadata_json, n.title
                FROM content_revisions r
                JOIN novels n ON n.current_revision_id = r.id
                ORDER BY r.created_at, r.id
                """
            ).fetchall()

            for revision in revisions:
                revision_id = str(revision["id"])
                metadata = _json_load(revision["metadata_json"], {})
                if not isinstance(metadata, dict):
                    metadata = {}
                try:
                    previous_planner_version = int(
                        metadata.get("planner_version") or 0
                    )
                except (TypeError, ValueError):
                    previous_planner_version = 0
                try:
                    wpm = float(metadata.get("estimate_wpm") or 210.0)
                except (TypeError, ValueError):
                    wpm = 210.0
                if not 60.0 <= wpm <= 500.0:
                    wpm = 210.0

                prepared = prepare_manuscript(
                    str(revision["body"]),
                    title=str(revision["title"]),
                    source_name=str(
                        revision["source_name"] or "pasted-story.txt"
                    ),
                    wpm=wpm,
                )
                # Automatic prose blocks may still be merged by duration.  A
                # repair is needed only when the manuscript actually contains
                # authored headings and the stored coverage differs.
                if not any(
                    bool(getattr(chapter, "is_explicit_boundary", False))
                    for chapter in prepared.chapters
                ):
                    metadata["planner_version"] = max(4, previous_planner_version)
                    connection.execute(
                        "UPDATE content_revisions SET metadata_json = ? WHERE id = ?",
                        (_json_dump(metadata), revision_id),
                    )
                    continue

                old_episodes = connection.execute(
                    """
                    SELECT id, ordinal, title, source_map_json, recap_text,
                           estimated_duration_seconds, status, metadata_json
                    FROM episodes
                    WHERE revision_id = ?
                    ORDER BY ordinal, id
                    """,
                    (revision_id,),
                ).fetchall()

                def source_ordinals(raw_source_map: Any) -> tuple[int, ...]:
                    source_map = _json_load(raw_source_map, [])
                    if not isinstance(source_map, list):
                        return ()
                    values: list[int] = []
                    for entry in source_map:
                        if not isinstance(entry, dict):
                            continue
                        raw_ordinals = entry.get("chapter_ordinals") or []
                        if not isinstance(raw_ordinals, (list, tuple)):
                            continue
                        for raw in raw_ordinals:
                            try:
                                ordinal = int(raw)
                            except (TypeError, ValueError):
                                continue
                            if ordinal > 0 and ordinal not in values:
                                values.append(ordinal)
                    return tuple(values)

                stored_signature = [
                    source_ordinals(row["source_map_json"])
                    for row in old_episodes
                ]
                explicit_chapter_ordinals = {
                    int(chapter.ordinal)
                    for chapter in prepared.chapters
                    if bool(getattr(chapter, "is_explicit_boundary", False))
                }
                # Repair only the historical defect where one stored episode
                # crossed two or more authored headings. Do not replan long
                # single-chapter segments merely because planner heuristics
                # changed in a later version.
                needs_rebuild = any(
                    len(set(source).intersection(explicit_chapter_ordinals)) > 1
                    for source in stored_signature
                )
                if not needs_rebuild:
                    metadata.update(
                        {
                            "planner_version": 4,
                            "source_word_count": prepared.word_count,
                            "estimated_duration_seconds": prepared.estimated_seconds,
                            "estimate_wpm": wpm,
                        }
                    )
                    connection.execute(
                        "UPDATE content_revisions SET metadata_json = ? WHERE id = ?",
                        (_json_dump(metadata), revision_id),
                    )
                    continue

                has_production_history = connection.execute(
                    """
                    SELECT 1
                    FROM production_records pr
                    JOIN episodes e ON e.id = pr.episode_id
                    WHERE e.revision_id = ?
                    LIMIT 1
                    """,
                    (revision_id,),
                ).fetchone()
                if has_production_history is not None:
                    # Production records are a historical contract.  A future
                    # content revision may use planner v4, but this revision is
                    # deliberately left untouched.
                    metadata["episode_choice_repair"] = {
                        "status": "blocked_by_production_history",
                        "detected_at": migrated_at,
                    }
                    connection.execute(
                        "UPDATE content_revisions SET metadata_json = ? WHERE id = ?",
                        (_json_dump(metadata), revision_id),
                    )
                    continue

                old_episode_sources = {
                    str(row["id"]): source_ordinals(row["source_map_json"])
                    for row in old_episodes
                }
                old_episode_ordinals = {
                    str(row["id"]): int(row["ordinal"])
                    for row in old_episodes
                }

                def stored_episode_metadata(row: sqlite3.Row) -> dict[str, Any]:
                    value = _json_load(row["metadata_json"], {})
                    return value if isinstance(value, dict) else {}

                draft_rows = connection.execute(
                    """
                    SELECT de.draft_id, de.episode_id, de.ordinal
                    FROM draft_episodes de
                    JOIN episodes e ON e.id = de.episode_id
                    WHERE e.revision_id = ?
                    ORDER BY de.draft_id, de.ordinal
                    """,
                    (revision_id,),
                ).fetchall()
                draft_selections: dict[str, list[str]] = {}
                for row in draft_rows:
                    draft_selections.setdefault(str(row["draft_id"]), []).append(
                        str(row["episode_id"])
                    )

                if draft_rows:
                    connection.executemany(
                        "DELETE FROM draft_episodes WHERE draft_id = ? AND episode_id = ?",
                        [
                            (str(row["draft_id"]), str(row["episode_id"]))
                            for row in draft_rows
                        ],
                    )
                # Keep a compact, text-free recovery snapshot for any manual
                # recap/status/metadata that cannot map one-to-one after a
                # merged episode is separated. The episode narration itself is
                # reconstructible from the immutable revision body.
                metadata["episode_choice_repair_backup"] = {
                    "repaired_at": migrated_at,
                    "reason": "merged_authored_chapters",
                    "episodes": [
                        {
                            "id": str(row["id"]),
                            "ordinal": int(row["ordinal"]),
                            "title": str(row["title"] or ""),
                            "source_map": _json_load(row["source_map_json"], []),
                            "recap_text": str(row["recap_text"] or ""),
                            "estimated_duration_seconds": row[
                                "estimated_duration_seconds"
                            ],
                            "status": str(row["status"] or "planned"),
                            "metadata": {
                                key: value
                                for key, value in stored_episode_metadata(row).items()
                                if key != "text"
                            },
                        }
                        for row in old_episodes
                    ],
                }
                connection.execute(
                    "DELETE FROM episodes WHERE revision_id = ?", (revision_id,)
                )
                connection.execute(
                    "DELETE FROM chapters WHERE revision_id = ?", (revision_id,)
                )

                for chapter in prepared.chapters:
                    connection.execute(
                        """
                        INSERT INTO chapters(
                            id, revision_id, ordinal, title, body, content_hash,
                            metadata_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _new_id(),
                            revision_id,
                            chapter.ordinal,
                            chapter.heading,
                            chapter.text,
                            manuscript_sha256(chapter.text),
                            _json_dump(
                                {
                                    "word_count": chapter.word_count,
                                    "estimator_version": 2,
                                    "is_explicit_boundary": bool(
                                        getattr(
                                            chapter, "is_explicit_boundary", False
                                        )
                                    ),
                                }
                            ),
                            migrated_at,
                        ),
                    )

                new_episodes: list[tuple[str, tuple[int, ...], int]] = []
                for episode in prepared.episodes:
                    episode_id = _new_id()
                    source_chapters = tuple(
                        int(item) for item in episode.source_chapter_ordinals
                    )
                    new_episodes.append(
                        (episode_id, source_chapters, int(episode.ordinal))
                    )
                    exact_old = next(
                        (
                            row
                            for row in old_episodes
                            if source_ordinals(row["source_map_json"])
                            == source_chapters
                        ),
                        None,
                    )
                    old_metadata = (
                        stored_episode_metadata(exact_old)
                        if exact_old is not None
                        else {}
                    )
                    planner_owned_keys = {
                        "text",
                        "word_count",
                        "estimator_version",
                        "boundary_reason",
                        "duration_warning",
                        "source_heading",
                        "original_title",
                        "source_part_index",
                        "source_part_count",
                        "explicit_source_boundary",
                    }
                    preserved_metadata = {
                        key: value
                        for key, value in old_metadata.items()
                        if key not in planner_owned_keys
                    }
                    connection.execute(
                        """
                        INSERT INTO episodes(
                            id, revision_id, ordinal, title, source_map_json,
                            recap_text, estimated_duration_seconds, status,
                            metadata_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            episode_id,
                            revision_id,
                            episode.ordinal,
                            episode.title,
                            _json_dump(
                                [
                                    {
                                        "chapter_ordinals": list(source_chapters),
                                        "start_word": episode.source_start_word,
                                        "end_word": episode.source_end_word,
                                    }
                                ]
                            ),
                            str(exact_old["recap_text"] or "")
                            if exact_old is not None
                            else "",
                            episode.estimated_seconds,
                            str(exact_old["status"] or "planned")
                            if exact_old is not None
                            else "planned",
                            _json_dump(
                                {
                                    **preserved_metadata,
                                    "text": episode.text,
                                    "word_count": episode.word_count,
                                    "estimator_version": 2,
                                    "boundary_reason": episode.boundary_reason,
                                    "duration_warning": episode.duration_warning,
                                    "source_heading": episode.source_heading,
                                    "original_title": episode.source_heading
                                    or episode.title,
                                    "source_part_index": episode.source_part_index,
                                    "source_part_count": episode.source_part_count,
                                    "explicit_source_boundary": episode.explicit_source_boundary,
                                }
                            ),
                            migrated_at,
                            migrated_at,
                        ),
                    )

                for draft_id, selected_old_ids in draft_selections.items():
                    replacement_ids: set[str] = set()
                    ambiguous = False
                    for old_episode_id in selected_old_ids:
                        selected_sources = set(
                            old_episode_sources.get(old_episode_id, ())
                        )
                        matches = [
                            episode_id
                            for episode_id, chapters, _ordinal in new_episodes
                            if selected_sources.intersection(chapters)
                        ]
                        if not matches:
                            # Ordinal fallback is only a best-effort recovery
                            # for a missing/malformed source map. Preserve all
                            # choices below rather than risk silently dropping
                            # story content from the draft.
                            ambiguous = True
                            old_ordinal = old_episode_ordinals.get(old_episode_id)
                            matches = [
                                episode_id
                                for episode_id, _chapters, ordinal in new_episodes
                                if ordinal == old_ordinal
                            ]
                        replacement_ids.update(matches)
                    replacements = [
                        episode_id
                        for episode_id, _chapters, _ordinal in new_episodes
                        if episode_id in replacement_ids
                    ]
                    # A single legacy episode often represented the complete
                    # manuscript but carried an empty source map. Preserve that
                    # intent by selecting every newly separated chapter.
                    if ambiguous or (not replacements and len(old_episodes) == 1):
                        replacements = [item[0] for item in new_episodes]
                    connection.executemany(
                        """
                        INSERT INTO draft_episodes(draft_id, episode_id, ordinal)
                        VALUES (?, ?, ?)
                        """,
                        [
                            (draft_id, episode_id, index)
                            for index, episode_id in enumerate(
                                replacements, start=1
                            )
                        ],
                    )

                metadata.update(
                    {
                        "planner_version": 4,
                        "source_word_count": prepared.word_count,
                        "estimated_duration_seconds": prepared.estimated_seconds,
                        "estimate_wpm": wpm,
                    }
                )
                connection.execute(
                    "UPDATE content_revisions SET metadata_json = ? WHERE id = ?",
                    (_json_dump(metadata), revision_id),
                )

            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (12, migrated_at),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_platform_branding_schema(connection: sqlite3.Connection) -> None:
        """Add optional platform branding without invalidating legacy rows."""

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(platforms)").fetchall()
        }
        definitions = {
            "logo_path": "TEXT NOT NULL DEFAULT ''",
            "brand_color": "TEXT NOT NULL DEFAULT ''",
        }
        connection.execute("BEGIN IMMEDIATE")
        try:
            for name, definition in definitions.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE platforms ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (8, utc_now()),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_job_archive_schema(connection: sqlite3.Connection) -> None:
        """Add a durable, reversible archive to production records.

        Archiving is deliberately metadata-only: render logs, artifacts and
        output files remain attached to the original production record.
        ``archive_snapshot_json`` stores the in-memory queue fields that are
        not otherwise represented by the catalog, so an archived task can be
        restored after the application restarts.
        """

        definitions = {
            "archived": "INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))",
            "archived_at": "TEXT",
            "archived_by_user_id": (
                "TEXT REFERENCES software_users(id) ON DELETE SET NULL"
            ),
            "archive_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(production_records)"
            ).fetchall()
        }
        connection.execute("BEGIN IMMEDIATE")
        try:
            for name, definition in definitions.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE production_records ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_records_archived
                ON production_records(site_id, archived, archived_at DESC)
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (9, utc_now()),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_device_management_schema(connection: sqlite3.Connection) -> None:
        """Add stable workstation identities and immutable desired config."""

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS hub_devices (
                    id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                    installation_id_hash TEXT NOT NULL,
                    name TEXT NOT NULL,
                    hostname TEXT NOT NULL DEFAULT '',
                    app_version TEXT NOT NULL DEFAULT '',
                    os_name TEXT NOT NULL DEFAULT '',
                    architecture TEXT NOT NULL DEFAULT '',
                    capabilities_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    last_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    row_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(site_id, installation_id_hash)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_hub_devices_site_seen
                ON hub_devices(site_id, active, last_seen_at DESC)
                """
            )
            token_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(hub_access_tokens)"
                ).fetchall()
            }
            if "device_id" not in token_columns:
                connection.execute(
                    """
                    ALTER TABLE hub_access_tokens
                    ADD COLUMN device_id TEXT REFERENCES hub_devices(id) ON DELETE SET NULL
                    """
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_hub_tokens_device
                ON hub_access_tokens(site_id, device_id, created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS device_config_revisions (
                    id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
                    config_schema_version INTEGER NOT NULL DEFAULT 1,
                    config_json TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    target_mode TEXT NOT NULL CHECK (
                        target_mode IN ('single', 'multiple', 'all')
                    ),
                    note TEXT NOT NULL DEFAULT '',
                    created_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(site_id, revision_number)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_device_config_revisions_site
                ON device_config_revisions(site_id, revision_number DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS device_config_targets (
                    revision_id TEXT NOT NULL REFERENCES device_config_revisions(id)
                        ON DELETE CASCADE,
                    device_id TEXT NOT NULL REFERENCES hub_devices(id) ON DELETE CASCADE,
                    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                    assigned_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    ack_status TEXT NOT NULL DEFAULT '' CHECK (
                        ack_status IN ('', 'applied', 'failed')
                    ),
                    ack_message TEXT NOT NULL DEFAULT '',
                    reported_config_hash TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(revision_id, device_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_device_config_targets_device
                ON device_config_targets(site_id, device_id, assigned_at DESC)
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (10, utc_now()),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_production_ledger_schema(connection: sqlite3.Connection) -> None:
        """Upgrade the flat render history into batches with durable attempts.

        Version 11 also removes the historical ten-video database constraint.
        Both parent tables must be rebuilt because SQLite cannot alter CHECK
        constraints in place.  Stable primary keys keep all existing child
        rows attached, and the foreign-key check is run before committing.
        """

        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS production_batches (
                    id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
                    external_run_id TEXT NOT NULL DEFAULT '',
                    draft_id TEXT REFERENCES production_drafts(id) ON DELETE SET NULL,
                    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE RESTRICT,
                    binding_id TEXT NOT NULL REFERENCES novel_platform_bindings(id) ON DELETE RESTRICT,
                    publishing_account_id TEXT REFERENCES publishing_accounts(id) ON DELETE SET NULL,
                    created_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
                    device_id TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
                    archived_at TEXT,
                    trashed_at TEXT,
                    trashed_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(site_id, external_run_id)
                )
                """
            )

            draft_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'production_drafts'"
            ).fetchone()
            draft_sql = str(draft_sql_row[0] or "") if draft_sql_row else ""
            if "BETWEEN 1 AND 10" in draft_sql.upper():
                connection.execute("DROP TABLE IF EXISTS production_drafts_v11")
                connection.execute(
                    """
                    CREATE TABLE production_drafts_v11 (
                        id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
                        novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE RESTRICT,
                        binding_id TEXT NOT NULL REFERENCES novel_platform_bindings(id) ON DELETE RESTRICT,
                        promo_code_id TEXT NOT NULL REFERENCES promo_codes(id) ON DELETE RESTRICT,
                        publishing_account_id TEXT REFERENCES publishing_accounts(id) ON DELETE SET NULL,
                        creative_line_count INTEGER NOT NULL CHECK (creative_line_count > 0),
                        novel_title_snapshot TEXT NOT NULL,
                        platform_name_snapshot TEXT NOT NULL,
                        promo_code_snapshot TEXT NOT NULL,
                        voice_profile TEXT NOT NULL DEFAULT '',
                        subtitle_style_id TEXT NOT NULL DEFAULT '',
                        outro_style_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL CHECK (status IN ('draft', 'ready', 'archived')),
                        created_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        row_version INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO production_drafts_v11(
                        id, site_id, novel_id, binding_id, promo_code_id,
                        publishing_account_id, creative_line_count,
                        novel_title_snapshot, platform_name_snapshot,
                        promo_code_snapshot, voice_profile, subtitle_style_id,
                        outro_style_id, status, created_by_user_id, metadata_json,
                        row_version, created_at, updated_at
                    )
                    SELECT id, site_id, novel_id, binding_id, promo_code_id,
                        publishing_account_id, creative_line_count,
                        novel_title_snapshot, platform_name_snapshot,
                        promo_code_snapshot, voice_profile, subtitle_style_id,
                        outro_style_id, status, created_by_user_id, metadata_json,
                        row_version, created_at, updated_at
                    FROM production_drafts
                    """
                )
                connection.execute("DROP TABLE production_drafts")
                connection.execute(
                    "ALTER TABLE production_drafts_v11 RENAME TO production_drafts"
                )

            record_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(production_records)"
                ).fetchall()
            }
            record_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'production_records'"
            ).fetchone()
            record_sql = str(record_sql_row[0] or "") if record_sql_row else ""
            required_record_columns = {
                "batch_id",
                "logical_task_key",
                "current_attempt",
                "cancel_requested_at",
                "cancelled_at",
                "cancel_requested_by_user_id",
                "cancellation_reason",
                "trashed_at",
                "trashed_by_user_id",
            }
            if (
                "BETWEEN 1 AND 10" in record_sql.upper()
                or not required_record_columns.issubset(record_columns)
            ):
                connection.execute("DROP TABLE IF EXISTS production_records_v11")
                connection.execute(
                    """
                    CREATE TABLE production_records_v11 (
                        id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
                        batch_id TEXT REFERENCES production_batches(id) ON DELETE SET NULL,
                        draft_id TEXT REFERENCES production_drafts(id) ON DELETE SET NULL,
                        job_id TEXT,
                        novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE RESTRICT,
                        binding_id TEXT NOT NULL REFERENCES novel_platform_bindings(id) ON DELETE RESTRICT,
                        episode_id TEXT REFERENCES episodes(id) ON DELETE SET NULL,
                        publishing_account_id TEXT REFERENCES publishing_accounts(id) ON DELETE SET NULL,
                        created_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
                        device_id TEXT NOT NULL DEFAULT '',
                        variant_index INTEGER NOT NULL CHECK (variant_index > 0),
                        logical_task_key TEXT NOT NULL DEFAULT '',
                        current_attempt INTEGER NOT NULL DEFAULT 1 CHECK (current_attempt > 0),
                        novel_title_snapshot TEXT NOT NULL,
                        platform_name_snapshot TEXT NOT NULL,
                        promo_code_snapshot TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN (
                            'queued', 'preflight', 'sample_ready', 'awaiting_approval', 'running',
                            'completed', 'failed', 'skipped', 'interrupted', 'cancelled'
                        )),
                        progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
                        output_path TEXT NOT NULL DEFAULT '',
                        error_message TEXT NOT NULL DEFAULT '',
                        started_at TEXT,
                        completed_at TEXT,
                        lease_owner_device TEXT NOT NULL DEFAULT '',
                        lease_expires_at TEXT,
                        heartbeat_at TEXT,
                        cancel_requested_at TEXT,
                        cancelled_at TEXT,
                        cancel_requested_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
                        cancellation_reason TEXT NOT NULL DEFAULT '',
                        archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
                        archived_at TEXT,
                        archived_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
                        archive_snapshot_json TEXT NOT NULL DEFAULT '{}',
                        trashed_at TEXT,
                        trashed_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        row_version INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(site_id, job_id)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO production_records_v11(
                        id, site_id, batch_id, draft_id, job_id, novel_id,
                        binding_id, episode_id, publishing_account_id,
                        created_by_user_id, device_id, variant_index,
                        logical_task_key, current_attempt, novel_title_snapshot,
                        platform_name_snapshot, promo_code_snapshot, status,
                        progress, output_path, error_message, started_at,
                        completed_at, lease_owner_device, lease_expires_at,
                        heartbeat_at, cancel_requested_at, cancelled_at,
                        cancel_requested_by_user_id, cancellation_reason,
                        archived, archived_at, archived_by_user_id,
                        archive_snapshot_json, trashed_at, trashed_by_user_id,
                        metadata_json, row_version, created_at, updated_at
                    )
                    SELECT id, site_id, NULL, draft_id, job_id, novel_id,
                        binding_id, episode_id, publishing_account_id,
                        created_by_user_id, device_id, variant_index,
                        '', 1, novel_title_snapshot, platform_name_snapshot,
                        promo_code_snapshot, status, progress, output_path,
                        error_message, started_at, completed_at,
                        lease_owner_device, lease_expires_at, heartbeat_at,
                        NULL, CASE WHEN status = 'cancelled' THEN completed_at ELSE NULL END,
                        NULL, '', archived, archived_at, archived_by_user_id,
                        archive_snapshot_json, NULL, NULL, metadata_json,
                        row_version, created_at, updated_at
                    FROM production_records
                    """
                )
                connection.execute("DROP TABLE production_records")
                connection.execute(
                    "ALTER TABLE production_records_v11 RENAME TO production_records"
                )

            ledger_statements = (
                "CREATE INDEX IF NOT EXISTS idx_batches_site_created ON production_batches(site_id, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_batches_novel_created ON production_batches(novel_id, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_records_site_created ON production_records(site_id, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_records_novel ON production_records(novel_id, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_records_publishing_account ON production_records(publishing_account_id, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_records_lease_expiry ON production_records(site_id, lease_expires_at)",
                "CREATE INDEX IF NOT EXISTS idx_records_archived ON production_records(site_id, archived, archived_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_records_batch_created ON production_records(batch_id, created_at ASC)",
                "CREATE INDEX IF NOT EXISTS idx_records_trash ON production_records(site_id, trashed_at, created_at DESC)",
                """
                CREATE TABLE IF NOT EXISTS production_record_attempts (
                    id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
                    record_id TEXT NOT NULL REFERENCES production_records(id) ON DELETE CASCADE,
                    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
                    job_id TEXT NOT NULL DEFAULT '',
                    device_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
                    output_path TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    completed_at TEXT,
                    cancel_requested_at TEXT,
                    cancelled_at TEXT,
                    cancellation_reason TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(record_id, attempt_no)
                )
                """,
                "CREATE INDEX IF NOT EXISTS idx_record_attempts_record ON production_record_attempts(record_id, attempt_no DESC)",
            )
            for statement in ledger_statements:
                connection.execute(statement)

            batch_ids: dict[tuple[str, str], str] = {}
            records = connection.execute(
                "SELECT * FROM production_records ORDER BY created_at, id"
            ).fetchall()
            for row in records:
                metadata = _json_load(row["metadata_json"], {})
                if not isinstance(metadata, dict):
                    metadata = {}
                if bool(metadata.get("lease_gate")):
                    continue
                external_run_id = str(metadata.get("production_run_id") or "").strip()
                if not external_run_id:
                    external_run_id = "legacy:" + str(row["draft_id"] or row["id"])
                key = (str(row["site_id"]), external_run_id)
                batch_id = batch_ids.get(key)
                if not batch_id:
                    existing = connection.execute(
                        "SELECT id FROM production_batches WHERE site_id = ? AND external_run_id = ?",
                        key,
                    ).fetchone()
                    batch_id = str(existing["id"]) if existing else _new_id()
                    if existing is None:
                        connection.execute(
                            """
                            INSERT INTO production_batches(
                                id, site_id, external_run_id, draft_id, novel_id,
                                binding_id, publishing_account_id,
                                created_by_user_id, device_id, label,
                                metadata_json, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                batch_id,
                                row["site_id"],
                                external_run_id,
                                row["draft_id"],
                                row["novel_id"],
                                row["binding_id"],
                                row["publishing_account_id"],
                                row["created_by_user_id"],
                                row["device_id"],
                                str(metadata.get("batch_label") or ""),
                                _json_dump({"migrated": True}),
                                row["created_at"],
                                row["updated_at"],
                            ),
                        )
                    batch_ids[key] = batch_id
                logical_task_key = str(
                    metadata.get("logical_task_key")
                    or f"{row['episode_id'] or 'story'}:{int(row['variant_index'])}"
                )
                connection.execute(
                    """
                    UPDATE production_records
                    SET batch_id = COALESCE(batch_id, ?),
                        logical_task_key = CASE
                            WHEN logical_task_key = '' THEN ? ELSE logical_task_key END
                    WHERE id = ?
                    """,
                    (batch_id, logical_task_key, row["id"]),
                )
                attempt_id = _new_id()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO production_record_attempts(
                        id, site_id, record_id, attempt_no, job_id, device_id,
                        status, progress, output_path, error_message,
                        started_at, completed_at, cancel_requested_at,
                        cancelled_at, cancellation_reason, metadata_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        row["site_id"],
                        row["id"],
                        str(row["job_id"] or ""),
                        str(row["device_id"] or ""),
                        str(row["status"]),
                        float(row["progress"]),
                        str(row["output_path"] or ""),
                        str(row["error_message"] or ""),
                        row["started_at"],
                        row["completed_at"],
                        row["cancel_requested_at"],
                        row["cancelled_at"],
                        str(row["cancellation_reason"] or ""),
                        str(row["metadata_json"] or "{}"),
                        str(row["created_at"]),
                        str(row["updated_at"]),
                    ),
                )

            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise CatalogError(
                    "production ledger migration failed foreign-key validation"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (11, utc_now()),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        existing = getattr(self._transaction_local, "connection", None)
        if existing is not None:
            yield existing
            return
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        actor_user_id: str | None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events(
                id, site_id, actor_user_id, action, entity_type, entity_id,
                before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_id(),
                self.site_id,
                actor_user_id or None,
                action,
                entity_type,
                entity_id,
                _json_dump(dict(before)) if before is not None else None,
                _json_dump(dict(after)) if after is not None else None,
                utc_now(),
            ),
        )

    @staticmethod
    def _require_row(
        connection: sqlite3.Connection,
        query: str,
        parameters: Sequence[Any],
        label: str,
    ) -> sqlite3.Row:
        row = connection.execute(query, parameters).fetchone()
        if row is None:
            raise CatalogNotFoundError(f"{label} not found")
        return row

    @staticmethod
    def _expected_version(value: Mapping[str, Any]) -> int | None:
        if "expected_version" not in value or value.get("expected_version") is None:
            return None
        return _positive_int(
            value.get("expected_version"),
            "expected_version",
            minimum=1,
            maximum=2_147_483_647,
        )

    @staticmethod
    def _check_version(row: sqlite3.Row, expected: int | None) -> None:
        if expected is not None and int(row["row_version"]) != expected:
            raise CatalogConflictError(
                f"row version changed (expected {expected}, current {row['row_version']})"
            )

    def bootstrap_summary(self) -> dict[str, Any]:
        with self._read_connection() as connection:
            count_queries = {
                "users": "SELECT COUNT(*) FROM software_users WHERE site_id = ?",
                "novels": "SELECT COUNT(*) FROM novels WHERE site_id = ?",
                "platforms": "SELECT COUNT(*) FROM platforms WHERE site_id = ?",
                "bindings": "SELECT COUNT(*) FROM novel_platform_bindings WHERE site_id = ?",
                "promo_codes": """
                    SELECT COUNT(*) FROM promo_codes c
                    JOIN novel_platform_bindings b ON b.id = c.binding_id
                    WHERE b.site_id = ?
                """,
                "publishing_accounts": "SELECT COUNT(*) FROM publishing_accounts WHERE site_id = ?",
                "drafts": "SELECT COUNT(*) FROM production_drafts WHERE site_id = ?",
                "records": "SELECT COUNT(*) FROM production_records WHERE site_id = ?",
                "failed_records": "SELECT COUNT(*) FROM production_records WHERE status = 'failed' AND site_id = ?",
                "artifacts": """
                    SELECT COUNT(*) FROM artifacts a
                    JOIN production_records r ON r.id = a.record_id
                    WHERE r.site_id = ?
                """,
                "media_usage_events": "SELECT COUNT(*) FROM media_usage_events WHERE site_id = ?",
                "hub_devices": "SELECT COUNT(*) FROM hub_devices WHERE site_id = ?",
                "device_config_revisions": "SELECT COUNT(*) FROM device_config_revisions WHERE site_id = ?",
                "audit_events": "SELECT COUNT(*) FROM audit_events WHERE site_id = ?",
            }
            counts = {
                label: int(connection.execute(query, (self.site_id,)).fetchone()[0])
                for label, query in count_queries.items()
            }
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            )
            return {
                "schema_version": version,
                "site": {"id": self.site_id, "name": self.site_name},
                "journal_mode": str(
                    connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).casefold(),
                "counts": counts,
            }

    def _can_manage_all_production_presets(
        self, actor_user_id: str | None
    ) -> bool:
        if not actor_user_id:
            # Internal maintenance and migration callers are trusted. Every
            # Web, Hub and desktop request supplies the authenticated actor.
            return True
        try:
            permissions = self.get_effective_permissions(str(actor_user_id))
        except CatalogNotFoundError:
            return False
        effective = dict(permissions.get("effective") or {})
        return bool(effective.get("hub.manage"))

    def list_production_presets(
        self, *, actor_user_id: str | None = None
    ) -> dict[str, Any]:
        """Return personal recipes visible to the authenticated actor.

        Administrators see every member's recipes; employees see only their
        own. Retired bundled/team recipes are intentionally omitted.
        """

        can_manage_all = self._can_manage_all_production_presets(actor_user_id)
        items = self._production_presets.list(
            viewer_user_id=str(actor_user_id or ""),
            can_manage_all=can_manage_all,
        )
        owner_ids = {
            str(item.get("owner_user_id") or "")
            for item in items
            if str(item.get("owner_user_id") or "")
        }
        owner_names: dict[str, str] = {}
        for owner_id in owner_ids:
            try:
                user = self._web_user_by_id(owner_id)
            except (CatalogValidationError, ValueError):
                user = None
            owner_names[owner_id] = str(
                (user or {}).get("display_name")
                or (user or {}).get("username")
                or "已删除账号"
            )
        for item in items:
            owner_id = str(item.get("owner_user_id") or "")
            item["owner_display_name"] = owner_names.get(owner_id, "")
        return {"items": items, "total": len(items)}

    def save_production_preset(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or replace one user-owned recipe and record the actor."""

        try:
            saved = self._production_presets.save(
                value,
                updated_by=str(actor_user_id or ""),
                can_manage_all=self._can_manage_all_production_presets(
                    actor_user_id
                ),
            )
        except PermissionError as error:
            raise CatalogPermissionError(str(error)) from None
        with self._write_connection() as connection:
            self._audit(
                connection,
                action="save",
                entity_type="production_preset",
                entity_id=str(saved["id"]),
                actor_user_id=actor_user_id,
                after=saved,
            )
        return saved

    def delete_production_preset(
        self,
        preset_id: str,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete a recipe the actor is allowed to manage."""

        try:
            result = self._production_presets.delete(
                str(preset_id),
                updated_by=str(actor_user_id or ""),
                can_manage_all=self._can_manage_all_production_presets(
                    actor_user_id
                ),
            )
        except PermissionError as error:
            raise CatalogPermissionError(str(error)) from None
        with self._write_connection() as connection:
            self._audit(
                connection,
                action="delete",
                entity_type="production_preset",
                entity_id=str(result["id"]),
                actor_user_id=actor_user_id,
                after=result,
            )
        return result

    @staticmethod
    def _novel_summary(row: sqlite3.Row) -> dict[str, Any]:
        language_code = str(row["language_code"] or row["language"] or "unknown")
        language_name = str(
            row["language_name"] or LANGUAGE_NAMES_ZH.get(language_code, "未识别")
        )
        detected_code = str(row["detected_language_code"] or "unknown")
        detected_name = str(
            row["detected_language_name"]
            or LANGUAGE_NAMES_ZH.get(detected_code, "未识别")
        )
        return {
            "id": str(row["id"]),
            "title": str(row["title"]),
            "synopsis": str(row["synopsis"]),
            # Keep ``language`` as the compact effective code for existing API
            # consumers, while exposing the richer classification alongside it.
            "language": language_code,
            "language_code": language_code,
            "language_name": language_name,
            "language_confidence": float(row["language_confidence"] or 0.0),
            "language_source": str(row["language_source"] or "auto"),
            "language_detection": {
                "code": language_code,
                "display_name": language_name,
                "confidence": float(row["language_confidence"] or 0.0),
                "source": str(row["language_source"] or "auto"),
                "detected_code": detected_code,
                "detected_display_name": detected_name,
                "detected_confidence": float(
                    row["detected_language_confidence"] or 0.0
                ),
                "detected_at": row["language_detected_at"],
            },
            "cover_path": str(row["cover_path"]),
            "current_revision_id": row["current_revision_id"],
            "archived": bool(row["archived"]),
            "metadata": _json_load(row["metadata_json"], {}),
            "row_version": int(row["row_version"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "revision_count": int(row["revision_count"] or 0)
            if "revision_count" in row.keys()
            else None,
            "binding_count": int(row["binding_count"] or 0)
            if "binding_count" in row.keys()
            else None,
            "episode_count": int(row["episode_count"] or 0)
            if "episode_count" in row.keys()
            else None,
            "successful_video_count": int(row["successful_video_count"] or 0)
            if "successful_video_count" in row.keys()
            else 0,
            "last_production_at": str(row["last_production_at"] or "")
            if "last_production_at" in row.keys()
            else "",
        }

    def list_novels(
        self,
        *,
        query: str = "",
        language_code: str = "",
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = _positive_int(limit, "limit", minimum=1, maximum=500)
        offset = _positive_int(offset, "offset", minimum=0, maximum=10_000_000)
        filters = ["n.site_id = ?"]
        parameters: list[Any] = [self.site_id]
        if not include_archived:
            filters.append("n.archived = 0")
        cleaned_query = str(query or "").strip()
        if cleaned_query:
            filters.append("(n.title LIKE ? ESCAPE '\\' OR n.synopsis LIKE ? ESCAPE '\\')")
            escaped = cleaned_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            parameters.extend((pattern, pattern))
        requested_language = str(language_code or "").strip()
        if requested_language and requested_language.casefold() not in {"all", "auto"}:
            try:
                normalized_language = normalize_language_code(requested_language)
            except ValueError as error:
                raise CatalogValidationError(str(error)) from error
            filters.append("n.language_code = ?")
            parameters.append(normalized_language)
        where = " AND ".join(filters)
        with self._read_connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM novels n WHERE {where}", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT n.*,
                    (SELECT COUNT(*) FROM content_revisions r WHERE r.novel_id = n.id) AS revision_count,
                    (SELECT COUNT(*) FROM novel_platform_bindings b WHERE b.novel_id = n.id) AS binding_count,
                    (SELECT COUNT(*) FROM episodes e
                        JOIN content_revisions r2 ON r2.id = e.revision_id
                        WHERE r2.novel_id = n.id) AS episode_count,
                    (SELECT COUNT(*) FROM production_records pr
                        WHERE pr.novel_id = n.id AND pr.status = 'completed'
                          AND COALESCE(json_extract(pr.metadata_json, '$.lease_gate'), 0) = 0
                          AND COALESCE(
                              json_extract(pr.metadata_json, '$.job_snapshot.settings_snapshot.output_mode'),
                              'video_and_mp3'
                          ) != 'audio_only') AS successful_video_count,
                    (SELECT MAX(pr.created_at)
                        FROM production_records pr
                        WHERE pr.novel_id = n.id
                          AND COALESCE(json_extract(pr.metadata_json, '$.lease_gate'), 0) = 0
                    ) AS last_production_at
                FROM novels n
                WHERE {where}
                ORDER BY n.updated_at DESC, n.title COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            return {
                "items": [self._novel_summary(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    @staticmethod
    def _chapter_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "revision_id": str(row["revision_id"]),
            "ordinal": int(row["ordinal"]),
            "title": str(row["title"]),
            "body": str(row["body"]),
            "content_hash": str(row["content_hash"]),
            "metadata": _json_load(row["metadata_json"], {}),
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _episode_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "revision_id": str(row["revision_id"]),
            "ordinal": int(row["ordinal"]),
            "title": str(row["title"]),
            "source_map": _json_load(row["source_map_json"], []),
            "recap_text": str(row["recap_text"]),
            "estimated_duration_seconds": row["estimated_duration_seconds"],
            "status": str(row["status"]),
            "metadata": _json_load(row["metadata_json"], {}),
            "row_version": int(row["row_version"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _promo_code_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "binding_id": str(row["binding_id"]),
            "slot_no": int(row["slot_no"]),
            "code": str(row["code"]),
            "status": str(row["status"]),
            "label": str(row["label"]),
            "notes": str(row["notes"]),
            "row_version": int(row["row_version"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _binding_dict(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_codes: bool = True,
    ) -> dict[str, Any]:
        result = {
            "id": str(row["id"]),
            "novel_id": str(row["novel_id"]),
            "platform_id": str(row["platform_id"]),
            "platform_name": str(row["platform_name"]),
            "external_book_id": str(row["external_book_id"]),
            "platform_title": str(row["platform_title"]),
            "language": str(row["language"]),
            "commission_rate": row["commission_rate"],
            "archived": bool(row["archived"]),
            "metadata": _json_load(row["metadata_json"], {}),
            "row_version": int(row["row_version"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        if include_codes:
            codes = connection.execute(
                "SELECT * FROM promo_codes WHERE binding_id = ? ORDER BY slot_no",
                (row["id"],),
            ).fetchall()
            result["promo_codes"] = [self._promo_code_dict(item) for item in codes]
            result["promo_code_slots_remaining"] = 5 - len(codes)
        return result

    def get_novel(self, novel_id: str) -> dict[str, Any]:
        novel_id = _required_text(novel_id, "novel_id", maximum=120)
        with self._read_connection() as connection:
            row = self._require_row(
                connection,
                """
                SELECT n.*,
                    (SELECT COUNT(*) FROM content_revisions r WHERE r.novel_id = n.id) AS revision_count,
                    (SELECT COUNT(*) FROM novel_platform_bindings b WHERE b.novel_id = n.id) AS binding_count,
                    (SELECT COUNT(*) FROM episodes e JOIN content_revisions r2 ON r2.id = e.revision_id
                     WHERE r2.novel_id = n.id) AS episode_count,
                    (SELECT COUNT(*) FROM production_records pr
                     WHERE pr.novel_id = n.id AND pr.status = 'completed'
                       AND COALESCE(json_extract(pr.metadata_json, '$.lease_gate'), 0) = 0
                       AND COALESCE(
                           json_extract(pr.metadata_json, '$.job_snapshot.settings_snapshot.output_mode'),
                           'video_and_mp3'
                       ) != 'audio_only') AS successful_video_count,
                    (SELECT MAX(pr.created_at)
                     FROM production_records pr
                     WHERE pr.novel_id = n.id
                       AND COALESCE(json_extract(pr.metadata_json, '$.lease_gate'), 0) = 0
                    ) AS last_production_at
                FROM novels n WHERE n.id = ? AND n.site_id = ?
                """,
                (novel_id, self.site_id),
                "novel",
            )
            result = self._novel_summary(row)
            revisions = connection.execute(
                """
                SELECT id, novel_id, revision_number, content_hash, source_format,
                       source_name, metadata_json, created_at,
                       CASE WHEN id = ? THEN 1 ELSE 0 END AS is_current
                FROM content_revisions
                WHERE novel_id = ?
                ORDER BY revision_number DESC
                """,
                (row["current_revision_id"], novel_id),
            ).fetchall()
            result["revisions"] = [
                {
                    "id": str(item["id"]),
                    "novel_id": str(item["novel_id"]),
                    "revision_number": int(item["revision_number"]),
                    "content_hash": str(item["content_hash"]),
                    "source_format": str(item["source_format"]),
                    "source_name": str(item["source_name"]),
                    "metadata": _json_load(item["metadata_json"], {}),
                    "created_at": str(item["created_at"]),
                    "is_current": bool(item["is_current"]),
                }
                for item in revisions
            ]
            if row["current_revision_id"]:
                revision = self._require_row(
                    connection,
                    "SELECT * FROM content_revisions WHERE id = ?",
                    (row["current_revision_id"],),
                    "current revision",
                )
                result["current_revision"] = {
                    "id": str(revision["id"]),
                    "novel_id": str(revision["novel_id"]),
                    "revision_number": int(revision["revision_number"]),
                    "body": str(revision["body"]),
                    "content_hash": str(revision["content_hash"]),
                    "source_format": str(revision["source_format"]),
                    "source_name": str(revision["source_name"]),
                    "metadata": _json_load(revision["metadata_json"], {}),
                    "created_at": str(revision["created_at"]),
                    "chapters": [
                        self._chapter_dict(item)
                        for item in connection.execute(
                            "SELECT * FROM chapters WHERE revision_id = ? ORDER BY ordinal",
                            (revision["id"],),
                        ).fetchall()
                    ],
                    "episodes": [
                        self._episode_dict(item)
                        for item in connection.execute(
                            "SELECT * FROM episodes WHERE revision_id = ? ORDER BY ordinal",
                            (revision["id"],),
                        ).fetchall()
                    ],
                }
            else:
                result["current_revision"] = None
            binding_rows = connection.execute(
                """
                SELECT b.*, p.name AS platform_name
                FROM novel_platform_bindings b
                JOIN platforms p ON p.id = b.platform_id
                WHERE b.novel_id = ?
                ORDER BY p.name COLLATE NOCASE
                """,
                (novel_id,),
            ).fetchall()
            result["bindings"] = [
                self._binding_dict(connection, item) for item in binding_rows
            ]
            return result

    def import_novel(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise CatalogValidationError("novel payload must be an object")
        title = _required_text(value.get("title"), "title")
        body = str(value.get("body") or "")
        normalized_body = normalize_manuscript_for_hash(body)
        if not normalized_body:
            raise CatalogValidationError("body cannot be empty")
        content_hash = hashlib.sha256(normalized_body.encode("utf-8")).hexdigest()
        detection = detect_language(body)
        requested_manual_language = _requested_language(value.get("language"))
        requested_novel_id = _optional_text(value.get("novel_id"), maximum=120)
        now = utc_now()

        with self._write_connection() as connection:
            duplicate = connection.execute(
                """
                SELECT id, novel_id FROM content_revisions
                WHERE site_id = ? AND content_hash = ?
                """,
                (self.site_id, content_hash),
            ).fetchone()
            if duplicate is not None:
                if requested_novel_id and requested_novel_id != duplicate["novel_id"]:
                    raise DuplicateContentError(
                        str(duplicate["novel_id"]),
                        str(duplicate["id"]),
                        content_hash,
                    )
                duplicate_novel_id = str(duplicate["novel_id"])
                duplicate_revision_id = str(duplicate["id"])
            else:
                duplicate_novel_id = ""
                duplicate_revision_id = ""

            if duplicate is None:
                if requested_novel_id:
                    novel = self._require_row(
                        connection,
                        "SELECT * FROM novels WHERE id = ? AND site_id = ?",
                        (requested_novel_id, self.site_id),
                        "novel",
                    )
                    novel_id = str(novel["id"])
                    existing_metadata = _json_load(novel["metadata_json"], {})
                    incoming_metadata = _metadata(value.get("metadata"))
                    merged_metadata = {**existing_metadata, **incoming_metadata}
                    if (
                        requested_manual_language is None
                        and str(novel["language_source"] or "auto") == "manual"
                    ):
                        language_values = {
                            "language": str(novel["language_code"]),
                            "language_code": str(novel["language_code"]),
                            "language_name": str(novel["language_name"]),
                            "language_confidence": float(
                                novel["language_confidence"] or 1.0
                            ),
                            "language_source": "manual",
                            "detected_language_code": detection.code,
                            "detected_language_name": detection.display_name,
                            "detected_language_confidence": detection.confidence,
                            "language_detected_at": now,
                        }
                    else:
                        language_values = _language_values(
                            detection, manual_code=requested_manual_language
                        )
                    connection.execute(
                        """
                        UPDATE novels SET title = ?, normalized_title = ?, synopsis = ?,
                            language = ?, language_code = ?, language_name = ?,
                            language_confidence = ?, language_source = ?,
                            detected_language_code = ?, detected_language_name = ?,
                            detected_language_confidence = ?, language_detected_at = ?,
                            cover_path = ?, metadata_json = ?,
                            row_version = row_version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            title,
                            _normalized_key(title),
                            _optional_text(value.get("synopsis", novel["synopsis"])),
                            language_values["language"],
                            language_values["language_code"],
                            language_values["language_name"],
                            language_values["language_confidence"],
                            language_values["language_source"],
                            language_values["detected_language_code"],
                            language_values["detected_language_name"],
                            language_values["detected_language_confidence"],
                            language_values["language_detected_at"],
                            _optional_text(value.get("cover_path", novel["cover_path"]), maximum=2000),
                            _json_dump(merged_metadata),
                            now,
                            novel_id,
                        ),
                    )
                else:
                    novel_id = _new_id()
                    language_values = _language_values(
                        detection, manual_code=requested_manual_language
                    )
                    connection.execute(
                        """
                        INSERT INTO novels(
                            id, site_id, title, normalized_title, synopsis, language,
                            language_code, language_name, language_confidence,
                            language_source, detected_language_code,
                            detected_language_name, detected_language_confidence,
                            language_detected_at, cover_path, metadata_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            novel_id,
                            self.site_id,
                            title,
                            _normalized_key(title),
                            _optional_text(value.get("synopsis")),
                            language_values["language"],
                            language_values["language_code"],
                            language_values["language_name"],
                            language_values["language_confidence"],
                            language_values["language_source"],
                            language_values["detected_language_code"],
                            language_values["detected_language_name"],
                            language_values["detected_language_confidence"],
                            language_values["language_detected_at"],
                            _optional_text(value.get("cover_path"), maximum=2000),
                            _json_dump(_metadata(value.get("metadata"))),
                            now,
                            now,
                        ),
                    )

                revision_number = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM content_revisions WHERE novel_id = ?",
                        (novel_id,),
                    ).fetchone()[0]
                )
                revision_id = _new_id()
                revision_metadata = _metadata(value.get("revision_metadata"))
                revision_metadata["language_detection"] = detection.to_dict()
                connection.execute(
                    """
                    INSERT INTO content_revisions(
                        id, site_id, novel_id, revision_number, body, content_hash,
                        source_format, source_name, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        self.site_id,
                        novel_id,
                        revision_number,
                        body,
                        content_hash,
                        _optional_text(value.get("source_format"), maximum=50) or "text",
                        _optional_text(value.get("source_name"), maximum=1000),
                        _json_dump(revision_metadata),
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE novels SET current_revision_id = ?, updated_at = ? WHERE id = ?",
                    (revision_id, now, novel_id),
                )

                chapters_value = value.get("chapters")
                if chapters_value is None:
                    chapters: Sequence[Any] = (
                        {"ordinal": 1, "title": "", "body": body},
                    )
                elif isinstance(chapters_value, Sequence) and not isinstance(
                    chapters_value, (str, bytes, bytearray)
                ):
                    chapters = chapters_value
                else:
                    raise CatalogValidationError("chapters must be an array")
                for index, chapter_value in enumerate(chapters, start=1):
                    if not isinstance(chapter_value, Mapping):
                        raise CatalogValidationError("each chapter must be an object")
                    chapter_body = str(chapter_value.get("body") or "")
                    chapter_hash = manuscript_sha256(chapter_body)
                    ordinal = _positive_int(
                        chapter_value.get("ordinal", index),
                        "chapter ordinal",
                        minimum=1,
                        maximum=1_000_000,
                    )
                    connection.execute(
                        """
                        INSERT INTO chapters(
                            id, revision_id, ordinal, title, body, content_hash,
                            metadata_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(chapter_value.get("id") or _new_id()),
                            revision_id,
                            ordinal,
                            _optional_text(chapter_value.get("title"), maximum=500),
                            chapter_body,
                            chapter_hash,
                            _json_dump(_metadata(chapter_value.get("metadata"))),
                            now,
                        ),
                    )

                episodes_value = value.get("episodes") or []
                if not isinstance(episodes_value, Sequence) or isinstance(
                    episodes_value, (str, bytes, bytearray)
                ):
                    raise CatalogValidationError("episodes must be an array")
                for index, episode_value in enumerate(episodes_value, start=1):
                    if not isinstance(episode_value, Mapping):
                        raise CatalogValidationError("each episode must be an object")
                    duration_value = episode_value.get("estimated_duration_seconds")
                    duration = None if duration_value in (None, "") else float(duration_value)
                    if duration is not None and duration < 0:
                        raise CatalogValidationError(
                            "estimated_duration_seconds cannot be negative"
                        )
                    source_map = episode_value.get("source_map") or []
                    if not isinstance(source_map, Sequence) or isinstance(
                        source_map, (str, bytes, bytearray)
                    ):
                        raise CatalogValidationError("episode source_map must be an array")
                    connection.execute(
                        """
                        INSERT INTO episodes(
                            id, revision_id, ordinal, title, source_map_json,
                            recap_text, estimated_duration_seconds, status,
                            metadata_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(episode_value.get("id") or _new_id()),
                            revision_id,
                            _positive_int(
                                episode_value.get("ordinal", index),
                                "episode ordinal",
                                minimum=1,
                                maximum=1_000_000,
                            ),
                            _optional_text(episode_value.get("title"), maximum=500),
                            _json_dump(list(source_map)),
                            _optional_text(episode_value.get("recap_text")),
                            duration,
                            _optional_text(episode_value.get("status"), maximum=50)
                            or "planned",
                            _json_dump(_metadata(episode_value.get("metadata"))),
                            now,
                            now,
                        ),
                    )
                self._audit(
                    connection,
                    action="novel.imported",
                    entity_type="novel",
                    entity_id=novel_id,
                    actor_user_id=actor_user_id,
                    after={
                        "title": title,
                        "revision_id": revision_id,
                        "revision_number": revision_number,
                        "content_hash": content_hash,
                        "chapter_count": len(chapters),
                        "episode_count": len(episodes_value),
                        "language_detection": detection.to_dict(),
                    },
                )
            else:
                novel_id = duplicate_novel_id
                revision_id = duplicate_revision_id
                duplicate_novel = self._require_row(
                    connection,
                    "SELECT * FROM novels WHERE id = ? AND site_id = ?",
                    (novel_id, self.site_id),
                    "novel",
                )
                if str(duplicate_novel["language_source"] or "auto") == "manual":
                    connection.execute(
                        """
                        UPDATE novels SET detected_language_code = ?,
                            detected_language_name = ?, detected_language_confidence = ?,
                            language_detected_at = ?
                        WHERE id = ?
                        """,
                        (
                            detection.code,
                            detection.display_name,
                            detection.confidence,
                            now,
                            novel_id,
                        ),
                    )
                else:
                    language_values = _language_values(detection)
                    connection.execute(
                        """
                        UPDATE novels SET language = ?, language_code = ?,
                            language_name = ?, language_confidence = ?,
                            language_source = ?, detected_language_code = ?,
                            detected_language_name = ?, detected_language_confidence = ?,
                            language_detected_at = ?
                        WHERE id = ?
                        """,
                        (
                            language_values["language"],
                            language_values["language_code"],
                            language_values["language_name"],
                            language_values["language_confidence"],
                            language_values["language_source"],
                            language_values["detected_language_code"],
                            language_values["detected_language_name"],
                            language_values["detected_language_confidence"],
                            language_values["language_detected_at"],
                            novel_id,
                        ),
                    )
                revision_number = int(
                    connection.execute(
                        "SELECT revision_number FROM content_revisions WHERE id = ?",
                        (revision_id,),
                    ).fetchone()[0]
                )

        return {
            "created": duplicate is None,
            "deduplicated": duplicate is not None,
            "content_hash": content_hash,
            "revision_id": revision_id,
            "revision_number": revision_number,
            "novel": self.get_novel(novel_id),
        }

    def save_novel(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        novel_id = _required_text(value.get("id"), "novel id", maximum=120)
        with self._write_connection() as connection:
            row = self._require_row(
                connection,
                "SELECT * FROM novels WHERE id = ? AND site_id = ?",
                (novel_id, self.site_id),
                "novel",
            )
            self._check_version(row, self._expected_version(value))
            before = {
                "title": row["title"],
                "synopsis": row["synopsis"],
                "language": row["language_code"],
                "language_source": row["language_source"],
                "cover_path": row["cover_path"],
                "archived": bool(row["archived"]),
            }
            title = _required_text(value.get("title", row["title"]), "title")
            archived = bool(value.get("archived", bool(row["archived"])))
            now = utc_now()
            language_values = {
                "language": str(row["language_code"] or "unknown"),
                "language_code": str(row["language_code"] or "unknown"),
                "language_name": str(row["language_name"] or "未识别"),
                "language_confidence": float(row["language_confidence"] or 0.0),
                "language_source": str(row["language_source"] or "auto"),
                "detected_language_code": str(
                    row["detected_language_code"] or "unknown"
                ),
                "detected_language_name": str(
                    row["detected_language_name"] or "未识别"
                ),
                "detected_language_confidence": float(
                    row["detected_language_confidence"] or 0.0
                ),
                "language_detected_at": row["language_detected_at"],
            }
            language_key = ""
            if "language" in value:
                language_key = "language"
            elif "language_code" in value:
                language_key = "language_code"
            if language_key or value.get("redetect_language") is True:
                revision = connection.execute(
                    "SELECT body FROM content_revisions WHERE id = ?",
                    (row["current_revision_id"],),
                ).fetchone()
                detection = detect_language(
                    str(revision["body"] or "") if revision is not None else ""
                )
                manual_code = (
                    _requested_language(value.get(language_key)) if language_key else None
                )
                language_values = _language_values(
                    detection,
                    manual_code=(
                        None if value.get("redetect_language") is True else manual_code
                    ),
                )
            connection.execute(
                """
                UPDATE novels SET title = ?, normalized_title = ?, synopsis = ?,
                    language = ?, language_code = ?, language_name = ?,
                    language_confidence = ?, language_source = ?,
                    detected_language_code = ?, detected_language_name = ?,
                    detected_language_confidence = ?, language_detected_at = ?,
                    cover_path = ?, archived = ?, metadata_json = ?,
                    row_version = row_version + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    title,
                    _normalized_key(title),
                    _optional_text(value.get("synopsis", row["synopsis"])),
                    language_values["language"],
                    language_values["language_code"],
                    language_values["language_name"],
                    language_values["language_confidence"],
                    language_values["language_source"],
                    language_values["detected_language_code"],
                    language_values["detected_language_name"],
                    language_values["detected_language_confidence"],
                    language_values["language_detected_at"],
                    _optional_text(value.get("cover_path", row["cover_path"]), maximum=2000),
                    int(archived),
                    _json_dump(
                        _metadata(
                            value.get("metadata", _json_load(row["metadata_json"], {}))
                        )
                    ),
                    now,
                    novel_id,
                ),
            )
            self._audit(
                connection,
                action="novel.updated",
                entity_type="novel",
                entity_id=novel_id,
                actor_user_id=actor_user_id,
                before=before,
                after={
                    "title": title,
                    "synopsis": value.get("synopsis", row["synopsis"]),
                    "language": language_values["language_code"],
                    "language_source": language_values["language_source"],
                    "cover_path": value.get("cover_path", row["cover_path"]),
                    "archived": archived,
                },
            )
        return self.get_novel(novel_id)

    def save_novel_classification(
        self,
        novel_id: str,
        classification: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist only the safe, revision-bound story classification field.

        Producers may call this through Hub while choosing work.  Keeping this
        narrower than ``save_novel`` prevents that convenience from granting
        permission to edit titles, manuscripts, covers, bindings or arbitrary
        metadata.
        """

        target_id = _required_text(novel_id, "novel id", maximum=120)
        if not isinstance(classification, Mapping):
            raise CatalogValidationError("classification must be an object")
        mood = str(classification.get("mood") or "").strip().casefold()
        if mood not in {"suspense", "romance", "sad", "revenge"}:
            raise CatalogValidationError("classification mood is unsupported")
        source = str(classification.get("source") or "").strip().casefold()
        if source not in {"ai", "local_rules", "local_fallback"}:
            raise CatalogValidationError("classification source is unsupported")
        supplied_hash = str(classification.get("content_hash") or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", supplied_hash):
            raise CatalogValidationError("classification content_hash must be SHA-256")

        with self._write_connection() as connection:
            novel = self._require_row(
                connection,
                "SELECT * FROM novels WHERE id = ? AND site_id = ?",
                (target_id, self.site_id),
                "novel",
            )
            revision = self._require_row(
                connection,
                "SELECT id, content_hash FROM content_revisions WHERE id = ? AND novel_id = ?",
                (novel["current_revision_id"], target_id),
                "content revision",
            )
            current_hash = str(revision["content_hash"] or "").casefold()
            if supplied_hash != current_hash:
                raise CatalogConflictError(
                    "classification was generated from an older manuscript revision"
                )
            labels = {
                "suspense": "悬念",
                "romance": "浪漫",
                "sad": "悲伤",
                "revenge": "复仇 / 爽文",
            }
            safe_value = {
                "mood": mood,
                "label": labels[mood],
                "source": source,
                "provider": _optional_text(classification.get("provider"), maximum=120),
                "model": _optional_text(classification.get("model"), maximum=200),
                "content_hash": current_hash,
                "revision_id": str(revision["id"]),
                "classified_at": utc_now(),
                "warning": _optional_text(classification.get("warning"), maximum=1000),
            }
            metadata = _json_load(novel["metadata_json"], {})
            if not isinstance(metadata, dict):
                metadata = {}
            before = metadata.get("story_classification")
            metadata["story_classification"] = safe_value
            now = utc_now()
            connection.execute(
                """
                UPDATE novels SET metadata_json = ?, row_version = row_version + 1,
                    updated_at = ? WHERE id = ? AND site_id = ?
                """,
                (_json_dump(metadata), now, target_id, self.site_id),
            )
            self._audit(
                connection,
                action="novel.classified",
                entity_type="novel",
                entity_id=target_id,
                actor_user_id=actor_user_id,
                before=before if isinstance(before, Mapping) else {},
                after=safe_value,
            )
        return self.get_novel(target_id)

    def save_novel_voice_state(
        self,
        novel_id: str,
        voice_state: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist only production-safe narration preview and lock fields.

        This deliberately does not accept a generic ``metadata`` mapping.
        Producers can therefore generate, share and select a voice without
        receiving permission to edit any of the novel's managed content.
        """

        target_id = _required_text(novel_id, "novel id", maximum=120)
        requested = _voice_state_mapping(
            voice_state,
            label="voice_state",
            allowed_fields=NOVEL_VOICE_STATE_FIELDS,
        )
        if not requested:
            raise CatalogValidationError("voice_state must contain at least one field")

        with self._write_connection() as connection:
            novel = self._require_row(
                connection,
                "SELECT * FROM novels WHERE id = ? AND site_id = ?",
                (target_id, self.site_id),
                "novel",
            )
            metadata = _json_load(novel["metadata_json"], {})
            if not isinstance(metadata, dict):
                metadata = {}

            existing_candidates = metadata.get("voice_candidates")
            before_candidates = (
                existing_candidates if isinstance(existing_candidates, list) else []
            )
            before = {
                "candidate_count": len(before_candidates),
                "locked_voice_provider": str(
                    metadata.get("locked_voice_provider") or ""
                ),
                "locked_voice_id": str(metadata.get("locked_voice_id") or ""),
            }

            updates: dict[str, Any] = {}
            if "voice_candidates" in requested:
                updates["voice_candidates"] = _voice_candidates(
                    requested["voice_candidates"]
                )
            for field, maximum in (
                ("preferred_voice_provider", 120),
                ("preferred_voice_id", 300),
                ("preferred_voice_label", 300),
                ("preferred_voice_profile", 120),
                ("locked_voice_provider", 120),
                ("locked_voice_id", 300),
                ("locked_voice_label", 300),
                ("locked_voice_profile", 120),
            ):
                if field in requested:
                    updates[field] = _optional_text(
                        requested[field], maximum=maximum
                    )
            if "voice_lock_history" in requested:
                updates["voice_lock_history"] = _voice_lock_history(
                    requested["voice_lock_history"]
                )

            if {
                "locked_voice_provider",
                "locked_voice_id",
            }.intersection(updates):
                provider = str(
                    updates.get(
                        "locked_voice_provider",
                        metadata.get("locked_voice_provider") or "",
                    )
                ).strip()
                voice_id = str(
                    updates.get(
                        "locked_voice_id", metadata.get("locked_voice_id") or ""
                    )
                ).strip()
                if bool(provider) != bool(voice_id):
                    raise CatalogValidationError(
                        "locked voice provider and voice_id must be set together"
                    )
                candidates = updates.get("voice_candidates", before_candidates)
                if voice_id and candidates and not any(
                    isinstance(candidate, Mapping)
                    and str(candidate.get("provider") or "").strip() == provider
                    and str(candidate.get("voice_id") or "").strip() == voice_id
                    for candidate in candidates
                ):
                    raise CatalogValidationError(
                        "locked voice must match a current voice candidate"
                    )

            if {
                "preferred_voice_provider",
                "preferred_voice_id",
            }.intersection(updates):
                provider = str(
                    updates.get(
                        "preferred_voice_provider",
                        metadata.get("preferred_voice_provider") or "",
                    )
                ).strip()
                voice_id = str(
                    updates.get(
                        "preferred_voice_id",
                        metadata.get("preferred_voice_id") or "",
                    )
                ).strip()
                if bool(provider) != bool(voice_id):
                    raise CatalogValidationError(
                        "preferred voice provider and voice_id must be set together"
                    )
                candidates = updates.get("voice_candidates", before_candidates)
                if voice_id and candidates and not any(
                    isinstance(candidate, Mapping)
                    and str(candidate.get("provider") or "").strip() == provider
                    and str(candidate.get("voice_id") or "").strip() == voice_id
                    for candidate in candidates
                ):
                    raise CatalogValidationError(
                        "preferred voice must match a current voice candidate"
                    )

            metadata.update(updates)
            after_candidates = metadata.get("voice_candidates")
            after = {
                "candidate_count": (
                    len(after_candidates)
                    if isinstance(after_candidates, list)
                    else 0
                ),
                "locked_voice_provider": str(
                    metadata.get("locked_voice_provider") or ""
                ),
                "locked_voice_id": str(metadata.get("locked_voice_id") or ""),
            }
            now = utc_now()
            connection.execute(
                """
                UPDATE novels SET metadata_json = ?, row_version = row_version + 1,
                    updated_at = ? WHERE id = ? AND site_id = ?
                """,
                (_json_dump(metadata), now, target_id, self.site_id),
            )
            self._audit(
                connection,
                action="novel.voice_state_updated",
                entity_type="novel",
                entity_id=target_id,
                actor_user_id=actor_user_id,
                before=before,
                after=after,
            )
        return self.get_novel(target_id)

    def save_episode(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or update one planned episode on an immutable revision."""

        if not isinstance(value, Mapping):
            raise CatalogValidationError("episode payload must be an object")
        episode_id = _optional_text(value.get("id"), maximum=120)
        revision_id = _required_text(
            value.get("revision_id"), "revision_id", maximum=120
        )
        now = utc_now()
        source_map = value.get("source_map") or []
        if not isinstance(source_map, Sequence) or isinstance(
            source_map, (str, bytes, bytearray)
        ):
            raise CatalogValidationError("episode source_map must be an array")
        duration_value = value.get("estimated_duration_seconds")
        duration = None if duration_value in (None, "") else float(duration_value)
        if duration is not None and duration < 0:
            raise CatalogValidationError("estimated_duration_seconds cannot be negative")
        with self._write_connection() as connection:
            self._require_row(
                connection,
                "SELECT id FROM content_revisions WHERE id = ? AND site_id = ?",
                (revision_id, self.site_id),
                "revision",
            )
            if episode_id:
                row = self._require_row(
                    connection,
                    """
                    SELECT e.* FROM episodes e
                    JOIN content_revisions r ON r.id = e.revision_id
                    WHERE e.id = ? AND r.site_id = ?
                    """,
                    (episode_id, self.site_id),
                    "episode",
                )
                if str(row["revision_id"]) != revision_id:
                    raise CatalogValidationError("an episode cannot move to another revision")
                self._check_version(row, self._expected_version(value))
                before = self._episode_dict(row)
                connection.execute(
                    """
                    UPDATE episodes SET ordinal = ?, title = ?, source_map_json = ?,
                        recap_text = ?, estimated_duration_seconds = ?, status = ?,
                        metadata_json = ?, row_version = row_version + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        _positive_int(
                            value.get("ordinal", row["ordinal"]),
                            "episode ordinal",
                            minimum=1,
                            maximum=1_000_000,
                        ),
                        _optional_text(value.get("title", row["title"]), maximum=500),
                        _json_dump(list(source_map) if "source_map" in value else _json_load(row["source_map_json"], [])),
                        _optional_text(value.get("recap_text", row["recap_text"])),
                        duration
                        if "estimated_duration_seconds" in value
                        else row["estimated_duration_seconds"],
                        _optional_text(value.get("status", row["status"]), maximum=50)
                        or "planned",
                        _json_dump(
                            _metadata(
                                value.get("metadata", _json_load(row["metadata_json"], {}))
                            )
                        ),
                        now,
                        episode_id,
                    ),
                )
                action = "episode.updated"
            else:
                episode_id = _new_id()
                connection.execute(
                    """
                    INSERT INTO episodes(
                        id, revision_id, ordinal, title, source_map_json, recap_text,
                        estimated_duration_seconds, status, metadata_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        episode_id,
                        revision_id,
                        _positive_int(
                            value.get("ordinal"),
                            "episode ordinal",
                            minimum=1,
                            maximum=1_000_000,
                        ),
                        _optional_text(value.get("title"), maximum=500),
                        _json_dump(list(source_map)),
                        _optional_text(value.get("recap_text")),
                        duration,
                        _optional_text(value.get("status"), maximum=50) or "planned",
                        _json_dump(_metadata(value.get("metadata"))),
                        now,
                        now,
                    ),
                )
                before = None
                action = "episode.created"
            after_row = self._require_row(
                connection,
                "SELECT * FROM episodes WHERE id = ?",
                (episode_id,),
                "episode",
            )
            after = self._episode_dict(after_row)
            self._audit(
                connection,
                action=action,
                entity_type="episode",
                entity_id=episode_id,
                actor_user_id=actor_user_id,
                before=before,
                after=after,
            )
            return after

    def save_platform(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a shared promotion-platform profile."""

        if not isinstance(value, Mapping):
            raise CatalogValidationError("platform payload must be an object")
        platform_id = _optional_text(value.get("id"), maximum=120)
        name = _required_text(value.get("name"), "platform name", maximum=300)
        now = utc_now()
        try:
            with self._write_connection() as connection:
                row: sqlite3.Row | None = None
                if platform_id:
                    row = connection.execute(
                        "SELECT * FROM platforms WHERE id = ? AND site_id = ?",
                        (platform_id, self.site_id),
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT * FROM platforms WHERE site_id = ? AND normalized_name = ?",
                        (self.site_id, _normalized_key(name)),
                    ).fetchone()
                    if row is not None:
                        platform_id = str(row["id"])
                if row is None:
                    platform_id = platform_id or _new_id()
                    connection.execute(
                        """
                        INSERT INTO platforms(
                            id, site_id, name, normalized_name, search_template,
                            ending_template, logo_path, brand_color, archived, metadata_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            platform_id,
                            self.site_id,
                            name,
                            _normalized_key(name),
                            _optional_text(value.get("search_template"), maximum=2000)
                            or "Search {platform}: {code}",
                            _optional_text(value.get("ending_template"), maximum=2000)
                            or "Download {platform} and search code {code} to continue reading.",
                            _optional_text(value.get("logo_path"), maximum=2000),
                            _optional_text(value.get("brand_color"), maximum=64),
                            int(bool(value.get("archived", False))),
                            _json_dump(_metadata(value.get("metadata"))),
                            now,
                            now,
                        ),
                    )
                    before = None
                    action = "platform.created"
                else:
                    self._check_version(row, self._expected_version(value))
                    before = {
                        "name": row["name"],
                        "search_template": row["search_template"],
                        "ending_template": row["ending_template"],
                        "logo_path": row["logo_path"],
                        "brand_color": row["brand_color"],
                        "archived": bool(row["archived"]),
                    }
                    connection.execute(
                        """
                        UPDATE platforms SET name = ?, normalized_name = ?,
                            search_template = ?, ending_template = ?, logo_path = ?,
                            brand_color = ?, archived = ?,
                            metadata_json = ?, row_version = row_version + 1,
                            updated_at = ? WHERE id = ?
                        """,
                        (
                            name,
                            _normalized_key(name),
                            _optional_text(value.get("search_template", row["search_template"]), maximum=2000),
                            _optional_text(value.get("ending_template", row["ending_template"]), maximum=2000),
                            _optional_text(value.get("logo_path", row["logo_path"]), maximum=2000),
                            _optional_text(value.get("brand_color", row["brand_color"]), maximum=64),
                            int(bool(value.get("archived", bool(row["archived"])))),
                            _json_dump(
                                _metadata(
                                    value.get("metadata", _json_load(row["metadata_json"], {}))
                                )
                            ),
                            now,
                            platform_id,
                        ),
                    )
                    action = "platform.updated"
                updated = self._require_row(
                    connection,
                    "SELECT * FROM platforms WHERE id = ?",
                    (platform_id,),
                    "platform",
                )
                result = {
                    "id": str(updated["id"]),
                    "name": str(updated["name"]),
                    "search_template": str(updated["search_template"]),
                    "ending_template": str(updated["ending_template"]),
                    "logo_path": str(updated["logo_path"]),
                    "brand_color": str(updated["brand_color"]),
                    "archived": bool(updated["archived"]),
                    "metadata": _json_load(updated["metadata_json"], {}),
                    "row_version": int(updated["row_version"]),
                    "created_at": str(updated["created_at"]),
                    "updated_at": str(updated["updated_at"]),
                }
                self._audit(
                    connection,
                    action=action,
                    entity_type="platform",
                    entity_id=platform_id,
                    actor_user_id=actor_user_id,
                    before=before,
                    after=result,
                )
                return result
        except sqlite3.IntegrityError as error:
            raise CatalogConflictError("platform name already exists") from error

    def list_platforms(
        self, *, include_archived: bool = False
    ) -> dict[str, Any]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT p.*,
                    (SELECT COUNT(*) FROM novel_platform_bindings b WHERE b.platform_id = p.id) AS binding_count
                FROM platforms p
                WHERE p.site_id = ? AND (? OR p.archived = 0)
                ORDER BY p.name COLLATE NOCASE
                """,
                (self.site_id, int(include_archived)),
            ).fetchall()
            return {
                "items": [
                    {
                        "id": str(row["id"]),
                        "name": str(row["name"]),
                        "search_template": str(row["search_template"]),
                        "ending_template": str(row["ending_template"]),
                        "logo_path": str(row["logo_path"]),
                        "brand_color": str(row["brand_color"]),
                        "archived": bool(row["archived"]),
                        "metadata": _json_load(row["metadata_json"], {}),
                        "row_version": int(row["row_version"]),
                        "binding_count": int(row["binding_count"]),
                        "created_at": str(row["created_at"]),
                        "updated_at": str(row["updated_at"]),
                    }
                    for row in rows
                ],
                "total": len(rows),
            }

    def save_novel_binding(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Upsert one novel-to-promotion-platform binding.

        Callers may pass an existing ``platform_id`` or a ``platform_name``.
        The latter creates or reuses a site platform without a separate API call.
        """

        if not isinstance(value, Mapping):
            raise CatalogValidationError("binding payload must be an object")
        novel_id = _required_text(value.get("novel_id"), "novel_id", maximum=120)
        binding_id = _optional_text(value.get("id"), maximum=120)
        platform_id = _optional_text(value.get("platform_id"), maximum=120)
        platform_name = _optional_text(value.get("platform_name"), maximum=300)
        if not platform_id and not platform_name:
            raise CatalogValidationError("platform_id or platform_name is required")
        now = utc_now()
        with self._write_connection() as connection:
            self._require_row(
                connection,
                "SELECT id FROM novels WHERE id = ? AND site_id = ?",
                (novel_id, self.site_id),
                "novel",
            )
            if platform_id:
                platform = self._require_row(
                    connection,
                    "SELECT * FROM platforms WHERE id = ? AND site_id = ?",
                    (platform_id, self.site_id),
                    "platform",
                )
                platform_name = str(platform["name"])
                if "platform_name" in value and _normalized_key(
                    str(value.get("platform_name") or "")
                ) != str(platform["normalized_name"]):
                    raise CatalogValidationError(
                        "platform_name does not match platform_id"
                    )
            else:
                normalized_platform = _normalized_key(platform_name)
                platform = connection.execute(
                    "SELECT * FROM platforms WHERE site_id = ? AND normalized_name = ?",
                    (self.site_id, normalized_platform),
                ).fetchone()
                if platform is None:
                    platform_id = _new_id()
                    connection.execute(
                        """
                        INSERT INTO platforms(
                            id, site_id, name, normalized_name, search_template,
                            ending_template, metadata_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            platform_id,
                            self.site_id,
                            platform_name,
                            normalized_platform,
                            _optional_text(value.get("search_template"), maximum=2000)
                            or "Search {platform}: {code}",
                            _optional_text(value.get("ending_template"), maximum=2000)
                            or "Download {platform} and search code {code} to continue reading.",
                            _json_dump(_metadata(value.get("platform_metadata"))),
                            now,
                            now,
                        ),
                    )
                    self._audit(
                        connection,
                        action="platform.created",
                        entity_type="platform",
                        entity_id=platform_id,
                        actor_user_id=actor_user_id,
                        after={"name": platform_name},
                    )
                else:
                    platform_id = str(platform["id"])
                    platform_name = str(platform["name"])

            row: sqlite3.Row | None
            if binding_id:
                row = self._require_row(
                    connection,
                    "SELECT * FROM novel_platform_bindings WHERE id = ? AND site_id = ?",
                    (binding_id, self.site_id),
                    "binding",
                )
                if str(row["novel_id"]) != novel_id or str(row["platform_id"]) != platform_id:
                    raise CatalogValidationError(
                        "a binding cannot move to another novel or platform"
                    )
            else:
                row = connection.execute(
                    """
                    SELECT * FROM novel_platform_bindings
                    WHERE novel_id = ? AND platform_id = ?
                    """,
                    (novel_id, platform_id),
                ).fetchone()
                if row is not None:
                    binding_id = str(row["id"])

            if row is None:
                binding_id = binding_id or _new_id()
                connection.execute(
                    """
                    INSERT INTO novel_platform_bindings(
                        id, site_id, novel_id, platform_id, external_book_id,
                        platform_title, language, commission_rate, archived,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding_id,
                        self.site_id,
                        novel_id,
                        platform_id,
                        _optional_text(value.get("external_book_id"), maximum=500),
                        _optional_text(value.get("platform_title"), maximum=500),
                        _optional_text(value.get("language"), maximum=50),
                        self._commission_rate(value.get("commission_rate")),
                        int(bool(value.get("archived", False))),
                        _json_dump(_metadata(value.get("metadata"))),
                        now,
                        now,
                    ),
                )
                before = None
                action = "binding.created"
            else:
                self._check_version(row, self._expected_version(value))
                before = {
                    "external_book_id": row["external_book_id"],
                    "platform_title": row["platform_title"],
                    "language": row["language"],
                    "commission_rate": row["commission_rate"],
                    "archived": bool(row["archived"]),
                }
                connection.execute(
                    """
                    UPDATE novel_platform_bindings SET external_book_id = ?,
                        platform_title = ?, language = ?, commission_rate = ?,
                        archived = ?, metadata_json = ?,
                        row_version = row_version + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        _optional_text(value.get("external_book_id", row["external_book_id"]), maximum=500),
                        _optional_text(value.get("platform_title", row["platform_title"]), maximum=500),
                        _optional_text(value.get("language", row["language"]), maximum=50),
                        self._commission_rate(
                            value.get("commission_rate", row["commission_rate"])
                        ),
                        int(bool(value.get("archived", bool(row["archived"])))),
                        _json_dump(
                            _metadata(
                                value.get("metadata", _json_load(row["metadata_json"], {}))
                            )
                        ),
                        now,
                        binding_id,
                    ),
                )
                action = "binding.updated"
            result_row = self._require_row(
                connection,
                """
                SELECT b.*, p.name AS platform_name
                FROM novel_platform_bindings b JOIN platforms p ON p.id = b.platform_id
                WHERE b.id = ?
                """,
                (binding_id,),
                "binding",
            )
            result = self._binding_dict(connection, result_row)
            self._audit(
                connection,
                action=action,
                entity_type="novel_platform_binding",
                entity_id=binding_id,
                actor_user_id=actor_user_id,
                before=before,
                after={
                    key: result[key]
                    for key in (
                        "novel_id",
                        "platform_id",
                        "external_book_id",
                        "platform_title",
                        "language",
                        "commission_rate",
                        "archived",
                    )
                },
            )
            return result

    @staticmethod
    def _commission_rate(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError) as error:
            raise CatalogValidationError("commission_rate must be a number") from error
        if not 0 <= parsed <= 100:
            raise CatalogValidationError("commission_rate must be between 0 and 100")
        return parsed

    def add_promo_code(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise CatalogValidationError("promo code payload must be an object")
        binding_id = _required_text(
            value.get("binding_id"), "binding_id", maximum=120
        )
        code = _required_text(value.get("code"), "code", maximum=200)
        status = _optional_text(value.get("status"), maximum=30) or "active"
        if status not in PROMO_CODE_STATUSES:
            raise CatalogValidationError("invalid promo code status")
        now = utc_now()
        try:
            with self._write_connection() as connection:
                self._require_row(
                    connection,
                    """
                    SELECT b.id FROM novel_platform_bindings b
                    WHERE b.id = ? AND b.site_id = ?
                    """,
                    (binding_id, self.site_id),
                    "binding",
                )
                used_slots = {
                    int(item[0])
                    for item in connection.execute(
                        "SELECT slot_no FROM promo_codes WHERE binding_id = ?",
                        (binding_id,),
                    ).fetchall()
                }
                available = next((slot for slot in range(1, 6) if slot not in used_slots), None)
                if available is None:
                    raise PromoCodeLimitError(
                        "this novel-platform binding already has five historical promo codes"
                    )
                promo_code_id = str(value.get("id") or _new_id())
                connection.execute(
                    """
                    INSERT INTO promo_codes(
                        id, binding_id, slot_no, code, normalized_code, status,
                        label, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        promo_code_id,
                        binding_id,
                        available,
                        code,
                        _normalized_key(code),
                        status,
                        _optional_text(value.get("label"), maximum=300),
                        _optional_text(value.get("notes"), maximum=4000),
                        now,
                        now,
                    ),
                )
                row = self._require_row(
                    connection,
                    "SELECT * FROM promo_codes WHERE id = ?",
                    (promo_code_id,),
                    "promo code",
                )
                result = self._promo_code_dict(row)
                self._audit(
                    connection,
                    action="promo_code.created",
                    entity_type="promo_code",
                    entity_id=promo_code_id,
                    actor_user_id=actor_user_id,
                    after=result,
                )
                return result
        except sqlite3.IntegrityError as error:
            message = str(error).casefold()
            if "historical limit" in message:
                raise PromoCodeLimitError(
                    "this novel-platform binding already has five historical promo codes"
                ) from error
            if "normalized_code" in message or "unique constraint" in message:
                raise CatalogConflictError(
                    "this promo code already exists for the binding"
                ) from error
            raise

    def update_promo_code(
        self,
        promo_code_id: str,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        promo_code_id = _required_text(
            promo_code_id, "promo_code_id", maximum=120
        )
        if not isinstance(value, Mapping):
            raise CatalogValidationError("promo code payload must be an object")
        with self._write_connection() as connection:
            row = self._require_row(
                connection,
                """
                SELECT c.* FROM promo_codes c
                JOIN novel_platform_bindings b ON b.id = c.binding_id
                WHERE c.id = ? AND b.site_id = ?
                """,
                (promo_code_id, self.site_id),
                "promo code",
            )
            self._check_version(row, self._expected_version(value))
            if "code" in value and str(value.get("code") or "").strip() != str(row["code"]):
                raise CatalogValidationError(
                    "promo code values are immutable; add another historical slot instead"
                )
            status = _optional_text(value.get("status", row["status"]), maximum=30)
            if status not in PROMO_CODE_STATUSES:
                raise CatalogValidationError("invalid promo code status")
            before = self._promo_code_dict(row)
            connection.execute(
                """
                UPDATE promo_codes SET status = ?, label = ?, notes = ?,
                    row_version = row_version + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    _optional_text(value.get("label", row["label"]), maximum=300),
                    _optional_text(value.get("notes", row["notes"]), maximum=4000),
                    utc_now(),
                    promo_code_id,
                ),
            )
            updated = self._require_row(
                connection,
                "SELECT * FROM promo_codes WHERE id = ?",
                (promo_code_id,),
                "promo code",
            )
            result = self._promo_code_dict(updated)
            self._audit(
                connection,
                action="promo_code.updated",
                entity_type="promo_code",
                entity_id=promo_code_id,
                actor_user_id=actor_user_id,
                before=before,
                after=result,
            )
            return result

    def list_promo_codes(
        self,
        binding_id: str,
        *,
        include_inactive: bool = True,
    ) -> dict[str, Any]:
        binding_id = _required_text(binding_id, "binding_id", maximum=120)
        with self._read_connection() as connection:
            self._require_row(
                connection,
                "SELECT id FROM novel_platform_bindings WHERE id = ? AND site_id = ?",
                (binding_id, self.site_id),
                "binding",
            )
            rows = connection.execute(
                """
                SELECT * FROM promo_codes
                WHERE binding_id = ? AND (? OR status = 'active')
                ORDER BY slot_no
                """,
                (binding_id, int(include_inactive)),
            ).fetchall()
            historical_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM promo_codes WHERE binding_id = ?",
                    (binding_id,),
                ).fetchone()[0]
            )
            return {
                "items": [self._promo_code_dict(row) for row in rows],
                "total": len(rows),
                "historical_count": historical_count,
                "slots_remaining": max(0, 5 - historical_count),
            }

    @staticmethod
    def _publishing_account_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "network": str(row["network"]),
            "handle": str(row["handle"]),
            "display_name": str(row["display_name"]),
            "status": str(row["status"]),
            "metadata": _json_load(row["metadata_json"], {}),
            "row_version": int(row["row_version"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "record_count": int(row["record_count"] or 0)
            if "record_count" in row.keys()
            else None,
        }

    def save_publishing_account(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise CatalogValidationError("publishing account payload must be an object")
        account_id = _optional_text(value.get("id"), maximum=120)
        network = _required_text(value.get("network"), "network", maximum=200)
        handle = _required_text(value.get("handle"), "handle", maximum=300)
        status = _optional_text(value.get("status"), maximum=30) or "active"
        if status not in PUBLISHING_ACCOUNT_STATUSES:
            raise CatalogValidationError("invalid publishing account status")
        now = utc_now()
        try:
            with self._write_connection() as connection:
                row: sqlite3.Row | None
                if account_id:
                    row = self._require_row(
                        connection,
                        "SELECT * FROM publishing_accounts WHERE id = ? AND site_id = ?",
                        (account_id, self.site_id),
                        "publishing account",
                    )
                else:
                    row = connection.execute(
                        """
                        SELECT * FROM publishing_accounts
                        WHERE site_id = ? AND normalized_network = ? AND normalized_handle = ?
                        """,
                        (self.site_id, _normalized_key(network), _normalized_key(handle)),
                    ).fetchone()
                    if row is not None:
                        account_id = str(row["id"])
                if row is None:
                    account_id = account_id or _new_id()
                    connection.execute(
                        """
                        INSERT INTO publishing_accounts(
                            id, site_id, network, normalized_network, handle,
                            normalized_handle, display_name, status, metadata_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            account_id,
                            self.site_id,
                            network,
                            _normalized_key(network),
                            handle,
                            _normalized_key(handle),
                            _optional_text(value.get("display_name"), maximum=500),
                            status,
                            _json_dump(_metadata(value.get("metadata"))),
                            now,
                            now,
                        ),
                    )
                    before = None
                    action = "publishing_account.created"
                else:
                    self._check_version(row, self._expected_version(value))
                    before = self._publishing_account_dict(row)
                    connection.execute(
                        """
                        UPDATE publishing_accounts SET network = ?, normalized_network = ?,
                            handle = ?, normalized_handle = ?, display_name = ?,
                            status = ?, metadata_json = ?,
                            row_version = row_version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            network,
                            _normalized_key(network),
                            handle,
                            _normalized_key(handle),
                            _optional_text(value.get("display_name", row["display_name"]), maximum=500),
                            status,
                            _json_dump(
                                _metadata(
                                    value.get("metadata", _json_load(row["metadata_json"], {}))
                                )
                            ),
                            now,
                            account_id,
                        ),
                    )
                    action = "publishing_account.updated"
                updated = self._require_row(
                    connection,
                    "SELECT * FROM publishing_accounts WHERE id = ?",
                    (account_id,),
                    "publishing account",
                )
                result = self._publishing_account_dict(updated)
                self._audit(
                    connection,
                    action=action,
                    entity_type="publishing_account",
                    entity_id=account_id,
                    actor_user_id=actor_user_id,
                    before=before,
                    after=result,
                )
                return result
        except sqlite3.IntegrityError as error:
            raise CatalogConflictError(
                "this publishing account already exists"
            ) from error

    def list_publishing_accounts(
        self,
        *,
        network: str = "",
        include_archived: bool = False,
    ) -> dict[str, Any]:
        filters = ["a.site_id = ?"]
        parameters: list[Any] = [self.site_id]
        if network:
            filters.append("a.normalized_network = ?")
            parameters.append(_normalized_key(network))
        if not include_archived:
            filters.append("a.status != 'archived'")
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT a.*,
                    (SELECT COUNT(*) FROM production_records r
                     WHERE r.publishing_account_id = a.id) AS record_count
                FROM publishing_accounts a
                WHERE {' AND '.join(filters)}
                ORDER BY a.network COLLATE NOCASE, a.handle COLLATE NOCASE
                """,
                parameters,
            ).fetchall()
            return {
                "items": [self._publishing_account_dict(row) for row in rows],
                "total": len(rows),
            }

    @staticmethod
    def _user_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"]),
            "role": str(row["role"]),
            "active": bool(row["active"]),
            "has_password": bool(row["password_hash"]),
            "metadata": _json_load(row["metadata_json"], {}),
            "row_version": int(row["row_version"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _super_admin_count(self, connection: sqlite3.Connection) -> int:
        permission_checks = " AND ".join(
            """
            COALESCE((
                SELECT allowed FROM user_permissions p
                WHERE p.user_id = u.id AND p.permission = ?
            ), 1) = 1
            """
            for _permission in sorted(SUPER_ADMIN_PERMISSIONS)
        )
        return int(
            connection.execute(
                f"""
                SELECT COUNT(*) FROM software_users u
                WHERE u.site_id = ? AND u.active = 1 AND u.role = ?
                  AND {permission_checks}
                """,
                (
                    self.site_id,
                    ROLE_ADMIN,
                    *sorted(SUPER_ADMIN_PERMISSIONS),
                ),
            ).fetchone()[0]
        )

    def _protect_last_super_admin(
        self, connection: sqlite3.Connection, previous_count: int
    ) -> None:
        if previous_count > 0 and self._super_admin_count(connection) == 0:
            raise CatalogConflictError(
                "cannot remove the last active administrator with users.manage, "
                "permissions.manage, and hub.manage"
            )

    def save_user(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a software user.

        ``password_hash`` is deliberately opaque.  Authentication code may use
        scrypt/Argon2 later without the catalog ever receiving a plaintext secret.
        """

        if not isinstance(value, Mapping):
            raise CatalogValidationError("user payload must be an object")
        user_id = _optional_text(value.get("id"), maximum=120)
        username = _required_text(value.get("username"), "username", maximum=200)
        requested_role = _optional_text(value.get("role"), maximum=30)
        if requested_role and requested_role not in USER_ROLES:
            raise CatalogValidationError("role must be admin or producer")
        now = utc_now()
        try:
            with self._write_connection() as connection:
                row: sqlite3.Row | None = None
                if user_id:
                    row = self._require_row(
                        connection,
                        "SELECT * FROM software_users WHERE id = ? AND site_id = ?",
                        (user_id, self.site_id),
                        "user",
                    )
                if row is None:
                    role = requested_role or ROLE_PRODUCER
                    active = bool(value.get("active", True))
                    user_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM software_users WHERE site_id = ?",
                            (self.site_id,),
                        ).fetchone()[0]
                    )
                    if user_count == 0 and (role != ROLE_ADMIN or not active):
                        raise CatalogValidationError(
                            "the first software user must be an active administrator"
                        )
                    user_id = user_id or _new_id()
                    connection.execute(
                        """
                        INSERT INTO software_users(
                            id, site_id, username, normalized_username, display_name,
                            role, password_hash, active, metadata_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            self.site_id,
                            username,
                            _normalized_key(username),
                            _optional_text(value.get("display_name"), maximum=500),
                            role,
                            _optional_text(value.get("password_hash"), maximum=5000),
                            int(active),
                            _json_dump(_metadata(value.get("metadata"))),
                            now,
                            now,
                        ),
                    )
                    before = None
                    action = "user.created"
                else:
                    self._check_version(row, self._expected_version(value))
                    previous_super_admin_count = self._super_admin_count(connection)
                    before = self._user_dict(row)
                    role = requested_role or str(row["role"])
                    password_hash = (
                        _optional_text(value.get("password_hash"), maximum=5000)
                        if "password_hash" in value
                        else str(row["password_hash"])
                    )
                    connection.execute(
                        """
                        UPDATE software_users SET username = ?, normalized_username = ?,
                            display_name = ?, role = ?, password_hash = ?, active = ?,
                            metadata_json = ?, row_version = row_version + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            username,
                            _normalized_key(username),
                            _optional_text(value.get("display_name", row["display_name"]), maximum=500),
                            role,
                            password_hash,
                            int(bool(value.get("active", bool(row["active"])))),
                            _json_dump(
                                _metadata(
                                    value.get("metadata", _json_load(row["metadata_json"], {}))
                                )
                            ),
                            now,
                            user_id,
                        ),
                    )
                    self._protect_last_super_admin(
                        connection, previous_super_admin_count
                    )
                    action = "user.updated"
                updated = self._require_row(
                    connection,
                    "SELECT * FROM software_users WHERE id = ?",
                    (user_id,),
                    "user",
                )
                result = self._user_dict(updated)
                self._audit(
                    connection,
                    action=action,
                    entity_type="software_user",
                    entity_id=user_id,
                    actor_user_id=actor_user_id,
                    before=before,
                    after=result,
                )
                return result
        except sqlite3.IntegrityError as error:
            raise CatalogConflictError("username already exists") from error

    def delete_user(
        self,
        user_id: str,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Permanently delete one account without deleting production history.

        Foreign keys on production drafts, records, device history and config
        revisions use ``ON DELETE SET NULL``.  Token and permission rows are
        account-owned and are removed by cascade.  The immutable audit event
        stores the deleted account's public snapshot so administrators retain
        a useful trail without keeping a login-capable tombstone account.
        """

        clean_id = _required_text(user_id, "user_id", maximum=120)
        with self._write_connection() as connection:
            row = self._require_row(
                connection,
                "SELECT * FROM software_users WHERE id = ? AND site_id = ?",
                (clean_id, self.site_id),
                "user",
            )
            before = self._user_dict(row)
            previous_super_admin_count = self._super_admin_count(connection)
            connection.execute(
                "DELETE FROM software_users WHERE id = ? AND site_id = ?",
                (clean_id, self.site_id),
            )
            self._protect_last_super_admin(connection, previous_super_admin_count)
            self._audit(
                connection,
                action="user.deleted",
                entity_type="software_user",
                entity_id=clean_id,
                actor_user_id=actor_user_id,
                before=before,
                after={"deleted": True},
            )
            return {
                "id": clean_id,
                "username": before["username"],
                "display_name": before["display_name"],
                "role": before["role"],
                "deleted": True,
            }

    def list_users(self, *, include_inactive: bool = True) -> dict[str, Any]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM software_users
                WHERE site_id = ? AND (? OR active = 1)
                ORDER BY username COLLATE NOCASE
                """,
                (self.site_id, int(include_inactive)),
            ).fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                item = self._user_dict(row)
                permission_rows = connection.execute(
                    "SELECT permission, allowed FROM user_permissions WHERE user_id = ?",
                    (row["id"],),
                ).fetchall()
                item["permission_overrides"] = {
                    str(permission["permission"]): bool(permission["allowed"])
                    for permission in permission_rows
                }
                items.append(item)
            return {"items": items, "total": len(items)}

    def _web_user_by_username(self, username: str) -> dict[str, Any] | None:
        """Return the minimal private credential record used by the LAN UI.

        This intentionally is not part of ``CATALOG_RPC_METHODS`` and must
        never be returned by a public API: unlike :meth:`list_users`, the
        result contains the opaque password verifier.
        """

        normalized = _normalized_key(_required_text(username, "username", maximum=200))
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT id, username, display_name, role, password_hash, active,
                    row_version
                FROM software_users
                WHERE site_id = ? AND normalized_username = ?
                """,
                (self.site_id, normalized),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"] or ""),
            "role": str(row["role"]),
            "password_hash": str(row["password_hash"] or ""),
            "active": bool(row["active"]),
            "row_version": int(row["row_version"]),
        }

    def _web_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        """Private counterpart of :meth:`_web_user_by_username`."""

        clean_id = _required_text(user_id, "user_id", maximum=200)
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT id, username, display_name, role, password_hash, active,
                    row_version
                FROM software_users
                WHERE site_id = ? AND id = ?
                """,
                (self.site_id, clean_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"] or ""),
            "role": str(row["role"]),
            "password_hash": str(row["password_hash"] or ""),
            "active": bool(row["active"]),
            "row_version": int(row["row_version"]),
        }

    def _set_web_password_hash(self, user_id: str, password_hash: str) -> None:
        """Atomically replace one user's private web password verifier."""

        clean_id = _required_text(user_id, "user_id", maximum=200)
        verifier = _required_text(password_hash, "password_hash", maximum=5000)
        with self._write_connection() as connection:
            row = self._require_row(
                connection,
                "SELECT * FROM software_users WHERE site_id = ? AND id = ?",
                (self.site_id, clean_id),
                "user",
            )
            connection.execute(
                """
                UPDATE software_users
                SET password_hash = ?, row_version = row_version + 1, updated_at = ?
                WHERE site_id = ? AND id = ?
                """,
                (verifier, utc_now(), self.site_id, clean_id),
            )
            self._audit(
                connection,
                action="user.web_password_updated",
                entity_type="software_user",
                entity_id=clean_id,
                actor_user_id=clean_id,
                before={"has_password": bool(row["password_hash"])},
                after={"has_password": True},
            )

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _hub_device_dict(
        cls,
        row: sqlite3.Row,
        *,
        online_cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        last_seen = cls._timestamp(row["last_seen_at"])
        active = bool(row["active"])
        online = bool(
            active
            and online_cutoff is not None
            and last_seen is not None
            and last_seen >= online_cutoff
        )
        keys = set(row.keys())
        metadata = _json_load(row["metadata_json"], {})
        return {
            "id": str(row["id"]),
            "installation_id_hash": str(row["installation_id_hash"]),
            "name": str(row["name"]),
            "hostname": str(row["hostname"] or ""),
            "app_version": str(row["app_version"] or ""),
            "os_name": str(row["os_name"] or ""),
            "architecture": str(row["architecture"] or ""),
            "capabilities": _json_load(row["capabilities_json"], {}),
            "metadata": metadata,
            "last_user_id": str(row["last_user_id"] or ""),
            "active": active,
            "online": online,
            "first_seen_at": str(row["first_seen_at"]),
            "last_seen_at": str(row["last_seen_at"]),
            "row_version": int(row["row_version"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "first_login_at": str(
                metadata.get("first_login_at") or row["first_seen_at"]
            ),
            "admin_reviewed_at": str(metadata.get("admin_reviewed_at") or ""),
            "needs_admin_review": bool(
                metadata.get("needs_admin_review", False)
            ),
            "active_token_count": int(row["active_token_count"] or 0)
            if "active_token_count" in keys
            else 0,
            "desired_revision_number": int(row["desired_revision_number"] or 0)
            if "desired_revision_number" in keys
            else 0,
        }

    def _hub_device_row(
        self, connection: sqlite3.Connection, device_id: str
    ) -> sqlite3.Row:
        return self._require_row(
            connection,
            """
            SELECT d.*,
                (SELECT COUNT(*) FROM hub_access_tokens t
                 WHERE t.device_id = d.id AND t.revoked_at IS NULL)
                    AS active_token_count,
                (SELECT MAX(r.revision_number)
                 FROM device_config_targets ct
                 JOIN device_config_revisions r ON r.id = ct.revision_id
                 WHERE ct.device_id = d.id AND ct.site_id = d.site_id)
                    AS desired_revision_number
            FROM hub_devices d
            WHERE d.id = ? AND d.site_id = ?
            """,
            (device_id, self.site_id),
            "Hub device",
        )

    def register_hub_device(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Register or refresh one stable installation for this site."""

        if not isinstance(value, Mapping):
            raise CatalogValidationError("Hub device payload must be an object")
        fingerprint = _installation_id_hash(value.get("installation_id_hash"))
        hostname = _optional_text(value.get("hostname"), maximum=255)
        name = _required_text(
            value.get("name") or hostname, "device name", maximum=160
        )
        app_version = _optional_text(value.get("app_version"), maximum=80)
        os_name = _optional_text(value.get("os_name"), maximum=120)
        architecture = _optional_text(value.get("architecture"), maximum=80)
        capabilities = (
            _bounded_json_object(
                value.get("capabilities"),
                label="capabilities",
                maximum_bytes=16_384,
            )
            if "capabilities" in value
            else None
        )
        metadata = (
            _bounded_json_object(
                value.get("metadata"),
                label="device metadata",
                maximum_bytes=16_384,
            )
            if "metadata" in value
            else None
        )
        last_user_id = _optional_text(
            value.get("last_user_id") or actor_user_id, maximum=120
        ) or None
        now = utc_now()
        with self._write_connection() as connection:
            if last_user_id:
                user = self._require_row(
                    connection,
                    "SELECT id, active FROM software_users WHERE id = ? AND site_id = ?",
                    (last_user_id, self.site_id),
                    "user",
                )
                if not bool(user["active"]):
                    raise CatalogValidationError("inactive users cannot register devices")
            existing = connection.execute(
                """
                SELECT * FROM hub_devices
                WHERE site_id = ? AND installation_id_hash = ?
                """,
                (self.site_id, fingerprint),
            ).fetchone()
            created = existing is None
            if created:
                initial_metadata = dict(metadata or {})
                # These fields are server-owned. A workstation cannot hide its
                # first login from the administrator by forging metadata.
                initial_metadata["first_login_at"] = now
                initial_metadata["admin_reviewed_at"] = ""
                initial_metadata["needs_admin_review"] = True
                device_id = _new_id()
                connection.execute(
                    """
                    INSERT INTO hub_devices(
                        id, site_id, installation_id_hash, name, hostname,
                        app_version, os_name, architecture, capabilities_json,
                        metadata_json, last_user_id, active, first_seen_at,
                        last_seen_at, row_version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 1, ?, ?)
                    """,
                    (
                        device_id,
                        self.site_id,
                        fingerprint,
                        name,
                        hostname,
                        app_version,
                        os_name,
                        architecture,
                        _json_dump(capabilities or {}),
                        _json_dump(initial_metadata),
                        last_user_id,
                        now,
                        now,
                        now,
                        now,
                    ),
                )
            else:
                device_id = str(existing["id"])
                merged_metadata: dict[str, Any] | None = None
                if metadata is not None:
                    existing_metadata = _json_load(existing["metadata_json"], {})
                    merged_metadata = dict(existing_metadata)
                    merged_metadata.update(metadata)
                    for reserved in (
                        "first_login_at",
                        "admin_reviewed_at",
                        "needs_admin_review",
                    ):
                        if reserved in existing_metadata:
                            merged_metadata[reserved] = existing_metadata[reserved]
                connection.execute(
                    """
                    UPDATE hub_devices
                    SET hostname = CASE WHEN ? = '' THEN hostname ELSE ? END,
                        app_version = CASE WHEN ? = '' THEN app_version ELSE ? END,
                        os_name = CASE WHEN ? = '' THEN os_name ELSE ? END,
                        architecture = CASE WHEN ? = '' THEN architecture ELSE ? END,
                        capabilities_json = COALESCE(?, capabilities_json),
                        metadata_json = COALESCE(?, metadata_json),
                        last_user_id = COALESCE(?, last_user_id), last_seen_at = ?,
                        row_version = row_version + 1, updated_at = ?
                    WHERE id = ? AND site_id = ?
                    """,
                    (
                        hostname,
                        hostname,
                        app_version,
                        app_version,
                        os_name,
                        os_name,
                        architecture,
                        architecture,
                        _json_dump(capabilities) if capabilities is not None else None,
                        _json_dump(merged_metadata)
                        if merged_metadata is not None
                        else None,
                        last_user_id,
                        now,
                        now,
                        device_id,
                        self.site_id,
                    ),
                )
            row = self._hub_device_row(connection, device_id)
            device = self._hub_device_dict(
                row, online_cutoff=datetime.now(timezone.utc) - timedelta(seconds=120)
            )
            if created:
                self._audit(
                    connection,
                    action="hub_device.registered",
                    entity_type="hub_device",
                    entity_id=device_id,
                    actor_user_id=actor_user_id,
                    after={
                        "name": device["name"],
                        "hostname": device["hostname"],
                        "app_version": device["app_version"],
                        "active": device["active"],
                    },
                )
            return {"created": created, "reused": not created, "device": device}

    def acknowledge_hub_device(
        self,
        device_id: str,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Clear the administrator's new-computer marker for one device."""

        clean_id = _required_text(device_id, "device_id", maximum=120)
        with self._write_connection() as connection:
            row = self._hub_device_row(connection, clean_id)
            before = self._hub_device_dict(row)
            metadata = _json_load(row["metadata_json"], {})
            reviewed_at = utc_now()
            metadata["needs_admin_review"] = False
            metadata["admin_reviewed_at"] = reviewed_at
            connection.execute(
                """
                UPDATE hub_devices
                SET metadata_json = ?, row_version = row_version + 1,
                    updated_at = ?
                WHERE id = ? AND site_id = ?
                """,
                (_json_dump(metadata), reviewed_at, clean_id, self.site_id),
            )
            result = self._hub_device_dict(
                self._hub_device_row(connection, clean_id)
            )
            self._audit(
                connection,
                action="hub_device.reviewed",
                entity_type="hub_device",
                entity_id=clean_id,
                actor_user_id=actor_user_id,
                before={"needs_admin_review": before["needs_admin_review"]},
                after={
                    "needs_admin_review": False,
                    "admin_reviewed_at": reviewed_at,
                },
            )
            return result

    def heartbeat_hub_device(
        self,
        device_id: str,
        *,
        user_id: str | None = None,
        app_version: str = "",
        capabilities: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        device_id = _required_text(device_id, "device_id", maximum=120)
        clean_user_id = _optional_text(user_id, maximum=120) or None
        clean_version = _optional_text(app_version, maximum=80)
        clean_capabilities = (
            _bounded_json_object(
                capabilities, label="capabilities", maximum_bytes=16_384
            )
            if capabilities is not None
            else None
        )
        now = utc_now()
        with self._write_connection() as connection:
            current = self._hub_device_row(connection, device_id)
            if not bool(current["active"]):
                raise CatalogConflictError("Hub device is inactive")
            if clean_user_id:
                user = self._require_row(
                    connection,
                    "SELECT id, active FROM software_users WHERE id = ? AND site_id = ?",
                    (clean_user_id, self.site_id),
                    "user",
                )
                if not bool(user["active"]):
                    raise CatalogValidationError("inactive users cannot heartbeat devices")
            connection.execute(
                """
                UPDATE hub_devices
                SET last_seen_at = ?, last_user_id = COALESCE(?, last_user_id),
                    app_version = CASE WHEN ? = '' THEN app_version ELSE ? END,
                    capabilities_json = COALESCE(?, capabilities_json),
                    updated_at = ?
                WHERE id = ? AND site_id = ?
                """,
                (
                    now,
                    clean_user_id,
                    clean_version,
                    clean_version,
                    _json_dump(clean_capabilities)
                    if clean_capabilities is not None
                    else None,
                    now,
                    device_id,
                    self.site_id,
                ),
            )
            device = self._hub_device_dict(
                self._hub_device_row(connection, device_id),
                online_cutoff=datetime.now(timezone.utc) - timedelta(seconds=120),
            )
            return {"heartbeat": True, "server_time": now, "device": device}

    def list_hub_devices(
        self,
        *,
        active: bool | None = None,
        online: bool | None = None,
        offline_after_seconds: int = 120,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if active is not None and not isinstance(active, bool):
            raise CatalogValidationError("active must be true, false, or null")
        if online is not None and not isinstance(online, bool):
            raise CatalogValidationError("online must be true, false, or null")
        ttl = _positive_int(
            offline_after_seconds,
            "offline_after_seconds",
            minimum=5,
            maximum=86_400,
        )
        limit = _positive_int(limit, "limit", minimum=1, maximum=500)
        offset = _positive_int(offset, "offset", minimum=0, maximum=10_000_000)
        filters = ["d.site_id = ?"]
        parameters: list[Any] = [self.site_id]
        if active is not None:
            filters.append("d.active = ?")
            parameters.append(int(active))
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT d.*,
                    (SELECT COUNT(*) FROM hub_access_tokens t
                     WHERE t.device_id = d.id AND t.revoked_at IS NULL)
                        AS active_token_count,
                    (SELECT MAX(r.revision_number)
                     FROM device_config_targets ct
                     JOIN device_config_revisions r ON r.id = ct.revision_id
                     WHERE ct.device_id = d.id AND ct.site_id = d.site_id)
                        AS desired_revision_number
                FROM hub_devices d
                WHERE {' AND '.join(filters)}
                ORDER BY d.active DESC, d.last_seen_at DESC, d.name, d.id
                """,
                parameters,
            ).fetchall()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl)
        all_items = [
            self._hub_device_dict(row, online_cutoff=cutoff) for row in rows
        ]
        if online is not None:
            all_items = [item for item in all_items if item["online"] is online]
        return {
            "items": all_items[offset : offset + limit],
            "total": len(all_items),
            "limit": limit,
            "offset": offset,
            "offline_after_seconds": ttl,
        }

    def get_hub_device(
        self, device_id: str, *, offline_after_seconds: int = 120
    ) -> dict[str, Any]:
        device_id = _required_text(device_id, "device_id", maximum=120)
        ttl = _positive_int(
            offline_after_seconds,
            "offline_after_seconds",
            minimum=5,
            maximum=86_400,
        )
        with self._read_connection() as connection:
            row = self._hub_device_row(connection, device_id)
            return self._hub_device_dict(
                row,
                online_cutoff=datetime.now(timezone.utc) - timedelta(seconds=ttl),
            )

    def hub_device_fleet_summary(
        self, *, offline_after_seconds: int = 120
    ) -> dict[str, Any]:
        ttl = _positive_int(
            offline_after_seconds,
            "offline_after_seconds",
            minimum=5,
            maximum=86_400,
        )
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT active, last_seen_at FROM hub_devices WHERE site_id = ?",
                (self.site_id,),
            ).fetchall()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl)
        active = sum(1 for row in rows if bool(row["active"]))
        online = sum(
            1
            for row in rows
            if bool(row["active"])
            and (self._timestamp(row["last_seen_at"]) or datetime.min.replace(tzinfo=timezone.utc))
            >= cutoff
        )
        return {
            "total": len(rows),
            "active": active,
            "inactive": len(rows) - active,
            "online": online,
            "offline": active - online,
            "offline_after_seconds": ttl,
        }

    def rename_hub_device(
        self,
        device_id: str,
        name: str,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        device_id = _required_text(device_id, "device_id", maximum=120)
        clean_name = _required_text(name, "device name", maximum=160)
        with self._write_connection() as connection:
            before_row = self._hub_device_row(connection, device_id)
            before = self._hub_device_dict(before_row)
            if before["name"] != clean_name:
                connection.execute(
                    """
                    UPDATE hub_devices
                    SET name = ?, row_version = row_version + 1, updated_at = ?
                    WHERE id = ? AND site_id = ?
                    """,
                    (clean_name, utc_now(), device_id, self.site_id),
                )
            after = self._hub_device_dict(
                self._hub_device_row(connection, device_id)
            )
            self._audit(
                connection,
                action="hub_device.renamed",
                entity_type="hub_device",
                entity_id=device_id,
                actor_user_id=actor_user_id,
                before={"name": before["name"]},
                after={"name": after["name"]},
            )
            return after

    def set_hub_device_active(
        self,
        device_id: str,
        active: bool,
        *,
        revoke_tokens: bool = True,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        device_id = _required_text(device_id, "device_id", maximum=120)
        if not isinstance(active, bool):
            raise CatalogValidationError("active must be true or false")
        if not isinstance(revoke_tokens, bool):
            raise CatalogValidationError("revoke_tokens must be true or false")
        with self._write_connection() as connection:
            before = self._hub_device_dict(
                self._hub_device_row(connection, device_id)
            )
            now = utc_now()
            connection.execute(
                """
                UPDATE hub_devices
                SET active = ?, row_version = row_version + 1, updated_at = ?
                WHERE id = ? AND site_id = ?
                """,
                (int(active), now, device_id, self.site_id),
            )
            revoked_count = 0
            if not active and revoke_tokens:
                cursor = connection.execute(
                    """
                    UPDATE hub_access_tokens SET revoked_at = ?
                    WHERE site_id = ? AND device_id = ? AND revoked_at IS NULL
                    """,
                    (now, self.site_id, device_id),
                )
                revoked_count = max(0, int(cursor.rowcount))
            after = self._hub_device_dict(
                self._hub_device_row(connection, device_id)
            )
            self._audit(
                connection,
                action="hub_device.activation_changed",
                entity_type="hub_device",
                entity_id=device_id,
                actor_user_id=actor_user_id,
                before={"active": before["active"]},
                after={"active": after["active"], "revoked_tokens": revoked_count},
            )
            return {"device": after, "revoked_tokens": revoked_count}

    @staticmethod
    def _device_config_revision_dict(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "id": str(row["id"]),
            "revision_number": int(row["revision_number"]),
            "config_schema_version": int(row["config_schema_version"]),
            "config": _json_load(row["config_json"], {}),
            "config_hash": str(row["config_hash"]),
            "target_mode": str(row["target_mode"]),
            "target_count": int(row["target_count"] or 0)
            if "target_count" in keys
            else 0,
            "note": str(row["note"] or ""),
            "created_by_user_id": str(row["created_by_user_id"] or ""),
            "created_at": str(row["created_at"]),
        }

    def create_device_config_revision(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one immutable portable config and materialize its targets."""

        if not isinstance(value, Mapping):
            raise CatalogValidationError("device config payload must be an object")
        config = normalize_portable_device_config(value.get("config"))
        target_mode = str(value.get("target_mode") or "").strip().casefold()
        if target_mode not in {"single", "multiple", "all"}:
            raise CatalogValidationError(
                "target_mode must be single, multiple, or all"
            )
        raw_ids = value.get("device_ids", [])
        if not isinstance(raw_ids, Sequence) or isinstance(
            raw_ids, (str, bytes, bytearray)
        ):
            raise CatalogValidationError("device_ids must be an array")
        device_ids = [
            _required_text(item, "device_id", maximum=120) for item in raw_ids
        ]
        if len(set(device_ids)) != len(device_ids):
            raise CatalogValidationError("device_ids cannot contain duplicates")
        if target_mode == "single" and len(device_ids) != 1:
            raise CatalogValidationError("single target requires exactly one device")
        if target_mode == "multiple" and not 1 <= len(device_ids) <= 500:
            raise CatalogValidationError("multiple target requires 1 to 500 devices")
        if target_mode == "all" and device_ids:
            raise CatalogValidationError("all target must not include device_ids")
        note = _optional_text(value.get("note"), maximum=1000)
        config_json = _json_dump(config)
        config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
        now = utc_now()
        with self._write_connection() as connection:
            if actor_user_id:
                self._require_row(
                    connection,
                    "SELECT id FROM software_users WHERE id = ? AND site_id = ?",
                    (actor_user_id, self.site_id),
                    "user",
                )
            if target_mode == "all":
                device_ids = [
                    str(row["id"])
                    for row in connection.execute(
                        """
                        SELECT id FROM hub_devices
                        WHERE site_id = ? AND active = 1 ORDER BY id
                        """,
                        (self.site_id,),
                    ).fetchall()
                ]
            else:
                placeholders = ",".join("?" for _ in device_ids)
                found = {
                    str(row["id"])
                    for row in connection.execute(
                        f"""
                        SELECT id FROM hub_devices
                        WHERE site_id = ? AND active = 1
                          AND id IN ({placeholders})
                        """,
                        (self.site_id, *device_ids),
                    ).fetchall()
                }
                if found != set(device_ids):
                    raise CatalogValidationError(
                        "every target device must be active and belong to this site"
                    )
            if not device_ids:
                raise CatalogValidationError("device config requires at least one target")
            revision_number = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(revision_number), 0) + 1
                    FROM device_config_revisions WHERE site_id = ?
                    """,
                    (self.site_id,),
                ).fetchone()[0]
            )
            revision_id = _new_id()
            connection.execute(
                """
                INSERT INTO device_config_revisions(
                    id, site_id, revision_number, config_schema_version,
                    config_json, config_hash, target_mode, note,
                    created_by_user_id, created_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    self.site_id,
                    revision_number,
                    config_json,
                    config_hash,
                    target_mode,
                    note,
                    actor_user_id,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO device_config_targets(
                    revision_id, device_id, site_id, assigned_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (revision_id, device_id, self.site_id, now)
                    for device_id in device_ids
                ],
            )
            row = self._require_row(
                connection,
                """
                SELECT r.*, COUNT(t.device_id) AS target_count
                FROM device_config_revisions r
                LEFT JOIN device_config_targets t ON t.revision_id = r.id
                WHERE r.id = ? AND r.site_id = ? GROUP BY r.id
                """,
                (revision_id, self.site_id),
                "device config revision",
            )
            revision = self._device_config_revision_dict(row)
            self._audit(
                connection,
                action="device_config.created",
                entity_type="device_config_revision",
                entity_id=revision_id,
                actor_user_id=actor_user_id,
                after={
                    **revision,
                    "target_device_ids": list(device_ids),
                },
            )
            return {**revision, "target_device_ids": list(device_ids)}

    def list_device_config_revisions(
        self, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        limit = _positive_int(limit, "limit", minimum=1, maximum=500)
        offset = _positive_int(offset, "offset", minimum=0, maximum=10_000_000)
        with self._read_connection() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM device_config_revisions WHERE site_id = ?",
                    (self.site_id,),
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT r.*, COUNT(t.device_id) AS target_count
                FROM device_config_revisions r
                LEFT JOIN device_config_targets t ON t.revision_id = r.id
                WHERE r.site_id = ?
                GROUP BY r.id
                ORDER BY r.revision_number DESC LIMIT ? OFFSET ?
                """,
                (self.site_id, limit, offset),
            ).fetchall()
            return {
                "items": [self._device_config_revision_dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def get_device_config_revision(self, revision_id: str) -> dict[str, Any]:
        revision_id = _required_text(revision_id, "revision_id", maximum=120)
        with self._read_connection() as connection:
            row = self._require_row(
                connection,
                """
                SELECT r.*, COUNT(t.device_id) AS target_count
                FROM device_config_revisions r
                LEFT JOIN device_config_targets t ON t.revision_id = r.id
                WHERE r.id = ? AND r.site_id = ? GROUP BY r.id
                """,
                (revision_id, self.site_id),
                "device config revision",
            )
            targets = connection.execute(
                """
                SELECT t.device_id, d.name AS device_name, d.active,
                    t.assigned_at, t.acknowledged_at, t.ack_status,
                    t.ack_message, t.reported_config_hash
                FROM device_config_targets t
                JOIN hub_devices d ON d.id = t.device_id
                WHERE t.revision_id = ? AND t.site_id = ? AND d.site_id = ?
                ORDER BY d.name, d.id
                """,
                (revision_id, self.site_id, self.site_id),
            ).fetchall()
            revision = self._device_config_revision_dict(row)
            return {
                **revision,
                "targets": [
                    {
                        "device_id": str(target["device_id"]),
                        "device_name": str(target["device_name"]),
                        "device_active": bool(target["active"]),
                        "assigned_at": str(target["assigned_at"]),
                        "acknowledged_at": str(target["acknowledged_at"] or ""),
                        "ack_status": str(target["ack_status"] or ""),
                        "ack_message": str(target["ack_message"] or ""),
                        "reported_config_hash": str(
                            target["reported_config_hash"] or ""
                        ),
                    }
                    for target in targets
                ],
            }

    def get_device_desired_config(
        self,
        device_id: str,
        *,
        current_revision_id: str = "",
    ) -> dict[str, Any]:
        device_id = _required_text(device_id, "device_id", maximum=120)
        current_revision_id = _optional_text(current_revision_id, maximum=120)
        with self._read_connection() as connection:
            device = self._hub_device_row(connection, device_id)
            if not bool(device["active"]):
                raise CatalogConflictError("Hub device is inactive")
            row = connection.execute(
                """
                SELECT r.*, 1 AS target_count, t.assigned_at,
                    t.acknowledged_at, t.ack_status, t.ack_message,
                    t.reported_config_hash
                FROM device_config_targets t
                JOIN device_config_revisions r ON r.id = t.revision_id
                WHERE t.site_id = ? AND t.device_id = ? AND r.site_id = ?
                ORDER BY r.revision_number DESC LIMIT 1
                """,
                (self.site_id, device_id, self.site_id),
            ).fetchone()
            if row is None:
                return {
                    "device_id": device_id,
                    "desired": None,
                    "needs_apply": False,
                    "server_time": utc_now(),
                }
            revision = self._device_config_revision_dict(row)
            desired = {
                **revision,
                "assigned_at": str(row["assigned_at"]),
                "acknowledged_at": str(row["acknowledged_at"] or ""),
                "ack_status": str(row["ack_status"] or ""),
                "ack_message": str(row["ack_message"] or ""),
                "reported_config_hash": str(row["reported_config_hash"] or ""),
            }
            needs_apply = bool(
                desired["ack_status"] != "applied"
                or (
                    current_revision_id
                    and current_revision_id != desired["id"]
                )
            )
            return {
                "device_id": device_id,
                "desired": desired,
                "needs_apply": needs_apply,
                "server_time": utc_now(),
            }

    def ack_device_config(
        self,
        device_id: str,
        revision_id: str,
        *,
        status: str = "applied",
        message: str = "",
        reported_config_hash: str = "",
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        device_id = _required_text(device_id, "device_id", maximum=120)
        revision_id = _required_text(revision_id, "revision_id", maximum=120)
        status = str(status or "").strip().casefold()
        if status not in {"applied", "failed"}:
            raise CatalogValidationError("ack status must be applied or failed")
        message = _optional_text(message, maximum=2000)
        reported_hash = str(reported_config_hash or "").strip().casefold()
        if reported_hash and not re.fullmatch(r"[0-9a-f]{64}", reported_hash):
            raise CatalogValidationError(
                "reported_config_hash must be a SHA-256 hex digest"
            )
        with self._write_connection() as connection:
            row = self._require_row(
                connection,
                """
                SELECT t.*, r.config_hash, r.revision_number,
                    d.active AS device_active
                FROM device_config_targets t
                JOIN device_config_revisions r ON r.id = t.revision_id
                JOIN hub_devices d ON d.id = t.device_id
                WHERE t.site_id = ? AND t.device_id = ? AND t.revision_id = ?
                  AND r.site_id = ? AND d.site_id = ?
                """,
                (
                    self.site_id,
                    device_id,
                    revision_id,
                    self.site_id,
                    self.site_id,
                ),
                "device config target",
            )
            if not bool(row["device_active"]):
                raise CatalogConflictError("Hub device is inactive")
            expected_hash = str(row["config_hash"])
            if status == "applied":
                if reported_hash and not secrets.compare_digest(
                    reported_hash, expected_hash
                ):
                    raise CatalogConflictError(
                        "reported config hash does not match desired config"
                    )
                reported_hash = reported_hash or expected_hash
            now = utc_now()
            connection.execute(
                """
                UPDATE device_config_targets
                SET acknowledged_at = ?, ack_status = ?, ack_message = ?,
                    reported_config_hash = ?
                WHERE site_id = ? AND device_id = ? AND revision_id = ?
                """,
                (
                    now,
                    status,
                    message,
                    reported_hash,
                    self.site_id,
                    device_id,
                    revision_id,
                ),
            )
            result = {
                "device_id": device_id,
                "revision_id": revision_id,
                "revision_number": int(row["revision_number"]),
                "status": status,
                "message": message,
                "reported_config_hash": reported_hash,
                "acknowledged_at": now,
            }
            self._audit(
                connection,
                action="device_config.acknowledged",
                entity_type="device_config_target",
                entity_id=f"{revision_id}:{device_id}",
                actor_user_id=actor_user_id,
                before={
                    "status": str(row["ack_status"] or ""),
                    "acknowledged_at": str(row["acknowledged_at"] or ""),
                },
                after=result,
            )
            return result

    @staticmethod
    def _hub_token_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "user_id": str(row["user_id"]),
            "device_id": str(row["device_id"] or ""),
            "label": str(row["label"]),
            "revoked": bool(row["revoked_at"]),
            "revoked_at": str(row["revoked_at"] or ""),
            "created_at": str(row["created_at"]),
        }

    def issue_hub_access_token(
        self,
        user_id: str,
        *,
        label: str = "",
        device_id: str | None = None,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Issue a user-scoped Hub token and persist only its SHA-256 hash."""

        user_id = _required_text(user_id, "user_id", maximum=120)
        label = _required_text(label, "label", maximum=120)
        clean_device_id = _optional_text(device_id, maximum=120) or None
        token_id = _new_id()
        raw_token = "sfh_" + secrets.token_urlsafe(36)
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        now = utc_now()
        with self._write_connection() as connection:
            user = self._require_row(
                connection,
                "SELECT * FROM software_users WHERE id = ? AND site_id = ?",
                (user_id, self.site_id),
                "user",
            )
            if not bool(user["active"]):
                raise CatalogValidationError(
                    "cannot issue a Hub token for an inactive user"
                )
            if clean_device_id:
                device = self._hub_device_row(connection, clean_device_id)
                if not bool(device["active"]):
                    raise CatalogValidationError(
                        "cannot issue a Hub token for an inactive device"
                    )
            connection.execute(
                """
                INSERT INTO hub_access_tokens(
                    id, site_id, user_id, device_id, token_hash, label,
                    revoked_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    token_id,
                    self.site_id,
                    user_id,
                    clean_device_id,
                    token_hash,
                    label,
                    now,
                ),
            )
            public = {
                "id": token_id,
                "user_id": user_id,
                "device_id": clean_device_id or "",
                "label": label,
                "revoked": False,
                "revoked_at": "",
                "created_at": now,
            }
            self._audit(
                connection,
                action="hub_token.issued",
                entity_type="hub_access_token",
                entity_id=token_id,
                actor_user_id=actor_user_id,
                before=None,
                after=public,
            )
        # The plaintext is intentionally returned once and never written to
        # SQLite, logs, metadata or audit events.
        return {**public, "token": raw_token}

    def bind_hub_access_token_device(
        self,
        token_id: str,
        device_id: str,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        token_id = _required_text(token_id, "token_id", maximum=120)
        device_id = _required_text(device_id, "device_id", maximum=120)
        with self._write_connection() as connection:
            token = self._require_row(
                connection,
                "SELECT * FROM hub_access_tokens WHERE id = ? AND site_id = ?",
                (token_id, self.site_id),
                "Hub access token",
            )
            if token["revoked_at"]:
                raise CatalogConflictError("revoked Hub tokens cannot be bound")
            device = self._hub_device_row(connection, device_id)
            if not bool(device["active"]):
                raise CatalogConflictError("Hub device is inactive")
            before = self._hub_token_dict(token)
            connection.execute(
                """
                UPDATE hub_access_tokens SET device_id = ?
                WHERE id = ? AND site_id = ?
                """,
                (device_id, token_id, self.site_id),
            )
            updated = self._require_row(
                connection,
                "SELECT * FROM hub_access_tokens WHERE id = ? AND site_id = ?",
                (token_id, self.site_id),
                "Hub access token",
            )
            result = self._hub_token_dict(updated)
            self._audit(
                connection,
                action="hub_token.device_bound",
                entity_type="hub_access_token",
                entity_id=token_id,
                actor_user_id=actor_user_id,
                before={"device_id": before["device_id"]},
                after={"device_id": result["device_id"]},
            )
            return result

    def rotate_hub_device_access_token(
        self,
        user_id: str,
        device_id: str,
        *,
        label: str,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically replace every active token for one stable installation."""

        user_id = _required_text(user_id, "user_id", maximum=120)
        device_id = _required_text(device_id, "device_id", maximum=120)
        label = _required_text(label, "label", maximum=120)
        token_id = _new_id()
        raw_token = "sfh_" + secrets.token_urlsafe(36)
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        now = utc_now()
        with self._write_connection() as connection:
            user = self._require_row(
                connection,
                "SELECT id, active FROM software_users WHERE id = ? AND site_id = ?",
                (user_id, self.site_id),
                "user",
            )
            if not bool(user["active"]):
                raise CatalogValidationError(
                    "cannot rotate a Hub token for an inactive user"
                )
            device = self._hub_device_row(connection, device_id)
            if not bool(device["active"]):
                raise CatalogValidationError(
                    "cannot rotate a Hub token for an inactive device"
                )
            revoked = connection.execute(
                """
                UPDATE hub_access_tokens SET revoked_at = ?
                WHERE site_id = ? AND device_id = ? AND revoked_at IS NULL
                """,
                (now, self.site_id, device_id),
            )
            connection.execute(
                """
                INSERT INTO hub_access_tokens(
                    id, site_id, user_id, device_id, token_hash, label,
                    revoked_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    token_id,
                    self.site_id,
                    user_id,
                    device_id,
                    token_hash,
                    label,
                    now,
                ),
            )
            public = {
                "id": token_id,
                "user_id": user_id,
                "device_id": device_id,
                "label": label,
                "revoked": False,
                "revoked_at": "",
                "created_at": now,
                "replaced_token_count": max(0, int(revoked.rowcount)),
            }
            self._audit(
                connection,
                action="hub_token.rotated",
                entity_type="hub_access_token",
                entity_id=token_id,
                actor_user_id=actor_user_id,
                after=public,
            )
        return {**public, "token": raw_token}

    def revoke_hub_device_tokens(
        self,
        device_id: str,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        device_id = _required_text(device_id, "device_id", maximum=120)
        with self._write_connection() as connection:
            self._hub_device_row(connection, device_id)
            now = utc_now()
            rows = connection.execute(
                """
                SELECT id FROM hub_access_tokens
                WHERE site_id = ? AND device_id = ? AND revoked_at IS NULL
                """,
                (self.site_id, device_id),
            ).fetchall()
            token_ids = [str(row["id"]) for row in rows]
            if token_ids:
                connection.execute(
                    """
                    UPDATE hub_access_tokens SET revoked_at = ?
                    WHERE site_id = ? AND device_id = ? AND revoked_at IS NULL
                    """,
                    (now, self.site_id, device_id),
                )
            result = {
                "device_id": device_id,
                "revoked_count": len(token_ids),
                "revoked_at": now if token_ids else "",
            }
            self._audit(
                connection,
                action="hub_device.tokens_revoked",
                entity_type="hub_device",
                entity_id=device_id,
                actor_user_id=actor_user_id,
                after=result,
            )
            return result

    def list_hub_access_tokens(
        self,
        user_id: str | None = None,
        *,
        include_revoked: bool = True,
    ) -> dict[str, Any]:
        parameters: list[Any] = [self.site_id]
        filters = ["site_id = ?"]
        if user_id not in (None, ""):
            filters.append("user_id = ?")
            parameters.append(_required_text(user_id, "user_id", maximum=120))
        if not include_revoked:
            filters.append("revoked_at IS NULL")
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM hub_access_tokens
                WHERE {' AND '.join(filters)}
                ORDER BY created_at DESC
                """,
                parameters,
            ).fetchall()
            items = [self._hub_token_dict(row) for row in rows]
            return {"items": items, "total": len(items)}

    def revoke_hub_access_token(
        self,
        token_id: str,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        token_id = _required_text(token_id, "token_id", maximum=120)
        with self._write_connection() as connection:
            row = self._require_row(
                connection,
                "SELECT * FROM hub_access_tokens WHERE id = ? AND site_id = ?",
                (token_id, self.site_id),
                "Hub access token",
            )
            before = self._hub_token_dict(row)
            revoked_at = str(row["revoked_at"] or utc_now())
            connection.execute(
                "UPDATE hub_access_tokens SET revoked_at = ? WHERE id = ?",
                (revoked_at, token_id),
            )
            updated = self._require_row(
                connection,
                "SELECT * FROM hub_access_tokens WHERE id = ?",
                (token_id,),
                "Hub access token",
            )
            result = self._hub_token_dict(updated)
            self._audit(
                connection,
                action="hub_token.revoked",
                entity_type="hub_access_token",
                entity_id=token_id,
                actor_user_id=actor_user_id,
                before=before,
                after=result,
            )
            return result

    def resolve_hub_access_token(self, raw_token: str) -> str | None:
        """Resolve an active token without exposing hashes or inactive users."""

        token = str(raw_token or "")
        if not token or len(token) > 512:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT t.user_id
                FROM hub_access_tokens t
                JOIN software_users u ON u.id = t.user_id
                LEFT JOIN hub_devices d ON d.id = t.device_id
                WHERE t.site_id = ? AND t.token_hash = ?
                  AND t.revoked_at IS NULL AND u.active = 1
                  AND (t.device_id IS NULL OR (
                      d.site_id = t.site_id AND d.active = 1
                  ))
                LIMIT 1
                """,
                (self.site_id, token_hash),
            ).fetchone()
            return str(row["user_id"]) if row is not None else None

    def resolve_hub_access_identity(self, raw_token: str) -> dict[str, Any]:
        """Return the authenticated user/device pair without token material."""

        token = str(raw_token or "")
        if not token or len(token) > 512:
            return {"authenticated": False}
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT t.id AS token_id, t.user_id, t.device_id,
                    u.username, u.display_name, u.role
                FROM hub_access_tokens t
                JOIN software_users u ON u.id = t.user_id
                LEFT JOIN hub_devices d ON d.id = t.device_id
                WHERE t.site_id = ? AND t.token_hash = ?
                  AND t.revoked_at IS NULL AND u.active = 1
                  AND (t.device_id IS NULL OR (
                      d.site_id = t.site_id AND d.active = 1
                  ))
                LIMIT 1
                """,
                (self.site_id, token_hash),
            ).fetchone()
            if row is None:
                return {"authenticated": False}
            return {
                "authenticated": True,
                "token_id": str(row["token_id"]),
                "user_id": str(row["user_id"]),
                "device_id": str(row["device_id"] or ""),
                "username": str(row["username"]),
                "display_name": str(row["display_name"] or ""),
                "role": str(row["role"]),
            }

    def set_user_permission(
        self,
        user_id: str,
        permission: str,
        allowed: bool | None,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        user_id = _required_text(user_id, "user_id", maximum=120)
        permission = _required_text(permission, "permission", maximum=200)
        if permission not in KNOWN_PERMISSIONS:
            raise CatalogValidationError(f"unknown permission: {permission}")
        if allowed is not None and not isinstance(allowed, bool):
            raise CatalogValidationError("allowed must be true, false, or null")
        with self._write_connection() as connection:
            previous_super_admin_count = self._super_admin_count(connection)
            self._require_row(
                connection,
                "SELECT id FROM software_users WHERE id = ? AND site_id = ?",
                (user_id, self.site_id),
                "user",
            )
            previous = connection.execute(
                "SELECT allowed FROM user_permissions WHERE user_id = ? AND permission = ?",
                (user_id, permission),
            ).fetchone()
            if allowed is None:
                connection.execute(
                    "DELETE FROM user_permissions WHERE user_id = ? AND permission = ?",
                    (user_id, permission),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO user_permissions(user_id, permission, allowed, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, permission) DO UPDATE SET
                        allowed = excluded.allowed, updated_at = excluded.updated_at
                    """,
                    (user_id, permission, int(allowed), utc_now()),
                )
            self._protect_last_super_admin(
                connection, previous_super_admin_count
            )
            self._audit(
                connection,
                action="permission.updated",
                entity_type="software_user",
                entity_id=user_id,
                actor_user_id=actor_user_id,
                before={
                    "permission": permission,
                    "override": None if previous is None else bool(previous["allowed"]),
                },
                after={"permission": permission, "override": allowed},
            )
        return self.get_effective_permissions(user_id)

    def get_effective_permissions(self, user_id: str) -> dict[str, Any]:
        user_id = _required_text(user_id, "user_id", maximum=120)
        with self._read_connection() as connection:
            row = self._require_row(
                connection,
                "SELECT * FROM software_users WHERE id = ? AND site_id = ?",
                (user_id, self.site_id),
                "user",
            )
            overrides = {
                str(item["permission"]): bool(item["allowed"])
                for item in connection.execute(
                    "SELECT permission, allowed FROM user_permissions WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
            }
            defaults = ROLE_DEFAULTS[str(row["role"])]
            effective = {
                permission: overrides.get(permission, permission in defaults)
                for permission in sorted(KNOWN_PERMISSIONS)
            }
            return {
                "user_id": user_id,
                "role": str(row["role"]),
                "active": bool(row["active"]),
                "overrides": overrides,
                "effective": effective,
                "allowed": [
                    permission for permission, is_allowed in effective.items() if is_allowed
                ],
            }

    def _draft_dict(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        episode_rows = connection.execute(
            """
            SELECT e.*, de.ordinal AS selection_ordinal
            FROM draft_episodes de
            JOIN episodes e ON e.id = de.episode_id
            WHERE de.draft_id = ?
            ORDER BY de.ordinal
            """,
            (row["id"],),
        ).fetchall()
        return {
            "id": str(row["id"]),
            "novel_id": str(row["novel_id"]),
            "binding_id": str(row["binding_id"]),
            "promo_code_id": str(row["promo_code_id"]),
            "publishing_account_id": row["publishing_account_id"],
            "creative_line_count": int(row["creative_line_count"]),
            "novel_title_snapshot": str(row["novel_title_snapshot"]),
            "platform_name_snapshot": str(row["platform_name_snapshot"]),
            "promo_code_snapshot": str(row["promo_code_snapshot"]),
            "voice_profile": str(row["voice_profile"]),
            "subtitle_style_id": str(row["subtitle_style_id"]),
            "outro_style_id": str(row["outro_style_id"]),
            "status": str(row["status"]),
            "created_by_user_id": row["created_by_user_id"],
            "metadata": _json_load(row["metadata_json"], {}),
            "row_version": int(row["row_version"]),
            "episodes": [
                {
                    **self._episode_dict(episode),
                    "selection_ordinal": int(episode["selection_ordinal"]),
                }
                for episode in episode_rows
            ],
            "episode_ids": [str(episode["id"]) for episode in episode_rows],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _creative_line_count(value: Mapping[str, Any], fallback: Any = None) -> int:
        incoming = value.get(
            "creative_line_count",
            value.get("variant_count", value.get("creative_lines", fallback)),
        )
        return _positive_int(
            incoming,
            "creative_line_count",
            minimum=1,
            # This is SQLite's physical integer boundary, not a product cap.
            maximum=SQLITE_MAX_INTEGER,
        )

    def save_draft(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a production draft.

        The novel/platform/code snapshots are frozen at creation.  A publishing
        account is optional, so a draft may remain visibly unassigned.
        """

        if not isinstance(value, Mapping):
            raise CatalogValidationError("draft payload must be an object")
        draft_id = _optional_text(value.get("id"), maximum=120)
        now = utc_now()
        with self._write_connection() as connection:
            row: sqlite3.Row | None = None
            if draft_id:
                row = self._require_row(
                    connection,
                    "SELECT * FROM production_drafts WHERE id = ? AND site_id = ?",
                    (draft_id, self.site_id),
                    "draft",
                )

            if row is None:
                novel_id = _required_text(
                    value.get("novel_id"), "novel_id", maximum=120
                )
                binding_id = _required_text(
                    value.get("binding_id"), "binding_id", maximum=120
                )
                promo_code_id = _required_text(
                    value.get("promo_code_id"), "promo_code_id", maximum=120
                )
                relationship = self._require_row(
                    connection,
                    """
                    SELECT n.title AS novel_title, b.novel_id, b.id AS binding_id,
                           p.name AS platform_name, c.id AS promo_code_id, c.code AS promo_code
                    FROM novel_platform_bindings b
                    JOIN novels n ON n.id = b.novel_id
                    JOIN platforms p ON p.id = b.platform_id
                    JOIN promo_codes c ON c.binding_id = b.id
                    WHERE b.id = ? AND b.novel_id = ? AND c.id = ?
                      AND b.site_id = ?
                    """,
                    (binding_id, novel_id, promo_code_id, self.site_id),
                    "matching novel, binding, and promo code",
                )
                account_id = self._validated_publishing_account(
                    connection, value.get("publishing_account_id")
                )
                status = _optional_text(value.get("status"), maximum=30) or "draft"
                if status not in DRAFT_STATUSES:
                    raise CatalogValidationError("invalid draft status")
                draft_id = str(value.get("id") or _new_id())
                connection.execute(
                    """
                    INSERT INTO production_drafts(
                        id, site_id, novel_id, binding_id, promo_code_id,
                        publishing_account_id, creative_line_count,
                        novel_title_snapshot, platform_name_snapshot,
                        promo_code_snapshot, voice_profile, subtitle_style_id,
                        outro_style_id, status, created_by_user_id, metadata_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft_id,
                        self.site_id,
                        novel_id,
                        binding_id,
                        promo_code_id,
                        account_id,
                        self._creative_line_count(value),
                        str(relationship["novel_title"]),
                        str(relationship["platform_name"]),
                        str(relationship["promo_code"]),
                        _optional_text(value.get("voice_profile"), maximum=300),
                        _optional_text(value.get("subtitle_style_id"), maximum=300),
                        _optional_text(value.get("outro_style_id"), maximum=300),
                        status,
                        value.get("created_by_user_id") or actor_user_id or None,
                        _json_dump(_metadata(value.get("metadata"))),
                        now,
                        now,
                    ),
                )
                before = None
                action = "draft.created"
            else:
                self._check_version(row, self._expected_version(value))
                for immutable_field in ("novel_id", "binding_id", "promo_code_id"):
                    if immutable_field in value and str(value.get(immutable_field) or "") != str(
                        row[immutable_field]
                    ):
                        raise CatalogValidationError(
                            f"{immutable_field} is frozen; create another draft to change it"
                        )
                account_id = (
                    self._validated_publishing_account(
                        connection, value.get("publishing_account_id")
                    )
                    if "publishing_account_id" in value
                    else row["publishing_account_id"]
                )
                status = _optional_text(value.get("status", row["status"]), maximum=30)
                if status not in DRAFT_STATUSES:
                    raise CatalogValidationError("invalid draft status")
                before = self._draft_dict(connection, row)
                connection.execute(
                    """
                    UPDATE production_drafts SET publishing_account_id = ?,
                        creative_line_count = ?, voice_profile = ?,
                        subtitle_style_id = ?, outro_style_id = ?, status = ?,
                        metadata_json = ?, row_version = row_version + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        account_id,
                        self._creative_line_count(value, row["creative_line_count"]),
                        _optional_text(value.get("voice_profile", row["voice_profile"]), maximum=300),
                        _optional_text(value.get("subtitle_style_id", row["subtitle_style_id"]), maximum=300),
                        _optional_text(value.get("outro_style_id", row["outro_style_id"]), maximum=300),
                        status,
                        _json_dump(
                            _metadata(
                                value.get("metadata", _json_load(row["metadata_json"], {}))
                            )
                        ),
                        now,
                        draft_id,
                    ),
                )
                action = "draft.updated"

            if "episode_ids" in value:
                raw_episode_ids = value.get("episode_ids") or []
                if not isinstance(raw_episode_ids, Sequence) or isinstance(
                    raw_episode_ids, (str, bytes, bytearray)
                ):
                    raise CatalogValidationError("episode_ids must be an array")
                episode_ids: list[str] = []
                for raw_episode_id in raw_episode_ids:
                    episode_id = _required_text(
                        raw_episode_id, "episode_id", maximum=120
                    )
                    if episode_id in episode_ids:
                        raise CatalogValidationError("episode_ids cannot contain duplicates")
                    episode = self._require_row(
                        connection,
                        """
                        SELECT e.id, r.novel_id FROM episodes e
                        JOIN content_revisions r ON r.id = e.revision_id
                        WHERE e.id = ? AND r.site_id = ?
                        """,
                        (episode_id, self.site_id),
                        "episode",
                    )
                    draft_novel_id = str(
                        connection.execute(
                            "SELECT novel_id FROM production_drafts WHERE id = ?",
                            (draft_id,),
                        ).fetchone()[0]
                    )
                    if str(episode["novel_id"]) != draft_novel_id:
                        raise CatalogValidationError(
                            "every selected episode must belong to the draft novel"
                        )
                    episode_ids.append(episode_id)
                connection.execute(
                    "DELETE FROM draft_episodes WHERE draft_id = ?", (draft_id,)
                )
                connection.executemany(
                    "INSERT INTO draft_episodes(draft_id, episode_id, ordinal) VALUES (?, ?, ?)",
                    [
                        (draft_id, episode_id, index)
                        for index, episode_id in enumerate(episode_ids, start=1)
                    ],
                )

            updated = self._require_row(
                connection,
                "SELECT * FROM production_drafts WHERE id = ?",
                (draft_id,),
                "draft",
            )
            result = self._draft_dict(connection, updated)
            self._audit(
                connection,
                action=action,
                entity_type="production_draft",
                entity_id=draft_id,
                actor_user_id=actor_user_id,
                before=before,
                after={
                    key: result[key]
                    for key in (
                        "novel_id",
                        "binding_id",
                        "promo_code_id",
                        "publishing_account_id",
                        "creative_line_count",
                        "novel_title_snapshot",
                        "platform_name_snapshot",
                        "promo_code_snapshot",
                        "status",
                        "episode_ids",
                    )
                },
            )
            return result

    def _validated_publishing_account(
        self, connection: sqlite3.Connection, value: Any
    ) -> str | None:
        if value in (None, ""):
            return None
        account_id = _required_text(value, "publishing_account_id", maximum=120)
        self._require_row(
            connection,
            "SELECT id FROM publishing_accounts WHERE id = ? AND site_id = ?",
            (account_id, self.site_id),
            "publishing account",
        )
        return account_id

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        draft_id = _required_text(draft_id, "draft_id", maximum=120)
        with self._read_connection() as connection:
            row = self._require_row(
                connection,
                "SELECT * FROM production_drafts WHERE id = ? AND site_id = ?",
                (draft_id, self.site_id),
                "draft",
            )
            return self._draft_dict(connection, row)

    def list_drafts(
        self,
        *,
        status: str = "",
        novel_id: str = "",
        created_by_user_id: str | None = None,
        publishing_account_id: str | None = None,
        only_unassigned: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = _positive_int(limit, "limit", minimum=1, maximum=500)
        offset = _positive_int(offset, "offset", minimum=0, maximum=10_000_000)
        filters = ["site_id = ?"]
        parameters: list[Any] = [self.site_id]
        if status:
            if status not in DRAFT_STATUSES:
                raise CatalogValidationError("invalid draft status")
            filters.append("status = ?")
            parameters.append(status)
        if novel_id:
            filters.append("novel_id = ?")
            parameters.append(novel_id)
        if created_by_user_id is not None:
            filters.append("created_by_user_id = ?")
            parameters.append(str(created_by_user_id))
        if publishing_account_id:
            filters.append("publishing_account_id = ?")
            parameters.append(publishing_account_id)
        if only_unassigned:
            filters.append("publishing_account_id IS NULL")
        where = " AND ".join(filters)
        with self._read_connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM production_drafts WHERE {where}", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM production_drafts WHERE {where}
                ORDER BY updated_at DESC LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            return {
                "items": [self._draft_dict(connection, row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def find_duplicate_draft_configuration(
        self,
        novel_id: str,
        configuration_fingerprint: str,
        *,
        exclude_draft_id: str = "",
    ) -> dict[str, Any]:
        """Return the newest matching draft without blocking a new batch."""

        target_novel_id = _required_text(novel_id, "novel_id", maximum=120)
        fingerprint = str(configuration_fingerprint or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise CatalogValidationError(
                "configuration_fingerprint must be a SHA-256 hex digest"
            )
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM production_drafts
                WHERE site_id = ? AND novel_id = ? AND id != ?
                  AND json_extract(metadata_json, '$.configuration_fingerprint') = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (
                    self.site_id,
                    target_novel_id,
                    str(exclude_draft_id or ""),
                    fingerprint,
                ),
            ).fetchone()
            if row is None:
                return {}
            return {
                "id": str(row["id"]),
                "status": str(row["status"]),
                "updated_at": str(row["updated_at"]),
            }

    def last_successful_voice(self, novel_id: str) -> dict[str, str]:
        """Return the voice used by the newest successful production, if any."""

        target_novel_id = _required_text(novel_id, "novel_id", maximum=120)
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT r.metadata_json, d.metadata_json AS draft_metadata_json
                FROM production_records r
                LEFT JOIN production_drafts d ON d.id = r.draft_id
                WHERE r.site_id = ? AND r.novel_id = ? AND r.status = 'completed'
                ORDER BY COALESCE(r.completed_at, r.updated_at) DESC, r.id DESC
                LIMIT 100
                """,
                (self.site_id, target_novel_id),
            ).fetchall()
        for row in rows:
            metadata = _json_load(row["metadata_json"], {})
            if not isinstance(metadata, Mapping) or metadata.get("lease_gate"):
                continue
            snapshot = metadata.get("job_snapshot")
            if not isinstance(snapshot, Mapping):
                snapshot = {}
            draft_metadata = _json_load(row["draft_metadata_json"], {})
            draft_voice = (
                draft_metadata.get("voice")
                if isinstance(draft_metadata, Mapping)
                and isinstance(draft_metadata.get("voice"), Mapping)
                else {}
            )
            provider = str(
                snapshot.get("locked_voice_provider")
                or draft_voice.get("provider")
                or ""
            ).strip()
            voice_id = str(
                snapshot.get("locked_voice_id")
                or draft_voice.get("voice_id")
                or ""
            ).strip()
            if provider and voice_id:
                return {
                    "provider": provider,
                    "voice_id": voice_id,
                    "label": str(draft_voice.get("label") or voice_id).strip(),
                    "profile": str(draft_voice.get("profile") or "").strip(),
                }
        return {}

    @staticmethod
    def _record_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "batch_id": row["batch_id"],
            "draft_id": row["draft_id"],
            "job_id": row["job_id"] or "",
            "novel_id": str(row["novel_id"]),
            "binding_id": str(row["binding_id"]),
            "episode_id": row["episode_id"],
            "publishing_account_id": row["publishing_account_id"],
            "created_by_user_id": row["created_by_user_id"],
            "device_id": str(row["device_id"]),
            "variant_index": int(row["variant_index"]),
            "logical_task_key": str(row["logical_task_key"] or ""),
            "current_attempt": int(row["current_attempt"] or 1),
            "novel_title_snapshot": str(row["novel_title_snapshot"]),
            "platform_name_snapshot": str(row["platform_name_snapshot"]),
            "promo_code_snapshot": str(row["promo_code_snapshot"]),
            "status": str(row["status"]),
            "progress": float(row["progress"]),
            "output_path": str(row["output_path"]),
            "error_message": str(row["error_message"]),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "lease_owner_device": str(row["lease_owner_device"] or ""),
            "lease_expires_at": row["lease_expires_at"],
            "heartbeat_at": row["heartbeat_at"],
            "cancel_requested_at": row["cancel_requested_at"],
            "cancelled_at": row["cancelled_at"],
            "cancel_requested_by_user_id": row["cancel_requested_by_user_id"],
            "cancellation_reason": str(row["cancellation_reason"] or ""),
            "archived": bool(row["archived"]),
            "archived_at": row["archived_at"],
            "archived_by_user_id": row["archived_by_user_id"],
            "trashed": bool(row["trashed_at"]),
            "trashed_at": row["trashed_at"],
            "trashed_by_user_id": row["trashed_by_user_id"],
            "metadata": _json_load(row["metadata_json"], {}),
            "row_version": int(row["row_version"]),
            "artifact_count": int(row["artifact_count"] or 0)
            if "artifact_count" in row.keys()
            else None,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "record_id": str(row["record_id"]),
            "attempt_no": int(row["attempt_no"]),
            "job_id": str(row["job_id"] or ""),
            "device_id": str(row["device_id"] or ""),
            "status": str(row["status"]),
            "progress": float(row["progress"]),
            "output_path": str(row["output_path"] or ""),
            "error_message": str(row["error_message"] or ""),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "cancel_requested_at": row["cancel_requested_at"],
            "cancelled_at": row["cancelled_at"],
            "cancellation_reason": str(row["cancellation_reason"] or ""),
            "metadata": _json_load(row["metadata_json"], {}),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _sync_record_attempt(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> None:
        """Mirror the current task state without overwriting earlier attempts."""

        connection.execute(
            """
            INSERT INTO production_record_attempts(
                id, site_id, record_id, attempt_no, job_id, device_id,
                status, progress, output_path, error_message, started_at,
                completed_at, cancel_requested_at, cancelled_at,
                cancellation_reason, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_id, attempt_no) DO UPDATE SET
                job_id = excluded.job_id,
                device_id = excluded.device_id,
                status = excluded.status,
                progress = excluded.progress,
                output_path = excluded.output_path,
                error_message = excluded.error_message,
                started_at = excluded.started_at,
                completed_at = excluded.completed_at,
                cancel_requested_at = excluded.cancel_requested_at,
                cancelled_at = excluded.cancelled_at,
                cancellation_reason = excluded.cancellation_reason,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                _new_id(),
                row["site_id"],
                row["id"],
                int(row["current_attempt"] or 1),
                str(row["job_id"] or ""),
                str(row["device_id"] or ""),
                str(row["status"]),
                float(row["progress"]),
                str(row["output_path"] or ""),
                str(row["error_message"] or ""),
                row["started_at"],
                row["completed_at"],
                row["cancel_requested_at"],
                row["cancelled_at"],
                str(row["cancellation_reason"] or ""),
                str(row["metadata_json"] or "{}"),
                str(row["created_at"]),
                str(row["updated_at"]),
            ),
        )

    def _ensure_production_batch(
        self,
        connection: sqlite3.Connection,
        *,
        record_id: str,
        value: Mapping[str, Any],
        metadata: Mapping[str, Any],
        draft_id: str,
        novel_id: str,
        binding_id: str,
        publishing_account_id: str | None,
        created_by_user_id: str | None,
        device_id: str,
        now: str,
    ) -> str | None:
        if bool(metadata.get("lease_gate")):
            return None
        requested = _optional_text(value.get("batch_id"), maximum=120)
        if requested:
            batch = self._require_row(
                connection,
                "SELECT * FROM production_batches WHERE id = ? AND site_id = ?",
                (requested, self.site_id),
                "production batch",
            )
            if str(batch["novel_id"]) != novel_id or str(batch["binding_id"]) != binding_id:
                raise CatalogValidationError(
                    "production batch must belong to the record novel and binding"
                )
            return requested
        external_run_id = _optional_text(
            metadata.get("production_run_id"), maximum=300
        ) or f"record:{record_id}"
        existing = connection.execute(
            "SELECT * FROM production_batches WHERE site_id = ? AND external_run_id = ?",
            (self.site_id, external_run_id),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["novel_id"]) != novel_id
                or str(existing["binding_id"]) != binding_id
            ):
                raise CatalogConflictError(
                    "production_run_id already belongs to another novel or platform"
                )
            return str(existing["id"])
        batch_id = _new_id()
        connection.execute(
            """
            INSERT INTO production_batches(
                id, site_id, external_run_id, draft_id, novel_id, binding_id,
                publishing_account_id, created_by_user_id, device_id, label,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                self.site_id,
                external_run_id,
                draft_id or None,
                novel_id,
                binding_id,
                publishing_account_id,
                created_by_user_id,
                device_id,
                _optional_text(metadata.get("batch_label"), maximum=500),
                _json_dump(
                    {
                        "production_run_id": external_run_id,
                        "source": "production_record",
                    }
                ),
                now,
                now,
            ),
        )
        return batch_id

    @staticmethod
    def _canonical_record_metadata(
        metadata: Mapping[str, Any],
        *,
        batch_id: str | None,
        record_id: str,
    ) -> dict[str, Any]:
        """Bind an embedded queue snapshot to its durable ledger identity.

        Render jobs are planned before the catalog creates a production batch,
        so their initial ``batch_id`` is only the draft id.  A persisted
        ``job_snapshot`` must never retain that provisional value: streamed
        queues and restart recovery load it after the in-memory planner has
        gone away.
        """

        normalized = dict(metadata)
        snapshot = normalized.get("job_snapshot")
        if isinstance(snapshot, Mapping):
            durable_snapshot = dict(snapshot)
            if batch_id:
                durable_snapshot["batch_id"] = str(batch_id)
            durable_snapshot["production_record_id"] = str(record_id)
            normalized["job_snapshot"] = durable_snapshot
        return normalized

    def save_production_records_bulk(
        self,
        values: Sequence[Mapping[str, Any]],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Create/update a bounded page of records in one SQLite commit."""

        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise CatalogValidationError("values must be a list of record objects")
        if not values:
            return {"items": [], "count": 0}
        if len(values) > 500:
            raise CatalogValidationError("a bulk record page cannot exceed 500 items")
        normalized = []
        for value in values:
            if not isinstance(value, Mapping):
                raise CatalogValidationError("record payload must be an object")
            normalized.append(dict(value))
        previous_connection = getattr(self._transaction_local, "connection", None)
        with self._write_connection() as connection:
            self._transaction_local.connection = connection
            try:
                items = [
                    self.save_production_record(
                        value,
                        actor_user_id=actor_user_id,
                    )
                    for value in normalized
                ]
            finally:
                self._transaction_local.connection = previous_connection
        return {"items": items, "count": len(items)}

    def save_production_record(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise CatalogValidationError("record payload must be an object")
        record_id = _optional_text(value.get("id"), maximum=120)
        now = utc_now()
        try:
            with self._write_connection() as connection:
                row: sqlite3.Row | None = None
                if record_id:
                    row = self._require_row(
                        connection,
                        "SELECT * FROM production_records WHERE id = ? AND site_id = ?",
                        (record_id, self.site_id),
                        "production record",
                    )
                if row is None:
                    draft_id = _optional_text(value.get("draft_id"), maximum=120)
                    if draft_id:
                        draft = self._require_row(
                            connection,
                            "SELECT * FROM production_drafts WHERE id = ? AND site_id = ?",
                            (draft_id, self.site_id),
                            "draft",
                        )
                        novel_id = str(draft["novel_id"])
                        binding_id = str(draft["binding_id"])
                        publishing_account_id = draft["publishing_account_id"]
                        novel_title_snapshot = str(draft["novel_title_snapshot"])
                        platform_name_snapshot = str(draft["platform_name_snapshot"])
                        promo_code_snapshot = str(draft["promo_code_snapshot"])
                        creative_line_count = int(draft["creative_line_count"])
                    else:
                        draft = None
                        novel_id = _required_text(
                            value.get("novel_id"), "novel_id", maximum=120
                        )
                        binding_id = _required_text(
                            value.get("binding_id"), "binding_id", maximum=120
                        )
                        relationship = self._require_row(
                            connection,
                            """
                            SELECT n.title AS novel_title, p.name AS platform_name
                            FROM novel_platform_bindings b
                            JOIN novels n ON n.id = b.novel_id
                            JOIN platforms p ON p.id = b.platform_id
                            WHERE b.id = ? AND b.novel_id = ? AND b.site_id = ?
                            """,
                            (binding_id, novel_id, self.site_id),
                            "matching novel and binding",
                        )
                        publishing_account_id = self._validated_publishing_account(
                            connection, value.get("publishing_account_id")
                        )
                        novel_title_snapshot = _optional_text(
                            value.get("novel_title_snapshot"), maximum=500
                        ) or str(relationship["novel_title"])
                        platform_name_snapshot = _optional_text(
                            value.get("platform_name_snapshot"), maximum=500
                        ) or str(relationship["platform_name"])
                        promo_code_snapshot = _required_text(
                            value.get("promo_code_snapshot"),
                            "promo_code_snapshot",
                            maximum=200,
                        )
                        creative_line_count = SQLITE_MAX_INTEGER
                    variant_index = _positive_int(
                        value.get("variant_index", 1),
                        "variant_index",
                        minimum=1,
                        maximum=creative_line_count,
                    )
                    episode_id = _optional_text(value.get("episode_id"), maximum=120) or None
                    if episode_id:
                        episode = self._require_row(
                            connection,
                            """
                            SELECT e.id, r.novel_id FROM episodes e
                            JOIN content_revisions r ON r.id = e.revision_id
                            WHERE e.id = ? AND r.site_id = ?
                            """,
                            (episode_id, self.site_id),
                            "episode",
                        )
                        if str(episode["novel_id"]) != novel_id:
                            raise CatalogValidationError(
                                "record episode must belong to the record novel"
                            )
                        if draft is not None:
                            selected_count = int(
                                connection.execute(
                                    "SELECT COUNT(*) FROM draft_episodes WHERE draft_id = ?",
                                    (draft_id,),
                                ).fetchone()[0]
                            )
                            selected = connection.execute(
                                "SELECT 1 FROM draft_episodes WHERE draft_id = ? AND episode_id = ?",
                                (draft_id, episode_id),
                            ).fetchone()
                            if selected_count and selected is None:
                                raise CatalogValidationError(
                                    "record episode is not selected by the draft"
                                )
                    status = _optional_text(value.get("status"), maximum=40) or "queued"
                    if status not in RECORD_STATUSES:
                        raise CatalogValidationError("invalid record status")
                    progress = self._progress(value.get("progress", 0))
                    record_id = str(value.get("id") or _new_id())
                    metadata = _metadata(value.get("metadata"))
                    created_by_user_id = value.get("created_by_user_id") or actor_user_id or None
                    device_id = _optional_text(value.get("device_id"), maximum=300)
                    batch_id = self._ensure_production_batch(
                        connection,
                        record_id=record_id,
                        value=value,
                        metadata=metadata,
                        draft_id=draft_id,
                        novel_id=novel_id,
                        binding_id=binding_id,
                        publishing_account_id=publishing_account_id,
                        created_by_user_id=created_by_user_id,
                        device_id=device_id,
                        now=now,
                    )
                    metadata = self._canonical_record_metadata(
                        metadata,
                        batch_id=batch_id,
                        record_id=record_id,
                    )
                    logical_task_key = _optional_text(
                        value.get("logical_task_key")
                        or metadata.get("logical_task_key"),
                        maximum=500,
                    ) or f"{episode_id or 'story'}:{variant_index}"
                    completed_at = value.get("completed_at") or (
                        now
                        if status in {"completed", "failed", "skipped", "cancelled"}
                        else None
                    )
                    cancel_requested_at = value.get("cancel_requested_at")
                    cancelled_at = value.get("cancelled_at") or (
                        now if status == "cancelled" else None
                    )
                    connection.execute(
                        """
                        INSERT INTO production_records(
                            id, site_id, batch_id, draft_id, job_id, novel_id, binding_id,
                            episode_id, publishing_account_id, created_by_user_id,
                            device_id, variant_index, logical_task_key, current_attempt,
                            novel_title_snapshot,
                            platform_name_snapshot, promo_code_snapshot, status,
                            progress, output_path, error_message, started_at,
                            completed_at, cancel_requested_at, cancelled_at,
                            cancel_requested_by_user_id, cancellation_reason,
                            metadata_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record_id,
                            self.site_id,
                            batch_id,
                            draft_id or None,
                            _optional_text(value.get("job_id"), maximum=200) or None,
                            novel_id,
                            binding_id,
                            episode_id,
                            publishing_account_id,
                            created_by_user_id,
                            device_id,
                            variant_index,
                            logical_task_key,
                            1,
                            novel_title_snapshot,
                            platform_name_snapshot,
                            promo_code_snapshot,
                            status,
                            progress,
                            _optional_text(value.get("output_path"), maximum=4000),
                            _optional_text(value.get("error_message"), maximum=10000),
                            value.get("started_at") or (now if status == "running" else None),
                            completed_at,
                            cancel_requested_at,
                            cancelled_at,
                            value.get("cancel_requested_by_user_id") or None,
                            _optional_text(value.get("cancellation_reason"), maximum=2000),
                            _json_dump(metadata),
                            now,
                            now,
                        ),
                    )
                    before = None
                    action = "production_record.created"
                else:
                    self._check_version(row, self._expected_version(value))
                    expected_lease_owner = _optional_text(
                        value.get("expected_lease_owner_device"), maximum=300
                    )
                    if expected_lease_owner:
                        now_dt = datetime.now(timezone.utc)
                        current_owner = str(row["lease_owner_device"] or "")
                        if current_owner != expected_lease_owner:
                            raise CatalogConflictError(
                                "production record lease belongs to another device"
                            )
                        if not self._lease_is_active(row, now_dt):
                            raise CatalogConflictError(
                                "production record lease expired; claim it again"
                            )
                    status = _optional_text(value.get("status", row["status"]), maximum=40)
                    if status not in RECORD_STATUSES:
                        raise CatalogValidationError("invalid record status")
                    # A workstation can report one final progress callback
                    # after an immediate cancel request.  The durable ledger
                    # must not resurrect that task as running/completed.
                    cancellation_is_final = bool(
                        row["cancel_requested_at"] and row["cancelled_at"]
                    )
                    if cancellation_is_final and status != "cancelled":
                        status = "cancelled"
                    before = self._record_dict(row)
                    completed_at = (
                        value.get("completed_at")
                        if "completed_at" in value
                        else row["completed_at"]
                    )
                    if completed_at is None and status in {
                        "completed",
                        "failed",
                        "skipped",
                        "cancelled",
                    }:
                        completed_at = now
                    started_at = (
                        value.get("started_at")
                        if "started_at" in value
                        else row["started_at"]
                    )
                    if started_at is None and status == "running":
                        started_at = now
                    cancel_requested_at = (
                        value.get("cancel_requested_at")
                        if "cancel_requested_at" in value
                        else row["cancel_requested_at"]
                    )
                    cancelled_at = (
                        value.get("cancelled_at")
                        if "cancelled_at" in value
                        else row["cancelled_at"]
                    )
                    if cancelled_at is None and status == "cancelled":
                        cancelled_at = now
                    existing_metadata = _json_load(row["metadata_json"], {})
                    if "metadata" in value:
                        incoming_metadata = _metadata(value.get("metadata"))
                        # A streamed task snapshot is its restart/recovery
                        # source. Routine status projections must not erase it.
                        if (
                            "job_snapshot" not in incoming_metadata
                            and isinstance(existing_metadata, Mapping)
                            and isinstance(
                                existing_metadata.get("job_snapshot"), Mapping
                            )
                        ):
                            incoming_metadata["job_snapshot"] = dict(
                                existing_metadata["job_snapshot"]
                            )
                    else:
                        incoming_metadata = _metadata(existing_metadata)
                    updated_metadata = self._canonical_record_metadata(
                        incoming_metadata,
                        batch_id=row["batch_id"],
                        record_id=record_id,
                    )
                    terminal_status = status in {
                        "completed",
                        "failed",
                        "skipped",
                        "interrupted",
                        "cancelled",
                    }
                    connection.execute(
                        """
                        UPDATE production_records SET job_id = ?, device_id = ?, status = ?,
                            progress = ?, output_path = ?, error_message = ?,
                            started_at = ?, completed_at = ?, cancel_requested_at = ?,
                            cancelled_at = ?, cancel_requested_by_user_id = ?,
                            cancellation_reason = ?, metadata_json = ?,
                            lease_owner_device = ?, lease_expires_at = ?, heartbeat_at = ?,
                            row_version = row_version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            _optional_text(value.get("job_id", row["job_id"]), maximum=200)
                            or None,
                            _optional_text(value.get("device_id", row["device_id"]), maximum=300),
                            status,
                            self._progress(
                                row["progress"]
                                if cancellation_is_final
                                else value.get("progress", row["progress"])
                            ),
                            _optional_text(
                                row["output_path"]
                                if cancellation_is_final
                                else value.get("output_path", row["output_path"]),
                                maximum=4000,
                            ),
                            _optional_text(
                                row["error_message"]
                                if cancellation_is_final
                                else value.get("error_message", row["error_message"]),
                                maximum=10000,
                            ),
                            started_at,
                            completed_at,
                            cancel_requested_at,
                            cancelled_at,
                            value.get(
                                "cancel_requested_by_user_id",
                                row["cancel_requested_by_user_id"],
                            ),
                            _optional_text(
                                value.get("cancellation_reason", row["cancellation_reason"]),
                                maximum=2000,
                            ),
                            _json_dump(updated_metadata),
                            "" if terminal_status else row["lease_owner_device"],
                            None if terminal_status else row["lease_expires_at"],
                            None if terminal_status else row["heartbeat_at"],
                            now,
                            record_id,
                        ),
                    )
                    action = "production_record.updated"
                updated = self._require_row(
                    connection,
                    """
                    SELECT r.*, (SELECT COUNT(*) FROM artifacts a WHERE a.record_id = r.id) AS artifact_count
                    FROM production_records r WHERE r.id = ?
                    """,
                    (record_id,),
                    "production record",
                )
                self._sync_record_attempt(connection, updated)
                result = self._record_dict(updated)
                self._audit(
                    connection,
                    action=action,
                    entity_type="production_record",
                    entity_id=record_id,
                    actor_user_id=actor_user_id,
                    before=before,
                    after={
                        key: result[key]
                        for key in (
                            "draft_id",
                            "job_id",
                            "novel_id",
                            "binding_id",
                            "episode_id",
                            "publishing_account_id",
                            "variant_index",
                            "promo_code_snapshot",
                            "status",
                            "progress",
                            "output_path",
                            "error_message",
                        )
                    },
                )
                return result
        except sqlite3.IntegrityError as error:
            if "job_id" in str(error).casefold() or "unique constraint" in str(error).casefold():
                raise CatalogConflictError("job_id already exists") from error
            raise

    @staticmethod
    def _progress(value: Any) -> float:
        try:
            progress = float(value)
        except (TypeError, ValueError) as error:
            raise CatalogValidationError("progress must be a number") from error
        if not 0 <= progress <= 1:
            raise CatalogValidationError("progress must be between 0 and 1")
        return progress

    def get_record(self, record_id: str) -> dict[str, Any]:
        record_id = _required_text(record_id, "record_id", maximum=120)
        with self._read_connection() as connection:
            row = self._require_row(
                connection,
                """
                SELECT r.*, (SELECT COUNT(*) FROM artifacts a WHERE a.record_id = r.id) AS artifact_count
                FROM production_records r WHERE r.id = ? AND r.site_id = ?
                """,
                (record_id, self.site_id),
                "production record",
            )
            result = self._record_dict(row)
            artifact_rows = connection.execute(
                "SELECT * FROM artifacts WHERE record_id = ? ORDER BY created_at",
                (record_id,),
            ).fetchall()
            result["artifacts"] = [self._artifact_dict(item) for item in artifact_rows]
            attempt_rows = connection.execute(
                """
                SELECT * FROM production_record_attempts
                WHERE record_id = ? ORDER BY attempt_no DESC
                """,
                (record_id,),
            ).fetchall()
            result["attempts"] = [self._attempt_dict(item) for item in attempt_rows]
            return result

    def get_record_by_job_id(self, job_id: str) -> dict[str, Any]:
        """Resolve the canonical production record for queue authorization."""

        job_id = _required_text(job_id, "job_id", maximum=200)
        with self._read_connection() as connection:
            row = self._require_row(
                connection,
                """
                SELECT r.*,
                    (SELECT COUNT(*) FROM artifacts a WHERE a.record_id = r.id) AS artifact_count
                FROM production_records r
                WHERE r.site_id = ? AND r.job_id = ?
                """,
                (self.site_id, job_id),
                "production record",
            )
            return self._record_dict(row)

    def list_records(
        self,
        *,
        status: str = "",
        novel_id: str = "",
        publishing_account_id: str | None = None,
        created_by_user_id: str | None = None,
        batch_id: str = "",
        device_id: str = "",
        archived: bool | None = None,
        trashed: bool | None = False,
        created_from: str = "",
        created_to: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = _positive_int(limit, "limit", minimum=1, maximum=500)
        offset = _positive_int(
            offset,
            "offset",
            minimum=0,
            maximum=SQLITE_MAX_INTEGER,
        )
        filters = ["r.site_id = ?"]
        parameters: list[Any] = [self.site_id]
        if status:
            if status not in RECORD_STATUSES:
                raise CatalogValidationError("invalid record status")
            filters.append("r.status = ?")
            parameters.append(status)
        if novel_id:
            filters.append("r.novel_id = ?")
            parameters.append(novel_id)
        if publishing_account_id:
            filters.append("r.publishing_account_id = ?")
            parameters.append(publishing_account_id)
        if created_by_user_id:
            filters.append("r.created_by_user_id = ?")
            parameters.append(created_by_user_id)
        if batch_id:
            filters.append("r.batch_id = ?")
            parameters.append(batch_id)
        if device_id:
            filters.append("r.device_id = ?")
            parameters.append(device_id)
        if archived is not None:
            filters.append("r.archived = ?")
            parameters.append(int(bool(archived)))
        if trashed is not None:
            filters.append("r.trashed_at IS NOT NULL" if trashed else "r.trashed_at IS NULL")
        if created_from:
            filters.append("r.created_at >= ?")
            parameters.append(_required_text(created_from, "created_from", maximum=80))
        if created_to:
            filters.append("r.created_at <= ?")
            parameters.append(_required_text(created_to, "created_to", maximum=80))
        where = " AND ".join(filters)
        with self._read_connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM production_records r WHERE {where}",
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT r.*, (SELECT COUNT(*) FROM artifacts a WHERE a.record_id = r.id) AS artifact_count
                FROM production_records r WHERE {where}
                ORDER BY r.created_at DESC, r.rowid DESC LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            return {
                "items": [self._record_dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def find_active_draft_gate(
        self,
        draft_id: str,
        *,
        active_at: str,
        created_by_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Find the live planning gate for a draft without a page-size race.

        Gate lookup used to scan the newest 500 production records.  A busy
        installation could therefore create a second gate for an older draft.
        This indexed/single-row query is independent of ledger size.
        """

        draft_id = _required_text(draft_id, "draft_id", maximum=120)
        active_at = _required_text(active_at, "active_at", maximum=80)
        filters = [
            "r.site_id = ?",
            "r.draft_id = ?",
            "COALESCE(r.job_id, '') = ''",
            "json_extract(r.metadata_json, '$.lease_gate') = 1",
            "COALESCE(r.lease_owner_device, '') <> ''",
            "r.lease_expires_at > ?",
        ]
        parameters: list[Any] = [self.site_id, draft_id, active_at]
        if created_by_user_id is not None:
            filters.append("r.created_by_user_id = ?")
            parameters.append(str(created_by_user_id))
        with self._read_connection() as connection:
            row = connection.execute(
                f"""
                SELECT r.*,
                    (SELECT COUNT(*) FROM artifacts a WHERE a.record_id = r.id) AS artifact_count
                FROM production_records r
                WHERE {' AND '.join(filters)}
                ORDER BY r.updated_at DESC, r.rowid DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            return {"item": self._record_dict(row) if row is not None else None}

    def list_reconciliation_records(
        self,
        device_id: str,
        *,
        created_by_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Return every unfinished/leased record for one render workstation.

        Startup reconciliation is intentionally unpaginated: the predicate is
        narrow and must not strand record 501 of a large durable batch.
        """

        device_id = _required_text(device_id, "device_id", maximum=300)
        filters = [
            "r.site_id = ?",
            "r.device_id = ?",
            "(" 
            "r.status IN ('queued','preflight','sample_ready','awaiting_approval','running') "
            "OR r.lease_owner_device = ? "
            "OR (COALESCE(r.job_id, '') = '' AND json_extract(r.metadata_json, '$.lease_gate') = 1)"
            ")",
        ]
        parameters: list[Any] = [self.site_id, device_id, device_id]
        if created_by_user_id is not None:
            filters.append("r.created_by_user_id = ?")
            parameters.append(str(created_by_user_id))
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT r.*,
                    (SELECT COUNT(*) FROM artifacts a WHERE a.record_id = r.id) AS artifact_count
                FROM production_records r
                WHERE {' AND '.join(filters)}
                ORDER BY r.created_at ASC, r.rowid ASC
                """,
                parameters,
            ).fetchall()
            return {"items": [self._record_dict(row) for row in rows], "total": len(rows)}

    def get_production_batch_summaries(
        self,
        batch_ids: Sequence[str],
        *,
        created_by_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate complete durable batch state in one SQL statement."""

        if not isinstance(batch_ids, Sequence) or isinstance(batch_ids, (str, bytes)):
            raise CatalogValidationError("batch_ids must be a list")
        normalized = list(
            dict.fromkeys(
                _required_text(item, "batch_id", maximum=120) for item in batch_ids
            )
        )
        if not normalized:
            return {"items": {}}
        if len(normalized) > 500:
            raise CatalogValidationError("batch_ids cannot contain more than 500 items")
        placeholders = ",".join("?" for _item in normalized)
        filters = [
            "r.site_id = ?",
            f"r.batch_id IN ({placeholders})",
            # Gate rows may be bound to a durable batch for lifecycle safety,
            # but they are not video tasks and must never affect totals.
            "COALESCE(r.job_id, '') <> ''",
        ]
        parameters: list[Any] = [self.site_id, *normalized]
        if created_by_user_id is not None:
            filters.append("r.created_by_user_id = ?")
            parameters.append(str(created_by_user_id))
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT r.batch_id,
                    COUNT(*) AS total,
                    SUM(CASE WHEN r.status IN (
                        'queued','preflight','sample_ready','awaiting_approval','running'
                    ) THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN r.status = 'queued' THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN r.status IN ('preflight','running')
                        THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN r.status IN ('sample_ready','awaiting_approval')
                        THEN 1 ELSE 0 END) AS approval,
                    SUM(CASE WHEN r.status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN r.status IN ('failed','skipped')
                        THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN r.status = 'interrupted' THEN 1 ELSE 0 END) AS interrupted,
                    SUM(CASE WHEN r.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                    AVG(CASE WHEN r.status IN (
                        'completed','failed','skipped','interrupted','cancelled'
                    ) THEN 1.0 ELSE MAX(0.0, MIN(1.0, r.progress)) END) AS overall_progress
                FROM production_records r
                WHERE {' AND '.join(filters)}
                GROUP BY r.batch_id
                """,
                parameters,
            ).fetchall()
        items: dict[str, dict[str, Any]] = {}
        for row in rows:
            active = int(row["active"] or 0)
            batch_id = str(row["batch_id"])
            items[batch_id] = {
                "batch_id": batch_id,
                "total": int(row["total"] or 0),
                "active": active,
                "unfinished": active,
                "queued": int(row["queued"] or 0),
                "running": int(row["running"] or 0),
                "approval": int(row["approval"] or 0),
                "completed": int(row["completed"] or 0),
                "failed": int(row["failed"] or 0),
                "interrupted": int(row["interrupted"] or 0),
                "cancelled": int(row["cancelled"] or 0),
                "overall_progress": float(row["overall_progress"] or 0.0),
            }
        return {"items": items}

    def bind_lease_gate_batch(
        self,
        record_id: str,
        batch_id: str,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Bind one planning gate to exactly one durable production batch."""

        record_id = _required_text(record_id, "record_id", maximum=120)
        batch_id = _required_text(batch_id, "batch_id", maximum=120)
        now = utc_now()
        with self._write_connection() as connection:
            row = self._require_row(
                connection,
                "SELECT * FROM production_records WHERE id = ? AND site_id = ?",
                (record_id, self.site_id),
                "production record",
            )
            metadata = _json_load(row["metadata_json"], {})
            if not bool(metadata.get("lease_gate")) or str(row["job_id"] or ""):
                raise CatalogValidationError("record is not a production lease gate")
            batch = self._require_row(
                connection,
                "SELECT * FROM production_batches WHERE id = ? AND site_id = ?",
                (batch_id, self.site_id),
                "production batch",
            )
            if str(batch["draft_id"] or "") != str(row["draft_id"] or ""):
                raise CatalogValidationError("lease gate and batch must belong to the same draft")
            existing_batch_id = str(metadata.get("durable_batch_id") or "")
            if existing_batch_id and existing_batch_id != batch_id:
                raise CatalogConflictError("lease gate is already bound to another batch")
            before = self._record_dict(row)
            metadata["durable_batch_id"] = batch_id
            connection.execute(
                """
                UPDATE production_records
                SET metadata_json = ?, row_version = row_version + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (_json_dump(metadata), now, record_id),
            )
            updated = self._require_row(
                connection,
                """
                SELECT r.*,
                    (SELECT COUNT(*) FROM artifacts a WHERE a.record_id = r.id) AS artifact_count
                FROM production_records r WHERE r.id = ?
                """,
                (record_id,),
                "production record",
            )
            result = self._record_dict(updated)
            self._audit(
                connection,
                action="production_gate.batch_bound",
                entity_type="production_record",
                entity_id=record_id,
                actor_user_id=actor_user_id,
                before={
                    "durable_batch_id": (
                        (before.get("metadata") or {}).get("durable_batch_id")
                    )
                },
                after={"durable_batch_id": batch_id},
            )
            return result

    @staticmethod
    def _batch_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "external_run_id": str(row["external_run_id"] or ""),
            "draft_id": row["draft_id"],
            "novel_id": str(row["novel_id"]),
            "binding_id": str(row["binding_id"]),
            "publishing_account_id": row["publishing_account_id"],
            "created_by_user_id": row["created_by_user_id"],
            "device_id": str(row["device_id"] or ""),
            "label": str(row["label"] or ""),
            "metadata": _json_load(row["metadata_json"], {}),
            "archived": bool(row["archived"]),
            "archived_at": row["archived_at"],
            "trashed": bool(row["trashed_at"]),
            "trashed_at": row["trashed_at"],
            "trashed_by_user_id": row["trashed_by_user_id"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _status_filter_values(status: str) -> tuple[str, ...]:
        normalized = str(status or "").strip().casefold()
        categories = {
            "active": (
                "queued",
                "preflight",
                "sample_ready",
                "awaiting_approval",
                "running",
            ),
            "success": ("completed",),
            "completed": ("completed",),
            "failed": ("failed", "skipped", "interrupted"),
            "cancelled": ("cancelled",),
        }
        if not normalized:
            return ()
        values = categories.get(normalized, (normalized,))
        if any(item not in RECORD_STATUSES for item in values):
            raise CatalogValidationError("invalid record status")
        return values

    def list_record_groups(
        self,
        *,
        status: str = "",
        novel_id: str = "",
        batch_id: str = "",
        created_by_user_id: str | None = None,
        device_id: str = "",
        created_from: str = "",
        created_to: str = "",
        archived: bool | None = None,
        trashed: bool | None = False,
        limit: int = 5000,
    ) -> dict[str, Any]:
        """Return Novel -> Batch -> logical task -> attempt history."""

        limit = _positive_int(limit, "limit", minimum=1, maximum=50_000)
        filters = ["r.site_id = ?"]
        parameters: list[Any] = [self.site_id]
        statuses = self._status_filter_values(status)
        if statuses:
            placeholders = ",".join("?" for _item in statuses)
            filters.append(f"r.status IN ({placeholders})")
            parameters.extend(statuses)
        if novel_id:
            filters.append("r.novel_id = ?")
            parameters.append(_required_text(novel_id, "novel_id", maximum=120))
        if batch_id:
            filters.append("r.batch_id = ?")
            parameters.append(_required_text(batch_id, "batch_id", maximum=120))
        if created_by_user_id:
            filters.append("r.created_by_user_id = ?")
            parameters.append(str(created_by_user_id))
        if device_id:
            filters.append("r.device_id = ?")
            parameters.append(_required_text(device_id, "device_id", maximum=300))
        if created_from:
            filters.append("r.created_at >= ?")
            parameters.append(_required_text(created_from, "created_from", maximum=80))
        if created_to:
            filters.append("r.created_at <= ?")
            parameters.append(_required_text(created_to, "created_to", maximum=80))
        if archived is not None:
            filters.append("r.archived = ?")
            parameters.append(int(bool(archived)))
        if trashed is not None:
            filters.append("r.trashed_at IS NOT NULL" if trashed else "r.trashed_at IS NULL")
        where = " AND ".join(filters)

        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT r.*,
                    (SELECT COUNT(*) FROM artifacts a WHERE a.record_id = r.id) AS artifact_count
                FROM production_records r
                WHERE {where}
                ORDER BY r.created_at DESC, r.variant_index, r.current_attempt DESC
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()
            if not rows:
                return {
                    "items": [],
                    "total_records": 0,
                    "summary": {
                        "active": 0,
                        "completed": 0,
                        "failed": 0,
                        "cancelled": 0,
                        "archived": 0,
                        "trashed": 0,
                    },
                    "facets": {"novels": [], "batches": [], "members": [], "devices": []},
                }
            record_ids = [str(row["id"]) for row in rows]
            placeholders = ",".join("?" for _item in record_ids)
            attempt_rows = connection.execute(
                f"""
                SELECT * FROM production_record_attempts
                WHERE record_id IN ({placeholders})
                ORDER BY record_id, attempt_no DESC
                """,
                record_ids,
            ).fetchall()
            attempts: dict[str, list[dict[str, Any]]] = {}
            for attempt_row in attempt_rows:
                attempts.setdefault(str(attempt_row["record_id"]), []).append(
                    self._attempt_dict(attempt_row)
                )
            batch_ids = sorted({str(row["batch_id"] or "") for row in rows if row["batch_id"]})
            batches: dict[str, dict[str, Any]] = {}
            if batch_ids:
                batch_placeholders = ",".join("?" for _item in batch_ids)
                for batch_row in connection.execute(
                    f"SELECT * FROM production_batches WHERE id IN ({batch_placeholders})",
                    batch_ids,
                ).fetchall():
                    batches[str(batch_row["id"])] = self._batch_dict(batch_row)
            user_rows = connection.execute(
                "SELECT id, username, display_name FROM software_users WHERE site_id = ?",
                (self.site_id,),
            ).fetchall()
            user_names = {
                str(row["id"]): str(row["display_name"] or row["username"])
                for row in user_rows
            }
            device_rows = connection.execute(
                "SELECT id, name FROM hub_devices WHERE site_id = ?",
                (self.site_id,),
            ).fetchall()
            device_names = {str(row["id"]): str(row["name"]) for row in device_rows}

            novels: dict[str, dict[str, Any]] = {}
            summary = {
                "active": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "archived": 0,
                "trashed": 0,
            }
            active_statuses = set(self._status_filter_values("active"))
            failed_statuses = set(self._status_filter_values("failed"))
            batch_groups: dict[str, dict[str, Any]] = {}
            for row in rows:
                record = self._record_dict(row)
                record["attempts"] = attempts.get(record["id"], [])
                record["member_name"] = user_names.get(
                    str(record.get("created_by_user_id") or ""), ""
                )
                record["device_name"] = device_names.get(
                    str(record.get("device_id") or ""),
                    str(record.get("device_id") or ""),
                )
                raw_status = str(record["status"])
                if raw_status in active_statuses:
                    summary["active"] += 1
                elif raw_status == "completed":
                    summary["completed"] += 1
                elif raw_status == "cancelled":
                    summary["cancelled"] += 1
                elif raw_status in failed_statuses:
                    summary["failed"] += 1
                if record["archived"]:
                    summary["archived"] += 1
                if record["trashed"]:
                    summary["trashed"] += 1

                novel = novels.setdefault(
                    record["novel_id"],
                    {
                        "novel_id": record["novel_id"],
                        "title": record["novel_title_snapshot"],
                        "batches": [],
                        "task_count": 0,
                    },
                )
                group_id = str(record.get("batch_id") or f"legacy:{record['id']}")
                batch = batch_groups.get(group_id)
                if batch is None:
                    batch = {
                        **batches.get(
                            group_id,
                            {
                                "id": group_id,
                                "external_run_id": "",
                                "label": "",
                                "created_at": record["created_at"],
                                "device_id": record["device_id"],
                                "created_by_user_id": record["created_by_user_id"],
                                "archived": record["archived"],
                                "trashed": record["trashed"],
                            },
                        ),
                        "tasks": [],
                        "status_counts": {
                            "active": 0,
                            "completed": 0,
                            "failed": 0,
                            "cancelled": 0,
                        },
                    }
                    batch["member_name"] = user_names.get(
                        str(batch.get("created_by_user_id") or ""), ""
                    )
                    batch["device_name"] = device_names.get(
                        str(batch.get("device_id") or ""),
                        str(batch.get("device_id") or ""),
                    )
                    batch_groups[group_id] = batch
                    novel["batches"].append(batch)
                batch["tasks"].append(record)
                novel["task_count"] += 1
                if raw_status in active_statuses:
                    batch["status_counts"]["active"] += 1
                elif raw_status == "completed":
                    batch["status_counts"]["completed"] += 1
                elif raw_status == "cancelled":
                    batch["status_counts"]["cancelled"] += 1
                elif raw_status in failed_statuses:
                    batch["status_counts"]["failed"] += 1

            facets = {
                "novels": [
                    {"id": item["novel_id"], "label": item["title"]}
                    for item in novels.values()
                ],
                "batches": [
                    {
                        "id": item["id"],
                        "label": item.get("label") or item.get("created_at") or item["id"],
                    }
                    for item in batch_groups.values()
                ],
                "members": [
                    {"id": key, "label": value}
                    for key, value in user_names.items()
                    if any(str(row["created_by_user_id"] or "") == key for row in rows)
                ],
                "devices": [
                    {
                        "id": key,
                        "label": device_names.get(key, key),
                    }
                    for key in sorted({str(row["device_id"] or "") for row in rows if row["device_id"]})
                ],
            }
            return {
                "items": list(novels.values()),
                "total_records": len(rows),
                "summary": summary,
                "facets": facets,
            }

    def begin_record_retry(
        self,
        record_id: str,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        record_id = _required_text(record_id, "record_id", maximum=120)
        with self._write_connection() as connection:
            row = self._record_row(connection, record_id)
            if str(row["status"]) not in {"failed", "cancelled", "interrupted", "skipped"}:
                raise CatalogConflictError("only failed, cancelled or interrupted records can be retried")
            if row["trashed_at"]:
                raise CatalogConflictError("trashed production records cannot be retried")
            self._sync_record_attempt(connection, row)
            now = utc_now()
            connection.execute(
                """
                UPDATE production_records
                SET current_attempt = current_attempt + 1,
                    status = 'queued', progress = 0, output_path = '',
                    error_message = '', started_at = NULL, completed_at = NULL,
                    cancel_requested_at = NULL, cancelled_at = NULL,
                    cancel_requested_by_user_id = NULL, cancellation_reason = '',
                    archived = 0, archived_at = NULL,
                    archived_by_user_id = NULL, archive_snapshot_json = '{}',
                    row_version = row_version + 1, updated_at = ?
                WHERE id = ? AND site_id = ?
                """,
                (now, record_id, self.site_id),
            )
            updated = self._record_row(connection, record_id)
            self._sync_record_attempt(connection, updated)
            result = self._record_dict(updated)
            self._audit(
                connection,
                action="production_record.retry_started",
                entity_type="production_record",
                entity_id=record_id,
                actor_user_id=actor_user_id,
                before={"attempt": int(row["current_attempt"]), "status": str(row["status"])},
                after={"attempt": result["current_attempt"], "status": "queued"},
            )
            return result

    def request_record_cancellation(
        self,
        record_ids: Sequence[str],
        *,
        reason: str = "",
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = list(dict.fromkeys(
            _required_text(item, "record_id", maximum=120) for item in record_ids
        ))
        if not normalized:
            raise CatalogValidationError("record_ids cannot be empty")
        now = utc_now()
        cancellation_reason = _optional_text(reason, maximum=2000)
        cancelled: list[str] = []
        ignored: list[str] = []
        with self._write_connection() as connection:
            for record_id in normalized:
                row = self._record_row(connection, record_id)
                if str(row["status"]) in {"completed", "failed", "skipped", "cancelled", "interrupted"}:
                    ignored.append(record_id)
                    continue
                connection.execute(
                    """
                    UPDATE production_records
                    SET status = 'cancelled', cancel_requested_at = ?,
                        cancelled_at = ?, cancel_requested_by_user_id = ?,
                        cancellation_reason = ?, completed_at = COALESCE(completed_at, ?),
                        lease_owner_device = '', lease_expires_at = NULL,
                        heartbeat_at = NULL, row_version = row_version + 1,
                        updated_at = ?
                    WHERE id = ? AND site_id = ?
                    """,
                    (
                        now,
                        now,
                        actor_user_id,
                        cancellation_reason,
                        now,
                        now,
                        record_id,
                        self.site_id,
                    ),
                )
                updated = self._record_row(connection, record_id)
                self._sync_record_attempt(connection, updated)
                self._audit(
                    connection,
                    action="production_record.cancelled",
                    entity_type="production_record",
                    entity_id=record_id,
                    actor_user_id=actor_user_id,
                    before={"status": str(row["status"])},
                    after={"status": "cancelled", "reason": cancellation_reason},
                )
                cancelled.append(record_id)
        return {"cancelled": cancelled, "ignored": ignored, "requested_at": now}

    def trash_production_records(
        self,
        record_ids: Sequence[str],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = list(dict.fromkeys(
            _required_text(item, "record_id", maximum=120) for item in record_ids
        ))
        if not normalized:
            raise CatalogValidationError("record_ids cannot be empty")
        terminal = {"completed", "failed", "skipped", "interrupted", "cancelled"}
        now = utc_now()
        with self._write_connection() as connection:
            for record_id in normalized:
                row = self._record_row(connection, record_id)
                if str(row["status"]) not in terminal:
                    raise CatalogConflictError("active production records cannot be trashed")
            placeholders = ",".join("?" for _item in normalized)
            connection.execute(
                f"""
                UPDATE production_records
                SET trashed_at = COALESCE(trashed_at, ?),
                    trashed_by_user_id = COALESCE(trashed_by_user_id, ?),
                    row_version = row_version + 1, updated_at = ?
                WHERE site_id = ? AND id IN ({placeholders})
                """,
                (now, actor_user_id, now, self.site_id, *normalized),
            )
            for record_id in normalized:
                self._audit(
                    connection,
                    action="production_record.trashed",
                    entity_type="production_record",
                    entity_id=record_id,
                    actor_user_id=actor_user_id,
                    after={"trashed_at": now},
                )
        return {"trashed": normalized, "trashed_at": now}

    def restore_trashed_records(
        self,
        record_ids: Sequence[str],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = list(dict.fromkeys(
            _required_text(item, "record_id", maximum=120) for item in record_ids
        ))
        if not normalized:
            raise CatalogValidationError("record_ids cannot be empty")
        now = utc_now()
        placeholders = ",".join("?" for _item in normalized)
        with self._write_connection() as connection:
            found = {
                str(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM production_records WHERE site_id = ? AND id IN ({placeholders})",
                    (self.site_id, *normalized),
                ).fetchall()
            }
            if found != set(normalized):
                raise CatalogNotFoundError("production record not found")
            connection.execute(
                f"""
                UPDATE production_records
                SET trashed_at = NULL, trashed_by_user_id = NULL,
                    row_version = row_version + 1, updated_at = ?
                WHERE site_id = ? AND id IN ({placeholders})
                """,
                (now, self.site_id, *normalized),
            )
            for record_id in normalized:
                self._audit(
                    connection,
                    action="production_record.trash_restored",
                    entity_type="production_record",
                    entity_id=record_id,
                    actor_user_id=actor_user_id,
                    after={"trashed_at": None},
                )
        return {"restored": normalized}

    def delete_trashed_records(
        self,
        record_ids: Sequence[str],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = list(dict.fromkeys(
            _required_text(item, "record_id", maximum=120) for item in record_ids
        ))
        if not normalized:
            raise CatalogValidationError("record_ids cannot be empty")
        placeholders = ",".join("?" for _item in normalized)
        with self._write_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id, output_path FROM production_records
                WHERE site_id = ? AND id IN ({placeholders}) AND trashed_at IS NOT NULL
                """,
                (self.site_id, *normalized),
            ).fetchall()
            if {str(row["id"]) for row in rows} != set(normalized):
                raise CatalogConflictError(
                    "only records already in the recycle bin can be deleted"
                )
            output_references = {
                str(row["id"]): str(row["output_path"] or "") for row in rows
            }
            for record_id in normalized:
                self._audit(
                    connection,
                    action="production_record.deleted",
                    entity_type="production_record",
                    entity_id=record_id,
                    actor_user_id=actor_user_id,
                    before={
                        "trashed": True,
                        "output_path_reference": output_references[record_id],
                    },
                )
            connection.execute(
                f"DELETE FROM production_records WHERE site_id = ? AND id IN ({placeholders})",
                (self.site_id, *normalized),
            )
        return {
            "deleted": normalized,
            "local_files_deleted": False,
            "message": "Only Hub metadata was deleted; workstation files were not touched.",
        }

    def archive_job_snapshot(
        self,
        job_id: str,
        snapshot: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one terminal queue item without touching its files.

        The production record remains the canonical owner of artifacts, logs
        and output paths.  The snapshot only preserves queue-specific fields
        needed to rebuild the film-strip card after a restart.
        """

        job_id = _required_text(job_id, "job_id", maximum=200)
        if not isinstance(snapshot, Mapping):
            raise CatalogValidationError("job snapshot must be an object")
        normalized = {str(key): value for key, value in snapshot.items()}
        if str(normalized.get("id") or "") != job_id:
            raise CatalogValidationError("job snapshot id does not match job_id")
        now = utc_now()
        terminal_statuses = {
            "completed",
            "failed",
            "skipped",
            "interrupted",
            "cancelled",
        }
        with self._write_connection() as connection:
            row = self._require_row(
                connection,
                "SELECT * FROM production_records WHERE site_id = ? AND job_id = ?",
                (self.site_id, job_id),
                "production record",
            )
            normalized["batch_id"] = str(
                row["batch_id"] or normalized.get("batch_id") or ""
            )
            normalized["production_record_id"] = str(row["id"])
            archive_json = _json_dump(normalized)
            if len(archive_json.encode("utf-8")) > 2 * 1024 * 1024:
                raise CatalogValidationError("job snapshot is too large")
            if str(row["status"]) not in terminal_statuses:
                raise CatalogConflictError(
                    "only finished production jobs can be archived"
                )
            before = {
                "archived": bool(row["archived"]),
                "archived_at": row["archived_at"],
            }
            archived_at = str(row["archived_at"] or now)
            archived_by = row["archived_by_user_id"] or actor_user_id or None
            connection.execute(
                """
                UPDATE production_records
                SET archived = 1, archived_at = ?, archived_by_user_id = ?,
                    archive_snapshot_json = ?, row_version = row_version + 1,
                    updated_at = ?
                WHERE id = ? AND site_id = ?
                """,
                (
                    archived_at,
                    archived_by,
                    archive_json,
                    now,
                    row["id"],
                    self.site_id,
                ),
            )
            updated = self._record_row(connection, str(row["id"]))
            record = self._record_dict(updated)
            self._audit(
                connection,
                action="production_job.archived",
                entity_type="production_record",
                entity_id=str(row["id"]),
                actor_user_id=actor_user_id,
                before=before,
                after={
                    "archived": True,
                    "archived_at": archived_at,
                    "job_id": job_id,
                },
            )
            return {
                "job": {
                    **normalized,
                    "archived": True,
                    "archived_at": archived_at,
                    "archived_by_user_id": archived_by,
                },
                "record": record,
            }

    def restore_job_snapshot(
        self,
        job_id: str,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Remove the archive marker and return the saved queue snapshot."""

        job_id = _required_text(job_id, "job_id", maximum=200)
        now = utc_now()
        with self._write_connection() as connection:
            row = self._require_row(
                connection,
                "SELECT * FROM production_records WHERE site_id = ? AND job_id = ?",
                (self.site_id, job_id),
                "production record",
            )
            if not bool(row["archived"]):
                raise CatalogConflictError("production job is not archived")
            snapshot = _json_load(row["archive_snapshot_json"], {})
            if not isinstance(snapshot, dict) or str(snapshot.get("id") or "") != job_id:
                raise CatalogConflictError("archived job snapshot is unavailable")
            snapshot = {
                **snapshot,
                "batch_id": str(row["batch_id"] or snapshot.get("batch_id") or ""),
                "production_record_id": str(row["id"]),
            }
            archived_at = row["archived_at"]
            connection.execute(
                """
                UPDATE production_records
                SET archived = 0, archived_at = NULL, archived_by_user_id = NULL,
                    row_version = row_version + 1, updated_at = ?
                WHERE id = ? AND site_id = ?
                """,
                (now, row["id"], self.site_id),
            )
            updated = self._record_row(connection, str(row["id"]))
            record = self._record_dict(updated)
            self._audit(
                connection,
                action="production_job.restored",
                entity_type="production_record",
                entity_id=str(row["id"]),
                actor_user_id=actor_user_id,
                before={
                    "archived": True,
                    "archived_at": archived_at,
                    "job_id": job_id,
                },
                after={"archived": False, "job_id": job_id},
            )
            return {"job": {**snapshot, "archived": False}, "record": record}

    @staticmethod
    def _archived_job_from_record_row(row: sqlite3.Row) -> dict[str, Any]:
        job_id = str(row["job_id"] or "")
        snapshot = _json_load(row["archive_snapshot_json"], {})
        if not isinstance(snapshot, dict) or str(snapshot.get("id") or "") != job_id:
            raise CatalogConflictError("archived job snapshot is unavailable")
        return {
            **snapshot,
            "id": job_id,
            "batch_id": str(row["batch_id"] or snapshot.get("batch_id") or ""),
            "production_record_id": str(row["id"]),
            "created_by_user_id": row["created_by_user_id"],
            "archived": True,
            "archived_at": row["archived_at"],
            "archived_by_user_id": row["archived_by_user_id"],
            "record_status": str(row["status"]),
        }

    def get_archived_batch(self, batch_id: str) -> dict[str, Any]:
        """Return every currently archived snapshot in one durable batch."""

        batch_id = _required_text(batch_id, "batch_id", maximum=120)
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM production_records
                WHERE site_id = ? AND batch_id = ? AND COALESCE(job_id, '') <> ''
                ORDER BY created_at, rowid
                """,
                (self.site_id, batch_id),
            ).fetchall()
            if not rows:
                raise CatalogNotFoundError("production batch not found")
            jobs = [
                self._archived_job_from_record_row(row)
                for row in rows
                if bool(row["archived"])
            ]
            return {
                "batch_id": batch_id,
                "jobs": jobs,
                "job_ids": [str(item["id"]) for item in jobs],
                "archived_count": len(jobs),
                "total_count": len(rows),
                "already_restored": not jobs,
            }

    def archive_batch_snapshots(
        self,
        batch_id: str,
        snapshots: Sequence[Mapping[str, Any]],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Archive every task in a production batch in one transaction.

        Already archived rows are preserved, which makes a repeated request
        idempotent and also lets this operation finish a batch that an older
        client archived one card at a time.  Every remaining row must have a
        valid snapshot and terminal status before any update is written.
        """

        batch_id = _required_text(batch_id, "batch_id", maximum=120)
        if not isinstance(snapshots, Sequence) or isinstance(snapshots, (str, bytes)):
            raise CatalogValidationError("job snapshots must be a list")
        normalized: dict[str, dict[str, Any]] = {}
        for raw in snapshots:
            if not isinstance(raw, Mapping):
                raise CatalogValidationError("job snapshots must be objects")
            snapshot = {str(key): value for key, value in raw.items()}
            job_id = _required_text(snapshot.get("id"), "job snapshot id", maximum=200)
            if job_id in normalized:
                raise CatalogValidationError("job snapshots contain a duplicate id")
            normalized[job_id] = snapshot
        now = utc_now()
        terminal_statuses = {
            "completed",
            "failed",
            "skipped",
            "interrupted",
            "cancelled",
        }
        with self._write_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM production_records
                WHERE site_id = ? AND batch_id = ? AND COALESCE(job_id, '') <> ''
                ORDER BY created_at, rowid
                """,
                (self.site_id, batch_id),
            ).fetchall()
            if not rows:
                raise CatalogNotFoundError("production batch not found")
            if any(str(row["status"]) not in terminal_statuses for row in rows):
                raise CatalogConflictError(
                    "only a batch whose every task is finished can be archived"
                )
            row_ids = {str(row["job_id"]): row for row in rows}
            unexpected = set(normalized).difference(row_ids)
            if unexpected:
                raise CatalogValidationError(
                    "one or more job snapshots do not belong to this batch"
                )
            missing = {
                str(row["job_id"])
                for row in rows
                if not bool(row["archived"]) and str(row["job_id"]) not in normalized
            }
            if missing:
                raise CatalogConflictError(
                    "every unarchived task in the batch requires a job snapshot"
                )

            changed_job_ids: list[str] = []
            for row in rows:
                job_id = str(row["job_id"])
                if bool(row["archived"]):
                    # Validate old data before claiming the whole batch is safe.
                    self._archived_job_from_record_row(row)
                    continue
                snapshot = normalized[job_id]
                snapshot["batch_id"] = str(row["batch_id"] or batch_id)
                snapshot["production_record_id"] = str(row["id"])
                archive_json = _json_dump(snapshot)
                if len(archive_json.encode("utf-8")) > 2 * 1024 * 1024:
                    raise CatalogValidationError("job snapshot is too large")
                archived_by = actor_user_id or None
                connection.execute(
                    """
                    UPDATE production_records
                    SET archived = 1, archived_at = ?, archived_by_user_id = ?,
                        archive_snapshot_json = ?, row_version = row_version + 1,
                        updated_at = ?
                    WHERE id = ? AND site_id = ?
                    """,
                    (
                        now,
                        archived_by,
                        archive_json,
                        now,
                        row["id"],
                        self.site_id,
                    ),
                )
                self._audit(
                    connection,
                    action="production_job.archived",
                    entity_type="production_record",
                    entity_id=str(row["id"]),
                    actor_user_id=actor_user_id,
                    before={"archived": False, "job_id": job_id},
                    after={
                        "archived": True,
                        "archived_at": now,
                        "job_id": job_id,
                        "batch_id": batch_id,
                    },
                )
                changed_job_ids.append(job_id)

            updated_rows = connection.execute(
                """
                SELECT * FROM production_records
                WHERE site_id = ? AND batch_id = ? AND COALESCE(job_id, '') <> ''
                ORDER BY created_at, rowid
                """,
                (self.site_id, batch_id),
            ).fetchall()
            jobs = [self._archived_job_from_record_row(row) for row in updated_rows]
            return {
                "batch_id": batch_id,
                "jobs": jobs,
                "job_ids": [str(item["id"]) for item in jobs],
                "changed_job_ids": changed_job_ids,
                "archived_count": len(jobs),
                "changed_count": len(changed_job_ids),
                "already_archived": not changed_job_ids,
            }

    def restore_batch_snapshots(
        self,
        batch_id: str,
        *,
        job_ids: Sequence[str] | None = None,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore archived snapshots from one batch in one transaction.

        ``job_ids`` is reserved for compensating a failed local queue update;
        normal callers omit it and restore every archived job in the batch.
        """

        batch_id = _required_text(batch_id, "batch_id", maximum=120)
        requested: set[str] | None = None
        if job_ids is not None:
            if not isinstance(job_ids, Sequence) or isinstance(job_ids, (str, bytes)):
                raise CatalogValidationError("job_ids must be a list")
            requested = {
                _required_text(item, "job_id", maximum=200) for item in job_ids
            }
            if not requested:
                return {
                    "batch_id": batch_id,
                    "jobs": [],
                    "job_ids": [],
                    "restored_count": 0,
                    "already_restored": True,
                }
        now = utc_now()
        with self._write_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM production_records
                WHERE site_id = ? AND batch_id = ? AND COALESCE(job_id, '') <> ''
                ORDER BY created_at, rowid
                """,
                (self.site_id, batch_id),
            ).fetchall()
            if not rows:
                raise CatalogNotFoundError("production batch not found")
            by_job_id = {str(row["job_id"]): row for row in rows}
            if requested is not None and not requested.issubset(by_job_id):
                raise CatalogNotFoundError(
                    "one or more production jobs were not found in this batch"
                )
            targets = [
                row
                for row in rows
                if bool(row["archived"])
                and (requested is None or str(row["job_id"]) in requested)
            ]
            jobs = [self._archived_job_from_record_row(row) for row in targets]
            for row, snapshot in zip(targets, jobs):
                job_id = str(row["job_id"])
                archived_at = row["archived_at"]
                connection.execute(
                    """
                    UPDATE production_records
                    SET archived = 0, archived_at = NULL, archived_by_user_id = NULL,
                        row_version = row_version + 1, updated_at = ?
                    WHERE id = ? AND site_id = ?
                    """,
                    (now, row["id"], self.site_id),
                )
                self._audit(
                    connection,
                    action="production_job.restored",
                    entity_type="production_record",
                    entity_id=str(row["id"]),
                    actor_user_id=actor_user_id,
                    before={
                        "archived": True,
                        "archived_at": archived_at,
                        "job_id": job_id,
                        "batch_id": batch_id,
                    },
                    after={
                        "archived": False,
                        "job_id": job_id,
                        "batch_id": batch_id,
                    },
                )
                snapshot["archived"] = False
                snapshot["archived_at"] = ""
                snapshot["archived_by_user_id"] = ""
            return {
                "batch_id": batch_id,
                "jobs": jobs,
                "job_ids": [str(item["id"]) for item in jobs],
                "restored_count": len(jobs),
                "already_restored": not jobs,
            }

    def list_archived_jobs(
        self,
        *,
        created_by_user_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return durable film-strip snapshots, newest archive first."""

        limit = _positive_int(limit, "limit", minimum=1, maximum=5000)
        offset = _positive_int(offset, "offset", minimum=0, maximum=10_000_000)
        filters = ["site_id = ?", "archived = 1"]
        parameters: list[Any] = [self.site_id]
        if created_by_user_id:
            filters.append("created_by_user_id = ?")
            parameters.append(created_by_user_id)
        where = " AND ".join(filters)
        with self._read_connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM production_records WHERE {where}",
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM production_records WHERE {where}
                ORDER BY archived_at DESC, created_at DESC LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                snapshot = _json_load(row["archive_snapshot_json"], {})
                if not isinstance(snapshot, dict):
                    snapshot = {}
                items.append(
                    {
                        **snapshot,
                        "id": str(row["job_id"] or snapshot.get("id") or ""),
                        "batch_id": str(
                            row["batch_id"] or snapshot.get("batch_id") or ""
                        ),
                        "production_record_id": str(row["id"]),
                        "created_by_user_id": row["created_by_user_id"],
                        "archived": True,
                        "archived_at": row["archived_at"],
                        "archived_by_user_id": row["archived_by_user_id"],
                        "record_status": str(row["status"]),
                    }
                )
            return {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def get_archived_job(self, job_id: str) -> dict[str, Any]:
        """Return one archived queue snapshot by its stable job id."""

        job_id = _required_text(job_id, "job_id", maximum=200)
        with self._read_connection() as connection:
            row = self._require_row(
                connection,
                """
                SELECT * FROM production_records
                WHERE site_id = ? AND job_id = ? AND archived = 1
                """,
                (self.site_id, job_id),
                "archived production job",
            )
            snapshot = _json_load(row["archive_snapshot_json"], {})
            if not isinstance(snapshot, dict) or str(snapshot.get("id") or "") != job_id:
                raise CatalogConflictError("archived job snapshot is unavailable")
            return {
                **snapshot,
                "id": job_id,
                "batch_id": str(row["batch_id"] or snapshot.get("batch_id") or ""),
                "production_record_id": str(row["id"]),
                "created_by_user_id": row["created_by_user_id"],
                "archived": True,
                "archived_at": row["archived_at"],
                "archived_by_user_id": row["archived_by_user_id"],
                "record_status": str(row["status"]),
            }

    @staticmethod
    def _lease_is_active(row: Mapping[str, Any], now: datetime) -> bool:
        if not str(row["lease_owner_device"] or "") or not row["lease_expires_at"]:
            return False
        try:
            expires_at = datetime.fromisoformat(str(row["lease_expires_at"]))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            return expires_at.astimezone(timezone.utc) > now
        except (TypeError, ValueError):
            # A malformed historical timestamp must never keep a job locked.
            return False

    @staticmethod
    def _lease_window(lease_seconds: Any) -> tuple[str, str]:
        seconds = _positive_int(
            lease_seconds, "lease_seconds", minimum=1, maximum=86_400
        )
        now = datetime.now(timezone.utc)
        return now.isoformat(), (now + timedelta(seconds=seconds)).isoformat()

    def _record_row(
        self, connection: sqlite3.Connection, record_id: str
    ) -> sqlite3.Row:
        return self._require_row(
            connection,
            """
            SELECT r.*,
                (SELECT COUNT(*) FROM artifacts a WHERE a.record_id = r.id) AS artifact_count
            FROM production_records r WHERE r.id = ? AND r.site_id = ?
            """,
            (record_id, self.site_id),
            "production record",
        )

    def claim_record_lease(
        self,
        record_id: str,
        device_id: str,
        *,
        lease_seconds: int = 120,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically claim or renew a production record for one device."""

        record_id = _required_text(record_id, "record_id", maximum=120)
        device_id = _required_text(device_id, "device_id", maximum=300)
        heartbeat_at, lease_expires_at = self._lease_window(lease_seconds)
        now = datetime.fromisoformat(heartbeat_at)
        with self._write_connection() as connection:
            row = self._record_row(connection, record_id)
            if str(row["status"]) in {"completed", "failed", "skipped", "cancelled"}:
                raise CatalogConflictError(
                    "terminal production records cannot be claimed"
                )
            previous_owner = str(row["lease_owner_device"] or "")
            active = self._lease_is_active(row, now)
            if active and previous_owner != device_id:
                raise CatalogConflictError(
                    "production record is already leased by another device"
                )
            renewed = bool(active and previous_owner == device_id)
            reclaimed = bool(previous_owner and not active)
            connection.execute(
                """
                UPDATE production_records
                SET lease_owner_device = ?, lease_expires_at = ?, heartbeat_at = ?
                WHERE id = ? AND site_id = ?
                """,
                (
                    device_id,
                    lease_expires_at,
                    heartbeat_at,
                    record_id,
                    self.site_id,
                ),
            )
            updated = self._record_row(connection, record_id)
            result = self._record_dict(updated)
            self._audit(
                connection,
                action=(
                    "production_record.lease_renewed"
                    if renewed
                    else "production_record.lease_reclaimed"
                    if reclaimed
                    else "production_record.lease_claimed"
                ),
                entity_type="production_record",
                entity_id=record_id,
                actor_user_id=actor_user_id,
                before={
                    "lease_owner_device": previous_owner,
                    "lease_expires_at": row["lease_expires_at"],
                },
                after={
                    "lease_owner_device": device_id,
                    "lease_expires_at": lease_expires_at,
                    "heartbeat_at": heartbeat_at,
                },
            )
            return {
                "claimed": True,
                "renewed": renewed,
                "reclaimed": reclaimed,
                "record": result,
            }

    def heartbeat_record_lease(
        self,
        record_id: str,
        device_id: str,
        *,
        lease_seconds: int = 120,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Extend an active lease held by exactly ``device_id``."""

        record_id = _required_text(record_id, "record_id", maximum=120)
        device_id = _required_text(device_id, "device_id", maximum=300)
        heartbeat_at, lease_expires_at = self._lease_window(lease_seconds)
        now = datetime.fromisoformat(heartbeat_at)
        with self._write_connection() as connection:
            row = self._record_row(connection, record_id)
            if str(row["lease_owner_device"] or "") != device_id:
                raise CatalogConflictError(
                    "production record lease belongs to another device"
                )
            if not self._lease_is_active(row, now):
                raise CatalogConflictError(
                    "production record lease expired; claim it again"
                )
            connection.execute(
                """
                UPDATE production_records
                SET lease_expires_at = ?, heartbeat_at = ?
                WHERE id = ? AND site_id = ? AND lease_owner_device = ?
                """,
                (
                    lease_expires_at,
                    heartbeat_at,
                    record_id,
                    self.site_id,
                    device_id,
                ),
            )
            result = self._record_dict(self._record_row(connection, record_id))
            return {"heartbeat": True, "record": result}

    def release_record_lease(
        self,
        record_id: str,
        device_id: str,
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Release a lease; retrying after a successful release is idempotent."""

        record_id = _required_text(record_id, "record_id", maximum=120)
        device_id = _required_text(device_id, "device_id", maximum=300)
        with self._write_connection() as connection:
            row = self._record_row(connection, record_id)
            previous_owner = str(row["lease_owner_device"] or "")
            if previous_owner and previous_owner != device_id:
                raise CatalogConflictError(
                    "production record lease belongs to another device"
                )
            if not previous_owner:
                return {
                    "released": False,
                    "record": self._record_dict(row),
                }
            connection.execute(
                """
                UPDATE production_records
                SET lease_owner_device = '', lease_expires_at = NULL, heartbeat_at = NULL
                WHERE id = ? AND site_id = ? AND lease_owner_device = ?
                """,
                (record_id, self.site_id, device_id),
            )
            result = self._record_dict(self._record_row(connection, record_id))
            self._audit(
                connection,
                action="production_record.lease_released",
                entity_type="production_record",
                entity_id=record_id,
                actor_user_id=actor_user_id,
                before={
                    "lease_owner_device": previous_owner,
                    "lease_expires_at": row["lease_expires_at"],
                    "heartbeat_at": row["heartbeat_at"],
                },
                after={
                    "lease_owner_device": "",
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                },
            )
            return {"released": True, "record": result}

    @staticmethod
    def _artifact_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "record_id": str(row["record_id"]),
            "kind": str(row["kind"]),
            "device_id": str(row["device_id"]),
            "local_path": str(row["local_path"]),
            "sha256": str(row["sha256"]),
            "mime_type": str(row["mime_type"]),
            "size_bytes": row["size_bytes"],
            "duration_seconds": row["duration_seconds"],
            "metadata": _json_load(row["metadata_json"], {}),
            "created_at": str(row["created_at"]),
        }

    def add_artifact(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise CatalogValidationError("artifact payload must be an object")
        record_id = _required_text(value.get("record_id"), "record_id", maximum=120)
        kind = _required_text(value.get("kind"), "kind", maximum=100)
        size_value = value.get("size_bytes")
        size_bytes = None if size_value in (None, "") else int(size_value)
        duration_value = value.get("duration_seconds")
        duration = None if duration_value in (None, "") else float(duration_value)
        if size_bytes is not None and size_bytes < 0:
            raise CatalogValidationError("size_bytes cannot be negative")
        if duration is not None and duration < 0:
            raise CatalogValidationError("duration_seconds cannot be negative")
        with self._write_connection() as connection:
            self._require_row(
                connection,
                "SELECT id FROM production_records WHERE id = ? AND site_id = ?",
                (record_id, self.site_id),
                "production record",
            )
            artifact_id = str(value.get("id") or _new_id())
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, record_id, kind, device_id, local_path, sha256,
                    mime_type, size_bytes, duration_seconds, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    record_id,
                    kind,
                    _optional_text(value.get("device_id"), maximum=300),
                    _optional_text(value.get("local_path"), maximum=4000),
                    _optional_text(value.get("sha256"), maximum=128),
                    _optional_text(value.get("mime_type"), maximum=200),
                    size_bytes,
                    duration,
                    _json_dump(_metadata(value.get("metadata"))),
                    utc_now(),
                ),
            )
            row = self._require_row(
                connection,
                "SELECT * FROM artifacts WHERE id = ?",
                (artifact_id,),
                "artifact",
            )
            result = self._artifact_dict(row)
            self._audit(
                connection,
                action="artifact.created",
                entity_type="artifact",
                entity_id=artifact_id,
                actor_user_id=actor_user_id,
                after=result,
            )
            return result

    def record_media_usage(
        self,
        value: Mapping[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise CatalogValidationError("media usage payload must be an object")
        fingerprint = _required_text(
            value.get("fingerprint"), "fingerprint", maximum=256
        )
        media_type = _required_text(
            value.get("media_type"), "media_type", maximum=100
        )
        fingerprint = _normalized_key(fingerprint)
        media_type = _normalized_key(media_type)
        use_count = _positive_int(
            value.get("use_count", 1), "use_count", minimum=1, maximum=1_000_000
        )
        record_id = _optional_text(value.get("record_id"), maximum=120) or None
        with self._write_connection() as connection:
            if record_id:
                self._require_row(
                    connection,
                    "SELECT id FROM production_records WHERE id = ? AND site_id = ?",
                    (record_id, self.site_id),
                    "production record",
                )
            event_id = str(value.get("id") or _new_id())
            used_at = str(value.get("used_at") or utc_now())
            connection.execute(
                """
                INSERT INTO media_usage_events(
                    id, site_id, fingerprint, media_type, display_name,
                    local_path, record_id, device_id, use_count,
                    metadata_json, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    self.site_id,
                    fingerprint,
                    media_type,
                    _optional_text(value.get("display_name"), maximum=1000),
                    _optional_text(value.get("local_path"), maximum=4000),
                    record_id,
                    _optional_text(value.get("device_id"), maximum=300),
                    use_count,
                    _json_dump(_metadata(value.get("metadata"))),
                    used_at,
                ),
            )
            result = {
                "id": event_id,
                "fingerprint": fingerprint,
                "media_type": media_type,
                "display_name": _optional_text(value.get("display_name"), maximum=1000),
                "local_path": _optional_text(value.get("local_path"), maximum=4000),
                "record_id": record_id,
                "device_id": _optional_text(value.get("device_id"), maximum=300),
                "use_count": use_count,
                "metadata": _metadata(value.get("metadata")),
                "used_at": used_at,
            }
            self._audit(
                connection,
                action="media_usage.recorded",
                entity_type="media_usage",
                entity_id=event_id,
                actor_user_id=actor_user_id,
                after={
                    "fingerprint": fingerprint,
                    "media_type": media_type,
                    "record_id": record_id,
                    "use_count": use_count,
                },
            )
            return result

    def list_media_usage(
        self,
        *,
        media_type: str = "",
        fingerprint: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = _positive_int(limit, "limit", minimum=1, maximum=500)
        offset = _positive_int(offset, "offset", minimum=0, maximum=10_000_000)
        filters = ["site_id = ?"]
        parameters: list[Any] = [self.site_id]
        if media_type:
            filters.append("media_type = ?")
            parameters.append(_normalized_key(media_type))
        if fingerprint:
            filters.append("fingerprint = ?")
            parameters.append(_normalized_key(fingerprint))
        where = " AND ".join(filters)
        with self._read_connection() as connection:
            total = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM (
                        SELECT 1 FROM media_usage_events WHERE {where}
                        GROUP BY fingerprint, media_type
                    )
                    """,
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT fingerprint, media_type,
                    MAX(display_name) AS display_name,
                    MAX(local_path) AS last_local_path,
                    SUM(use_count) AS total_uses,
                    COUNT(*) AS event_count,
                    MAX(used_at) AS last_used_at
                FROM media_usage_events
                WHERE {where}
                GROUP BY fingerprint, media_type
                ORDER BY total_uses DESC, last_used_at DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            return {
                "items": [
                    {
                        "fingerprint": str(row["fingerprint"]),
                        "media_type": str(row["media_type"]),
                        "display_name": str(row["display_name"] or ""),
                        "last_local_path": str(row["last_local_path"] or ""),
                        "total_uses": int(row["total_uses"]),
                        "event_count": int(row["event_count"]),
                        "last_used_at": str(row["last_used_at"]),
                    }
                    for row in rows
                ],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def list_audit_events(
        self,
        *,
        entity_type: str = "",
        actor_user_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = _positive_int(limit, "limit", minimum=1, maximum=500)
        offset = _positive_int(offset, "offset", minimum=0, maximum=10_000_000)
        filters = ["site_id = ?"]
        parameters: list[Any] = [self.site_id]
        if entity_type:
            filters.append("entity_type = ?")
            parameters.append(entity_type)
        if actor_user_id:
            filters.append("actor_user_id = ?")
            parameters.append(actor_user_id)
        where = " AND ".join(filters)
        with self._read_connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM audit_events WHERE {where}", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM audit_events WHERE {where}
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            return {
                "items": [
                    {
                        "id": str(row["id"]),
                        "actor_user_id": row["actor_user_id"],
                        "action": str(row["action"]),
                        "entity_type": str(row["entity_type"]),
                        "entity_id": str(row["entity_id"]),
                        "before": _json_load(row["before_json"], None),
                        "after": _json_load(row["after_json"], None),
                        "created_at": str(row["created_at"]),
                    }
                    for row in rows
                ],
                "total": total,
                "limit": limit,
                "offset": offset,
            }


__all__ = [
    "CatalogConflictError",
    "CatalogError",
    "CatalogNotFoundError",
    "CatalogRepository",
    "CatalogValidationError",
    "DuplicateContentError",
    "KNOWN_PERMISSIONS",
    "PromoCodeLimitError",
    "ROLE_ADMIN",
    "ROLE_PRODUCER",
    "SCHEMA_VERSION",
    "SUPER_ADMIN_PERMISSIONS",
    "installation_id_sha256",
    "manuscript_sha256",
    "normalize_portable_device_config",
    "normalize_manuscript_for_hash",
]
