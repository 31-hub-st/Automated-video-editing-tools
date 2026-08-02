from __future__ import annotations

"""Single source of truth for StoryForge's RPC surfaces.

The desktop bridge, Hub catalog and workstation bridge are separate trust
boundaries.  Keeping their method names and permission rules here prevents a
rolling update from adding a callable method on one surface while forgetting
the corresponding permission or client compatibility rule on another.

This module intentionally contains data only.  Dispatch still lives beside
the implementation in ``web.py``, ``hub.py`` and ``worker.py``.
"""

from collections.abc import Mapping


RPC_CONTRACT_VERSION = 1

TEXT_POLISH_RPC_METHOD = "text_polish"
ACCOUNT_PASSWORD_VERIFY_RPC_METHOD = "account_password_verify"
LOCAL_WORKER_TICKET_RPC_METHOD = "local_worker_ticket_redeem"


DEVICE_ADMIN_RPC_METHODS = frozenset(
    {
        "devices_list",
        "device_get",
        "device_acknowledge",
        "device_rename",
        "device_set_active",
        "device_delete",
        "device_config_create",
        "device_config_list",
        "device_config_get",
    }
)

DEVICE_CLIENT_RPC_METHODS = frozenset(
    {
        "device_session",
        "device_heartbeat",
        "device_desired_config",
        "device_config_ack",
        ACCOUNT_PASSWORD_VERIFY_RPC_METHOD,
        LOCAL_WORKER_TICKET_RPC_METHOD,
    }
)

DEVICE_SERVICE_RPC_METHODS = DEVICE_ADMIN_RPC_METHODS | DEVICE_CLIENT_RPC_METHODS


CATALOG_READ_METHODS = frozenset(
    {
        "bootstrap_summary",
        "list_novels",
        "get_novel",
        "list_platforms",
        "list_promo_codes",
        "list_publishing_accounts",
        "list_users",
        "get_effective_permissions",
        "get_draft",
        "list_drafts",
        "find_duplicate_draft_configuration",
        "last_successful_voice",
        "get_record",
        "get_record_by_job_id",
        "list_records",
        "list_record_groups",
        "find_active_draft_gate",
        "list_reconciliation_records",
        "get_production_batch_summaries",
        "get_archived_job",
        "get_archived_batch",
        "list_archived_jobs",
        "list_media_usage",
        "list_audit_events",
        "list_production_presets",
    }
)

CATALOG_WRITE_METHODS = frozenset(
    {
        "import_novel",
        "save_novel",
        "delete_novel",
        "save_novel_classification",
        "save_novel_voice_state",
        "save_episode",
        "save_platform",
        "delete_platform",
        "save_novel_binding",
        "add_promo_code",
        "update_promo_code",
        "delete_promo_code",
        "save_publishing_account",
        "delete_publishing_account",
        "save_user",
        "delete_user",
        "set_user_permission",
        "save_draft",
        "save_production_record",
        "save_production_records_bulk",
        "begin_record_retry",
        "request_record_cancellation",
        "trash_production_records",
        "restore_trashed_records",
        "delete_trashed_records",
        "archive_job_snapshot",
        "restore_job_snapshot",
        "archive_batch_snapshots",
        "restore_batch_snapshots",
        "claim_record_lease",
        "heartbeat_record_lease",
        "release_record_lease",
        "bind_lease_gate_batch",
        "add_artifact",
        "record_media_usage",
        "save_production_preset",
        "delete_production_preset",
    }
)

CATALOG_RPC_METHODS = CATALOG_READ_METHODS | CATALOG_WRITE_METHODS


HUB_RPC_PERMISSION_ANY: dict[str, tuple[str, ...]] = {
    "list_novels": ("library.view",),
    "get_novel": ("library.view",),
    "import_novel": ("library.edit",),
    "save_novel": ("library.edit",),
    "delete_novel": ("library.edit",),
    "save_novel_classification": ("text.assist", "library.edit"),
    "save_novel_voice_state": ("voice.preview", "library.edit"),
    "save_episode": ("library.edit",),
    "list_platforms": ("library.view", "platforms.manage"),
    "save_platform": ("platforms.manage",),
    "delete_platform": ("platforms.manage",),
    "save_novel_binding": ("platforms.manage",),
    "list_promo_codes": ("promo_codes.use", "promo_codes.manage"),
    "add_promo_code": ("promo_codes.manage",),
    "update_promo_code": ("promo_codes.manage",),
    "delete_promo_code": ("promo_codes.manage",),
    "list_publishing_accounts": ("drafts.create", "publishing_accounts.manage"),
    "save_publishing_account": ("publishing_accounts.manage",),
    "delete_publishing_account": ("publishing_accounts.manage",),
    "list_users": ("users.manage",),
    "save_user": ("users.manage",),
    "delete_user": ("users.manage",),
    "set_user_permission": ("permissions.manage",),
    "get_draft": ("drafts.create", "drafts.manage_all"),
    "list_drafts": ("drafts.create", "drafts.manage_all"),
    "find_duplicate_draft_configuration": ("drafts.create", "drafts.manage_all"),
    "last_successful_voice": ("library.view", "drafts.create", "voice.preview"),
    "save_draft": ("drafts.create", "drafts.manage_all"),
    "get_record": ("records.view_own", "records.view_all"),
    "get_record_by_job_id": ("records.view_own", "records.view_all"),
    "list_records": ("records.view_own", "records.view_all"),
    "list_record_groups": ("records.view_own", "records.view_all"),
    "find_active_draft_gate": ("drafts.create", "hub.manage"),
    "list_reconciliation_records": ("production.execute", "hub.manage"),
    "get_production_batch_summaries": ("records.view_own", "records.view_all"),
    "get_archived_job": ("records.view_own", "records.view_all"),
    "get_archived_batch": ("records.view_own", "records.view_all"),
    "list_archived_jobs": ("records.view_own", "records.view_all"),
    "save_production_record": ("drafts.create", "hub.manage"),
    "save_production_records_bulk": ("drafts.create", "hub.manage"),
    "begin_record_retry": ("jobs.retry_own", "jobs.retry_all"),
    "request_record_cancellation": ("jobs.retry_own", "jobs.retry_all"),
    "trash_production_records": ("records.view_all", "hub.manage"),
    "restore_trashed_records": ("records.view_all", "hub.manage"),
    "delete_trashed_records": ("records.view_all", "hub.manage"),
    "archive_job_snapshot": ("jobs.retry_own", "jobs.retry_all"),
    "restore_job_snapshot": ("jobs.retry_own", "jobs.retry_all"),
    "archive_batch_snapshots": ("jobs.retry_own", "jobs.retry_all"),
    "restore_batch_snapshots": ("jobs.retry_own", "jobs.retry_all"),
    "claim_record_lease": ("drafts.create", "hub.manage"),
    "heartbeat_record_lease": ("drafts.create", "hub.manage"),
    "release_record_lease": ("drafts.create", "hub.manage"),
    "bind_lease_gate_batch": ("drafts.create", "hub.manage"),
    "add_artifact": ("drafts.create", "hub.manage"),
    "record_media_usage": ("drafts.create", "hub.manage"),
    "list_media_usage": ("library.view", "hub.manage"),
    "list_audit_events": ("users.manage", "hub.manage"),
    "list_production_presets": ("drafts.create", "hub.manage"),
    "save_production_preset": ("presets.manage_own", "hub.manage"),
    "delete_production_preset": ("presets.manage_own", "hub.manage"),
}


WEB_RPC_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "get_bootstrap": (),
    "get_local_runtime_snapshot": (),
    "get_local_self_check": (),
    "get_local_storage_status": (),
    "cleanup_local_storage_cache": (),
    "create_local_support_bundle": (),
    "get_visual_style_presets": (),
    "get_production_presets": ("drafts.create", "hub.manage"),
    "save_production_preset": ("presets.manage_own", "hub.manage"),
    "delete_production_preset": ("presets.manage_own", "hub.manage"),
    "get_hub_status": (),
    "get_hub_backup_status": ("hub.manage",),
    "list_hub_backups": ("hub.manage",),
    "create_hub_backup": ("hub.manage",),
    "get_update_status": (),
    "check_for_updates": ("updates.manage_own", "hub.manage"),
    "download_update": ("updates.manage_own", "hub.manage"),
    "schedule_update_on_restart": ("updates.manage_own", "hub.manage"),
    "cancel_scheduled_update": ("updates.manage_own", "hub.manage"),
    "save_local_update_preferences": ("updates.manage_own", "hub.manage"),
    "publish_update": ("hub.manage",),
    "clear_published_update": ("hub.manage",),
    "get_component_update_status": (),
    "check_component_updates": ("updates.manage_own", "hub.manage"),
    "install_component_update": ("updates.manage_own", "hub.manage"),
    "rollback_component_update": ("updates.manage_own", "hub.manage"),
    "publish_component_update": ("hub.manage",),
    "clear_published_component": ("hub.manage",),
    "save_platform": ("platforms.manage",),
    "delete_platform": ("platforms.manage",),
    "save_settings": ("hub.manage",),
    "get_library_bootstrap": ("library.view",),
    "import_novel_text": ("library.edit",),
    "import_novel_file": ("library.edit",),
    "read_text_document": ("library.edit",),
    "get_novel": ("library.view",),
    "save_novel": ("library.edit",),
    "delete_novel": ("hub.manage",),
    "save_novel_binding": ("platforms.manage",),
    "add_promo_code": ("promo_codes.manage",),
    "update_promo_code": ("promo_codes.manage",),
    "delete_promo_code": ("hub.manage",),
    "save_publishing_account": ("publishing_accounts.manage",),
    "delete_publishing_account": ("hub.manage",),
    "save_production_draft": ("drafts.create", "drafts.manage_all"),
    "queue_production_draft": ("drafts.create", "drafts.manage_all"),
    "generate_voice_candidates": ("voice.preview",),
    "set_local_tts_provider": ("voice.preview",),
    "generate_intro_card_copy": ("text.assist",),
    "classify_novel": ("text.assist",),
    "lock_novel_voice": ("voice.preview",),
    "save_software_user": ("users.manage",),
    "delete_software_user": ("users.manage",),
    "list_software_users": ("users.manage",),
    "list_managed_devices": ("hub.manage",),
    "get_managed_device": ("hub.manage",),
    "acknowledge_managed_device": ("hub.manage",),
    "rename_managed_device": ("hub.manage",),
    "set_managed_device_active": ("hub.manage",),
    "delete_managed_device": ("hub.manage",),
    "create_managed_device_config": ("hub.manage",),
    "list_managed_device_configs": ("hub.manage",),
    "get_managed_device_config": ("hub.manage",),
    "set_user_permission": ("permissions.manage",),
    "get_effective_permissions": (),
    "get_record_artifacts": ("records.view_own", "records.view_all"),
    "get_production_record_groups": ("records.view_own", "records.view_all"),
    "cancel_production_records": ("jobs.retry_own", "jobs.retry_all"),
    "choose_folder": ("drafts.create", "hub.manage"),
    "open_output_folder": (
        "drafts.create",
        "records.view_own",
        "records.view_all",
        "hub.manage",
    ),
    "trash_production_records": ("records.view_all", "hub.manage"),
    "restore_trashed_production_records": ("records.view_all", "hub.manage"),
    "delete_trashed_production_records": ("records.view_all", "hub.manage"),
    "start_queue": ("drafts.manage_all", "hub.manage"),
    "cancel_queue": ("drafts.manage_all", "hub.manage"),
    "get_jobs": ("drafts.create", "records.view_own", "records.view_all"),
    "get_queue_connection": ("drafts.create", "records.view_own", "records.view_all"),
    "get_archived_jobs": ("records.view_own", "records.view_all"),
    "archive_job": ("jobs.retry_own", "jobs.retry_all"),
    "restore_job": ("jobs.retry_own", "jobs.retry_all"),
    "archive_batch": ("jobs.retry_own", "jobs.retry_all"),
    "restore_batch": ("jobs.retry_own", "jobs.retry_all"),
    "archive_finished_jobs": ("jobs.retry_own", "jobs.retry_all"),
    "clear_finished_jobs": ("drafts.create", "drafts.manage_all", "hub.manage"),
    "approve_preview": ("samples.approve_own", "samples.approve_all"),
    "regenerate_preview": ("samples.approve_own", "samples.approve_all"),
    "retry_failed": ("jobs.retry_own", "jobs.retry_all"),
    "analyze_story": ("library.edit",),
}


WEB_DESKTOP_ONLY_MEDIA_METHODS = frozenset(
    {
        "queue_production_draft",
        "generate_voice_candidates",
        "set_local_tts_provider",
        "start_queue",
        "cancel_queue",
        "approve_preview",
        "regenerate_preview",
        "retry_failed",
        "get_jobs",
        "get_queue_connection",
        "get_archived_jobs",
        "archive_job",
        "restore_job",
        "archive_batch",
        "restore_batch",
        "archive_finished_jobs",
        "clear_finished_jobs",
        "choose_folder",
        "open_output_folder",
        "get_local_runtime_snapshot",
        "get_local_self_check",
        "get_local_storage_status",
        "cleanup_local_storage_cache",
        "create_local_support_bundle",
        "install_component_update",
        "rollback_component_update",
    }
)

CLIENT_LOCAL_MEDIA_METHODS = WEB_DESKTOP_ONLY_MEDIA_METHODS - frozenset(
    {"approve_preview", "regenerate_preview"}
)


LOCAL_WORKER_RPC_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "queue_production_draft": ("drafts.create", "drafts.manage_all", "hub.manage"),
    "generate_voice_candidates": ("voice.preview", "hub.manage"),
    "set_local_tts_provider": ("voice.preview", "hub.manage"),
    "generate_intro_card_copy": ("text.assist", "hub.manage"),
    "start_queue": ("drafts.create", "drafts.manage_all", "hub.manage"),
    "cancel_queue": ("drafts.create", "drafts.manage_all", "hub.manage"),
    "get_jobs": ("drafts.create", "records.view_own", "records.view_all", "hub.manage"),
    "get_queue_connection": ("drafts.create", "records.view_own", "records.view_all", "hub.manage"),
    "get_archived_jobs": ("records.view_own", "records.view_all", "hub.manage"),
    "retry_failed": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "archive_job": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "restore_job": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "archive_batch": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "restore_batch": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "archive_finished_jobs": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "clear_finished_jobs": ("drafts.create", "drafts.manage_all", "hub.manage"),
    "get_record_artifacts": ("records.view_own", "records.view_all", "hub.manage"),
    "cancel_production_records": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "open_output_folder": ("drafts.create", "records.view_own", "records.view_all", "hub.manage"),
    "choose_folder": ("drafts.create", "hub.manage"),
    "worker_profile": ("drafts.create", "hub.manage"),
    "worker_runtime_snapshot": ("drafts.create", "hub.manage"),
    "worker_self_check": ("drafts.create", "hub.manage"),
    "get_local_storage_status": ("drafts.create", "hub.manage"),
    "cleanup_local_storage_cache": ("drafts.create", "hub.manage"),
    "create_local_support_bundle": ("drafts.create", "hub.manage"),
    "worker_set_folders": ("drafts.create", "hub.manage"),
}

LOCAL_WORKER_PRIVATE_METHODS = frozenset(
    {
        "worker_profile",
        "worker_runtime_snapshot",
        "worker_self_check",
        "worker_set_folders",
    }
)


def validate_rpc_contract() -> tuple[str, ...]:
    """Return contract errors without importing any server implementation."""

    errors: list[str] = []
    missing_hub_permissions = sorted(
        (CATALOG_RPC_METHODS - {"bootstrap_summary", "get_effective_permissions"})
        - HUB_RPC_PERMISSION_ANY.keys()
    )
    if missing_hub_permissions:
        errors.append(
            "Hub methods missing permission rules: "
            + ", ".join(missing_hub_permissions)
        )
    unknown_hub_permissions = sorted(HUB_RPC_PERMISSION_ANY.keys() - CATALOG_RPC_METHODS)
    if unknown_hub_permissions:
        errors.append(
            "Hub permission rules reference unknown methods: "
            + ", ".join(unknown_hub_permissions)
        )
    unknown_local_methods = sorted(
        LOCAL_WORKER_RPC_PERMISSIONS.keys()
        - WEB_RPC_PERMISSIONS.keys()
        - LOCAL_WORKER_PRIVATE_METHODS
    )
    if unknown_local_methods:
        errors.append(
            "Local worker methods are absent from the web contract: "
            + ", ".join(unknown_local_methods)
        )
    unknown_media_methods = sorted(WEB_DESKTOP_ONLY_MEDIA_METHODS - WEB_RPC_PERMISSIONS.keys())
    if unknown_media_methods:
        errors.append(
            "Desktop-only methods are absent from the web contract: "
            + ", ".join(unknown_media_methods)
        )
    if not CLIENT_LOCAL_MEDIA_METHODS <= WEB_DESKTOP_ONLY_MEDIA_METHODS:
        errors.append("Client-local media methods must be desktop-only methods")
    return tuple(errors)


def permissions_for(
    surface: str,
) -> Mapping[str, tuple[str, ...]]:
    """Return the named permission map for tests and protocol introspection."""

    normalized = str(surface or "").strip().casefold()
    if normalized == "web":
        return WEB_RPC_PERMISSIONS
    if normalized == "worker":
        return LOCAL_WORKER_RPC_PERMISSIONS
    if normalized == "hub":
        return HUB_RPC_PERMISSION_ANY
    raise KeyError(f"unknown RPC surface: {surface}")


__all__ = [
    "ACCOUNT_PASSWORD_VERIFY_RPC_METHOD",
    "CATALOG_READ_METHODS",
    "CATALOG_RPC_METHODS",
    "CATALOG_WRITE_METHODS",
    "CLIENT_LOCAL_MEDIA_METHODS",
    "DEVICE_ADMIN_RPC_METHODS",
    "DEVICE_CLIENT_RPC_METHODS",
    "DEVICE_SERVICE_RPC_METHODS",
    "HUB_RPC_PERMISSION_ANY",
    "LOCAL_WORKER_RPC_PERMISSIONS",
    "LOCAL_WORKER_PRIVATE_METHODS",
    "LOCAL_WORKER_TICKET_RPC_METHOD",
    "RPC_CONTRACT_VERSION",
    "TEXT_POLISH_RPC_METHOD",
    "WEB_DESKTOP_ONLY_MEDIA_METHODS",
    "WEB_RPC_PERMISSIONS",
    "permissions_for",
    "validate_rpc_contract",
]
