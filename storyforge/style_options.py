"""Validation and serialization helpers for editable video visual styles."""

from __future__ import annotations

from dataclasses import asdict
import math
import re
from typing import Any, Mapping

from .models import (
    CodeCardStyle,
    INTRO_ANIMATIONS,
    IntroCardStyle,
    OutroCardStyle,
    SUBTITLE_ANIMATIONS,
    SubtitleStyle,
    VISUAL_STYLE_PRESETS,
)


STYLE_TYPES = {
    "subtitle": SubtitleStyle,
    "intro_card": IntroCardStyle,
    "code_card": CodeCardStyle,
    "outro_card": OutroCardStyle,
}

PRESET_FIELDS = {
    "subtitle": "subtitle_preset",
    "intro_card": "intro_card_preset",
    "code_card": "code_card_preset",
    "outro_card": "outro_card_preset",
}

_COLORS = {
    "subtitle": {
        "text_color",
        "outline_color",
        "background_color",
        "unread_color",
        "active_color",
        "read_color",
    },
    "intro_card": {
        "headline_color",
        "body_color",
        "label_color",
        "background_color",
        "border_color",
    },
    "code_card": {
        "text_color",
        "background_color",
        "outline_color",
    },
    "outro_card": {
        "title_color",
        "body_color",
        "code_color",
        "background_color",
        "border_color",
    },
}

_BOOLEANS = {
    "subtitle": {"bold", "italic", "word_sync_enabled"},
    "code_card": {"bold"},
}

_ENUMS = {
    "subtitle": {"alignment": {"left", "center", "right"}},
    "intro_card": {"text_alignment": {"left", "center", "right"}},
    "code_card": {"alignment": {"left", "center", "right"}},
    "outro_card": {"text_alignment": {"left", "center", "right"}},
}

# Numeric ranges are intentionally wider than the initial UI controls.  They
# keep imported/shared recipes valid while still preventing off-canvas or
# pathological ASS/FFmpeg values.  The renderer applies an additional canvas-
# aware clamp after output dimensions are known.
_NUMBERS: dict[str, dict[str, tuple[float, float, type[int] | type[float]]]] = {
    "subtitle": {
        "font_size": (24, 96, int),
        "outline_width": (0, 10, int),
        "bottom_margin": (80, 960, int),
        "horizontal_margin": (40, 360, int),
        "max_chars_per_line": (12, 60, int),
        "shadow_width": (0, 8, float),
        "background_opacity": (0, 1, float),
        "position_x_percent": (10, 90, float),
        "max_lines": (1, 4, int),
        "pop_scale": (100, 150, int),
        "pop_duration_ms": (40, 500, int),
        "pop_intensity": (0, 1, float),
    },
    "intro_card": {
        "headline_font_size": (28, 96, int),
        "body_font_size": (20, 72, int),
        "label_font_size": (16, 52, int),
        "background_opacity": (0.15, 1, float),
        "border_width": (0, 12, int),
        "shadow_opacity": (0, 0.8, float),
        "width_percent": (40, 82, float),
        "position_x_percent": (20, 80, float),
        "position_y_percent": (12, 58, float),
        "padding": (16, 120, int),
        "radius": (0, 72, int),
        "max_lines": (2, 8, int),
    },
    "code_card": {
        "font_size": (20, 72, int),
        "opacity": (0.15, 1, float),
        "top_margin": (80, 500, int),
        "horizontal_margin": (40, 360, int),
        "outline_width": (0, 8, float),
        "position_x_percent": (10, 90, float),
        "position_y_percent": (5, 30, float),
        "width_percent": (28, 82, float),
        "padding": (4, 48, int),
        "radius": (0, 48, int),
    },
    "outro_card": {
        "title_font_size": (28, 96, int),
        "body_font_size": (20, 72, int),
        "code_font_size": (24, 96, int),
        "background_opacity": (0.15, 1, float),
        "border_width": (0, 12, int),
        "width_percent": (40, 82, float),
        "height_percent": (28, 62, float),
        "position_x_percent": (20, 80, float),
        "position_y_percent": (12, 52, float),
        "padding": (16, 120, int),
        "radius": (0, 72, int),
    },
}


def preset_names(kind: str) -> frozenset[str]:
    return frozenset(VISUAL_STYLE_PRESETS.get(kind, {}))


def subtitle_animation_names() -> frozenset[str]:
    """Return animation IDs supported by both settings and ASS rendering."""

    return SUBTITLE_ANIMATIONS


def validate_subtitle_animation(value: Any) -> str:
    """Normalize one animation ID and reject renderer-unknown values."""

    normalized = str(value or "none").strip().casefold()
    if normalized not in SUBTITLE_ANIMATIONS:
        raise ValueError("invalid subtitle animation")
    return normalized


def intro_animation_names() -> frozenset[str]:
    """Return seek-safe opening-card animation IDs."""

    return INTRO_ANIMATIONS


def validate_intro_animation(value: Any) -> str:
    """Normalize one opening-card animation ID and reject unknown effects."""

    normalized = str(value or "fade_rise").strip().casefold()
    if normalized not in INTRO_ANIMATIONS:
        raise ValueError("invalid intro animation")
    return normalized


def resolved_visual_style_presets() -> dict[str, dict[str, dict[str, Any]]]:
    """Return complete editable values for every built-in preset."""

    result: dict[str, dict[str, dict[str, Any]]] = {}
    for kind in STYLE_TYPES:
        result[kind] = {
            # Pass built-ins through the same strict path as user patches so a
            # typo or out-of-range value fails during bootstrap/tests instead
            # of reaching libass later in a production render.
            name: validate_style_patch(
                kind,
                values,
                preset=name,
                reset_to_preset=True,
            )
            for name, values in VISUAL_STYLE_PRESETS[kind].items()
        }
    return result


def validate_style_patch(
    kind: str,
    value: Mapping[str, Any] | None,
    *,
    base: Mapping[str, Any] | None = None,
    preset: str | None = None,
    reset_to_preset: bool = False,
) -> dict[str, Any]:
    """Validate a custom style and return a complete normalized mapping.

    ``reset_to_preset`` is used when a caller changes only a preset selector;
    otherwise a partial patch is merged over the current editable values.
    """

    if kind not in STYLE_TYPES:
        raise ValueError(f"unknown visual style kind: {kind}")
    style_type = STYLE_TYPES[kind]
    allowed = asdict(style_type())
    selected_preset = str(preset or next(iter(VISUAL_STYLE_PRESETS[kind]))).casefold()
    if selected_preset not in VISUAL_STYLE_PRESETS[kind]:
        raise ValueError(f"invalid {kind} preset")
    if reset_to_preset:
        result = {**allowed, **VISUAL_STYLE_PRESETS[kind][selected_preset]}
    else:
        result = {**allowed, **dict(base or {})}
    patch = dict(value or {}) if isinstance(value, Mapping) else {}
    unknown = set(patch) - set(allowed)
    if unknown:
        raise ValueError(f"unsupported {kind} fields: {', '.join(sorted(unknown))}")

    for key, raw in patch.items():
        if key == "font_family":
            font = " ".join(str(raw or "").split())
            if (
                not font
                or len(font) > 80
                or any(ord(char) < 32 for char in font)
                or any(char in font for char in ",{}\\")
            ):
                raise ValueError(f"invalid {kind} font_family")
            result[key] = font
        elif key in _COLORS.get(kind, set()):
            color = str(raw or "").strip().upper()
            if not re.fullmatch(r"#[0-9A-F]{6}", color):
                raise ValueError(f"invalid {kind} color: {key}")
            result[key] = color
        elif key in _BOOLEANS.get(kind, set()):
            if not isinstance(raw, bool):
                raise ValueError(f"invalid {kind} boolean: {key}")
            result[key] = raw
        elif key in _ENUMS.get(kind, {}):
            normalized = str(raw or "").strip().casefold()
            if normalized not in _ENUMS[kind][key]:
                raise ValueError(f"invalid {kind} option: {key}")
            result[key] = normalized
        elif key in _NUMBERS[kind]:
            minimum, maximum, number_type = _NUMBERS[kind][key]
            try:
                number = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"invalid {kind} number: {key}") from None
            if not math.isfinite(number) or not minimum <= number <= maximum:
                raise ValueError(
                    f"{kind} {key} must be between {minimum:g} and {maximum:g}"
                )
            result[key] = int(round(number)) if number_type is int else number
        else:
            # Current style schemas contain only font, colour, boolean, enum
            # and numeric fields.  Keeping this explicit catches future fields
            # that were added without a validation rule.
            raise ValueError(f"unvalidated {kind} field: {key}")
    return result
