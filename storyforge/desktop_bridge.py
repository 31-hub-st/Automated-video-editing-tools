from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from .web import WEB_RPC_PERMISSIONS
from .worker import LOCAL_WORKER_RPC_PERMISSIONS


# Methods available to an authenticated employee on the installed workstation.
# Shared library/platform/code/account/Hub mutations are intentionally absent.
EMPLOYEE_DESKTOP_METHODS = frozenset(
    {
        "get_bootstrap",
        "get_local_runtime_snapshot",
        "get_local_self_check",
        "get_visual_style_presets",
        "get_production_presets",
        "save_production_preset",
        "delete_production_preset",
        "get_hub_status",
        "get_device_sync_status",
        "get_update_status",
        "check_for_updates",
        "download_update",
        "schedule_update_on_restart",
        "restart_to_apply_update",
        "cancel_scheduled_update",
        "save_local_update_preferences",
        "get_library_bootstrap",
        "get_novel",
        "get_effective_permissions",
        "choose_folder",
        "choose_file",
        "read_text_document",
        "save_production_draft",
        "queue_production_draft",
        "generate_voice_candidates",
        "set_local_tts_provider",
        "generate_intro_card_copy",
        "classify_novel",
        "lock_novel_voice",
        "scan_batch",
        "queue_batch",
        "start_queue",
        "cancel_queue",
        "get_jobs",
        "get_queue_connection",
        "get_archived_jobs",
        "get_production_record_groups",
        "get_record_artifacts",
        "cancel_production_records",
        "archive_job",
        "restore_job",
        "archive_finished_jobs",
        "clear_finished_jobs",
        "retry_failed",
        "open_output_folder",
        "analyze_story",
    }
)


ADMIN_DESKTOP_METHODS = EMPLOYEE_DESKTOP_METHODS | frozenset(
    {
        "sync_device_config_now",
        "get_hub_backup_status",
        "list_hub_backups",
        "create_hub_backup",
        "publish_update",
        "clear_published_update",
        "reconnect_hub",
        "connect_hub_with_password",
        "save_platform",
        "delete_platform",
        "save_settings",
        "import_novel_text",
        "import_novel_file",
        "save_novel",
        "save_novel_binding",
        "add_promo_code",
        "update_promo_code",
        "save_publishing_account",
        "save_software_user",
        "list_software_users",
        "delete_software_user",
        "list_managed_devices",
        "get_managed_device",
        "acknowledge_managed_device",
        "rename_managed_device",
        "set_managed_device_active",
        "create_managed_device_config",
        "list_managed_device_configs",
        "get_managed_device_config",
        "set_user_permission",
        "trash_production_records",
        "restore_trashed_production_records",
        "delete_trashed_production_records",
    }
)


# Installation, team and Hub ownership cannot be delegated to an employee by
# an unrelated content permission. Shared library/platform/code permissions,
# however, remain opt-in so an administrator can deliberately extend one
# employee account without promoting it to administrator.
HARD_ADMIN_ONLY_DESKTOP_METHODS = frozenset(
    {
        "sync_device_config_now",
        "get_hub_backup_status",
        "list_hub_backups",
        "create_hub_backup",
        "publish_update",
        "clear_published_update",
        "reconnect_hub",
        "connect_hub_with_password",
        "save_settings",
        "save_software_user",
        "list_software_users",
        "delete_software_user",
        "list_managed_devices",
        "get_managed_device",
        "acknowledge_managed_device",
        "rename_managed_device",
        "set_managed_device_active",
        "create_managed_device_config",
        "list_managed_device_configs",
        "get_managed_device_config",
        "set_user_permission",
        "trash_production_records",
        "restore_trashed_production_records",
        "delete_trashed_production_records",
    }
)


# Apply the same effective-permission contracts in all three entry points:
# Hub Web, localhost worker and desktop shell. Desktop-only convenience methods
# are added explicitly. Tuples use the same "any of" semantics as both HTTP
# transports (for example own-record OR all-record access).
DESKTOP_RPC_PERMISSIONS: dict[str, tuple[str, ...]] = {
    **WEB_RPC_PERMISSIONS,
    **LOCAL_WORKER_RPC_PERMISSIONS,
    "choose_file": ("drafts.create", "library.edit", "hub.manage"),
    "read_text_document": ("drafts.create", "library.edit", "hub.manage"),
    "scan_batch": ("drafts.create", "hub.manage"),
    "queue_batch": ("drafts.create", "hub.manage"),
    "lock_novel_voice": ("voice.preview", "hub.manage"),
    "analyze_story": ("text.assist", "hub.manage"),
    "set_local_tts_provider": ("voice.preview", "hub.manage"),
    # Updating the installed client is a workstation maintenance action, not
    # Hub/team administration. Every authenticated desktop employee may update
    # this computer; publishing a release remains administrator-only.
    "get_update_status": (),
    "check_for_updates": (),
    "download_update": (),
    "schedule_update_on_restart": (),
    "restart_to_apply_update": (),
    "cancel_scheduled_update": (),
}


class StoryForgeDesktopBridge:
    """The only Python object exposed to pywebview.

    Keeping ``StoryForgeApi`` itself private is the desktop equivalent of the
    Hub Web RPC allowlist: hiding a button is never treated as authorization.
    """

    def __init__(self, api: Any) -> None:
        self._api = api

    def desktop_session_status(self) -> dict[str, Any]:
        return self._api.desktop_session_status()

    def desktop_login(self, username: str, password: str) -> dict[str, Any]:
        return self._api.desktop_login(username, password)

    def desktop_logout(self) -> dict[str, Any]:
        return self._api.desktop_logout()

    def get_library_bootstrap(self) -> dict[str, Any]:
        """Expose the shared library as an explicit pywebview contract.

        Most application calls travel through :meth:`desktop_rpc`, but the
        library is required to render the first production screen.  Keeping an
        explicit method here makes that startup dependency visible to
        pywebview and prevents a stale/generic bridge surface from being
        mistaken for a missing ``StoryForgeApi`` method.  Authorization and
        actor scoping still go through the same desktop RPC gate.
        """

        return self.desktop_rpc("get_library_bootstrap", [])

    def desktop_rpc(self, method: Any, args: Any = None) -> dict[str, Any]:
        try:
            clean_method = str(method or "").strip()
            prepared = list(args or [])
            if not clean_method or not isinstance(args if args is not None else [], list):
                raise ValueError("桌面请求格式不正确。")
            if len(prepared) > 12:
                raise ValueError("桌面请求参数过多。")
            session = self._api._desktop_session_payload()
            if not session or not session.get("authenticated"):
                raise PermissionError("请先登录 StoryForge。")
            user = dict(session.get("user") or {})
            actor_user_id = str(user.get("id") or "")
            role = str(user.get("role") or "producer")
            if clean_method not in ADMIN_DESKTOP_METHODS:
                raise PermissionError("该功能未开放给桌面程序。")
            permissions = {
                str(item) for item in session.get("permissions") or [] if str(item)
            }
            required = DESKTOP_RPC_PERMISSIONS.get(clean_method, ())
            if required and not permissions.intersection(required):
                raise PermissionError("当前账号没有执行此操作的权限。")
            if role != "admin" and clean_method not in EMPLOYEE_DESKTOP_METHODS:
                if clean_method in HARD_ADMIN_ONLY_DESKTOP_METHODS or not required:
                    raise PermissionError(
                        "员工账号不能管理团队账号、电脑或 Hub 设置。"
                    )
            if clean_method == "get_effective_permissions":
                target_user_id = str(prepared[0] if prepared else "")
                if role != "admin" and target_user_id != actor_user_id:
                    raise PermissionError("员工只能查看自己的权限。")
            if clean_method == "get_record_artifacts" and prepared:
                self._api._require_job_record_access(str(prepared[0]))
            target = getattr(self._api, clean_method, None)
            if not callable(target):
                raise RuntimeError("当前版本尚未提供该功能。")
            actor_scope = getattr(self._api, "_web_actor_scope", None)
            scope = actor_scope(actor_user_id) if callable(actor_scope) else nullcontext()
            with scope:
                result = target(*prepared)
            if not isinstance(result, dict) or "ok" not in result:
                raise RuntimeError("桌面后端返回了无法识别的结果。")
            if (
                role != "admin"
                and clean_method == "get_bootstrap"
                and bool(result.get("ok"))
                and isinstance(result.get("data"), dict)
            ):
                # The employee UI needs production defaults and a tiny local
                # TTS projection, not the workstation's cloud/Hub connection
                # configuration.  Enforce that boundary in the bridge as well
                # as in CSS so hidden fields can never be inspected through
                # the desktop RPC response.
                data = dict(result["data"])
                settings = dict(data.get("settings") or {})
                providers = dict(settings.get("providers") or {})
                settings["providers"] = {
                    "tts_provider": str(
                        providers.get("tts_provider") or "local_kokoro"
                    ),
                    "kokoro_endpoint": (
                        "configured" if providers.get("kokoro_endpoint") else ""
                    ),
                    "allow_provider_fallback": bool(
                        providers.get("allow_provider_fallback", True)
                    ),
                    "monthly_character_limit": int(
                        providers.get("monthly_character_limit") or 0
                    ),
                    "has_text_api_key": False,
                    "has_tts_api_key": False,
                }
                hub = dict(settings.get("hub") or {})
                settings["hub"] = {
                    key: hub[key]
                    for key in (
                        "device_name",
                        "app_version",
                        "auto_update_enabled",
                        "auto_download_updates",
                        "update_check_minutes",
                    )
                    if key in hub
                }
                data["settings"] = settings
                result = {**result, "data": data}
            return result
        except (KeyError, OSError, PermissionError, RuntimeError, TypeError, ValueError) as error:
            return {"ok": False, "error": str(error) or type(error).__name__}


__all__ = [
    "ADMIN_DESKTOP_METHODS",
    "DESKTOP_RPC_PERMISSIONS",
    "EMPLOYEE_DESKTOP_METHODS",
    "HARD_ADMIN_ONLY_DESKTOP_METHODS",
    "StoryForgeDesktopBridge",
]
