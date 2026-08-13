from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
import math
import socket
from typing import Any, Mapping
import unicodedata
from uuid import UUID, uuid4

from . import __version__


DEFAULT_PREVIEW_SECONDS = 15
MIN_STRUCTURED_PREVIEW_SECONDS = 12
MAX_PLATFORM_SEARCH_TEXT = 300
MAX_PLATFORM_ENDING_TEXT = 1200


def normalize_platform_copy(value: Any, field_name: str) -> str:
    """Validate one concrete per-batch platform line, never a shared template."""

    text = str(value or "")
    if any(
        unicodedata.category(character) == "Cf"
        or (
            unicodedata.category(character) == "Cc"
            and character not in {"\r", "\n", "\t"}
        )
        for character in text
    ):
        raise ValueError(f"{field_name} cannot contain control characters")
    normalized = " ".join(text.split())
    maximum = (
        MAX_PLATFORM_SEARCH_TEXT
        if field_name == "platform_search_text"
        else MAX_PLATFORM_ENDING_TEXT
    )
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} cannot exceed {maximum} characters")
    return normalized


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStatus(str, Enum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    PREPARING = "preparing"
    POLISHING = "polishing"
    NARRATING = "narrating"
    COMPOSING = "composing"
    PREVIEWING = "previewing"
    AWAITING_APPROVAL = "awaiting_approval"
    WAITING_PREVIEW = "waiting_preview"
    APPROVED = "approved"
    RENDERING = "rendering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(slots=True)
class PlatformProfile:
    id: str = field(default_factory=lambda: uuid4().hex)
    name: str = ""
    search_template: str = "Search {platform}: {code}"
    ending_template: str = (
        "Download {platform} and search code {code} to continue reading."
    )
    logo_path: str = ""
    brand_color: str = ""

    def render_search(self, code: str) -> str:
        return self.search_template.format(platform=self.name, code=code)

    def render_ending(self, code: str) -> str:
        return self.ending_template.format(platform=self.name, code=code)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PlatformProfile":
        return cls(
            id=str(value.get("id") or uuid4().hex),
            name=str(value.get("name") or "").strip(),
            search_template=str(
                value.get("search_template") or "Search {platform}: {code}"
            ),
            ending_template=str(
                value.get("ending_template")
                or "Download {platform} and search code {code} to continue reading."
            ),
            logo_path=str(value.get("logo_path") or "").strip(),
            brand_color=str(value.get("brand_color") or "").strip(),
        )


@dataclass(slots=True)
class SubtitleStyle:
    font_family: str = "Arial"
    font_size: int = 52
    text_color: str = "#FFFFFF"
    outline_color: str = "#101828"
    outline_width: int = 4
    bottom_margin: int = 310
    horizontal_margin: int = 180
    max_chars_per_line: int = 28
    bold: bool = True
    italic: bool = False
    shadow_width: float = 1.0
    background_color: str = "#000000"
    background_opacity: float = 0.0
    alignment: str = "center"
    position_x_percent: float = 50.0
    max_lines: int = 3
    word_sync_enabled: bool = False
    unread_color: str = "#D0D5DD"
    active_color: str = "#FFE06A"
    read_color: str = "#FFFFFF"
    pop_scale: int = 112
    pop_duration_ms: int = 140
    pop_intensity: float = 0.65


@dataclass(slots=True)
class IntroCardStyle:
    """Editable opening synopsis card, expressed on a percentage canvas."""

    font_family: str = "Arial"
    headline_font_size: int = 62
    headline_color: str = "#FFE06A"
    body_font_size: int = 36
    body_color: str = "#172033"
    label_font_size: int = 26
    label_color: str = "#667085"
    background_color: str = "#FFFFFF"
    background_opacity: float = 1.0
    border_color: str = "#FFFFFF"
    border_width: int = 0
    shadow_opacity: float = 0.34
    width_percent: float = 65.2
    position_x_percent: float = 50.0
    position_y_percent: float = 27.1
    padding: int = 56
    radius: int = 32
    text_alignment: str = "center"
    max_lines: int = 5
    layout: str = "standard"


@dataclass(slots=True)
class CodeCardStyle:
    font_family: str = "Arial"
    font_size: int = 42
    text_color: str = "#FFFFFF"
    background_color: str = "#2446C8"
    opacity: float = 0.92
    top_margin: int = 180
    horizontal_margin: int = 150
    bold: bool = True
    outline_color: str = "#FFFFFF"
    outline_width: float = 0.0
    alignment: str = "center"
    position_x_percent: float = 50.0
    position_y_percent: float = 7.8
    width_percent: float = 65.2
    padding: int = 14
    radius: int = 18


@dataclass(slots=True)
class OutroCardStyle:
    """Editable closing call-to-action card."""

    font_family: str = "Arial"
    title_font_size: int = 58
    title_color: str = "#111827"
    body_font_size: int = 38
    body_color: str = "#111827"
    code_font_size: int = 56
    code_color: str = "#3535E5"
    background_color: str = "#FFFFFF"
    background_opacity: float = 1.0
    border_color: str = "#FFFFFF"
    border_width: int = 0
    width_percent: float = 65.2
    height_percent: float = 46.9
    position_x_percent: float = 50.0
    position_y_percent: float = 21.9
    padding: int = 56
    radius: int = 32
    text_alignment: str = "center"


# Animation identifiers are shared by settings validation and the ASS renderer.
# Keeping the public vocabulary beside the visual-style schema prevents a saved
# recipe from selecting an effect that the renderer does not understand.
SUBTITLE_ANIMATIONS = frozenset(
    {"none", "fade", "soft_pop", "rise", "mask_reveal", "typewriter"}
)
INTRO_ANIMATIONS = frozenset(
    {
        "none",
        "fade_rise",
        "soft_scale",
        "side_reveal",
        "layered_story",
        "paper_drop",
    }
)
COLOR_GRADES = frozenset(
    {
        "neutral",
        "suspense_cool",
        "romance_warm",
        "sad_muted",
        "revenge_contrast",
        "night_lift",
    }
)
COVER_ANIMATIONS = frozenset(
    {
        "none",
        "fade",
        "gentle_push",
        "gentle_pull",
        "slow_pan",
        "soft_parallax",
        "vertical_drift",
        "focus_reveal",
        "cinematic_push",
        "ken_burns_left",
        "ken_burns_right",
        "soft_flash",
    }
)


# These presets are no longer offered for new work, but saved settings, drafts,
# portable workstation configs and personal production presets may still refer
# to them.  Keep their complete visual patches here so migration never changes
# the appearance of an existing recipe merely because its preset id retired.
RETIRED_SUBTITLE_PRESET_MIGRATIONS: dict[str, dict[str, Any]] = {
    "word_pop_sync": {
        "font_size": 52,
        "outline_width": 4,
        "bold": True,
        "max_lines": 2,
        "word_sync_enabled": True,
        "unread_color": "#D0D5DD",
        "active_color": "#FFE06A",
        "read_color": "#FFFFFF",
        "pop_scale": 112,
        "pop_duration_ms": 140,
        "pop_intensity": 0.65,
    },
    "minimal_bottom": {
        "font_family": "Segoe UI",
        "font_size": 46,
        "text_color": "#FFFFFF",
        "outline_color": "#111827",
        "outline_width": 3,
        "shadow_width": 0.5,
        "bottom_margin": 250,
        "horizontal_margin": 200,
        "bold": False,
        "max_lines": 2,
    },
}


def normalize_retired_subtitle_settings(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return an idempotent compatibility projection for retired presets.

    The old preset's complete patch is materialized into ``subtitle`` before
    selecting ``clear_outline``.  Explicit values saved by the user win over
    that compatibility patch.  Word-follow behavior is now represented solely
    by ``subtitle_word_mode`` rather than the retired style boolean.
    """

    result = dict(value)
    selected = str(result.get("subtitle_preset") or "").strip().casefold()
    retired_patch = RETIRED_SUBTITLE_PRESET_MIGRATIONS.get(selected)
    if retired_patch is None:
        return result

    incoming_patch = result.get("subtitle")
    merged_patch = dict(retired_patch)
    if isinstance(incoming_patch, Mapping):
        merged_patch.update(incoming_patch)
    merged_patch.pop("word_sync_enabled", None)
    result["subtitle"] = merged_patch
    result["subtitle_preset"] = "clear_outline"
    if selected == "word_pop_sync":
        word_mode = str(result.get("subtitle_word_mode") or "").strip().casefold()
        if word_mode not in {"single", "cumulative"}:
            result["subtitle_word_mode"] = "single"
    return result


# Presets are complete style patches rather than renderer-only aliases.  This
# lets the UI display/edit every resolved value, and lets a production draft
# freeze the exact appearance even when presets evolve in a future release.
VISUAL_STYLE_PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "subtitle": {
        "clear_outline": {},
        "cinematic_shadow": {"shadow_width": 3.0, "bold": True},
        "clean_minimal": {
            "outline_width": 2,
            "shadow_width": 0.0,
            "bold": False,
            "max_lines": 2,
        },
        "bold_drama": {
            "font_size": 58,
            "outline_width": 5,
            "shadow_width": 1.5,
            "bold": True,
            "max_lines": 2,
        },
        "reader_focus": {
            "font_family": "Georgia",
            "font_size": 48,
            "outline_width": 3,
            "shadow_width": 0.5,
            "max_chars_per_line": 30,
        },
        "soft_box": {
            "font_family": "Segoe UI",
            "font_size": 48,
            "text_color": "#17243C",
            "outline_color": "#FFFFFF",
            "outline_width": 0,
            "shadow_width": 2.0,
            "background_color": "#F7F9FC",
            "background_opacity": 0.86,
            "max_lines": 2,
        },
        "romance_glow": {
            "font_family": "Georgia",
            "font_size": 54,
            "text_color": "#FFF1F5",
            "outline_color": "#7A2448",
            "outline_width": 4,
            "shadow_width": 2.0,
            "bold": True,
            "max_lines": 2,
            "active_color": "#FFD1E3",
        },
        "suspense_noir": {
            "font_size": 56,
            "text_color": "#FFFFFF",
            "outline_color": "#05070D",
            "outline_width": 5,
            "shadow_width": 3.0,
            "background_color": "#05070D",
            "background_opacity": 0.22,
            "bold": True,
            "max_lines": 2,
        },
        "confession_clean": {
            "font_family": "Segoe UI",
            "font_size": 48,
            "text_color": "#FFFFFF",
            "outline_color": "#172033",
            "outline_width": 2,
            "shadow_width": 0.0,
            "background_color": "#172033",
            "background_opacity": 0.72,
            "horizontal_margin": 200,
            "bold": False,
            "max_lines": 2,
        },
        "golden_hook": {
            "font_size": 58,
            "text_color": "#FFE06A",
            "outline_color": "#15100A",
            "outline_width": 5,
            "shadow_width": 2.0,
            "bold": True,
            "max_lines": 2,
        },
        "midnight_reader": {
            "font_family": "Georgia",
            "font_size": 50,
            "text_color": "#E8EEFF",
            "outline_color": "#14204A",
            "outline_width": 4,
            "shadow_width": 1.0,
            "background_color": "#101936",
            "background_opacity": 0.42,
            "max_chars_per_line": 30,
            "max_lines": 3,
        },
    },
    "intro_card": {
        "editorial_white": {},
        "cover_story_dark": {
            "layout": "cover_split",
            "font_family": "Segoe UI",
            "headline_font_size": 62,
            "headline_color": "#FFFFFF",
            "body_font_size": 44,
            "body_color": "#F8FAFC",
            "label_font_size": 22,
            "label_color": "#FF6B4A",
            "background_color": "#0B1220",
            "background_opacity": 0.94,
            "border_color": "#42516B",
            "border_width": 2,
            "shadow_opacity": 0.46,
            "width_percent": 84.0,
            "position_y_percent": 27.0,
            "padding": 52,
            "radius": 28,
            "text_alignment": "left",
            "max_lines": 8,
        },
        "cover_story_noir": {
            "layout": "cover_split_noir",
            "font_family": "Segoe UI",
            "headline_font_size": 64,
            "headline_color": "#FFFFFF",
            "body_font_size": 44,
            "body_color": "#FFF7ED",
            "label_font_size": 22,
            "label_color": "#FF7657",
            "background_color": "#080B12",
            "background_opacity": 0.96,
            "border_color": "#596274",
            "border_width": 2,
            "shadow_opacity": 0.58,
            "width_percent": 85.0,
            "position_y_percent": 27.0,
            "padding": 50,
            "radius": 24,
            "text_alignment": "left",
            "max_lines": 8,
        },
        "cinematic_dark": {
            "headline_color": "#FFE06A",
            "body_color": "#F8FAFC",
            "label_color": "#CBD5E1",
            "background_color": "#111827",
            "background_opacity": 0.94,
            "border_color": "#334155",
            "border_width": 2,
            "shadow_opacity": 0.55,
        },
        "romance_soft": {
            "font_family": "Georgia",
            "headline_color": "#FFF1F2",
            "body_color": "#4C2433",
            "label_color": "#9F365F",
            "background_color": "#FFF1F5",
            "border_color": "#F9A8D4",
            "border_width": 2,
            "radius": 42,
        },
        "minimal_clean": {
            "headline_color": "#FFFFFF",
            "body_color": "#111827",
            "label_color": "#475467",
            "background_opacity": 0.94,
            "shadow_opacity": 0.18,
            "radius": 18,
        },
        "social_post": {
            "font_family": "Segoe UI",
            "headline_font_size": 58,
            "headline_color": "#2446C8",
            "body_font_size": 34,
            "body_color": "#172033",
            "label_color": "#667085",
            "background_color": "#FFFFFF",
            "background_opacity": 0.98,
            "border_color": "#D7E0FF",
            "border_width": 2,
            "shadow_opacity": 0.24,
            "width_percent": 70.0,
            "padding": 44,
            "radius": 28,
        },
        "paper_note": {
            "font_family": "Georgia",
            "headline_font_size": 56,
            "headline_color": "#6B4423",
            "body_color": "#3F3024",
            "label_color": "#8A6A4D",
            "background_color": "#FFF8E7",
            "background_opacity": 0.98,
            "border_color": "#E7D3AD",
            "border_width": 2,
            "shadow_opacity": 0.22,
            "width_percent": 68.0,
            "padding": 52,
            "radius": 18,
        },
        "golden_luxe": {
            "font_family": "Georgia",
            "headline_font_size": 60,
            "headline_color": "#F8D878",
            "body_color": "#FFF8E6",
            "label_color": "#D6B55E",
            "background_color": "#17130E",
            "background_opacity": 0.96,
            "border_color": "#CBA94A",
            "border_width": 3,
            "shadow_opacity": 0.48,
            "padding": 54,
            "radius": 24,
        },
        "suspense_red": {
            "headline_font_size": 64,
            "headline_color": "#FF5A67",
            "body_color": "#F8FAFC",
            "label_color": "#FFB4BC",
            "background_color": "#140E16",
            "background_opacity": 0.96,
            "border_color": "#D83A4A",
            "border_width": 3,
            "shadow_opacity": 0.58,
            "width_percent": 68.0,
            "padding": 50,
            "radius": 20,
        },
        "blue_glass": {
            "font_family": "Segoe UI",
            "headline_font_size": 58,
            "headline_color": "#DDE9FF",
            "body_color": "#F6F9FF",
            "label_color": "#AFC7FF",
            "background_color": "#15264F",
            "background_opacity": 0.88,
            "border_color": "#6C91E6",
            "border_width": 2,
            "shadow_opacity": 0.38,
            "width_percent": 70.0,
            "padding": 48,
            "radius": 34,
        },
        "warm_story": {
            "font_family": "Georgia",
            "headline_font_size": 58,
            "headline_color": "#7C3F24",
            "body_color": "#4B3125",
            "label_color": "#A45E3F",
            "background_color": "#FFF0E2",
            "background_opacity": 0.97,
            "border_color": "#EAB99C",
            "border_width": 2,
            "shadow_opacity": 0.25,
            "padding": 50,
            "radius": 36,
        },
    },
    "code_card": {
        "brand_pill": {},
        "dark_glass": {
            "background_color": "#101828",
            "text_color": "#FFFFFF",
            "opacity": 0.78,
            "outline_color": "#475467",
            "outline_width": 1.0,
            "radius": 24,
        },
        "light_chip": {
            "background_color": "#FFFFFF",
            "text_color": "#172033",
            "opacity": 0.94,
            "outline_color": "#D0D5DD",
            "outline_width": 1.0,
            "radius": 14,
        },
        "outline_only": {
            "background_color": "#000000",
            "text_color": "#FFFFFF",
            "opacity": 0.18,
            "outline_color": "#FFFFFF",
            "outline_width": 2.0,
            "radius": 12,
        },
        "warning_red": {
            "font_size": 42,
            "text_color": "#FFFFFF",
            "background_color": "#C83245",
            "opacity": 0.94,
            "outline_color": "#FFB3BC",
            "outline_width": 1.0,
            "radius": 16,
        },
        "golden_ticket": {
            "font_family": "Georgia",
            "font_size": 42,
            "text_color": "#F8D878",
            "background_color": "#17130E",
            "opacity": 0.94,
            "outline_color": "#CBA94A",
            "outline_width": 2.0,
            "radius": 12,
        },
        "romance_blush": {
            "font_family": "Georgia",
            "font_size": 40,
            "text_color": "#7A2448",
            "background_color": "#FFF1F5",
            "opacity": 0.96,
            "outline_color": "#F2A6C2",
            "outline_width": 1.0,
            "radius": 24,
        },
        "minimal_dark": {
            "font_family": "Segoe UI",
            "font_size": 38,
            "text_color": "#FFFFFF",
            "background_color": "#101828",
            "opacity": 0.88,
            "outline_color": "#101828",
            "outline_width": 0.0,
            "padding": 12,
            "radius": 8,
        },
    },
    "outro_card": {
        "editorial_white": {},
        "cinematic_dark": {
            "title_color": "#FFFFFF",
            "body_color": "#E2E8F0",
            "code_color": "#FFE06A",
            "background_color": "#0F172A",
            "background_opacity": 0.95,
            "border_color": "#334155",
            "border_width": 2,
        },
        "brand_focus": {
            "title_color": "#172033",
            "body_color": "#344054",
            "code_font_size": 62,
            "code_color": "#2446C8",
            "background_color": "#FFFFFF",
            "border_color": "#2446C8",
            "border_width": 3,
        },
        "minimal_clean": {
            "title_font_size": 52,
            "body_font_size": 34,
            "background_opacity": 0.90,
            "border_color": "#D0D5DD",
            "border_width": 1,
            "radius": 18,
        },
    },
}


def visual_style_presets() -> dict[str, dict[str, dict[str, Any]]]:
    """Return complete, detached preset values safe for UI serialization."""

    style_types = {
        "subtitle": SubtitleStyle,
        "intro_card": IntroCardStyle,
        "code_card": CodeCardStyle,
        "outro_card": OutroCardStyle,
    }
    return {
        kind: {
            name: {**asdict(style_types[kind]()), **values}
            for name, values in presets.items()
        }
        for kind, presets in VISUAL_STYLE_PRESETS.items()
    }


def _style_from_patch(
    style_type: type[Any],
    patch: Any,
    *,
    preset_kind: str,
    preset_name: str,
) -> Any:
    defaults = style_type()
    allowed = asdict(defaults)
    preset = VISUAL_STYLE_PRESETS.get(preset_kind, {}).get(preset_name, {})
    incoming = patch if isinstance(patch, dict) else {}
    return style_type(
        **{
            **allowed,
            **{key: value for key, value in preset.items() if key in allowed},
            **{key: value for key, value in incoming.items() if key in allowed},
        }
    )


@dataclass(slots=True)
class ProviderSettings:
    text_provider: str = "local"
    text_model: str = ""
    text_endpoint: str = ""
    text_api_key: str = ""
    tts_provider: str = "local_kokoro"
    tts_endpoint: str = ""
    tts_api_key: str = ""
    kokoro_endpoint: str = ""
    kokoro_command: str = ""
    monthly_character_limit: int = 0
    allow_provider_fallback: bool = True


@dataclass(slots=True)
class HubSettings:
    mode: str = "local"
    endpoint: str = "http://127.0.0.1:8765"
    access_token: str = ""
    account_username: str = ""
    # ``installation_id`` is generated once and follows this installation
    # across display-name changes.  Hub-issued ``device_id`` is deliberately
    # separate: it is the server-side identity bound to the bearer token.
    # Installation identity is runtime state, not a user-facing recipe value;
    # exclude it from dataclass equality so two default settings objects still
    # compare by their actual configuration.
    installation_id: str = field(
        default_factory=lambda: str(uuid4()), compare=False
    )
    device_id: str = ""
    device_name: str = field(default_factory=socket.gethostname)
    app_version: str = __version__
    capabilities: dict[str, Any] = field(
        default_factory=lambda: {
            "device_config_sync": 1,
            "production_contract": 2,
            "local_render": True,
            "local_tts": True,
            "local_subtitles": True,
        }
    )
    applied_config_revision_id: str = ""
    applied_config_hash: str = ""
    listen_host: str = "0.0.0.0"
    listen_port: int = 8765
    # Production artifacts are workstation-local by contract.  Keep these
    # legacy fields for settings-file/API compatibility, but they are always
    # normalized to ``False`` and can no longer enable media uploads.
    share_previews: bool = False
    share_narration: bool = False
    auto_update_enabled: bool = True
    auto_download_updates: bool = True
    update_check_minutes: int = 1
    web_allowed_roots: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AppSettings:
    language: str = "en-US"
    retention_min: float = 0.85
    retention_max: float = 0.90
    adult_mode: str = "engaging"
    narration_wpm: int = 240
    output_mode: str = "video_and_mp3"
    video_playback_speed: float = 1.0
    video_transition: str = "cut"
    subtitle_word_mode: str = "off"
    chapter_pause_seconds: float = 0.8
    output_width: int = 1080
    output_height: int = 1920
    output_fps: int = 60
    export_narration_audio: bool = False
    video_encoder: str = "auto"
    bgm_volume: float = 0.28
    bgm_mode: str = "auto"
    bgm_file: str = ""
    caption_mode: str = "semantic"
    subtitle_preset: str = "clear_outline"
    intro_card_preset: str = "editorial_white"
    code_card_preset: str = "brand_pill"
    outro_card_preset: str = "editorial_white"
    subtitle_animation: str = "none"
    intro_animation: str = "fade_rise"
    # ``None`` is accepted only at construction time so old direct callers can
    # infer the switch from their legacy template.  ``__post_init__`` always
    # resolves it to a real bool before settings are exposed or serialized.
    intro_card_enabled: bool | None = None
    intro_card_start_seconds: float = 0.0
    intro_card_duration_seconds: float = 5.5
    code_card_enabled: bool = True
    code_card_start_seconds: float = 0.0
    # Schema 0 only: zero means "from the absolute start above through the end
    # of the video". Schema 1 uses ``code_card_display_mode`` explicitly.
    code_card_duration_seconds: float = 0.0
    # Schema 0 preserves frozen pre-hybrid jobs. Schema 1 resolves explicit
    # start/display modes against the actual narrated body duration.
    card_timeline_schema_version: int = 1
    intro_card_start_mode: str = "seconds"
    intro_card_start_value: float = 0.0
    intro_card_display_mode: str = "seconds"
    intro_card_display_value: float = 5.5
    code_card_start_mode: str = "seconds"
    code_card_start_value: float = 0.0
    code_card_display_mode: str = "body_end"
    code_card_display_value: float = 0.0
    preview_seconds: int = DEFAULT_PREVIEW_SECONDS
    max_episode_minutes: float = 10.0
    cover_animation: str = "gentle_push"
    cover_outro_enabled: bool = True
    color_grade: str = "neutral"
    end_card_seconds: float = 6.0
    render_mode: str = "speed"
    video_template: str = "classic"
    voice_by_mood: dict[str, str] = field(
        default_factory=lambda: {
            "suspense": "dramatic",
            "romance": "warm",
            "sad": "calm",
            "revenge": "confident",
        }
    )
    subtitle: SubtitleStyle = field(default_factory=SubtitleStyle)
    intro_card: IntroCardStyle = field(default_factory=IntroCardStyle)
    code_card: CodeCardStyle = field(default_factory=CodeCardStyle)
    outro_card: OutroCardStyle = field(default_factory=OutroCardStyle)
    providers: ProviderSettings = field(default_factory=ProviderSettings)
    hub: HubSettings = field(default_factory=HubSettings)

    def __post_init__(self) -> None:
        if self.intro_card_enabled is None:
            self.intro_card_enabled = self.video_template == "platform_story_card"
        if not isinstance(self.intro_card_enabled, bool):
            raise ValueError("intro_card_enabled must be a boolean")
        if not isinstance(self.code_card_enabled, bool):
            raise ValueError("code_card_enabled must be a boolean")
        for name in ("intro_card_start_seconds", "code_card_start_seconds"):
            number = float(getattr(self, name))
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"{name} must be a non-negative finite number")
            setattr(self, name, number)
        code_duration = float(self.code_card_duration_seconds)
        if not math.isfinite(code_duration) or code_duration < 0:
            raise ValueError(
                "code_card_duration_seconds must be a non-negative finite number"
            )
        self.code_card_duration_seconds = code_duration
        if isinstance(self.card_timeline_schema_version, bool):
            raise ValueError("card_timeline_schema_version must be 0 or 1")
        self.card_timeline_schema_version = int(self.card_timeline_schema_version)
        if self.card_timeline_schema_version not in {0, 1}:
            raise ValueError("card_timeline_schema_version must be 0 or 1")
        for prefix in ("intro_card", "code_card"):
            start_mode = str(getattr(self, f"{prefix}_start_mode") or "").strip().casefold()
            display_mode = str(getattr(self, f"{prefix}_display_mode") or "").strip().casefold()
            if start_mode not in {"seconds", "body_percent"}:
                raise ValueError(f"{prefix}_start_mode is invalid")
            if display_mode not in {"seconds", "body_percent", "body_end"}:
                raise ValueError(f"{prefix}_display_mode is invalid")
            setattr(self, f"{prefix}_start_mode", start_mode)
            setattr(self, f"{prefix}_display_mode", display_mode)
            for suffix in ("start_value", "display_value"):
                name = f"{prefix}_{suffix}"
                number = float(getattr(self, name))
                if not math.isfinite(number) or number < 0:
                    raise ValueError(f"{name} must be a non-negative finite number")
                if (
                    (suffix == "start_value" and start_mode == "body_percent")
                    or (suffix == "display_value" and display_mode == "body_percent")
                ) and number > 100:
                    raise ValueError(f"{name} must not exceed 100 percent")
                if (
                    suffix == "display_value"
                    and display_mode in {"seconds", "body_percent"}
                    and number <= 0
                ):
                    raise ValueError(
                        f"{name} must be positive unless display_mode is body_end"
                    )
                setattr(self, name, number)
            if (
                self.card_timeline_schema_version == 1
                and bool(getattr(self, f"{prefix}_enabled"))
                and start_mode == "body_percent"
                and float(getattr(self, f"{prefix}_start_value")) >= 100
            ):
                raise ValueError(
                    f"{prefix}_start_value must be below 100 percent when the card is enabled"
                )

    def to_dict(self, *, redact_secrets: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if redact_secrets:
            provider_data = data["providers"]
            provider_data["text_api_key"] = "" if not provider_data["text_api_key"] else "********"
            provider_data["tts_api_key"] = "" if not provider_data["tts_api_key"] else "********"
            hub_data = data["hub"]
            hub_data["access_token"] = "" if not hub_data["access_token"] else "********"
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AppSettings":
        if not isinstance(value, dict):
            value = {}
        value = normalize_retired_subtitle_settings(value)
        defaults = cls()
        subtitle_patch = value.get("subtitle")
        intro_card_patch = value.get("intro_card")
        code_card_patch = value.get("code_card")
        outro_card_patch = value.get("outro_card")
        provider_patch = value.get("providers")
        hub_patch = value.get("hub")
        subtitle_preset = str(
            value.get("subtitle_preset") or defaults.subtitle_preset
        ).strip().casefold()
        intro_card_preset = str(
            value.get("intro_card_preset") or defaults.intro_card_preset
        ).strip().casefold()
        code_card_preset = str(
            value.get("code_card_preset") or defaults.code_card_preset
        ).strip().casefold()
        outro_card_preset = str(
            value.get("outro_card_preset") or defaults.outro_card_preset
        ).strip().casefold()
        for kind, selected, fallback in (
            ("subtitle", subtitle_preset, defaults.subtitle_preset),
            ("intro_card", intro_card_preset, defaults.intro_card_preset),
            ("code_card", code_card_preset, defaults.code_card_preset),
            ("outro_card", outro_card_preset, defaults.outro_card_preset),
        ):
            if selected not in VISUAL_STYLE_PRESETS[kind]:
                if kind == "subtitle":
                    subtitle_preset = fallback
                elif kind == "intro_card":
                    intro_card_preset = fallback
                elif kind == "code_card":
                    code_card_preset = fallback
                else:
                    outro_card_preset = fallback
        subtitle = _style_from_patch(
            SubtitleStyle,
            subtitle_patch,
            preset_kind="subtitle",
            preset_name=subtitle_preset,
        )
        intro_card = _style_from_patch(
            IntroCardStyle,
            intro_card_patch,
            preset_kind="intro_card",
            preset_name=intro_card_preset,
        )
        code_card = _style_from_patch(
            CodeCardStyle,
            code_card_patch,
            preset_kind="code_card",
            preset_name=code_card_preset,
        )
        outro_card = _style_from_patch(
            OutroCardStyle,
            outro_card_patch,
            preset_kind="outro_card",
            preset_name=outro_card_preset,
        )
        providers = ProviderSettings(
            **{
                **asdict(defaults.providers),
                **(
                    {
                        key: item
                        for key, item in provider_patch.items()
                        if key in asdict(defaults.providers)
                    }
                    if isinstance(provider_patch, dict)
                    else {}
                ),
            }
        )
        hub = HubSettings(
            **{
                **asdict(defaults.hub),
                **(
                    {
                        key: item
                        for key, item in hub_patch.items()
                        if key in asdict(defaults.hub)
                    }
                    if isinstance(hub_patch, dict)
                    else {}
                ),
            }
        )
        # Production MP4/MP3, preview renders, narration and alignment files
        # never leave the workstation that created them.  Old settings and
        # forged API payloads cannot reopen the retired upload path.
        hub.share_previews = False
        hub.share_narration = False
        try:
            hub.installation_id = str(UUID(str(hub.installation_id).strip()))
        except (AttributeError, TypeError, ValueError):
            hub.installation_id = str(uuid4())
        hub.device_id = str(hub.device_id or "").strip()[:120]
        hub.app_version = str(hub.app_version or __version__).strip()[:80]
        default_capabilities = dict(defaults.hub.capabilities)
        incoming_capabilities = hub.capabilities
        if isinstance(incoming_capabilities, dict):
            for name, expected in tuple(default_capabilities.items()):
                raw = incoming_capabilities.get(name, expected)
                if isinstance(expected, bool):
                    default_capabilities[name] = (
                        raw if isinstance(raw, bool) else expected
                    )
                else:
                    try:
                        default_capabilities[name] = max(1, min(100, int(raw)))
                    except (TypeError, ValueError):
                        default_capabilities[name] = expected
        hub.capabilities = default_capabilities
        hub.applied_config_revision_id = str(
            hub.applied_config_revision_id or ""
        ).strip()[:120]
        applied_hash = str(hub.applied_config_hash or "").strip().casefold()
        hub.applied_config_hash = (
            applied_hash
            if len(applied_hash) == 64
            and all(character in "0123456789abcdef" for character in applied_hash)
            else ""
        )
        if not isinstance(hub.web_allowed_roots, list):
            hub.web_allowed_roots = []
        else:
            hub.web_allowed_roots = [
                str(item).strip()
                for item in hub.web_allowed_roots
                if str(item).strip()
            ][:32]
        legacy_voices = {
            "af_heart": "dramatic",
            "af_bella": "warm",
            "af_nicole": "calm",
            "af_sarah": "confident",
        }
        allowed_voice_profiles = {"dramatic", "warm", "calm", "confident"}
        voice_by_mood = dict(defaults.voice_by_mood)
        incoming_voices = value.get("voice_by_mood")
        if isinstance(incoming_voices, dict):
            for mood in voice_by_mood:
                profile = str(incoming_voices.get(mood) or "").strip().casefold()
                profile = legacy_voices.get(profile, profile)
                if profile in allowed_voice_profiles:
                    voice_by_mood[mood] = profile
        scalar = {
            key: value.get(key, getattr(defaults, key))
            for key in (
                "language",
                "retention_min",
                "retention_max",
                "adult_mode",
                "narration_wpm",
                "output_mode",
                "video_playback_speed",
                "video_transition",
                "subtitle_word_mode",
                "chapter_pause_seconds",
                "output_width",
                "output_height",
                "output_fps",
                "export_narration_audio",
                "video_encoder",
                "bgm_volume",
                "bgm_mode",
                "bgm_file",
                "caption_mode",
                "subtitle_preset",
                "intro_card_preset",
                "code_card_preset",
                "outro_card_preset",
                "subtitle_animation",
                "intro_animation",
                "intro_card_enabled",
                "intro_card_start_seconds",
                "intro_card_duration_seconds",
                "code_card_enabled",
                "code_card_start_seconds",
                "code_card_duration_seconds",
                "card_timeline_schema_version",
                "intro_card_start_mode",
                "intro_card_start_value",
                "intro_card_display_mode",
                "intro_card_display_value",
                "code_card_start_mode",
                "code_card_start_value",
                "code_card_display_mode",
                "code_card_display_value",
                "preview_seconds",
                "max_episode_minutes",
                "cover_animation",
                "cover_outro_enabled",
                "color_grade",
                "end_card_seconds",
                "render_mode",
                "video_template",
            )
        }
        try:
            parsed_output_fps = int(scalar["output_fps"])
        except (TypeError, ValueError):
            parsed_output_fps = defaults.output_fps
        scalar["output_fps"] = (
            parsed_output_fps if parsed_output_fps in {30, 60} else defaults.output_fps
        )
        enum_fields = {
            "output_mode": {"video_and_mp3", "audio_only", "reuse_audio"},
            "video_transition": {"cut", "fade"},
            "subtitle_word_mode": {"off", "cumulative", "single"},
            "bgm_mode": {"auto", "manual", "none"},
        }
        for key, allowed in enum_fields.items():
            normalized = str(scalar[key] or "").strip().casefold()
            scalar[key] = normalized if normalized in allowed else getattr(defaults, key)
        scalar["bgm_file"] = str(scalar["bgm_file"] or "").strip()
        try:
            playback_speed = float(scalar["video_playback_speed"])
        except (TypeError, ValueError):
            playback_speed = defaults.video_playback_speed
        scalar["video_playback_speed"] = (
            playback_speed
            if 0.8 <= playback_speed <= 3.0
            else defaults.video_playback_speed
        )
        try:
            narration_wpm = float(scalar["narration_wpm"])
        except (TypeError, ValueError):
            narration_wpm = defaults.narration_wpm
        scalar["narration_wpm"] = (
            int(round(narration_wpm))
            if 200 <= narration_wpm <= 280
            else defaults.narration_wpm
        )
        raw_intro_enabled = value.get("intro_card_enabled")
        scalar["intro_card_enabled"] = (
            raw_intro_enabled
            if isinstance(raw_intro_enabled, bool)
            else str(scalar.get("video_template") or "classic")
            == "platform_story_card"
        )
        for start_key in ("intro_card_start_seconds", "code_card_start_seconds"):
            try:
                start_value = float(scalar[start_key])
            except (TypeError, ValueError):
                start_value = getattr(defaults, start_key)
            scalar[start_key] = (
                start_value
                if math.isfinite(start_value) and start_value >= 0
                else getattr(defaults, start_key)
            )
        try:
            code_card_duration = float(scalar["code_card_duration_seconds"])
        except (TypeError, ValueError):
            code_card_duration = defaults.code_card_duration_seconds
        scalar["code_card_duration_seconds"] = (
            code_card_duration
            if math.isfinite(code_card_duration) and code_card_duration >= 0
            else defaults.code_card_duration_seconds
        )
        try:
            intro_card_duration = float(scalar["intro_card_duration_seconds"])
        except (TypeError, ValueError):
            intro_card_duration = defaults.intro_card_duration_seconds
        scalar["intro_card_duration_seconds"] = (
            intro_card_duration
            if 2.5 <= intro_card_duration <= 8.0
            else defaults.intro_card_duration_seconds
        )
        try:
            timeline_schema = int(scalar["card_timeline_schema_version"])
        except (TypeError, ValueError):
            timeline_schema = defaults.card_timeline_schema_version
        scalar["card_timeline_schema_version"] = (
            timeline_schema if timeline_schema in {0, 1} else 1
        )
        if "intro_card_start_mode" not in value:
            scalar["intro_card_start_mode"] = "seconds"
            scalar["intro_card_start_value"] = scalar["intro_card_start_seconds"]
            scalar["intro_card_display_mode"] = "seconds"
            scalar["intro_card_display_value"] = scalar["intro_card_duration_seconds"]
        if "code_card_start_mode" not in value:
            scalar["code_card_start_mode"] = "seconds"
            scalar["code_card_start_value"] = scalar["code_card_start_seconds"]
            if scalar["code_card_duration_seconds"] == 0:
                scalar["code_card_display_mode"] = "body_end"
                scalar["code_card_display_value"] = 0.0
            else:
                scalar["code_card_display_mode"] = "seconds"
                scalar["code_card_display_value"] = scalar[
                    "code_card_duration_seconds"
                ]
        for prefix in ("intro_card", "code_card"):
            start_key = f"{prefix}_start_mode"
            display_key = f"{prefix}_display_mode"
            normalized_start_mode = str(scalar[start_key] or "").strip().casefold()
            normalized_display_mode = str(scalar[display_key] or "").strip().casefold()
            scalar[start_key] = (
                normalized_start_mode
                if normalized_start_mode in {"seconds", "body_percent"}
                else getattr(defaults, start_key)
            )
            scalar[display_key] = (
                normalized_display_mode
                if normalized_display_mode in {"seconds", "body_percent", "body_end"}
                else getattr(defaults, display_key)
            )
            for suffix in ("start_value", "display_value"):
                name = f"{prefix}_{suffix}"
                try:
                    number = float(scalar[name])
                except (TypeError, ValueError):
                    number = float(getattr(defaults, name))
                if not math.isfinite(number) or number < 0:
                    number = float(getattr(defaults, name))
                if (
                    (suffix == "start_value" and scalar[start_key] == "body_percent")
                    or (suffix == "display_value" and scalar[display_key] == "body_percent")
                ) and number > 100:
                    number = float(getattr(defaults, name))
                scalar[name] = number
        for boolean_key in (
            "export_narration_audio",
            "cover_outro_enabled",
            "code_card_enabled",
        ):
            raw_boolean = scalar[boolean_key]
            scalar[boolean_key] = (
                raw_boolean
                if isinstance(raw_boolean, bool)
                else getattr(defaults, boolean_key)
            )
        scalar["voice_by_mood"] = voice_by_mood
        scalar["subtitle_preset"] = subtitle_preset
        scalar["intro_card_preset"] = intro_card_preset
        scalar["code_card_preset"] = code_card_preset
        scalar["outro_card_preset"] = outro_card_preset
        return cls(
            **scalar,
            subtitle=subtitle,
            intro_card=intro_card,
            code_card=code_card,
            outro_card=outro_card,
            providers=providers,
            hub=hub,
        )


@dataclass(slots=True)
class BatchSpec:
    platform_id: str
    text_folder: str
    video_folder: str
    music_folder: str
    output_folder: str
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=utc_now)
    novel_id: str = ""
    revision_id: str = ""
    listing_id: str = ""
    promo_code_id: str = ""
    promo_code_snapshot: str = ""
    episode_ids: tuple[str, ...] = ()
    creative_count: int = 1
    publishing_account_id: str = ""
    preview_required: bool = False
    output_mode: str = "video_and_mp3"
    source_narration_audio: str = ""
    # Compatibility only for pre-V0.4 batch payloads. New code freezes the
    # explicit output_mode on each RenderJob.
    export_narration_audio: bool = False

    def __post_init__(self) -> None:
        self.episode_ids = tuple(str(item) for item in self.episode_ids)
        self.creative_count = max(1, int(self.creative_count))
        self.output_mode = str(self.output_mode or "video_and_mp3").strip().casefold()
        if self.output_mode not in {"video_and_mp3", "audio_only", "reuse_audio"}:
            raise ValueError(
                "output_mode must be video_and_mp3, audio_only, or reuse_audio"
            )
        self.source_narration_audio = str(self.source_narration_audio or "").strip()
        if self.output_mode == "reuse_audio" and not self.source_narration_audio:
            raise ValueError("source_narration_audio is required for reuse_audio")
        if not isinstance(self.export_narration_audio, bool):
            raise ValueError("export_narration_audio must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RenderJob:
    batch_id: str
    platform_id: str
    source_file: str
    title: str
    code: str
    video_folder: str
    music_folder: str
    output_folder: str
    id: str = field(default_factory=lambda: uuid4().hex)
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    stage_label: str = "等待处理"
    # Canonical, serial pipeline telemetry.  The Chinese ``stage_label`` stays
    # user-facing; these stable keys let Hub diagnostics compare machines and
    # versions without parsing translated copy.
    pipeline_stage: str = "queued"
    pipeline_stage_started_at: str = field(default_factory=utc_now)
    pipeline_stage_history: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    error_log: str = ""
    # A small, sanitized diagnostic snapshot may be synchronized to the Hub.
    # The full log stays on the render machine.  Explicit artifact/output path
    # fields may be recorded so that the originating employee computer can
    # reopen its own local results; free-form diagnostic text is sanitized at
    # the API synchronization boundary.
    failure_diagnostics: dict[str, Any] = field(default_factory=dict)
    output_file: str = ""
    narration_audio_file: str = ""
    # ``output_folder`` remains the employee-selected root.  Completed media
    # is written to one flat folder per production run, while every technical
    # sidecar lives under the application's private data directory.
    publish_batch_folder: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    novel_id: str = ""
    revision_id: str = ""
    episode_id: str = ""
    # ``episode_id`` remains the compatibility anchor used by older catalog
    # rows and foreign keys.  New merged-episode batches carry the complete
    # ordered selection here and expose a stable human-readable filename
    # label (for example ``E001-E003``).
    episode_ids: tuple[str, ...] = ()
    episode_label: str = ""
    listing_id: str = ""
    promo_code_id: str = ""
    promo_code_snapshot: str = ""
    production_draft_id: str = ""
    production_run_id: str = ""
    production_record_id: str = ""
    # Frozen renderer contract for this individual task.  Snapshots written
    # before the field existed intentionally deserialize as contract one.
    required_production_contract: int = 1
    publishing_account_id: str = ""
    publishing_account_label: str = ""
    # ``batch_total_count`` and ``batch_ordinal`` describe the complete
    # durable production run, not merely the bounded in-memory stream window.
    # Zero remains the compatibility sentinel for snapshots written before
    # these fields existed.
    batch_total_count: int = 0
    batch_ordinal: int = 0
    episode_number: int = 1
    # Older in-memory/serialized jobs do not carry series context.  Zero/False
    # deliberately falls back to the neutral STORY BRIEF card instead of
    # incorrectly labelling an unknown episode as the finale.
    episode_count: int = 0
    is_final_episode: bool = False
    variant_index: int = 1
    variant_count: int = 1
    variant_seed: int = 0
    job_kind: str = "full"
    preview_file: str = ""
    preview_uri: str = ""
    preview_approved: bool = False
    locked_voice_provider: str = ""
    locked_voice_id: str = ""
    story_mood: str = ""
    story_mood_source: str = "auto"
    content_fingerprint: str = ""
    recipe_hash: str = ""
    cover_path: str = ""
    cover_outro_enabled: bool = True
    intro_card_text: str = ""
    intro_card_source: str = ""
    platform_search_text: str = ""
    platform_ending_text: str = ""
    platform_copy_schema_version: int = 0
    platform_name_snapshot: str = ""
    platform_search_template_snapshot: str = ""
    platform_ending_template_snapshot: str = ""
    platform_authoritative_ending_text: str = ""
    platform_ending_prefix: str = ""
    platform_ending_suffix: str = ""
    card_timeline_resolved: dict[str, Any] = field(default_factory=dict)
    production_preset_id: str = ""
    production_preset_revision: int = 0
    production_preset_hash: str = ""
    production_preset_dirty: bool = False
    settings_snapshot: dict[str, Any] = field(default_factory=dict)
    archived: bool = False
    archived_at: str = ""
    archived_by_user_id: str = ""

    def __post_init__(self) -> None:
        normalized_episode_ids = tuple(
            str(item).strip() for item in self.episode_ids if str(item).strip()
        )
        if not normalized_episode_ids and self.episode_id:
            normalized_episode_ids = (str(self.episode_id),)
        self.episode_ids = normalized_episode_ids
        if not self.episode_id and normalized_episode_ids:
            self.episode_id = normalized_episode_ids[0]
        self.episode_label = str(self.episode_label or "").strip()
        self.batch_total_count = max(0, int(self.batch_total_count or 0))
        self.batch_ordinal = max(0, int(self.batch_ordinal or 0))
        if isinstance(self.required_production_contract, bool):
            raise ValueError("required_production_contract must be a positive integer")
        self.required_production_contract = int(
            self.required_production_contract or 1
        )
        if self.required_production_contract < 1:
            raise ValueError("required_production_contract must be a positive integer")
        self.platform_search_text = normalize_platform_copy(
            self.platform_search_text, "platform_search_text"
        )
        self.platform_ending_text = normalize_platform_copy(
            self.platform_ending_text, "platform_ending_text"
        )
        self.platform_copy_schema_version = max(
            0, int(self.platform_copy_schema_version or 0)
        )
        self.platform_authoritative_ending_text = normalize_platform_copy(
            self.platform_authoritative_ending_text, "platform_ending_text"
        )
        self.platform_ending_prefix = normalize_platform_copy(
            self.platform_ending_prefix, "platform_ending_prefix"
        )
        self.platform_ending_suffix = normalize_platform_copy(
            self.platform_ending_suffix, "platform_ending_suffix"
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RenderJob":
        """Rebuild a queue item from a durable archive snapshot."""

        if not isinstance(value, dict):
            raise TypeError("job snapshot must be an object")
        allowed = {item.name for item in fields(cls)}
        payload = {key: item for key, item in value.items() if key in allowed}
        if not isinstance(payload.get("cover_outro_enabled", True), bool):
            payload["cover_outro_enabled"] = True
        try:
            payload["status"] = JobStatus(str(payload.get("status") or "queued"))
        except ValueError as error:
            raise ValueError("job snapshot has an invalid status") from error
        return cls(**payload)
