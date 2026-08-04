from __future__ import annotations

import base64
import ctypes
import json
import math
import os
import string
import tempfile
from ctypes import wintypes
from dataclasses import asdict, fields
from pathlib import Path
from threading import RLock
from typing import Any

from .models import (
    DEFAULT_PREVIEW_SECONDS,
    MIN_STRUCTURED_PREVIEW_SECONDS,
    AppSettings,
    BatchSpec,
    COLOR_GRADES,
    COVER_ANIMATIONS,
    PlatformProfile,
    normalize_retired_subtitle_settings,
)
from .style_options import (
    validate_intro_animation,
    preset_names,
    validate_style_patch,
    validate_subtitle_animation,
)


APP_NAME = "StoryForge Studio"
MASKED_SECRET = "********"
SETTINGS_SCHEMA_VERSION = 20


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class SecretProtector:
    """Protect API keys with the current Windows user's DPAPI credentials."""

    PREFIX = "dpapi:"

    @staticmethod
    def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(data)
        blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        return blob, buffer

    def protect(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith(self.PREFIX):
            return value
        if os.name != "nt":
            raise RuntimeError("API key protection is only supported on Windows.")
        source, source_buffer = self._blob(value.encode("utf-8"))
        destination = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        ok = crypt32.CryptProtectData(
            ctypes.byref(source),
            APP_NAME,
            None,
            None,
            None,
            0,
            ctypes.byref(destination),
        )
        if not ok:
            raise ctypes.WinError()
        try:
            # Keep the Python-owned source buffer alive through the native call.
            _ = source_buffer
            encrypted = ctypes.string_at(destination.pbData, destination.cbData)
            return self.PREFIX + base64.b64encode(encrypted).decode("ascii")
        finally:
            kernel32.LocalFree(destination.pbData)

    def unprotect(self, value: str) -> str:
        if not value:
            return ""
        if not value.startswith(self.PREFIX):
            return value
        if os.name != "nt":
            raise RuntimeError("API key protection is only supported on Windows.")
        encrypted = base64.b64decode(value[len(self.PREFIX) :])
        source, source_buffer = self._blob(encrypted)
        destination = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(destination),
        )
        if not ok:
            raise ctypes.WinError()
        try:
            _ = source_buffer
            return ctypes.string_at(destination.pbData, destination.cbData).decode("utf-8")
        finally:
            kernel32.LocalFree(destination.pbData)


def default_data_dir() -> Path:
    override = os.environ.get("STORYFORGE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "StoryForgeStudio"


class SettingsRepository:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = (data_dir or default_data_dir()).resolve()
        self.settings_path = self.data_dir / "settings.json"
        self.usage_path = self.data_dir / "asset-usage.json"
        self._lock = RLock()
        self._protector = SecretProtector()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _read_json(self, path: Path, fallback: Any) -> Any:
        if not path.exists():
            return fallback
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return fallback

    def _write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def load(self) -> tuple[AppSettings, list[PlatformProfile], list[BatchSpec]]:
        with self._lock:
            raw = self._read_json(self.settings_path, {})
            if not isinstance(raw, dict):
                raw = {}
            try:
                schema_version = int(raw.get("schema_version") or 0)
            except (TypeError, ValueError):
                schema_version = 0
            settings_value = raw.get("settings")
            settings_data = dict(settings_value) if isinstance(settings_value, dict) else {}
            if schema_version < 2:
                if settings_data.get("narration_wpm") == 155:
                    settings_data["narration_wpm"] = 180
                subtitle_value = settings_data.get("subtitle")
                if isinstance(subtitle_value, dict):
                    subtitle_data = dict(subtitle_value)
                    if subtitle_data.get("max_chars_per_line") == 34:
                        subtitle_data["max_chars_per_line"] = 28
                    settings_data["subtitle"] = subtitle_data
            if schema_version < 3 and settings_data.get("narration_wpm") == 180:
                # Version 2 used 180 WPM as its default. Existing installations
                # should receive the faster narration requested for version 3,
                # while every non-default/custom value remains untouched.
                settings_data["narration_wpm"] = 210
            if schema_version < 4 and settings_data.get("bgm_volume") == 0.16:
                # Version 3's default was too quiet after side-chain ducking.
                # Migrate only that exact default and preserve user choices.
                settings_data["bgm_volume"] = 0.28
            if schema_version < 5:
                # V5 adds production-quality defaults. They are additive and do
                # not overwrite an operator's existing voice, subtitle or audio
                # choices.
                settings_data.setdefault("caption_mode", "semantic")
                settings_data.setdefault("subtitle_preset", "clear_outline")
                settings_data.setdefault("subtitle_animation", "none")
                settings_data.setdefault("preview_seconds", 30)
                settings_data.setdefault("max_episode_minutes", 10.0)
                settings_data.setdefault("cover_animation", "gentle_push")
                settings_data.setdefault("end_card_seconds", 6.0)
                settings_data.setdefault("render_mode", "speed")
            if schema_version < 6:
                settings_data.setdefault("hub", {})
            if schema_version < 7:
                # The reference-inspired platform/story card is optional.
                # Existing installations keep their current visual output.
                settings_data.setdefault("video_template", "classic")
            if schema_version < 8:
                # Earlier builds always rendered final videos at 30 FPS and did
                # not expose a frame-rate choice.  Promote that legacy default
                # to the new recommended 60 FPS once; an explicit 30 FPS choice
                # made after this migration is preserved by schema version 8.
                settings_data["output_fps"] = 60
            if schema_version < 9:
                # V9 makes every text/card layer editable.  Missing mappings
                # intentionally resolve to the previous renderer defaults, so
                # upgrading does not alter an existing project's appearance.
                settings_data.setdefault("intro_card_preset", "editorial_white")
                settings_data.setdefault("code_card_preset", "brand_pill")
                settings_data.setdefault("outro_card_preset", "editorial_white")
                settings_data.setdefault("intro_card", {})
                settings_data.setdefault("outro_card", {})
                subtitle = dict(settings_data.get("subtitle") or {})
                preset = str(
                    settings_data.get("subtitle_preset") or "clear_outline"
                ).casefold()
                if preset == "cinematic_shadow":
                    subtitle.setdefault("shadow_width", 3.0)
                    subtitle.setdefault("bold", True)
                elif preset == "clean_minimal":
                    subtitle["outline_width"] = min(
                        3, int(subtitle.get("outline_width", 4))
                    )
                    subtitle.setdefault("shadow_width", 0.0)
                    subtitle.setdefault("bold", False)
                    subtitle.setdefault("max_lines", 2)
                elif preset == "bold_drama":
                    subtitle["font_size"] = min(
                        96, int(subtitle.get("font_size", 52)) + 6
                    )
                    subtitle["outline_width"] = max(
                        5, int(subtitle.get("outline_width", 4))
                    )
                    subtitle.setdefault("shadow_width", 1.5)
                    subtitle.setdefault("bold", True)
                    subtitle.setdefault("max_lines", 2)
                if settings_data.get("video_template") == "platform_story_card":
                    subtitle["font_size"] = min(
                        96, int(subtitle.get("font_size", 52)) + 4
                    )
                    subtitle["outline_width"] = max(
                        5, int(subtitle.get("outline_width", 4))
                    )
                    subtitle["bottom_margin"] = max(
                        340, int(subtitle.get("bottom_margin", 310))
                    )
                    subtitle.setdefault("shadow_width", 1.0)
                    subtitle.setdefault("bold", True)
                    subtitle.setdefault("max_lines", 2)
                settings_data["subtitle"] = subtitle
            if schema_version < 14:
                # Opening-card motion is new in V14.  The conservative default
                # uses only a short ASS move/fade and is suitable for low-power
                # workstations as well as the normal quality renderer.
                settings_data.setdefault("intro_animation", "fade_rise")
                settings_data.setdefault("color_grade", "neutral")
            if schema_version < 15:
                # Exporting a standalone narration track is opt-in. Existing
                # installations and saved batches keep their prior behaviour.
                settings_data.setdefault("export_narration_audio", False)
            if schema_version < 16:
                # Cover-led endings were the only legacy behaviour. Keep them
                # enabled until an operator explicitly selects a caption-only
                # ending for a draft or production preset.
                settings_data.setdefault("cover_outro_enabled", True)
            if schema_version < 19:
                settings_data.setdefault("output_mode", "video_and_mp3")
                settings_data.setdefault("video_playback_speed", 1.0)
                settings_data.setdefault("video_transition", "cut")
                settings_data.setdefault("subtitle_word_mode", "off")
                settings_data.setdefault("bgm_mode", "auto")
                settings_data.setdefault("bgm_file", "")
                try:
                    legacy_wpm = float(settings_data.get("narration_wpm", 240))
                except (TypeError, ValueError):
                    legacy_wpm = 240
                if not math.isfinite(legacy_wpm) or not 200 <= legacy_wpm <= 280:
                    settings_data["narration_wpm"] = 240
            # Production artifacts are permanently local to the workstation.
            # Reset both historical switches regardless of schema version so
            # an already-current settings file with a legacy ``true`` cannot
            # reactivate upload after an upgrade.
            hub_value = settings_data.get("hub")
            hub_defaults = dict(hub_value) if isinstance(hub_value, dict) else {}
            artifact_sharing_needs_reset = bool(
                hub_defaults.get("share_previews")
                or hub_defaults.get("share_narration")
            )
            hub_defaults["share_previews"] = False
            hub_defaults["share_narration"] = False
            settings_data["hub"] = hub_defaults
            if schema_version < 10:
                hub_value = settings_data.get("hub")
                hub_defaults = (
                    dict(hub_value) if isinstance(hub_value, dict) else {}
                )
                hub_defaults.setdefault("auto_update_enabled", True)
                hub_defaults.setdefault("auto_download_updates", True)
                hub_defaults.setdefault("update_check_minutes", 1)
                settings_data["hub"] = hub_defaults
            if schema_version < 11:
                hub_value = settings_data.get("hub")
                hub_defaults = dict(hub_value) if isinstance(hub_value, dict) else {}
                hub_defaults.setdefault("web_allowed_roots", [])
                settings_data["hub"] = hub_defaults
            if schema_version < 12:
                # Version 12 replaces the old 30-second default with a compact
                # structured approval sample. Preserve deliberate longer
                # custom durations, while migrating the former default and
                # values too short to show opening, body and ending phases.
                try:
                    existing_preview_seconds = int(
                        settings_data.get("preview_seconds", 30)
                    )
                except (TypeError, ValueError):
                    existing_preview_seconds = 30
                if (
                    existing_preview_seconds == 30
                    or existing_preview_seconds < MIN_STRUCTURED_PREVIEW_SECONDS
                ):
                    settings_data["preview_seconds"] = DEFAULT_PREVIEW_SECONDS
            if schema_version < 13:
                # V13 assigns every installed client a durable private UUID.
                # AppSettings.from_dict generates it exactly once and the
                # migration save below persists it before enrollment.
                hub_value = settings_data.get("hub")
                hub_defaults = dict(hub_value) if isinstance(hub_value, dict) else {}
                hub_defaults.setdefault("device_id", "")
                hub_defaults.setdefault("applied_config_revision_id", "")
                hub_defaults.setdefault("applied_config_hash", "")
                settings_data["hub"] = hub_defaults
            provider_value = settings_data.get("providers")
            providers = dict(provider_value) if isinstance(provider_value, dict) else {}
            for key in ("text_api_key", "tts_api_key"):
                try:
                    providers[key] = self._protector.unprotect(str(providers.get(key) or ""))
                except (OSError, ValueError, RuntimeError):
                    providers[key] = ""
            settings_data["providers"] = providers
            hub_value = settings_data.get("hub")
            hub = dict(hub_value) if isinstance(hub_value, dict) else {}
            try:
                hub["access_token"] = self._protector.unprotect(
                    str(hub.get("access_token") or "")
                )
            except (OSError, ValueError, RuntimeError):
                hub["access_token"] = ""
            settings_data["hub"] = hub
            settings = AppSettings.from_dict(settings_data)
            identity_needs_save = (
                str(hub.get("installation_id") or "").strip()
                != settings.hub.installation_id
            )
            platform_values = raw.get("platforms")
            if not isinstance(platform_values, list):
                platform_values = []
            platforms = [
                PlatformProfile.from_dict(item)
                for item in platform_values
                if isinstance(item, dict)
            ]
            batch_values = raw.get("batches")
            if not isinstance(batch_values, list):
                batch_values = []
            batch_fields = {item.name for item in fields(BatchSpec)}
            required_batch_fields = {
                "platform_id",
                "text_folder",
                "video_folder",
                "music_folder",
                "output_folder",
            }
            batches: list[BatchSpec] = []
            batch_output_mode_migrated = False
            for item in batch_values:
                if not isinstance(item, dict) or not required_batch_fields.issubset(item):
                    continue
                try:
                    batch_data = {
                        key: value for key, value in item.items() if key in batch_fields
                    }
                    if "output_mode" not in batch_data:
                        # The old boolean controlled an additional MP3 next to
                        # a video. False was video-only, never pure audio. V0.4
                        # upgrades every old batch to its complete output pair.
                        batch_data["output_mode"] = "video_and_mp3"
                        batch_output_mode_migrated = True
                    batches.append(
                        BatchSpec(**batch_data)
                    )
                except (TypeError, ValueError):
                    continue
            if (
                schema_version < SETTINGS_SCHEMA_VERSION
                or identity_needs_save
                or batch_output_mode_migrated
                or artifact_sharing_needs_reset
            ):
                self.save(settings, platforms, batches)
            return settings, platforms, batches

    def save(
        self,
        settings: AppSettings,
        platforms: list[PlatformProfile],
        batches: list[BatchSpec],
    ) -> None:
        with self._lock:
            settings_data = settings.to_dict()
            provider_data = settings_data["providers"]
            provider_data["text_api_key"] = self._protector.protect(
                provider_data.get("text_api_key", "")
            )
            provider_data["tts_api_key"] = self._protector.protect(
                provider_data.get("tts_api_key", "")
            )
            hub_data = settings_data["hub"]
            # Defense in depth: only business metadata and local references
            # are synchronized.  Production artifact bytes never enter Hub.
            hub_data["share_previews"] = False
            hub_data["share_narration"] = False
            hub_data["access_token"] = self._protector.protect(
                hub_data.get("access_token", "")
            )
            self._write_json(
                self.settings_path,
                {
                    "schema_version": SETTINGS_SCHEMA_VERSION,
                    "settings": settings_data,
                    "platforms": [profile.to_dict() for profile in platforms],
                    "batches": [batch.to_dict() for batch in batches],
                },
            )

    def load_usage(self) -> dict[str, int]:
        with self._lock:
            raw = self._read_json(self.usage_path, {})
            if not isinstance(raw, dict):
                return {}
            usage: dict[str, int] = {}
            for key, value in raw.items():
                try:
                    count = int(value)
                except (TypeError, ValueError):
                    continue
                if count >= 0:
                    usage[str(key)] = count
            return usage

    def save_usage(self, usage: dict[str, int]) -> None:
        with self._lock:
            self._write_json(self.usage_path, usage)


class ApplicationState:
    def __init__(self, repository: SettingsRepository) -> None:
        self.repository = repository
        self._lock = RLock()
        self.settings, self.platforms, self.batches = repository.load()

    def persist(self) -> None:
        with self._lock:
            self.repository.save(self.settings, self.platforms, self.batches)

    def platform_by_id(self, platform_id: str) -> PlatformProfile | None:
        return next((item for item in self.platforms if item.id == platform_id), None)

    def upsert_platform(self, value: dict[str, Any]) -> PlatformProfile:
        with self._lock:
            incoming = dict(value)
            existing = next(
                (
                    item
                    for item in self.platforms
                    if item.id == str(incoming.get("id") or "")
                ),
                None,
            )
            if existing is not None:
                # Older UI builds do not submit the optional branding fields.
                # Preserve them during an otherwise-valid platform edit instead
                # of silently clearing a logo selected on another computer.
                incoming.setdefault("logo_path", existing.logo_path)
                incoming.setdefault("brand_color", existing.brand_color)
            profile = PlatformProfile.from_dict(incoming)
            if not profile.name:
                raise ValueError("平台名称不能为空。")
            try:
                formatter = string.Formatter()
                for label, template in (
                    ("屏幕口令", profile.search_template),
                    ("结尾旁白", profile.ending_template),
                ):
                    fields_in_template: set[str] = set()
                    for _literal, field_name, format_spec, conversion in formatter.parse(template):
                        if field_name is None:
                            continue
                        if field_name not in {"platform", "code"} or format_spec or conversion:
                            raise ValueError(f"{label}模板只允许 {{platform}} 和 {{code}}。")
                        fields_in_template.add(field_name)
                    if "code" not in fields_in_template:
                        raise ValueError(f"{label}模板必须包含 {{code}}。")
                profile.render_search("123456")
                profile.render_ending("123456")
            except (KeyError, AttributeError, IndexError) as error:
                raise ValueError(f"模板变量无效：{error}") from error
            for index, current in enumerate(self.platforms):
                if current.id == profile.id:
                    self.platforms[index] = profile
                    break
            else:
                self.platforms.append(profile)
            self.persist()
            return profile

    def delete_platform(self, platform_id: str) -> None:
        with self._lock:
            if any(batch.platform_id == platform_id for batch in self.batches):
                raise ValueError("该平台仍被批次使用，不能删除。")
            self.platforms = [item for item in self.platforms if item.id != platform_id]
            self.persist()

    def update_settings(self, value: dict[str, Any]) -> AppSettings:
        with self._lock:
            value = normalize_retired_subtitle_settings(value)
            enum_fields = {
                "output_mode": {"video_and_mp3", "audio_only", "reuse_audio"},
                "video_transition": {"cut", "fade"},
                "subtitle_word_mode": {"off", "cumulative", "single"},
                "bgm_mode": {"auto", "manual", "none"},
            }
            for key, allowed in enum_fields.items():
                if key not in value:
                    continue
                normalized = str(value[key] or "").strip().casefold()
                if normalized not in allowed:
                    raise ValueError(f"{key} 选项无效。")
                value[key] = normalized
            if "narration_wpm" in value:
                try:
                    requested_wpm = float(value["narration_wpm"])
                except (TypeError, ValueError) as error:
                    raise ValueError("目标语速必须是数字。") from error
                if (
                    not math.isfinite(requested_wpm)
                    or not requested_wpm.is_integer()
                    or not 200 <= requested_wpm <= 280
                ):
                    raise ValueError("目标语速必须在 200 到 280 WPM 之间。")
            if "video_playback_speed" in value:
                try:
                    requested_speed = float(value["video_playback_speed"])
                except (TypeError, ValueError) as error:
                    raise ValueError("视频播放速度必须是数字。") from error
                if not math.isfinite(requested_speed) or not 0.8 <= requested_speed <= 3.0:
                    raise ValueError("视频播放速度必须在 0.8 到 3.0 倍之间。")
            incoming_hub = value.get("hub")
            if isinstance(incoming_hub, dict) and "web_allowed_roots" in incoming_hub:
                incoming_roots = incoming_hub["web_allowed_roots"]
                if not isinstance(incoming_roots, list) or any(
                    not isinstance(item, str) for item in incoming_roots
                ):
                    raise ValueError("网页工作目录必须是路径字符串列表。")
            if "output_fps" in value:
                try:
                    requested_output_fps = int(value["output_fps"])
                except (TypeError, ValueError) as error:
                    raise ValueError("输出帧率必须是 30 或 60 FPS。") from error
                if requested_output_fps not in {30, 60}:
                    raise ValueError("输出帧率必须是 30 或 60 FPS。")
            if "export_narration_audio" in value and not isinstance(
                value["export_narration_audio"], bool
            ):
                raise ValueError("纯旁白配音导出设置必须是开启或关闭。")
            if "cover_outro_enabled" in value and not isinstance(
                value["cover_outro_enabled"], bool
            ):
                raise ValueError("cover_outro_enabled must be a boolean")
            current = self.settings.to_dict()
            style_kinds = ("subtitle", "intro_card", "code_card", "outro_card")
            for kind in style_kinds:
                preset_key = "subtitle_preset" if kind == "subtitle" else f"{kind}_preset"
                selected_preset = str(
                    value.get(preset_key, current.get(preset_key, "")) or ""
                ).strip().casefold()
                if selected_preset not in preset_names(kind):
                    raise ValueError(f"{preset_key} option is invalid")
                patch = value.get(kind)
                if patch is not None and not isinstance(patch, dict):
                    raise ValueError(f"{kind} settings must be an object")
                preset_changed = (
                    preset_key in value
                    and selected_preset != str(current.get(preset_key) or "").casefold()
                )
                base = validate_style_patch(
                    kind,
                    {},
                    base=current.get(kind) if isinstance(current.get(kind), dict) else {},
                    preset=selected_preset,
                    reset_to_preset=preset_changed,
                )
                current[kind] = validate_style_patch(
                    kind,
                    patch or {},
                    base=base,
                    preset=selected_preset,
                )
                current[preset_key] = selected_preset
            for key, incoming in value.items():
                if key in style_kinds or key in {
                    "subtitle_preset",
                    "intro_card_preset",
                    "code_card_preset",
                    "outro_card_preset",
                }:
                    continue
                if key in {"providers", "hub"} and isinstance(incoming, dict):
                    current[key] = {**current.get(key, {}), **incoming}
                else:
                    current[key] = incoming
            new_settings = AppSettings.from_dict(current)
            if not 0.5 <= new_settings.retention_min <= new_settings.retention_max <= 1.0:
                raise ValueError("剧情保留比例必须在 50% 到 100% 之间。")
            if new_settings.adult_mode not in {"direct", "engaging"}:
                raise ValueError("成人表达模式无效。")
            try:
                narration_wpm = float(new_settings.narration_wpm)
            except (TypeError, ValueError) as error:
                raise ValueError("目标语速必须是数字。") from error
            if (
                not math.isfinite(narration_wpm)
                or not narration_wpm.is_integer()
                or not 200 <= narration_wpm <= 280
            ):
                raise ValueError("目标语速必须在 200 到 280 WPM 之间。")
            new_settings.narration_wpm = int(round(narration_wpm))
            try:
                video_playback_speed = float(new_settings.video_playback_speed)
            except (TypeError, ValueError) as error:
                raise ValueError("视频播放速度必须是数字。") from error
            if (
                not math.isfinite(video_playback_speed)
                or not 0.8 <= video_playback_speed <= 3.0
            ):
                raise ValueError("视频播放速度必须在 0.8 到 3.0 倍之间。")
            new_settings.video_playback_speed = video_playback_speed
            new_settings.bgm_file = str(new_settings.bgm_file or "").strip()
            if new_settings.bgm_mode == "manual" and not new_settings.bgm_file:
                raise ValueError("手动背景音乐模式必须选择一个音乐文件。")
            try:
                output_fps = int(new_settings.output_fps)
            except (TypeError, ValueError) as error:
                raise ValueError("输出帧率必须是 30 或 60 FPS。") from error
            if output_fps not in {30, 60}:
                raise ValueError("输出帧率必须是 30 或 60 FPS。")
            new_settings.output_fps = output_fps
            try:
                bgm_volume = float(new_settings.bgm_volume)
            except (TypeError, ValueError) as error:
                raise ValueError("背景音乐音量必须是数字。") from error
            if not math.isfinite(bgm_volume) or not 0 <= bgm_volume <= 1:
                raise ValueError("背景音乐音量必须在 0% 到 100% 之间。")
            new_settings.bgm_volume = bgm_volume
            if new_settings.caption_mode not in {"sentence", "semantic"}:
                raise ValueError("字幕切分模式无效。")
            if new_settings.subtitle_preset not in preset_names("subtitle"):
                raise ValueError("字幕样式预设无效。")
            if new_settings.intro_card_preset not in preset_names("intro_card"):
                raise ValueError("简介卡样式预设无效。")
            if new_settings.code_card_preset not in preset_names("code_card"):
                raise ValueError("口令卡样式预设无效。")
            if new_settings.outro_card_preset not in preset_names("outro_card"):
                raise ValueError("结尾卡样式预设无效。")
            try:
                new_settings.subtitle_animation = validate_subtitle_animation(
                    new_settings.subtitle_animation
                )
            except ValueError as error:
                raise ValueError("字幕动画选项无效。") from error
            try:
                new_settings.intro_animation = validate_intro_animation(
                    new_settings.intro_animation
                )
            except ValueError as error:
                raise ValueError("简介卡动画选项无效。") from error
            if new_settings.cover_animation not in COVER_ANIMATIONS:
                raise ValueError("封面动画选项无效。")
            if new_settings.color_grade not in COLOR_GRADES:
                raise ValueError("画面调色选项无效。")
            if new_settings.render_mode not in {"speed", "quality", "compatibility"}:
                raise ValueError("渲染模式无效。")
            if new_settings.video_template not in {"classic", "platform_story_card"}:
                raise ValueError("视频模板无效。")
            try:
                preview_seconds = int(new_settings.preview_seconds)
            except (TypeError, ValueError) as error:
                raise ValueError("样片长度必须是整数秒。") from error
            if not MIN_STRUCTURED_PREVIEW_SECONDS <= preview_seconds <= 60:
                raise ValueError(
                    f"样片长度必须在 {MIN_STRUCTURED_PREVIEW_SECONDS} 到 60 秒之间。"
                )
            new_settings.preview_seconds = preview_seconds
            try:
                max_episode_minutes = float(new_settings.max_episode_minutes)
                end_card_seconds = float(new_settings.end_card_seconds)
            except (TypeError, ValueError) as error:
                raise ValueError("视频时长设置必须是数字。") from error
            if not math.isfinite(max_episode_minutes) or not 3 <= max_episode_minutes <= 30:
                raise ValueError("单集提醒线必须在 3 到 30 分钟之间。")
            if not math.isfinite(end_card_seconds) or not 5 <= end_card_seconds <= 7:
                raise ValueError("结尾卡时长必须在 5 到 7 秒之间。")
            new_settings.max_episode_minutes = max_episode_minutes
            new_settings.end_card_seconds = end_card_seconds
            if new_settings.hub.mode not in {"local", "host", "client"}:
                raise ValueError("StoryForge Hub 模式无效。")
            if not new_settings.hub.device_name.strip():
                raise ValueError("设备名称不能为空。")
            try:
                hub_port = int(new_settings.hub.listen_port)
            except (TypeError, ValueError) as error:
                raise ValueError("Hub 端口必须是数字。") from error
            if not 1 <= hub_port <= 65535:
                raise ValueError("Hub 端口必须在 1 到 65535 之间。")
            new_settings.hub.listen_port = hub_port
            if not isinstance(new_settings.hub.auto_update_enabled, bool):
                raise ValueError("自动检查更新必须是开启或关闭。")
            if not isinstance(new_settings.hub.auto_download_updates, bool):
                raise ValueError("自动下载更新必须是开启或关闭。")
            if isinstance(new_settings.hub.update_check_minutes, bool):
                raise ValueError("更新检查间隔必须是整数分钟。")
            try:
                update_check_minutes = int(new_settings.hub.update_check_minutes)
            except (TypeError, ValueError) as error:
                raise ValueError("更新检查间隔必须是整数分钟。") from error
            if not 1 <= update_check_minutes <= 1440:
                raise ValueError("更新检查间隔必须在 1 到 1440 分钟之间。")
            new_settings.hub.update_check_minutes = update_check_minutes
            roots_value = new_settings.hub.web_allowed_roots
            if not isinstance(roots_value, list):
                raise ValueError("网页工作目录必须是路径列表。")
            if len(roots_value) > 32:
                raise ValueError("网页工作目录最多配置 32 个。")
            cleaned_roots: list[str] = []
            seen_roots: set[str] = set()
            for raw_root in roots_value:
                root_text = str(raw_root or "").strip()
                normalized_slashes = root_text.replace("/", "\\")
                if (
                    not root_text
                    or "\x00" in root_text
                    or normalized_slashes.startswith("\\\\")
                    or normalized_slashes.startswith("\\??\\")
                ):
                    raise ValueError("网页工作目录不允许 UNC 或设备路径。")
                root_path = Path(root_text).expanduser()
                if not root_path.is_absolute() or root_path.parent == root_path:
                    raise ValueError("网页工作目录必须是非根目录的本机绝对路径。")
                resolved_root = root_path.resolve(strict=False)
                if not resolved_root.is_dir():
                    raise ValueError("网页工作目录必须已在 Hub 主机上存在。")
                key = os.path.normcase(str(resolved_root))
                if key in seen_roots:
                    continue
                seen_roots.add(key)
                cleaned_roots.append(str(resolved_root))
            new_settings.hub.web_allowed_roots = cleaned_roots
            if new_settings.hub.mode == "client":
                endpoint = new_settings.hub.endpoint.strip().rstrip("/")
                if not endpoint.startswith(("http://", "https://")):
                    raise ValueError("Hub 地址必须以 http:// 或 https:// 开头。")
                new_settings.hub.endpoint = endpoint
            self.settings = new_settings
            self.persist()
            return self.settings
