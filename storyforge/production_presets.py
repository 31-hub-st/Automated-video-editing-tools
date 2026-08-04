from __future__ import annotations

import json
import hashlib
import math
import os
import re
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .models import (
    COLOR_GRADES,
    COVER_ANIMATIONS,
    INTRO_ANIMATIONS,
    SUBTITLE_ANIMATIONS,
    normalize_retired_subtitle_settings,
)
from .style_options import preset_names, validate_style_patch


PRESET_SCHEMA_VERSION = 5
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FORBIDDEN_RECIPE_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "password",
        "providers",
        "hub",
        "command",
        "ffmpeg_path",
        "novel_id",
        "platform_id",
        "promo_code_id",
        "episode_ids",
        "publishing_account_id",
        "video_folder",
        "music_folder",
        "output_folder",
        "source_file",
        "output_file",
        "cover_path",
        "local_path",
        "device_id",
        "voice_id",
        "voice",
    }
)
_PRODUCTION_ENUMS: dict[str, frozenset[str]] = {
    "output_mode": frozenset({"video_and_mp3", "audio_only", "reuse_audio"}),
    "video_transition": frozenset({"cut", "fade"}),
    "subtitle_word_mode": frozenset({"off", "cumulative", "single"}),
    "bgm_mode": frozenset({"auto", "manual", "none"}),
    "adult_mode": frozenset({"direct", "engaging"}),
    "caption_mode": frozenset({"semantic", "sentence"}),
    "subtitle_preset": frozenset(preset_names("subtitle")),
    "intro_card_preset": frozenset(preset_names("intro_card")),
    "code_card_preset": frozenset(preset_names("code_card")),
    "outro_card_preset": frozenset(preset_names("outro_card")),
    "subtitle_animation": SUBTITLE_ANIMATIONS,
    "intro_animation": INTRO_ANIMATIONS,
    "cover_animation": COVER_ANIMATIONS,
    "color_grade": COLOR_GRADES,
    "render_mode": frozenset({"speed", "quality", "compatibility"}),
    "video_template": frozenset({"classic", "platform_story_card"}),
}
_PRODUCTION_NUMBERS: dict[str, tuple[float, float, bool]] = {
    "retention_min": (0.50, 1.0, False),
    "retention_max": (0.50, 1.0, False),
    "narration_wpm": (200, 280, True),
    "video_playback_speed": (0.8, 3.0, False),
    "chapter_pause_seconds": (0.0, 3.0, False),
    "bgm_volume": (0.05, 0.50, False),
    "max_episode_minutes": (1.0, 60.0, False),
    # The full-screen cover ending is deliberately a brief 5–7 second CTA.
    "end_card_seconds": (5.0, 7.0, False),
    "intro_card_duration_seconds": (2.5, 8.0, False),
}
_ALLOWED_RECIPE_KEYS = frozenset(
    {"story_mood", "voice_profile", "target_video_count", "production_settings"}
)
_ALLOWED_PRODUCTION_SETTING_KEYS = frozenset(
    {
        "retention_min",
        "retention_max",
        "adult_mode",
        "narration_wpm",
        "chapter_pause_seconds",
        "output_width",
        "output_height",
        "output_fps",
        "output_mode",
        "video_playback_speed",
        "video_transition",
        "subtitle_word_mode",
        "export_narration_audio",
        "bgm_volume",
        "bgm_mode",
        "bgm_file",
        "subtitle",
        "intro_card",
        "code_card",
        "outro_card",
        "caption_mode",
        "subtitle_preset",
        "intro_card_preset",
        "code_card_preset",
        "outro_card_preset",
        "subtitle_animation",
        "intro_animation",
        "max_episode_minutes",
        "cover_animation",
        "cover_outro_enabled",
        "color_grade",
        "end_card_seconds",
        "intro_card_duration_seconds",
        "render_mode",
        "video_template",
    }
)


def _recipe(
    *,
    mood: str,
    wpm: int,
    bgm: float,
    intro: str,
    intro_animation: str,
    subtitle: str,
    subtitle_animation: str,
    subtitle_word_mode: str = "off",
    code: str,
    cover_animation: str,
    color_grade: str,
    render_mode: str = "quality",
) -> dict[str, Any]:
    return {
        "story_mood": mood,
        "voice_profile": {
            "suspense": "dramatic",
            "romance": "warm",
            "sad": "calm",
            "revenge": "confident",
        }[mood],
        "production_settings": {
            "narration_wpm": wpm,
            "bgm_volume": bgm,
            "adult_mode": "engaging",
            "video_template": "platform_story_card",
            "intro_card_preset": intro,
            "intro_animation": intro_animation,
            "caption_mode": "semantic",
            "subtitle_preset": subtitle,
            "subtitle_animation": subtitle_animation,
            "subtitle_word_mode": subtitle_word_mode,
            "code_card_preset": code,
            "cover_animation": cover_animation,
            "color_grade": color_grade,
            "render_mode": render_mode,
            "output_fps": 60,
        },
    }


CURATED_PRODUCTION_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "suspense_retention",
        "name": "悬疑高留存",
        "description": "冷调电影感、紧凑女声与高辨识字幕，适合悬疑和背叛故事。",
        "recipe": _recipe(
            mood="suspense",
            wpm=225,
            bgm=0.30,
            intro="cinematic_dark",
            intro_animation="layered_story",
            subtitle="suspense_noir",
            subtitle_animation="mask_reveal",
            code="warning_red",
            cover_animation="cinematic_push",
            color_grade="suspense_cool",
        ),
    },
    {
        "id": "romance_immersive",
        "name": "浪漫沉浸",
        "description": "暖柔画面、亲密女声与柔光字幕，保持情绪但不拖慢节奏。",
        "recipe": _recipe(
            mood="romance",
            wpm=212,
            bgm=0.27,
            intro="romance_soft",
            intro_animation="soft_scale",
            subtitle="romance_glow",
            subtitle_animation="fade",
            code="romance_blush",
            cover_animation="gentle_push",
            color_grade="romance_warm",
        ),
    },
    {
        "id": "sad_emotional",
        "name": "悲伤克制",
        "description": "低饱和、较慢但仍符合短视频阅读节奏，适合遗憾与虐心题材。",
        "recipe": _recipe(
            mood="sad",
            wpm=200,
            bgm=0.24,
            intro="paper_note",
            intro_animation="paper_drop",
            subtitle="midnight_reader",
            subtitle_animation="fade",
            code="minimal_dark",
            cover_animation="focus_reveal",
            color_grade="sad_muted",
        ),
    },
    {
        "id": "revenge_fast",
        "name": "复仇爽文快节奏",
        "description": "高对比、强钩子和更快旁白，适合逆袭、打脸与复仇。",
        "recipe": _recipe(
            mood="revenge",
            wpm=230,
            bgm=0.32,
            intro="golden_luxe",
            intro_animation="side_reveal",
            subtitle="golden_hook",
            subtitle_animation="rise",
            code="golden_ticket",
            cover_animation="soft_flash",
            color_grade="revenge_contrast",
        ),
    },
    {
        "id": "clean_reader",
        "name": "通用清晰阅读",
        "description": "最稳妥的美式英语阅读方案，适合大多数小说和低配电脑。",
        "recipe": _recipe(
            mood="suspense",
            wpm=218,
            bgm=0.28,
            intro="editorial_white",
            intro_animation="fade_rise",
            subtitle="clear_outline",
            subtitle_animation="none",
            code="brand_pill",
            cover_animation="gentle_push",
            color_grade="neutral",
            render_mode="speed",
        ),
    },
    {
        "id": "minimal_speed",
        "name": "极简快速渲染",
        "description": "减少复杂动画和透明叠层，优先本机批量生成效率。",
        "recipe": _recipe(
            mood="revenge",
            wpm=225,
            bgm=0.28,
            intro="minimal_clean",
            intro_animation="none",
            subtitle="clear_outline",
            subtitle_animation="none",
            code="minimal_dark",
            cover_animation="none",
            color_grade="neutral",
            render_mode="speed",
        ),
    },
    {
        "id": "confession_dialogue",
        "name": "第一人称对白",
        "description": "社交帖式简介与轻透字幕，适合婚姻、秘密和第一人称叙事。",
        "recipe": _recipe(
            mood="romance",
            wpm=208,
            bgm=0.26,
            intro="social_post",
            intro_animation="fade_rise",
            subtitle="confession_clean",
            subtitle_animation="typewriter",
            code="light_chip",
            cover_animation="vertical_drift",
            color_grade="romance_warm",
        ),
    },
    {
        "id": "word_sync_hook",
        "name": "逐词变色钩子",
        "description": "字幕随配音逐词弹出变色，适合需要强节奏感的开头。",
        "recipe": _recipe(
            mood="suspense",
            wpm=220,
            bgm=0.30,
            intro="blue_glass",
            intro_animation="layered_story",
            subtitle="clear_outline",
            subtitle_animation="soft_pop",
            subtitle_word_mode="single",
            code="dark_glass",
            cover_animation="ken_burns_left",
            color_grade="night_lift",
        ),
    },
)


def _clean_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise ValueError("preset recipe is too deeply nested")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 4096:
            raise ValueError("preset value is too long")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            if not key or len(key) > 80:
                raise ValueError("preset recipe key is invalid")
            if key.casefold() in _FORBIDDEN_RECIPE_KEYS:
                raise ValueError(f"preset recipe cannot store {key}")
            result[key] = _clean_json(raw_value, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 500:
            raise ValueError("preset list is too large")
        return [_clean_json(item, depth=depth + 1) for item in value]
    raise ValueError("preset recipe contains an unsupported value")


def validate_production_preset(value: Mapping[str, Any], *, existing_id: str = "") -> dict[str, Any]:
    preset_id = str(value.get("id") or existing_id or f"custom_{uuid4().hex[:12]}").strip().casefold()
    if not _ID_PATTERN.fullmatch(preset_id):
        raise ValueError("preset id is invalid")
    name = str(value.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("preset name must be 1 to 80 characters")
    description = str(value.get("description") or "").strip()
    if len(description) > 240:
        raise ValueError("preset description is too long")
    raw_recipe = value.get("recipe")
    if not isinstance(raw_recipe, Mapping):
        raise ValueError("preset recipe must be an object")
    recipe = _clean_json(raw_recipe)
    unsupported = sorted(set(recipe) - _ALLOWED_RECIPE_KEYS)
    if unsupported:
        raise ValueError(
            "preset recipe cannot store content-specific fields: "
            + ", ".join(unsupported)
        )
    production_settings = recipe.get("production_settings")
    if production_settings is not None:
        if not isinstance(production_settings, Mapping):
            raise ValueError("production_settings must be an object")
        unsupported_settings = sorted(
            set(production_settings) - _ALLOWED_PRODUCTION_SETTING_KEYS
        )
        if unsupported_settings:
            raise ValueError(
                "production preset cannot store device or provider settings: "
                + ", ".join(unsupported_settings)
            )
        normalized_settings = normalize_retired_subtitle_settings(
            production_settings
        )
        for boolean_key in ("export_narration_audio", "cover_outro_enabled"):
            if boolean_key in normalized_settings and not isinstance(
                normalized_settings[boolean_key], bool
            ):
                raise ValueError(
                    f"production preset {boolean_key} must be a boolean"
                )
        for key, allowed in _PRODUCTION_ENUMS.items():
            if key not in normalized_settings:
                continue
            normalized = str(normalized_settings[key] or "").strip().casefold()
            if normalized not in allowed:
                raise ValueError(f"invalid production preset option: {key}")
            normalized_settings[key] = normalized
        for key, (minimum, maximum, integer) in _PRODUCTION_NUMBERS.items():
            if key not in normalized_settings:
                continue
            raw = normalized_settings[key]
            if isinstance(raw, bool):
                raise ValueError(f"invalid production preset number: {key}")
            try:
                number = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"invalid production preset number: {key}") from None
            if not math.isfinite(number) or not minimum <= number <= maximum:
                raise ValueError(
                    f"production preset {key} must be between {minimum:g} and {maximum:g}"
                )
            if integer and not number.is_integer():
                raise ValueError(f"production preset {key} must be an integer")
            normalized_settings[key] = int(round(number)) if integer else number
        for dimension, expected in (("output_width", 1080), ("output_height", 1920)):
            if dimension in normalized_settings:
                try:
                    parsed_dimension = int(normalized_settings[dimension])
                except (TypeError, ValueError):
                    raise ValueError(f"invalid production preset number: {dimension}") from None
                if parsed_dimension != expected:
                    raise ValueError(f"production preset {dimension} must be {expected}")
                normalized_settings[dimension] = expected
        if "output_fps" in normalized_settings:
            try:
                output_fps = int(normalized_settings["output_fps"])
            except (TypeError, ValueError):
                raise ValueError("invalid production preset number: output_fps") from None
            if output_fps not in {30, 60}:
                raise ValueError("production preset output_fps must be 30 or 60")
            normalized_settings["output_fps"] = output_fps
        if (
            "retention_min" in normalized_settings
            and "retention_max" in normalized_settings
            and float(normalized_settings["retention_min"])
            > float(normalized_settings["retention_max"])
        ):
            raise ValueError("production preset retention_min cannot exceed retention_max")
        normalized_settings["bgm_file"] = str(
            normalized_settings.get("bgm_file") or ""
        ).strip()
        if (
            normalized_settings.get("bgm_mode") == "manual"
            and not normalized_settings["bgm_file"]
        ):
            raise ValueError("manual BGM mode requires bgm_file")
        for kind in ("subtitle", "intro_card", "code_card", "outro_card"):
            if kind not in normalized_settings:
                continue
            preset_key = "subtitle_preset" if kind == "subtitle" else f"{kind}_preset"
            selected = str(normalized_settings.get(preset_key) or "").strip().casefold()
            normalized_settings[kind] = validate_style_patch(
                kind,
                normalized_settings[kind],
                preset=selected or None,
                reset_to_preset=True,
            )
        recipe["production_settings"] = normalized_settings
    mood = recipe.get("story_mood")
    if mood is not None:
        normalized_mood = str(mood or "").strip().casefold()
        if normalized_mood not in {"suspense", "romance", "sad", "revenge"}:
            raise ValueError("invalid production preset story_mood")
        recipe["story_mood"] = normalized_mood
    voice_profile = recipe.get("voice_profile")
    if voice_profile is not None:
        normalized_profile = str(voice_profile or "").strip().casefold()
        if normalized_profile not in {"dramatic", "warm", "calm", "confident"}:
            raise ValueError("invalid production preset voice_profile")
        recipe["voice_profile"] = normalized_profile
    target = recipe.get("target_video_count")
    if target is not None:
        try:
            parsed_target = int(target)
        except (TypeError, ValueError):
            raise ValueError("target_video_count must be a positive integer") from None
        if parsed_target < 1:
            raise ValueError("target_video_count must be a positive integer")
        recipe["target_video_count"] = parsed_target
    return {
        "id": preset_id,
        "name": name,
        "description": description,
        "recipe": recipe,
    }


class ProductionPresetStore:
    """Persist user-owned production recipes.

    Older releases exposed bundled and ownerless team recipes here. They stay
    readable on disk for rollback compatibility, but are no longer injected
    into listings. Newly saved recipes always belong to one account.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read_overrides(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        try:
            schema_version = int(payload.get("schema_version") or 0)
        except (AttributeError, TypeError, ValueError):
            schema_version = 0
        if not isinstance(payload, Mapping) or schema_version not in {
            1,
            2,
            3,
            4,
            PRESET_SCHEMA_VERSION,
        }:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for item in payload.get("presets") or []:
            if not isinstance(item, Mapping):
                continue
            candidate = deepcopy(dict(item))
            if schema_version < 4:
                recipe = candidate.get("recipe")
                production_settings = (
                    recipe.get("production_settings")
                    if isinstance(recipe, Mapping)
                    else None
                )
                if isinstance(production_settings, Mapping) and (
                    "narration_wpm" in production_settings
                ):
                    migrated_settings = dict(production_settings)
                    try:
                        legacy_wpm = float(migrated_settings["narration_wpm"])
                    except (TypeError, ValueError):
                        legacy_wpm = 240.0
                    if not math.isfinite(legacy_wpm):
                        legacy_wpm = 240.0
                    migrated_settings["narration_wpm"] = max(
                        200, min(280, int(round(legacy_wpm)))
                    )
                    migrated_recipe = dict(recipe)
                    migrated_recipe["production_settings"] = migrated_settings
                    candidate["recipe"] = migrated_recipe
            try:
                parsed = validate_production_preset(candidate)
            except ValueError:
                continue
            parsed.update(self._metadata(candidate, parsed, default_revision=1))
            result[parsed["id"]] = parsed
        return result

    @staticmethod
    def _content_hash(value: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            {
                "name": str(value.get("name") or ""),
                "description": str(value.get("description") or ""),
                "recipe": value.get("recipe") or {},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def _metadata(
        cls,
        source: Mapping[str, Any],
        value: Mapping[str, Any],
        *,
        default_revision: int,
    ) -> dict[str, Any]:
        try:
            revision = max(1, int(source.get("revision") or default_revision))
        except (TypeError, ValueError):
            revision = max(1, int(default_revision))
        return {
            "revision": revision,
            "content_hash": cls._content_hash(value),
            "updated_at": str(source.get("updated_at") or ""),
            "updated_by": str(source.get("updated_by") or ""),
            # Version 1/2 files did not have reliable ownership. Keep the
            # empty owner instead of guessing; list() hides those obsolete
            # team recipes while preserving their data for rollback.
            "owner_user_id": str(source.get("owner_user_id") or ""),
        }

    def list(
        self,
        *,
        viewer_user_id: str = "",
        can_manage_all: bool = True,
    ) -> list[dict[str, Any]]:
        with self._lock:
            overrides = self._read_overrides()
            result: list[dict[str, Any]] = []
            retired_builtin_ids = {
                str(item["id"]) for item in CURATED_PRODUCTION_PRESETS
            }
            for item in overrides.values():
                value = deepcopy(item)
                if str(value.get("id") or "") in retired_builtin_ids:
                    # Never resurrect an edited copy of a retired built-in.
                    continue
                owner_user_id = str(value.get("owner_user_id") or "")
                if not owner_user_id:
                    # Legacy team recipes have no trustworthy owner and are
                    # intentionally hidden from every account.
                    continue
                if (
                    not can_manage_all
                    and owner_user_id != str(viewer_user_id or "")
                ):
                    # Personal recipes are visible to their owner and to
                    # administrators only.
                    continue
                owned = bool(
                    owner_user_id
                    and owner_user_id == str(viewer_user_id or "")
                )
                editable = bool(can_manage_all or owned)
                value.update(
                    {
                        "owner_user_id": owner_user_id,
                        "scope": "personal",
                        "curated": False,
                        "editable": editable,
                        "deletable": editable,
                        "resettable": False,
                        "owned_by_current_user": owned,
                    }
                )
                result.append(value)
            return result

    def save(
        self,
        value: Mapping[str, Any],
        *,
        updated_by: str = "",
        can_manage_all: bool = True,
    ) -> dict[str, Any]:
        parsed = validate_production_preset(value)
        actor_user_id = str(updated_by or "").strip()
        with self._lock:
            overrides = self._read_overrides()
            curated = any(
                item["id"] == parsed["id"] for item in CURATED_PRODUCTION_PRESETS
            )
            if curated:
                raise PermissionError(
                    "内置制作方案已停用，请另存为个人方案。"
                )
            existing = overrides.get(parsed["id"])
            if not actor_user_id:
                # Trusted maintenance callers have no Web session. Assign a
                # real owner marker instead of recreating ownerless team data.
                actor_user_id = "system"
            if existing is not None:
                owner_user_id = str(existing.get("owner_user_id") or "")
                if not can_manage_all and owner_user_id != actor_user_id:
                    raise PermissionError("只能修改自己创建的制作方案。")
                if not owner_user_id:
                    if not can_manage_all:
                        raise PermissionError(
                            "只能修改自己创建的制作方案。"
                        )
                    owner_user_id = actor_user_id
            else:
                owner_user_id = actor_user_id
            current_revision = int((existing or {}).get("revision") or (1 if existing else 0))
            parsed.update(
                {
                    "revision": current_revision + 1,
                    "content_hash": self._content_hash(parsed),
                    "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "updated_by": actor_user_id,
                    "owner_user_id": owner_user_id,
                }
            )
            overrides[parsed["id"]] = parsed
            self._write(overrides.values())
            return next(
                item
                for item in self.list(
                    viewer_user_id=actor_user_id,
                    can_manage_all=can_manage_all,
                )
                if item["id"] == parsed["id"]
            )

    def delete(
        self,
        preset_id: str,
        *,
        updated_by: str = "",
        can_manage_all: bool = True,
    ) -> dict[str, Any]:
        normalized = str(preset_id or "").strip().casefold()
        actor_user_id = str(updated_by or "")
        with self._lock:
            if any(item["id"] == normalized for item in CURATED_PRODUCTION_PRESETS):
                if not can_manage_all:
                    raise PermissionError("只有管理员可以删除所有制作方案。")
                overrides = self._read_overrides()
                existed = overrides.pop(normalized, None) is not None
                self._write(overrides.values())
                return {
                    "id": normalized,
                    "deleted": existed,
                    "reset_to_curated": False,
                }
            overrides = self._read_overrides()
            existing = overrides.get(normalized)
            if existing is not None and not can_manage_all:
                owner_user_id = str(existing.get("owner_user_id") or "")
                if not actor_user_id or owner_user_id != actor_user_id:
                    raise PermissionError("只能删除自己创建的制作方案。")
            existed = overrides.pop(normalized, None) is not None
            self._write(overrides.values())
            return {"id": normalized, "deleted": existed, "reset_to_curated": False}

    def _write(self, presets: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": PRESET_SCHEMA_VERSION,
            "presets": list(presets),
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("production preset file is too large")
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temporary.write_text(encoded, encoding="utf-8", newline="\n")
        os.replace(temporary, self.path)
