from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from storyforge.catalog import CatalogRepository, CatalogValidationError
from storyforge.credentials import hash_password
from storyforge.hub import (
    CATALOG_RPC_METHODS,
    HUB_PROTOCOL_VERSION,
    MINIMUM_RENDER_CLIENT_VERSION,
    HubAuthenticationError,
    HubCatalogProxy,
    HubClient,
    HubConnectionError,
    HubRemoteError,
    HubServer,
    HubServerStateError,
    HubTextProvider,
    _HubRequestHandler,
    _device_capabilities,
    _render_client_version_is_supported,
)
from storyforge.library_service import LibraryService
from storyforge.models import AppSettings
from storyforge.providers.base import ProviderConfig, ProviderError
from storyforge.providers.text import TextRequest, TextResult
from storyforge.rpc_contract import (
    DEVICE_CAPABILITY_FIELDS,
    LEGACY_DEVICE_CAPABILITY_FIELDS,
    RPC_CONTRACT_VERSION,
)


TOKEN = "correct-hub-token"


class HubTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "data"
        self.attachment_root = self.root / "attachments"
        self.data_root.mkdir()
        self.attachment_root.mkdir()
        self.catalog = CatalogRepository(
            self.root / "catalog.sqlite3",
            site_id="hub-test-site",
            site_name="Hub Test",
            busy_timeout_ms=5000,
        )
        self.actor = self.catalog.save_user(
            {"username": "hub-owner", "role": "admin"}
        )
        self.server = HubServer(
            self.catalog,
            {TOKEN: self.actor["id"], "anonymous-token": None},
            host="127.0.0.1",
            port=0,
            data_root=self.data_root,
            attachment_root=self.attachment_root,
        ).start()
        self.addCleanup(self.server.stop)
        self.client = HubClient(self.server.base_url, TOKEN, timeout_seconds=5)

    def import_story(self, index: int = 1) -> dict:
        return self.client.call(
            "import_novel",
            {
                "value": {
                    "title": f"Network Story {index}",
                    "body": f"A unique network body number {index}.",
                    "episodes": [
                        {
                            "ordinal": 1,
                            "title": "Opening",
                            "source_map": [{"chapter": 1}],
                        }
                    ],
                },
                # The server must discard this forged audit identity.
                "actor_user_id": "forged-user",
            },
        )


class DeviceCapabilityContractTests(unittest.TestCase):
    def test_worker_fault_reason_and_plain_language_message_are_preserved(self) -> None:
        result = _device_capabilities(
            {
                "device_config_sync": 1,
                "worker_state": "fault",
                "worker_reason": "output_volume_unavailable",
                "worker_message": "输出磁盘不可用，请重新连接移动硬盘。",
            }
        )

        self.assertEqual(result["worker_state"], "fault")
        self.assertEqual(result["worker_reason"], "output_volume_unavailable")
        self.assertIn("输出磁盘不可用", result["worker_message"])

    def test_worker_reason_rejects_paths_or_story_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported characters"):
            _device_capabilities(
                {
                    "worker_state": "fault",
                    "worker_reason": r"C:\\private\\output",
                }
            )


class HealthLifecycleAndAuthenticationTests(HubTestCase):
    def test_text_rpc_uses_dedicated_long_timeout(self) -> None:
        captured: dict[str, object] = {}

        class CapturingClient(HubClient):
            def call(
                self,
                method,
                params=None,
                *,
                request_id=None,
                timeout_seconds=None,
            ):
                del params, request_id
                captured["method"] = method
                captured["timeout_seconds"] = timeout_seconds
                return {
                    "polished_text": "Safe text.",
                    "hook": "Safe hook.",
                    "ending_cta": "Continue reading.",
                    "mood": "suspense",
                    "provider": "host-ai",
                    "model": "host-model",
                    "retention_ratio": 1.0,
                }

        client = CapturingClient(
            "http://127.0.0.1:1",
            TOKEN,
            timeout_seconds=8,
            text_timeout_seconds=120,
        )

        result = client.text_polish(TextRequest(text="Safe text."))

        self.assertEqual(result.polished_text, "Safe text.")
        self.assertEqual(captured["method"], "text_polish")
        self.assertEqual(captured["timeout_seconds"], 120.0)

    def test_root_is_safe_human_readable_hub_landing_page(self) -> None:
        secret_in_query = "must-not-be-reflected"
        with urlopen(
            self.server.base_url + "/?access_token=" + secret_in_query,
            timeout=3,
        ) as response:
            document = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.headers.get_content_type(),
                "text/html",
            )
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
            self.assertIn("default-src 'none'", response.headers.get("Content-Security-Policy", ""))

        self.assertIn("StoryForge Hub 已启动", document)
        self.assertIn("这里不是网页后台", document)
        self.assertIn("输入员工账号和密码", document)
        self.assertIn("自动找到 Hub", document)
        self.assertIn("登记设备并启动本机制作服务", document)
        self.assertNotIn("填写上方主电脑地址", document)
        self.assertNotIn("旧令牌", document)
        self.assertIn(self.server.base_url, document)
        self.assertNotIn(secret_in_query, document)
        self.assertNotIn(TOKEN, document)
        self.assertNotIn("hub-owner", document)
        self.assertNotIn("Hub Test", document)

    def test_user_scoped_token_works_immediately_and_revocation_is_immediate(self) -> None:
        producer = self.catalog.save_user(
            {"username": "dynamic-producer", "role": "producer", "active": True}
        )
        issued = self.catalog.issue_hub_access_token(
            producer["id"], label="Render PC 2"
        )
        producer_client = HubClient(
            self.server.base_url, issued["token"], timeout_seconds=5
        )

        self.assertEqual(producer_client.call("list_novels")["total"], 0)
        with self.assertRaises(HubRemoteError) as forbidden:
            producer_client.call("list_users")
        self.assertEqual(forbidden.exception.status, 403)

        self.catalog.revoke_hub_access_token(issued["id"])
        with self.assertRaises(HubAuthenticationError):
            producer_client.call("list_novels")

    def test_health_is_small_public_probe_and_rpc_requires_bearer(self) -> None:
        health = self.client.health()

        self.assertTrue(health["ok"])
        self.assertEqual(health["service"], "storyforge-hub")
        self.assertEqual(health["protocol_version"], 1)
        self.assertEqual(health["minimum_client_protocol_version"], 1)
        self.assertEqual(health["rpc_contract_version"], RPC_CONTRACT_VERSION)
        self.assertEqual(
            set(health["device_capability_fields"]), DEVICE_CAPABILITY_FIELDS
        )
        self.assertIn("bootstrap_summary", health["rpc_methods"])
        self.assertIn("device_heartbeat", health["rpc_methods"])
        self.assertIn("text_polish", health["rpc_methods"])
        self.assertTrue(health["app_version"])
        self.assertEqual(health["site"]["id"], "hub-test-site")
        with urlopen(self.server.base_url + "/health", timeout=3) as response:
            public_health = json.loads(response.read().decode("utf-8"))
        self.assertTrue(public_health["ok"])
        with self.assertRaises(HubAuthenticationError) as caught:
            HubClient(self.server.base_url, "wrong-token").call("bootstrap_summary")
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertFalse(self.server.authenticate("Bearer 中文无效令牌").authenticated)

    def test_identity_handshake_exposes_optional_rpc_capabilities(self) -> None:
        identity = self.client.verify_identity()
        compatibility = identity["hub_compatibility"]

        self.assertEqual(
            compatibility["rpc_contract_version"], RPC_CONTRACT_VERSION
        )
        self.assertEqual(
            set(compatibility["device_capability_fields"]),
            DEVICE_CAPABILITY_FIELDS,
        )
        self.assertIn("bootstrap_summary", compatibility["rpc_methods"])

    def test_old_hub_without_capability_manifest_receives_legacy_fields(self) -> None:
        client = HubClient("http://127.0.0.1:1", TOKEN)
        old_health = {
            "ok": True,
            "service": "storyforge-hub",
            "protocol_version": 1,
            "minimum_client_protocol_version": 1,
        }
        with (
            patch.object(client, "health", return_value=old_health),
            patch.object(client, "call", return_value={"ok": True}) as call,
        ):
            client.heartbeat_device(
                app_version="1.0.0",
                capabilities={
                    "device_config_sync": 2,
                    "local_render": True,
                    "local_tts": False,
                    "local_subtitles": True,
                    "worker_state": "busy",
                    "worker_reason": "rendering",
                    "worker_message": "Rendering one video",
                },
            )

        sent = call.call_args.args[1]["capabilities"]
        self.assertEqual(set(sent), LEGACY_DEVICE_CAPABILITY_FIELDS)
        self.assertNotIn("worker_state", sent)

    def test_client_projects_capabilities_to_server_manifest(self) -> None:
        client = HubClient("http://127.0.0.1:1", TOKEN)
        health = {
            "ok": True,
            "service": "storyforge-hub",
            "protocol_version": 1,
            "minimum_client_protocol_version": 1,
            "device_capability_fields": [
                "local_render",
                "worker_state",
                "future_server_field",
            ],
        }
        with (
            patch.object(client, "health", return_value=health),
            patch.object(client, "call", return_value={"ok": True}) as call,
        ):
            client.heartbeat_device(
                capabilities={"local_render": False, "worker_state": "ready"}
            )

        self.assertEqual(
            call.call_args.args[1]["capabilities"],
            {"local_render": False, "worker_state": "ready"},
        )

    def test_identity_handshake_rejects_incompatible_hub_or_workstation(self) -> None:
        client = HubClient("http://127.0.0.1:1", TOKEN)
        with (
            patch.object(
                client,
                "health",
                return_value={
                    "ok": True,
                    "service": "storyforge-hub",
                    "protocol_version": 0,
                    "minimum_client_protocol_version": 0,
                },
            ),
            self.assertRaisesRegex(HubConnectionError, "Hub protocol is too old"),
        ):
            client.verify_identity()

        with (
            patch.object(
                client,
                "health",
                return_value={
                    "ok": True,
                    "service": "storyforge-hub",
                    "protocol_version": HUB_PROTOCOL_VERSION + 1,
                    "minimum_client_protocol_version": HUB_PROTOCOL_VERSION + 1,
                },
            ),
            self.assertRaisesRegex(HubConnectionError, "workstation is too old"),
        ):
            client.verify_identity()

    def test_missing_authorization_has_bearer_challenge(self) -> None:
        body = json.dumps(
            {"id": "auth", "method": "bootstrap_summary", "params": {}}
        ).encode("utf-8")
        request = Request(
            self.server.base_url + "/rpc",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        error = caught.exception
        try:
            payload = json.loads(error.read().decode("utf-8"))
            self.assertEqual(error.code, 401)
            self.assertIn("Bearer", error.headers.get("WWW-Authenticate", ""))
            self.assertEqual(payload["error"]["code"], "unauthorized")
        finally:
            error.close()

    def test_server_start_stop_are_idempotent_and_restartable(self) -> None:
        first_address = self.server.address
        self.assertIs(self.server.start(), self.server)
        self.assertEqual(self.server.address, first_address)

        self.server.stop()
        self.server.stop()
        self.assertFalse(self.server.is_running)
        with self.assertRaises(HubServerStateError):
            _ = self.server.address
        with self.assertRaises(HubConnectionError):
            self.client.health()

        self.server.start()
        self.assertTrue(self.server.is_running)
        self.client = HubClient(self.server.base_url, TOKEN, timeout_seconds=5)
        self.assertTrue(self.client.health()["ok"])

    def test_constructor_rejects_empty_tokens_and_whitelist_expansion(self) -> None:
        with self.assertRaises(ValueError):
            HubServer(self.catalog, [])
        with self.assertRaisesRegex(ValueError, "whitespace"):
            HubServer(self.catalog, "token with spaces")
        with self.assertRaisesRegex(ValueError, "ASCII"):
            HubServer(self.catalog, "令牌")
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            HubServer(self.catalog, TOKEN, enabled_methods=[])
        with self.assertRaisesRegex(ValueError, "max_upload_bytes"):
            HubServer(self.catalog, TOKEN, max_upload_bytes=0)
        with self.assertRaisesRegex(ValueError, "outside the fixed allowlist"):
            HubServer(self.catalog, TOKEN, enabled_methods=["_connect"])

    def test_text_service_is_authenticated_bounded_and_not_a_catalog_proxy(self) -> None:
        secret_config = ProviderConfig(
            name="groq",
            model="host-model",
            endpoint="https://host-secret.invalid/v1",
            api_key="host-secret-key",
            options={"command": "host-only-command"},
        )
        seen: list[tuple[ProviderConfig, TextRequest]] = []

        class HostProvider:
            def __init__(self, config: ProviderConfig) -> None:
                self.config = config

            def polish(self, request: TextRequest) -> TextResult:
                seen.append((self.config, request))
                return TextResult(
                    polished_text=request.text,
                    hook="A safe hook.",
                    ending_cta="Continue reading.",
                    mood="suspense",
                    provider="groq",
                    model="host-model",
                    retention_ratio=1.0,
                )

        producer = self.catalog.save_user(
            {"username": "text-worker", "role": "producer", "active": True}
        )
        issued = self.catalog.issue_hub_access_token(
            producer["id"], label="Text Worker"
        )
        service = HubServer(
            self.catalog,
            {issued["token"]: producer["id"]},
            host="127.0.0.1",
            port=0,
            text_provider_config_getter=lambda: secret_config,
            text_provider_factory=HostProvider,
            text_polish_max_concurrency=1,
        ).start()
        self.addCleanup(service.stop)
        client = HubClient(service.base_url, issued["token"], timeout_seconds=5)
        provider = HubTextProvider(client)

        result = provider.polish(TextRequest(text="The locked door opened."))

        self.assertEqual(result.polished_text, "The locked door opened.")
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][0], secret_config)
        self.assertEqual(seen[0][1].text, "The locked door opened.")
        serialized = json.dumps(result.to_dict())
        self.assertNotIn(secret_config.api_key, serialized)
        self.assertNotIn(secret_config.endpoint, serialized)
        self.assertNotIn("host-only-command", serialized)
        self.assertNotIn("text_polish", CATALOG_RPC_METHODS)
        with self.assertRaises(AttributeError):
            getattr(HubCatalogProxy(client), "text_polish")

        with self.assertRaises(HubRemoteError) as unknown_field:
            client.call(
                "text_polish",
                {"request": {"text": "Safe", "api_key": "injected"}},
            )
        self.assertEqual(unknown_field.exception.status, 400)
        self.assertEqual(unknown_field.exception.code, "invalid_text_request")
        with self.assertRaises(HubRemoteError) as too_long:
            client.call(
                "text_polish",
                {"request": {"text": "x" * 200_001}},
            )
        self.assertEqual(too_long.exception.status, 400)

        # There are no media execution service RPCs on Hub.
        for method in ("tts_synthesize", "ffmpeg_render"):
            with self.assertRaises(HubRemoteError) as unavailable:
                client.call(method, {})
            self.assertEqual(unavailable.exception.status, 404)
        self.assertEqual(len(seen), 1)

        class FailingHostProvider:
            def __init__(self, _config: ProviderConfig) -> None:
                pass

            def polish(self, _request: TextRequest) -> TextResult:
                raise RuntimeError("host model is temporarily unavailable")

        service._text_provider_factory = FailingHostProvider
        with self.assertRaises(ProviderError) as strict_failure:
            provider.polish(TextRequest(text="Keep this story text."))
        self.assertEqual(strict_failure.exception.provider, "hub_text")
        self.assertTrue(strict_failure.exception.retryable)
        self.assertEqual(len(seen), 1)

        legacy_fallback = HubTextProvider(
            client,
            allow_local_fallback=True,
            max_attempts=1,
            retry_delay_seconds=0,
        ).polish(TextRequest(text="Keep this story text."))
        self.assertEqual(legacy_fallback.polished_text, "Keep this story text.")
        self.assertEqual(legacy_fallback.provider, "local")

        self.catalog.set_user_permission(
            producer["id"],
            "text.assist",
            False,
            actor_user_id=self.actor["id"],
        )
        with self.assertRaises(HubRemoteError) as forbidden:
            client.text_polish(TextRequest(text="No permission."))
        self.assertEqual(forbidden.exception.status, 403)


class DeviceEnrollmentTests(HubTestCase):
    PASSWORD = "Hr1!2026"

    def _save_member(self, username: str, *, active: bool = True, password: bool = True) -> dict:
        value = {
            "username": username,
            "role": "producer",
            "active": active,
        }
        if password:
            value["password_hash"] = hash_password(self.PASSWORD)
        return self.catalog.save_user(value)

    def test_password_enrollment_verifies_and_revocation_is_immediate(self) -> None:
        member = self._save_member("password-renderer")

        enrolled = HubClient.enroll_device(
            self.server.base_url,
            member["username"],
            self.PASSWORD,
            "Render PC 7",
            timeout_seconds=5,
        )

        self.assertTrue(enrolled["token"].startswith("sfh_"))
        self.assertEqual(enrolled["device_name"], "Render PC 7")
        self.assertEqual(enrolled["user"]["id"], member["id"])
        enrolled_client = HubClient(
            self.server.base_url, enrolled["token"], timeout_seconds=5
        )
        self.assertEqual(
            enrolled_client.verify_identity()["site"]["id"], "hub-test-site"
        )
        listed = self.catalog.list_hub_access_tokens(member["id"])
        self.assertEqual(listed["total"], 1)
        self.assertNotIn("token", listed["items"][0])
        self.assertEqual(listed["items"][0]["label"], "Render PC 7")

        self.catalog.revoke_hub_access_token(enrolled["token_id"])
        with self.assertRaises(HubAuthenticationError):
            enrolled_client.verify_identity()

    def test_enrollment_projects_new_fields_for_an_old_hub(self) -> None:
        member = self._save_member("legacy-capability-hub")
        old_health = {
            "ok": True,
            "service": "storyforge-hub",
            "protocol_version": 1,
            "minimum_client_protocol_version": 1,
        }

        with patch.object(HubClient, "health", return_value=old_health):
            enrolled = HubClient.enroll_device(
                self.server.base_url,
                member["username"],
                self.PASSWORD,
                "Rolling Update PC",
                capabilities={
                    "device_config_sync": 1,
                    "local_render": True,
                    "local_tts": True,
                    "local_subtitles": True,
                    "worker_state": "ready",
                    "worker_reason": "",
                    "worker_message": "Ready",
                },
                timeout_seconds=5,
            )

        stored = enrolled["device"]["capabilities"]
        self.assertEqual(set(stored), LEGACY_DEVICE_CAPABILITY_FIELDS)

    def test_current_hub_persists_extended_capabilities_across_heartbeat(self) -> None:
        member = self._save_member("current-capability-hub")
        enrolled = HubClient.enroll_device(
            self.server.base_url,
            member["username"],
            self.PASSWORD,
            "Current Worker PC",
            capabilities={
                "device_config_sync": 1,
                "local_render": True,
                "local_tts": True,
                "local_subtitles": True,
                "worker_state": "ready",
                "worker_reason": "",
                "worker_message": "Ready for production",
            },
            timeout_seconds=5,
        )
        self.assertEqual(
            enrolled["device"]["capabilities"]["worker_state"],
            "ready",
        )

        client = HubClient(
            self.server.base_url,
            enrolled["token"],
            timeout_seconds=5,
        )
        heartbeat = client.heartbeat_device(
            app_version="1.0.1",
            capabilities={
                "device_config_sync": 1,
                "local_render": True,
                "local_tts": True,
                "local_subtitles": True,
                "worker_state": "busy",
                "worker_reason": "rendering",
                "worker_message": "Rendering one video",
            },
        )

        stored = heartbeat["device"]["capabilities"]
        self.assertEqual(stored["worker_state"], "busy")
        self.assertEqual(stored["worker_reason"], "rendering")
        self.assertEqual(stored["worker_message"], "Rendering one video")

    def test_browser_worker_ticket_is_device_bound_short_lived_and_one_use(self) -> None:
        member = self._save_member("browser-renderer")
        enrolled = HubClient.enroll_device(
            self.server.base_url,
            member["username"],
            self.PASSWORD,
            "Browser Render PC",
            timeout_seconds=5,
        )
        client = HubClient(self.server.base_url, enrolled["token"], timeout_seconds=5)
        nonce = "worker-nonce-" + uuid4().hex
        issued = self.server.issue_local_worker_ticket(
            member["id"],
            device_id=enrolled["device_id"],
            worker_nonce=nonce,
            browser_origin="http://10.0.0.225:8765",
        )

        redeemed = client.redeem_local_worker_ticket(
            issued["ticket"],
            worker_nonce=nonce,
            browser_origin="http://10.0.0.225:8765",
        )
        self.assertEqual(redeemed["actor_user_id"], member["id"])
        self.assertEqual(redeemed["device_id"], enrolled["device_id"])
        self.assertIn("drafts.create", redeemed["permissions"])
        with self.assertRaises(HubAuthenticationError) as reused:
            client.redeem_local_worker_ticket(
                issued["ticket"],
                worker_nonce=nonce,
                browser_origin="http://10.0.0.225:8765",
            )
        self.assertEqual(reused.exception.code, "worker_ticket_invalid")

    def test_wrong_unknown_unconfigured_and_inactive_accounts_are_indistinguishable(self) -> None:
        valid = self._save_member("wrong-password-member")
        inactive = self._save_member("inactive-member", active=False)
        unconfigured = self._save_member("unconfigured-member", password=False)
        attempts = (
            (valid["username"], "WrongPassword2026"),
            ("unknown-member", self.PASSWORD),
            (inactive["username"], self.PASSWORD),
            (unconfigured["username"], self.PASSWORD),
        )

        for username, password in attempts:
            with self.subTest(username=username):
                with self.assertRaises(HubAuthenticationError) as caught:
                    HubClient.enroll_device(
                        self.server.base_url,
                        username,
                        password,
                        "Untrusted PC",
                        timeout_seconds=5,
                    )
                self.assertEqual(caught.exception.status, 401)
                self.assertEqual(caught.exception.code, "enrollment_failed")
                self.assertEqual(
                    caught.exception.message,
                    "account or password is incorrect",
                )

    def test_stable_installation_reuses_device_and_rotates_its_token(self) -> None:
        member = self._save_member("stable-renderer")
        installation_id = str(uuid4())
        first = HubClient.enroll_device(
            self.server.base_url,
            member["username"],
            self.PASSWORD,
            "Render PC",
            installation_id=installation_id,
            app_version="0.3.3",
            capabilities={"device_config_sync": 1, "local_render": True},
            hostname="render-pc",
            os_name="Windows",
            architecture="AMD64",
            timeout_seconds=5,
        )
        second = HubClient.enroll_device(
            self.server.base_url,
            member["username"],
            self.PASSWORD,
            "A renamed client value",
            installation_id=installation_id,
            app_version="0.3.4",
            capabilities={"device_config_sync": 1, "local_render": True},
            timeout_seconds=5,
        )

        self.assertEqual(first["device_id"], second["device_id"])
        self.assertEqual(self.catalog.list_hub_devices()["total"], 1)
        self.assertEqual(second["device_name"], "Render PC")
        with self.assertRaises(HubAuthenticationError):
            HubClient(self.server.base_url, first["token"]).verify_identity()
        identity = self.catalog.resolve_hub_access_identity(second["token"])
        self.assertEqual(identity["device_id"], first["device_id"])
        self.assertTrue(identity["token_id"])

    def test_old_render_client_must_update_before_claiming_new_work(self) -> None:
        member = self._save_member("outdated-renderer")
        enrolled = HubClient.enroll_device(
            self.server.base_url,
            member["username"],
            self.PASSWORD,
            "Outdated Render PC",
            installation_id=str(uuid4()),
            app_version="0.4.6",
            timeout_seconds=5,
        )
        client = HubClient(self.server.base_url, enrolled["token"], timeout_seconds=5)

        with self.assertRaises(HubRemoteError) as outdated:
            client.call(
                "claim_record_lease",
                {"record_id": "missing-record", "device_id": enrolled["device_id"]},
            )

        self.assertEqual(outdated.exception.status, 426)
        self.assertEqual(outdated.exception.code, "client_update_required")
        self.assertIn("版本过旧，请更新", outdated.exception.message)
        self.assertIn(MINIMUM_RENDER_CLIENT_VERSION, outdated.exception.message)

    def test_current_and_future_render_clients_are_not_version_blocked(self) -> None:
        supported = (
            "0.4.7",
            "0.4.8-rc1",
            "0.5.0-rc1",
            "1.0.0",
        )
        for version in supported:
            with self.subTest(version=version):
                self.assertTrue(_render_client_version_is_supported(version))
        for version in (
            "",
            "0.4.7-rc1",
            "0.4.7-rc99",
            "0.4.6",
            "0.4.0-rc7",
            "0.3.99",
            "development",
            "0.4",
        ):
            with self.subTest(version=version):
                self.assertFalse(_render_client_version_is_supported(version))

    def test_device_rpc_permissions_and_bound_identity_prevent_spoofing(self) -> None:
        first_member = self._save_member("first-device")
        second_member = self._save_member("second-device")
        first = HubClient.enroll_device(
            self.server.base_url,
            first_member["username"],
            self.PASSWORD,
            "First PC",
            installation_id=str(uuid4()),
            timeout_seconds=5,
        )
        second = HubClient.enroll_device(
            self.server.base_url,
            second_member["username"],
            self.PASSWORD,
            "Second PC",
            installation_id=str(uuid4()),
            timeout_seconds=5,
        )
        first_client = HubClient(self.server.base_url, first["token"])
        second_client = HubClient(self.server.base_url, second["token"])

        with self.assertRaises(HubRemoteError) as forbidden:
            first_client.call("devices_list")
        self.assertEqual(forbidden.exception.status, 403)
        revision = self.client.call(
            "device_config_create",
            {
                "value": {
                    "target_mode": "single",
                    "device_ids": [first["device_id"]],
                    "config": {"output_fps": 60, "bgm_volume": 0.33},
                }
            },
        )
        self.assertEqual(revision["target_device_ids"], [first["device_id"]])
        desired = first_client.get_desired_device_config()
        self.assertTrue(desired["needs_apply"])
        self.assertIsNone(second_client.get_desired_device_config()["desired"])

        with self.assertRaises(HubRemoteError) as spoofed:
            first_client.call(
                "device_desired_config",
                {"device_id": second["device_id"]},
            )
        self.assertEqual(spoofed.exception.status, 400)
        acknowledged = first_client.acknowledge_device_config(
            revision["id"], reported_config_hash=revision["config_hash"]
        )
        self.assertEqual(acknowledged["device_id"], first["device_id"])

        legacy = self.catalog.issue_hub_access_token(
            first_member["id"], label="Legacy token"
        )
        legacy_client = HubClient(self.server.base_url, legacy["token"])
        self.assertEqual(legacy_client.call("list_novels")["total"], 0)
        with self.assertRaises(HubRemoteError) as unbound:
            legacy_client.heartbeat_device(app_version="0.3.3")
        self.assertEqual(unbound.exception.code, "device_identity_required")


class RpcTests(HubTestCase):
    def test_whitelisted_write_and_read_calls_return_json_dicts(self) -> None:
        imported = self.import_story()
        listed = self.client.call(
            "list_novels", {"query": "Network Story", "limit": 20, "offset": 0}
        )
        fetched = self.client.call(
            "get_novel", {"novel_id": imported["novel"]["id"]}
        )

        self.assertTrue(imported["created"])
        self.assertEqual(listed["total"], 1)
        self.assertEqual(fetched["id"], imported["novel"]["id"])
        self.assertEqual(
            fetched["current_revision"]["body"], "A unique network body number 1."
        )

    def test_private_and_unknown_methods_are_not_dispatchable(self) -> None:
        for method in ("_connect", "__class__", "delete_everything", "missing"):
            with self.subTest(method=method):
                with self.assertRaises(HubRemoteError) as caught:
                    self.client.call(method, request_id=f"request-{method}")
                self.assertEqual(caught.exception.status, 404)
                self.assertEqual(caught.exception.code, "method_not_allowed")
                self.assertEqual(caught.exception.request_id, f"request-{method}")

    def test_catalog_validation_and_missing_entities_remain_structured(self) -> None:
        with self.assertRaises(HubRemoteError) as invalid:
            self.client.call("import_novel", {"value": {"title": "No body"}})
        self.assertEqual(invalid.exception.status, 400)
        self.assertEqual(invalid.exception.code, "validation_error")

        with self.assertRaises(HubRemoteError) as missing:
            self.client.call("get_novel", {"novel_id": "not-here"})
        self.assertEqual(missing.exception.status, 404)
        self.assertEqual(missing.exception.code, "catalog_not_found")

    def test_token_identity_overrides_forged_actor_for_write_audit(self) -> None:
        imported = self.import_story()
        events = self.catalog.list_audit_events(entity_type="novel")["items"]
        matching = [
            event
            for event in events
            if event["entity_id"] == imported["novel"]["id"]
            and event["action"] == "novel.imported"
        ]

        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["actor_user_id"], self.actor["id"])

    def test_anonymous_token_can_check_health_but_cannot_call_rpc(self) -> None:
        anonymous = HubClient(self.server.base_url, "anonymous-token")
        self.assertTrue(anonymous.health()["ok"])
        for method, params in (
            ("bootstrap_summary", {}),
            ("list_novels", {}),
            (
                "import_novel",
                {
                    "value": {"title": "Anonymous", "body": "Anonymous body."},
                    "actor_user_id": self.actor["id"],
                },
            ),
        ):
            with self.subTest(method=method):
                with self.assertRaises(HubRemoteError) as caught:
                    anonymous.call(method, params)
                self.assertEqual(caught.exception.status, 403)
                self.assertEqual(caught.exception.code, "forbidden")

    def test_concurrent_identical_imports_are_atomically_deduplicated(self) -> None:
        def import_copy(index: int) -> dict:
            client = HubClient(self.server.base_url, TOKEN, timeout_seconds=10)
            return client.call(
                "import_novel",
                {
                    "value": {
                        "title": f"Concurrent Copy {index}",
                        "body": "The exact same concurrent manuscript body.",
                    }
                },
            )

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(import_copy, range(10)))

        self.assertEqual(sum(bool(item["created"]) for item in results), 1)
        self.assertEqual(sum(bool(item["deduplicated"]) for item in results), 9)
        self.assertEqual(len({item["novel"]["id"] for item in results}), 1)
        self.assertEqual(self.client.call("list_novels")["total"], 1)

    def test_malformed_json_and_wrong_content_type_are_rejected(self) -> None:
        for body, content_type, expected in (
            (b"{broken", "application/json", "invalid_json"),
            (b"{}", "text/plain", "unsupported_media_type"),
        ):
            with self.subTest(expected=expected):
                request = Request(
                    self.server.base_url + "/rpc",
                    data=body,
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Content-Type": content_type,
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=3)
                error = caught.exception
                try:
                    payload = json.loads(error.read().decode("utf-8"))
                    self.assertEqual(payload["error"]["code"], expected)
                finally:
                    error.close()

    def test_request_body_limit_is_enforced_before_json_dispatch(self) -> None:
        limited = HubServer(
            self.catalog,
            TOKEN,
            host="127.0.0.1",
            port=0,
            max_request_bytes=1024,
        ).start()
        self.addCleanup(limited.stop)
        request = Request(
            limited.base_url + "/rpc",
            data=b"{" + b"x" * 1100,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        error = caught.exception
        try:
            payload = json.loads(error.read().decode("utf-8"))
            self.assertEqual(error.code, 413)
            self.assertEqual(payload["error"]["code"], "request_too_large")
        finally:
            error.close()


class CatalogProxyTests(HubTestCase):
    def test_proxy_binds_repository_positional_arguments_into_rpc_params(self) -> None:
        proxy = HubCatalogProxy(self.client)
        imported = proxy.import_novel(
            {"title": "Proxy Story", "body": "A story imported through the proxy."}
        )
        novel = proxy.get_novel(imported["novel"]["id"])
        binding = proxy.save_novel_binding(
            {
                "novel_id": novel["id"],
                "platform_name": "GoodNovel",
                "platform_title": "Proxy Story",
            }
        )
        code = proxy.add_promo_code(
            {"binding_id": binding["id"], "code": "PX73165"}
        )
        updated = proxy.update_promo_code(code["id"], {"status": "inactive"})
        producer = proxy.save_user({"username": "proxy-producer", "role": "producer"})
        permissions = proxy.set_user_permission(
            producer["id"], "library.edit", True
        )

        self.assertEqual(novel["title"], "Proxy Story")
        self.assertEqual(updated["status"], "inactive")
        self.assertTrue(permissions["effective"]["library.edit"])

        service = LibraryService(proxy, AppSettings, self.root / "remote-library")
        self.assertEqual(service.novel_for_ui(novel["id"])["id"], novel["id"])

    def test_proxy_preserves_keyword_only_and_argument_count_validation(self) -> None:
        proxy = HubCatalogProxy(self.client)
        self.assertEqual(proxy.list_novels(limit=5, offset=0)["limit"], 5)
        with self.assertRaises(TypeError):
            proxy.get_novel("one", "unexpected")
        with self.assertRaises(TypeError):
            proxy.list_novels(5)
        with self.assertRaises(AttributeError):
            _ = proxy._connect

    def test_proxy_retries_selection_key_for_legacy_voice_schema(self) -> None:
        proxy = HubCatalogProxy(self.client)
        attempts: list[dict] = []

        def legacy_call(method: str, parameters: dict) -> dict:
            attempts.append({"method": method, "parameters": parameters})
            candidates = parameters["voice_state"]["voice_candidates"]
            if "selection_key" in candidates[0]:
                raise HubRemoteError(
                    400,
                    "validation_error",
                    "voice candidate 1 contains unsupported fields: selection_key",
                )
            return {"id": "novel-1", "metadata": {"voice_candidates": candidates}}

        with patch.object(self.client, "call", side_effect=legacy_call):
            saved = proxy.save_novel_voice_state(
                "novel-1",
                {
                    "voice_candidates": [
                        {
                            "provider": "local_kokoro",
                            "voice_id": "af_heart",
                            "selection_key": "stable-key",
                        }
                    ]
                },
            )

        self.assertEqual(len(attempts), 2)
        self.assertIn(
            "selection_key",
            attempts[0]["parameters"]["voice_state"]["voice_candidates"][0],
        )
        self.assertNotIn(
            "selection_key",
            attempts[1]["parameters"]["voice_state"]["voice_candidates"][0],
        )
        self.assertEqual(
            saved["metadata"]["voice_candidates"][0]["voice_id"],
            "af_heart",
        )


class PermissionEnforcementTests(HubTestCase):
    def restart_with_users(self, users: dict[str, str | None]) -> dict[str, HubClient]:
        self.server.stop()
        tokens = {TOKEN: self.actor["id"], "anonymous-token": None, **users}
        self.server = HubServer(
            self.catalog,
            tokens,
            host="127.0.0.1",
            port=0,
            data_root=self.data_root,
            attachment_root=self.attachment_root,
        ).start()
        self.addCleanup(self.server.stop)
        self.client = HubClient(self.server.base_url, TOKEN, timeout_seconds=5)
        return {
            token: HubClient(self.server.base_url, token, timeout_seconds=5)
            for token in users
        }

    def story_relationships(self, suffix: str = "access") -> tuple[dict, dict, dict]:
        novel = self.catalog.import_novel(
            {
                "title": f"Permission Story {suffix}",
                "body": f"Permission-controlled manuscript {suffix}.",
                "episodes": [{"ordinal": 1, "title": "Opening"}],
            }
        )["novel"]
        binding = self.catalog.save_novel_binding(
            {
                "novel_id": novel["id"],
                "platform_name": "GoodNovel",
                "platform_title": novel["title"],
            }
        )
        code = self.catalog.add_promo_code(
            {"binding_id": binding["id"], "code": f"CODE{suffix.upper()}"}
        )
        return novel, binding, code

    def make_draft(
        self,
        actor_user_id: str,
        novel: dict,
        binding: dict,
        code: dict,
    ) -> dict:
        return self.catalog.save_draft(
            {
                "novel_id": novel["id"],
                "binding_id": binding["id"],
                "promo_code_id": code["id"],
                "creative_line_count": 1,
                "episode_ids": [novel["current_revision"]["episodes"][0]["id"]],
            },
            actor_user_id=actor_user_id,
        )

    def test_role_defaults_and_account_overrides_are_enforced_on_every_call(self) -> None:
        producer = self.catalog.save_user(
            {"username": "permission-producer", "role": "producer"}
        )
        novel, _binding, _code = self.story_relationships("role")
        producer_client = self.restart_with_users({"producer-token": producer["id"]})[
            "producer-token"
        ]

        self.assertEqual(producer_client.call("list_novels")["total"], 1)
        self.assertEqual(
            producer_client.call(
                "get_effective_permissions", {"user_id": producer["id"]}
            )["user_id"],
            producer["id"],
        )
        with self.assertRaises(HubRemoteError) as other_permissions:
            producer_client.call(
                "get_effective_permissions", {"user_id": self.actor["id"]}
            )
        self.assertEqual(other_permissions.exception.status, 403)
        with self.assertRaises(HubRemoteError) as edit_denied:
            producer_client.call(
                "import_novel",
                {"value": {"title": "Denied", "body": "No permission."}},
            )
        self.assertEqual(edit_denied.exception.status, 403)

        self.catalog.set_user_permission(
            producer["id"], "library.edit", True, actor_user_id=self.actor["id"]
        )
        created = producer_client.call(
            "import_novel",
            {"value": {"title": "Granted", "body": "Permission granted."}},
        )
        self.assertTrue(created["created"])
        self.catalog.set_user_permission(
            producer["id"], "library.view", False, actor_user_id=self.actor["id"]
        )
        with self.assertRaises(HubRemoteError) as view_denied:
            producer_client.call("get_novel", {"novel_id": novel["id"]})
        self.assertEqual(view_denied.exception.status, 403)

        # Administrators start with every permission, but an explicit per-user
        # deny must still win over the role default.
        self.catalog.set_user_permission(
            self.actor["id"], "library.edit", False, actor_user_id=self.actor["id"]
        )
        with self.assertRaises(HubRemoteError) as admin_override:
            self.client.call(
                "import_novel",
                {"value": {"title": "Admin Denied", "body": "Override wins."}},
            )
        self.assertEqual(admin_override.exception.status, 403)

    def test_removed_supervisor_role_is_rejected(self) -> None:
        with self.assertRaisesRegex(CatalogValidationError, "admin or producer"):
            self.catalog.save_user(
                {"username": "content-supervisor", "role": "supervisor"}
            )

    def test_inactive_mapped_user_is_denied_even_minimal_rpc(self) -> None:
        inactive = self.catalog.save_user(
            {"username": "inactive-producer", "role": "producer", "active": False}
        )
        inactive_client = self.restart_with_users({"inactive-token": inactive["id"]})[
            "inactive-token"
        ]
        with self.assertRaises(HubRemoteError) as denied:
            inactive_client.call("bootstrap_summary")
        self.assertEqual(denied.exception.status, 403)
        self.assertEqual(denied.exception.code, "forbidden")

    def test_method_permission_families_and_nested_actor_identity(self) -> None:
        producer = self.catalog.save_user(
            {"username": "method-producer", "role": "producer"}
        )
        novel, binding, code = self.story_relationships("methods")
        producer_client = self.restart_with_users({"producer-token": producer["id"]})[
            "producer-token"
        ]

        # Producer defaults permit promo-code use and draft creation, but not
        # management of platforms, codes, publishing accounts, or users.
        self.assertEqual(
            producer_client.call(
                "list_promo_codes", {"binding_id": binding["id"]}
            )["historical_count"],
            1,
        )
        draft = producer_client.call(
            "save_draft",
            {
                "value": {
                    "novel_id": novel["id"],
                    "binding_id": binding["id"],
                    "promo_code_id": code["id"],
                    "creative_line_count": 1,
                    "created_by_user_id": self.actor["id"],
                    "metadata": {
                        "platform_search_text": "Search this batch: CODEMETHODS",
                        "platform_ending_text": "Continue this exact batch.",
                    },
                }
            },
        )
        self.assertEqual(draft["created_by_user_id"], producer["id"])
        self.assertEqual(
            draft["metadata"]["platform_search_text"],
            "Search this batch: CODEMETHODS",
        )
        self.assertEqual(
            draft["metadata"]["platform_ending_text"],
            "Continue this exact batch.",
        )
        effective = producer_client.call(
            "get_effective_permissions", {"user_id": producer["id"]}
        )["effective"]
        self.assertTrue(effective["drafts.create"])
        self.assertFalse(effective["platforms.manage"])
        self.catalog.set_user_permission(
            producer["id"], "promo_codes.use", False, actor_user_id=self.actor["id"]
        )
        with self.assertRaises(HubRemoteError) as code_use_denied:
            producer_client.call(
                "save_draft",
                {
                    "value": {
                        "novel_id": novel["id"],
                        "binding_id": binding["id"],
                        "promo_code_id": code["id"],
                        "creative_line_count": 1,
                    }
                },
            )
        self.assertEqual(code_use_denied.exception.status, 403)
        self.catalog.set_user_permission(
            producer["id"], "promo_codes.use", None, actor_user_id=self.actor["id"]
        )

        denied_calls = (
            ("save_platform", {"value": {"name": "Denied Platform"}}),
            (
                "add_promo_code",
                {"value": {"binding_id": binding["id"], "code": "DENIED"}},
            ),
            (
                "save_publishing_account",
                {"value": {"network": "TikTok", "handle": "denied"}},
            ),
            ("save_user", {"value": {"username": "denied-user"}}),
            (
                "set_user_permission",
                {
                    "user_id": producer["id"],
                    "permission": "library.edit",
                    "allowed": True,
                },
            ),
        )
        for method, params in denied_calls:
            with self.subTest(method=method):
                with self.assertRaises(HubRemoteError) as denied:
                    producer_client.call(method, params)
                self.assertEqual(denied.exception.status, 403)

        for permission in (
            "platforms.manage",
            "promo_codes.manage",
            "publishing_accounts.manage",
            "users.manage",
            "permissions.manage",
        ):
            self.catalog.set_user_permission(
                producer["id"], permission, True, actor_user_id=self.actor["id"]
            )
        self.assertEqual(
            producer_client.call(
                "update_promo_code",
                {"promo_code_id": code["id"], "value": {"status": "inactive"}},
            )["status"],
            "inactive",
        )
        self.assertEqual(
            producer_client.call(
                "save_platform", {"value": {"name": "Granted Platform"}}
            )["name"],
            "Granted Platform",
        )
        self.assertEqual(
            producer_client.call(
                "save_publishing_account",
                {"value": {"network": "TikTok", "handle": "granted"}},
            )["handle"],
            "granted",
        )
        self.assertGreaterEqual(producer_client.call("list_users")["total"], 2)
        permissions = producer_client.call(
            "set_user_permission",
            {
                "user_id": producer["id"],
                "permission": "library.edit",
                "allowed": True,
            },
        )
        self.assertTrue(permissions["effective"]["library.edit"])

    def test_producer_draft_and_record_queries_are_forced_to_own_scope(self) -> None:
        first = self.catalog.save_user(
            {"username": "scope-first", "role": "producer"}
        )
        second = self.catalog.save_user(
            {"username": "scope-second", "role": "producer"}
        )
        novel, binding, code = self.story_relationships("scope")
        first_draft = self.make_draft(first["id"], novel, binding, code)
        second_draft = self.make_draft(second["id"], novel, binding, code)
        first_record = self.catalog.save_production_record(
            {
                "draft_id": first_draft["id"],
                "device_id": "first-device",
                "job_id": "scope-first-job",
            },
            actor_user_id=first["id"],
        )
        second_record = self.catalog.save_production_record(
            {
                "draft_id": second_draft["id"],
                "device_id": "second-device",
                "job_id": "scope-second-job",
            },
            actor_user_id=second["id"],
        )
        first_client = self.restart_with_users(
            {"first-token": first["id"], "second-token": second["id"]}
        )["first-token"]

        records = first_client.call(
            "list_records", {"created_by_user_id": second["id"], "limit": 20}
        )
        drafts = first_client.call(
            "list_drafts", {"created_by_user_id": second["id"], "limit": 20}
        )
        self.assertEqual([item["id"] for item in records["items"]], [first_record["id"]])
        self.assertEqual([item["id"] for item in drafts["items"]], [first_draft["id"]])
        summaries = first_client.call(
            "get_production_batch_summaries",
            {"batch_ids": [first_record["batch_id"], second_record["batch_id"]]},
        )["items"]
        self.assertEqual(set(summaries), {first_record["batch_id"]})
        self.assertEqual(summaries[first_record["batch_id"]]["total"], 1)
        with self.assertRaises(HubRemoteError) as other_record:
            first_client.call("get_record", {"record_id": second_record["id"]})
        self.assertEqual(other_record.exception.status, 403)
        with self.assertRaises(HubRemoteError) as other_draft:
            first_client.call("get_draft", {"draft_id": second_draft["id"]})
        self.assertEqual(other_draft.exception.status, 403)

        self.catalog.set_user_permission(
            first["id"], "records.view_own", False, actor_user_id=self.actor["id"]
        )
        with self.assertRaises(HubRemoteError) as no_record_view:
            first_client.call("list_records")
        self.assertEqual(no_record_view.exception.status, 403)
        self.catalog.set_user_permission(
            first["id"], "records.view_all", True, actor_user_id=self.actor["id"]
        )
        with self.assertRaises(HubRemoteError) as view_is_not_write:
            first_client.call(
                "save_production_record",
                {"value": {"id": second_record["id"], "status": "running"}},
            )
        self.assertEqual(view_is_not_write.exception.status, 403)
        self.catalog.set_user_permission(
            first["id"], "drafts.manage_all", True, actor_user_id=self.actor["id"]
        )
        self.assertEqual(first_client.call("list_records")["total"], 2)
        self.assertEqual(first_client.call("list_drafts")["total"], 2)
        self.assertEqual(
            first_client.call("get_record", {"record_id": second_record["id"]})["id"],
            second_record["id"],
        )
        self.assertEqual(
            first_client.call("get_draft", {"draft_id": second_draft["id"]})["id"],
            second_draft["id"],
        )

    def test_bulk_record_rpc_is_atomic_and_cannot_forge_ownership(self) -> None:
        first = self.catalog.save_user(
            {"username": "bulk-first", "role": "producer"}
        )
        second = self.catalog.save_user(
            {"username": "bulk-second", "role": "producer"}
        )
        novel, binding, code = self.story_relationships("bulk")
        first_draft = self.make_draft(first["id"], novel, binding, code)
        second_draft = self.make_draft(second["id"], novel, binding, code)
        client = self.restart_with_users(
            {"bulk-first-token": first["id"], "bulk-second-token": second["id"]}
        )["bulk-first-token"]

        with self.assertRaises(HubRemoteError):
            client.call(
                "save_production_records_bulk",
                {
                    "values": [
                        {"draft_id": first_draft["id"], "job_id": "rollback-ok"},
                        {
                            "draft_id": first_draft["id"],
                            "job_id": "rollback-invalid",
                            "variant_index": 2,
                        },
                    ]
                },
            )
        self.assertEqual(client.call("list_records", {"limit": 20})["total"], 0)

        result = client.call(
            "save_production_records_bulk",
            {
                "values": [
                    {
                        "draft_id": first_draft["id"],
                        "job_id": f"bulk-job-{index}",
                        "variant_index": 1,
                        "created_by_user_id": second["id"],
                    }
                    for index in range(1, 4)
                ]
            },
        )
        self.assertEqual(result["count"], 3)
        self.assertEqual(
            {item["created_by_user_id"] for item in result["items"]},
            {first["id"]},
        )
        with self.assertRaises(HubRemoteError) as forbidden:
            client.call(
                "save_production_records_bulk",
                {"values": [{"draft_id": second_draft["id"], "job_id": "forged"}]},
            )
        self.assertEqual(forbidden.exception.status, 403)

    def test_job_archive_rpc_is_durable_and_forced_to_own_scope(self) -> None:
        first = self.catalog.save_user(
            {"username": "archive-first", "role": "producer"}
        )
        second = self.catalog.save_user(
            {"username": "archive-second", "role": "producer"}
        )
        novel, binding, code = self.story_relationships("archive")
        first_draft = self.make_draft(first["id"], novel, binding, code)
        second_draft = self.make_draft(second["id"], novel, binding, code)
        first_record = self.catalog.save_production_record(
            {"draft_id": first_draft["id"], "job_id": "first-archive", "status": "failed"},
            actor_user_id=first["id"],
        )
        self.catalog.save_production_record(
            {"draft_id": second_draft["id"], "job_id": "second-archive", "status": "failed"},
            actor_user_id=second["id"],
        )
        first_client = self.restart_with_users(
            {"first-archive-token": first["id"], "second-archive-token": second["id"]}
        )["first-archive-token"]

        archived = first_client.call(
            "archive_job_snapshot",
            {
                "job_id": "first-archive",
                "snapshot": {"id": "first-archive", "status": "failed", "message": "keep me"},
            },
        )

        self.assertTrue(archived["job"]["archived"])
        own = first_client.call("list_archived_jobs")
        self.assertEqual([item["id"] for item in own["items"]], ["first-archive"])
        with self.assertRaises(HubRemoteError) as forbidden:
            first_client.call(
                "archive_job_snapshot",
                {
                    "job_id": "second-archive",
                    "snapshot": {"id": "second-archive", "status": "failed"},
                },
            )
        self.assertEqual(forbidden.exception.status, 403)

        restored = first_client.call(
            "restore_job_snapshot", {"job_id": "first-archive"}
        )
        self.assertFalse(restored["job"]["archived"])
        self.assertFalse(self.catalog.get_record(first_record["id"])["archived"])
        self.assertEqual(first_client.call("list_archived_jobs")["total"], 0)

    def test_batch_archive_rpc_is_atomic_idempotent_and_forced_to_own_scope(self) -> None:
        first = self.catalog.save_user(
            {"username": "batch-archive-first", "role": "producer"}
        )
        second = self.catalog.save_user(
            {"username": "batch-archive-second", "role": "producer"}
        )
        novel, binding, code = self.story_relationships("batch-archive")
        first_draft = self.make_draft(first["id"], novel, binding, code)
        second_draft = self.make_draft(second["id"], novel, binding, code)
        run_id = "hub-batch-archive-run"
        first_records = [
            self.catalog.save_production_record(
                {
                    "draft_id": first_draft["id"],
                    "job_id": f"hub-batch-archive-{index}",
                    "status": status,
                    "metadata": {"production_run_id": run_id},
                },
                actor_user_id=first["id"],
            )
            for index, status in ((1, "completed"), (2, "failed"))
        ]
        foreign = self.catalog.save_production_record(
            {
                "draft_id": second_draft["id"],
                "job_id": "hub-foreign-batch-job",
                "status": "failed",
            },
            actor_user_id=second["id"],
        )
        batch_id = str(first_records[0]["batch_id"])
        clients = self.restart_with_users(
            {
                "batch-archive-first-token": first["id"],
                "batch-archive-second-token": second["id"],
            }
        )
        first_client = clients["batch-archive-first-token"]
        snapshots = [
            {
                "id": str(record["job_id"]),
                "batch_id": batch_id,
                "status": str(record["status"]),
                "platform_id": "platform",
            }
            for record in first_records
        ]

        archived = first_client.call(
            "archive_batch_snapshots",
            {"batch_id": batch_id, "snapshots": snapshots},
        )
        self.assertEqual(archived["changed_count"], 2)
        archived_batch = first_client.call(
            "get_archived_batch", {"batch_id": batch_id}
        )
        self.assertEqual(archived_batch["archived_count"], 2)
        repeated = first_client.call(
            "archive_batch_snapshots",
            {"batch_id": batch_id, "snapshots": []},
        )
        self.assertTrue(repeated["already_archived"])

        with self.assertRaises(HubRemoteError) as forbidden:
            first_client.call(
                "archive_batch_snapshots",
                {
                    "batch_id": foreign["batch_id"],
                    "snapshots": [
                        {"id": foreign["job_id"], "status": "failed"}
                    ],
                },
            )
        self.assertEqual(forbidden.exception.status, 403)
        with self.assertRaises(HubRemoteError) as missing:
            first_client.call(
                "archive_batch_snapshots",
                {"batch_id": "missing-batch", "snapshots": []},
            )
        self.assertEqual(missing.exception.status, 404)

        restored = first_client.call(
            "restore_batch_snapshots", {"batch_id": batch_id}
        )
        self.assertEqual(restored["restored_count"], 2)
        repeated_restore = first_client.call(
            "restore_batch_snapshots", {"batch_id": batch_id}
        )
        self.assertTrue(repeated_restore["already_restored"])

    def test_lease_rpc_proxy_lifecycle_and_expired_reclaim(self) -> None:
        novel, binding, code = self.story_relationships("lease-rpc")
        draft = self.make_draft(self.actor["id"], novel, binding, code)
        record = self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "hub-lease-lifecycle"},
            actor_user_id=self.actor["id"],
        )
        proxy = HubCatalogProxy(self.client)

        claimed = proxy.claim_record_lease(
            record["id"], "hub-worker-a", lease_seconds=60
        )
        self.assertTrue(claimed["claimed"])
        first_generation = claimed["record"]["lease_generation"]
        with self.assertRaises(HubRemoteError) as same_device_process:
            proxy.claim_record_lease(record["id"], "hub-worker-a", lease_seconds=60)
        self.assertEqual(same_device_process.exception.status, 409)
        with self.assertRaises(HubRemoteError) as wrong_device:
            proxy.heartbeat_record_lease(record["id"], "hub-worker-b")
        self.assertEqual(wrong_device.exception.status, 409)
        heartbeat = proxy.heartbeat_record_lease(
            record["id"],
            "hub-worker-a",
            lease_generation=first_generation,
            lease_seconds=90,
        )
        self.assertTrue(heartbeat["heartbeat"])

        with self.catalog._write_connection() as connection:
            connection.execute(
                """
                UPDATE production_records
                SET lease_expires_at = '2000-01-01T00:00:00+00:00'
                WHERE id = ?
                """,
                (record["id"],),
            )
        reclaimed = proxy.claim_record_lease(
            record["id"], "hub-worker-b", lease_seconds=60
        )
        self.assertTrue(reclaimed["reclaimed"])
        self.assertEqual(
            reclaimed["record"]["lease_owner_device"], "hub-worker-b"
        )
        released = proxy.release_record_lease(
            record["id"],
            "hub-worker-b",
            lease_generation=reclaimed["record"]["lease_generation"],
        )
        self.assertTrue(released["released"])
        with self.assertRaises(TypeError):
            proxy.claim_record_lease(record["id"], "device", 60)

    def test_concurrent_hub_lease_claim_has_one_winner(self) -> None:
        novel, binding, code = self.story_relationships("lease-race")
        draft = self.make_draft(self.actor["id"], novel, binding, code)
        record = self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "hub-lease-race"},
            actor_user_id=self.actor["id"],
        )

        def claim(index: int) -> tuple[str, str]:
            device = f"hub-device-{index}"
            client = HubClient(self.server.base_url, TOKEN, timeout_seconds=10)
            try:
                result = client.call(
                    "claim_record_lease",
                    {"record_id": record["id"], "device_id": device},
                )
                return "claimed", result["record"]["lease_owner_device"]
            except HubRemoteError as error:
                self.assertEqual(error.status, 409)
                return "conflict", device

        with ThreadPoolExecutor(max_workers=10) as executor:
            outcomes = list(executor.map(claim, range(10)))
        winners = [device for status, device in outcomes if status == "claimed"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(
            self.catalog.get_record(record["id"])["lease_owner_device"], winners[0]
        )

    def test_lease_permissions_are_own_scoped_and_offline_proxy_cannot_create(self) -> None:
        password = "Lease1!Password"
        first = self.catalog.save_user(
            {
                "username": "lease-owner",
                "role": "producer",
                "password_hash": hash_password(password),
            }
        )
        second = self.catalog.save_user(
            {
                "username": "lease-other",
                "role": "producer",
                "password_hash": hash_password(password),
            }
        )
        novel, binding, code = self.story_relationships("lease-scope")
        first_draft = self.make_draft(first["id"], novel, binding, code)
        second_draft = self.make_draft(second["id"], novel, binding, code)
        first_record = self.catalog.save_production_record(
            {"draft_id": first_draft["id"], "job_id": "lease-owned"},
            actor_user_id=first["id"],
        )
        second_record = self.catalog.save_production_record(
            {"draft_id": second_draft["id"], "job_id": "lease-other"},
            actor_user_id=second["id"],
        )
        clients = self.restart_with_users(
            {"lease-owner-token": first["id"], "lease-other-token": second["id"]}
        )
        # A producer's legacy bearer token is intentionally not sufficient
        # for a lease: the Hub must know which enrolled computer owns it.
        with self.assertRaises(HubRemoteError) as unbound:
            HubCatalogProxy(clients["lease-owner-token"]).claim_record_lease(
                first_record["id"], "forged-device"
            )
        self.assertEqual(unbound.exception.status, 403)
        self.assertEqual(unbound.exception.code, "device_identity_required")

        enrolled = HubClient.enroll_device(
            self.server.base_url,
            first["username"],
            password,
            "Lease owner PC",
            timeout_seconds=5,
        )
        proxy = HubCatalogProxy(
            HubClient(self.server.base_url, enrolled["token"], timeout_seconds=5)
        )
        self.assertTrue(
            proxy.claim_record_lease(
                first_record["id"], enrolled["device_id"]
            )["claimed"]
        )
        with self.assertRaises(HubRemoteError) as other_denied:
            proxy.claim_record_lease(second_record["id"], enrolled["device_id"])
        self.assertEqual(other_denied.exception.status, 403)

        before_total = self.catalog.list_records()["total"]
        self.server.stop()
        with self.assertRaises(HubConnectionError):
            proxy.save_production_record(
                {"draft_id": first_draft["id"], "job_id": "must-not-exist"}
            )
        self.assertEqual(self.catalog.list_records()["total"], before_total)
        self.assertEqual(
            self.catalog.get_record(second_record["id"])["lease_owner_device"], ""
        )

    def test_retry_and_claim_is_bound_to_the_authenticated_workstation(self) -> None:
        password = "Retry1!Password"
        producer = self.catalog.save_user(
            {
                "username": "retry-owner",
                "role": "producer",
                "password_hash": hash_password(password),
            }
        )
        novel, binding, code = self.story_relationships("retry-claim")
        draft = self.make_draft(producer["id"], novel, binding, code)
        record = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "retry-claim-owned",
                "status": "failed",
            },
            actor_user_id=producer["id"],
        )
        self.restart_with_users({"retry-owner-token": producer["id"]})
        enrolled = HubClient.enroll_device(
            self.server.base_url,
            producer["username"],
            password,
            "Retry owner PC",
            timeout_seconds=5,
        )
        proxy = HubCatalogProxy(
            HubClient(self.server.base_url, enrolled["token"], timeout_seconds=5)
        )

        with self.assertRaises(HubRemoteError) as mismatch:
            proxy.begin_record_retry(
                record["id"],
                device_id="forged-device",
                lease_seconds=60,
            )
        self.assertEqual(mismatch.exception.status, 403)
        self.assertEqual(mismatch.exception.code, "device_identity_mismatch")

        retried = proxy.begin_record_retry(
            record["id"],
            device_id=enrolled["device_id"],
            lease_seconds=60,
        )
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["current_attempt"], 2)
        self.assertEqual(retried["lease_owner_device"], enrolled["device_id"])
        self.assertGreater(retried["lease_generation"], 0)
        updated = proxy.save_production_record(
            {
                "id": record["id"],
                "status": "running",
                "expected_lease_generation": retried["lease_generation"],
            }
        )
        self.assertEqual(updated["status"], "running")

    def test_enrolled_device_cannot_overwrite_same_users_other_device_lease(self) -> None:
        password = "Lease2!Password"
        user = self.catalog.save_user(
            {
                "username": "shared-member-two-devices",
                "role": "producer",
                "password_hash": hash_password(password),
            }
        )
        novel, binding, code = self.story_relationships("device-owner")
        draft = self.make_draft(user["id"], novel, binding, code)
        record = self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "device-owned-job"},
            actor_user_id=user["id"],
        )
        first = HubClient.enroll_device(
            self.server.base_url,
            user["username"],
            password,
            "First render PC",
            timeout_seconds=5,
        )
        second = HubClient.enroll_device(
            self.server.base_url,
            user["username"],
            password,
            "Second render PC",
            timeout_seconds=5,
        )
        first_proxy = HubCatalogProxy(
            HubClient(self.server.base_url, first["token"], timeout_seconds=5)
        )
        second_proxy = HubCatalogProxy(
            HubClient(self.server.base_url, second["token"], timeout_seconds=5)
        )
        first_proxy.claim_record_lease(record["id"], first["device_id"])

        with self.assertRaises(HubRemoteError) as single_denied:
            second_proxy.save_production_record(
                {"id": record["id"], "status": "completed", "progress": 1.0}
            )
        self.assertEqual(single_denied.exception.status, 409)

        with self.assertRaises(HubRemoteError) as bulk_denied:
            second_proxy.save_production_records_bulk(
                [
                    {
                        "id": record["id"],
                        "status": "completed",
                        "progress": 1.0,
                    }
                ]
            )
        self.assertEqual(bulk_denied.exception.status, 409)
        persisted = self.catalog.get_record(record["id"])
        self.assertEqual(persisted["status"], "queued")
        self.assertEqual(persisted["lease_owner_device"], first["device_id"])


class SecureDownloadTests(HubTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sample = self.data_root / "previews" / "sample.mp4"
        self.sample.parent.mkdir()
        self.sample.write_bytes(b"sample-video-bytes")
        self.narration = self.attachment_root / "voice" / "episode-01.wav"
        self.narration.parent.mkdir()
        self.narration.write_bytes(b"RIFF-narration-bytes")

    def test_downloads_sample_and_narration_from_named_roots(self) -> None:
        sample_bytes = self.client.download_file("data", "previews/sample.mp4")
        narration_bytes = self.client.download_file(
            "attachments", r"voice\episode-01.wav"
        )
        destination = self.root / "client-output" / "copy.wav"
        saved = self.client.download_file(
            "attachments", "voice/episode-01.wav", destination=destination
        )

        self.assertEqual(sample_bytes, b"sample-video-bytes")
        self.assertEqual(narration_bytes, b"RIFF-narration-bytes")
        self.assertEqual(destination.read_bytes(), b"RIFF-narration-bytes")
        self.assertEqual(saved["path"], str(destination.resolve()))
        self.assertEqual(saved["size_bytes"], len(b"RIFF-narration-bytes"))
        self.assertEqual(
            saved["sha256"],
            hashlib.sha256(b"RIFF-narration-bytes").hexdigest(),
        )

    def test_download_response_declares_size_and_sha256(self) -> None:
        response = self.client._request("/files/data/previews/sample.mp4")
        with response:
            self.assertEqual(
                int(response.headers["Content-Length"]),
                len(b"sample-video-bytes"),
            )
            self.assertEqual(
                response.headers["X-Content-SHA256"],
                hashlib.sha256(b"sample-video-bytes").hexdigest(),
            )

    def test_head_metadata_is_lightweight_cached_and_detects_replacement(self) -> None:
        first = self.client.file_metadata("data", "previews/sample.mp4")
        second = self.client.file_metadata("data", "previews/sample.mp4")
        self.assertEqual(first, second)
        self.assertEqual(first["size_bytes"], len(b"sample-video-bytes"))
        self.assertEqual(
            first["sha256"], hashlib.sha256(b"sample-video-bytes").hexdigest()
        )
        self.assertTrue(first["etag"].startswith('"sha256-'))
        self.assertEqual(len(self.server._download_metadata_cache), 1)

        replacement = b"R" * len(b"sample-video-bytes")
        self.sample.write_bytes(replacement)
        changed = self.client.file_metadata("data", "previews/sample.mp4")
        self.assertEqual(changed["size_bytes"], len(replacement))
        self.assertEqual(changed["sha256"], hashlib.sha256(replacement).hexdigest())
        self.assertNotEqual(changed["sha256"], first["sha256"])
        self.assertEqual(len(self.server._download_metadata_cache), 1)

    def test_file_metadata_falls_back_for_legacy_hub_without_head_route(self) -> None:
        client = HubClient("http://127.0.0.1:1", TOKEN)

        def legacy_head(*_args, **_kwargs):
            raise HubRemoteError(404, "not_found", "route not found")

        client._request = legacy_head  # type: ignore[method-assign]
        self.assertIsNone(client.file_metadata("attachments", "cover.jpg"))

    def test_incomplete_or_tampered_download_never_replaces_destination(self) -> None:
        class StaticResponse:
            def __init__(self, payload: bytes, *, size: int, sha256: str) -> None:
                self.payload = payload
                self.headers = {
                    "Content-Length": str(size),
                    "X-Content-SHA256": sha256,
                    "Content-Type": "image/png",
                }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size: int = -1) -> bytes:
                payload, self.payload = self.payload, b""
                return payload

        destination = self.root / "client-output" / "logo.png"
        destination.parent.mkdir()
        destination.write_bytes(b"known-good-cache")
        client = HubClient("http://127.0.0.1:1", TOKEN)

        incomplete = StaticResponse(
            b"short",
            size=20,
            sha256=hashlib.sha256(b"short").hexdigest(),
        )
        client._request = lambda *_args, **_kwargs: incomplete  # type: ignore[method-assign]
        with self.assertRaisesRegex(HubConnectionError, "incomplete"):
            client.download_file("attachments", "logo.png", destination=destination)
        self.assertEqual(destination.read_bytes(), b"known-good-cache")

        tampered = StaticResponse(
            b"tampered",
            size=len(b"tampered"),
            sha256=hashlib.sha256(b"expected").hexdigest(),
        )
        client._request = lambda *_args, **_kwargs: tampered  # type: ignore[method-assign]
        with self.assertRaisesRegex(HubConnectionError, "SHA-256"):
            client.download_file("attachments", "logo.png", destination=destination)
        self.assertEqual(destination.read_bytes(), b"known-good-cache")
        self.assertEqual(list(destination.parent.glob(".*.part")), [])

    def test_download_requires_authentication(self) -> None:
        with self.assertRaises(HubAuthenticationError):
            HubClient(self.server.base_url, "wrong-token").download_file(
                "data", "previews/sample.mp4"
            )

    def test_parent_traversal_cannot_escape_root_even_to_allowed_extension(self) -> None:
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"secret-outside-root")

        for traversal in ("../outside.mp4", r"..\outside.mp4", "previews/../../outside.mp4"):
            with self.subTest(traversal=traversal):
                with self.assertRaises(HubRemoteError) as caught:
                    self.client.download_file("data", traversal)
                self.assertEqual(caught.exception.status, 403)
                self.assertEqual(caught.exception.code, "path_outside_root")

        with self.assertRaises(HubRemoteError) as absolute:
            self.client.download_file("data", str(outside.resolve()))
        self.assertEqual(absolute.exception.status, 403)
        self.assertEqual(absolute.exception.code, "path_outside_root")

    def test_unknown_root_missing_file_directory_and_disallowed_type_fail_closed(self) -> None:
        forbidden = self.data_root / "catalog.sqlite3"
        forbidden.write_bytes(b"not-a-real-db")

        cases = (
            ("other", "sample.mp4", 404, "download_root_not_found"),
            ("data", "missing.mp4", 404, "file_not_found"),
            ("data", "previews", 404, "file_not_found"),
            ("data", "catalog.sqlite3", 403, "file_type_not_allowed"),
        )
        for alias, path, status, code in cases:
            with self.subTest(alias=alias, path=path):
                with self.assertRaises(HubRemoteError) as caught:
                    self.client.download_file(alias, path)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(caught.exception.code, code)

    def test_symlink_to_outside_root_is_rejected_when_supported(self) -> None:
        outside = self.root / "outside-link-target.mp4"
        outside.write_bytes(b"outside")
        link = self.data_root / "linked.mp4"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symbolic links unavailable: {error}")

        with self.assertRaises(HubRemoteError) as caught:
            self.client.download_file("data", "linked.mp4")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(caught.exception.code, "path_outside_root")


class SecureUploadTests(HubTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sources = self.root / "client-sources"
        self.sources.mkdir()
        self.sample_source = self.sources / "sample.mp4"
        self.sample_source.write_bytes(b"remote-sample-video")
        self.narration_source = self.sources / "narration.wav"
        self.narration_source.write_bytes(b"RIFF-remote-narration")
        self.alignment_source = self.sources / "alignment.ass"
        self.alignment_source.write_text(
            "[Script Info]\nTitle: Alignment\n", encoding="utf-8"
        )
        self.srt_source = self.sources / "captions.srt"
        self.srt_source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nOpening\n", encoding="utf-8"
        )
        self.json_source = self.sources / "alignment.json"
        self.json_source.write_text('{"segments": []}', encoding="utf-8")

    def test_uploads_sample_and_narration_then_admin_downloads_them(self) -> None:
        sample = self.client.upload_file(
            "data", "previews/sample.mp4", self.sample_source
        )
        narration = self.client.upload_file(
            "attachments", r"voice\narration.wav", self.narration_source
        )

        self.assertFalse(sample["replaced"])
        self.assertEqual(sample["size_bytes"], self.sample_source.stat().st_size)
        self.assertEqual(
            sample["sha256"], hashlib.sha256(self.sample_source.read_bytes()).hexdigest()
        )
        self.assertEqual(narration["path"], "voice/narration.wav")
        self.assertEqual(
            self.client.download_file("data", "previews/sample.mp4"),
            self.sample_source.read_bytes(),
        )
        self.assertEqual(
            self.client.download_file("attachments", "voice/narration.wav"),
            self.narration_source.read_bytes(),
        )

    def test_subtitle_alignment_extensions_have_explicit_non_fallback_mime(self) -> None:
        cases = (
            (self.alignment_source, "subtitles/alignment.ass", "text/x-ssa"),
            (self.srt_source, "subtitles/captions.srt", "application/x-subrip"),
            (self.json_source, "subtitles/alignment.json", "application/json"),
        )
        for source, remote_path, expected_type in cases:
            with self.subTest(remote_path=remote_path):
                uploaded = self.client.upload_file(
                    "attachments", remote_path, source
                )
                destination = self.root / "downloaded" / source.name
                downloaded = self.client.download_file(
                    "attachments", remote_path, destination=destination
                )
                self.assertTrue(uploaded["content_type"].startswith(expected_type))
                self.assertTrue(downloaded["content_type"].startswith(expected_type))
                self.assertNotEqual(
                    downloaded["content_type"], "application/octet-stream"
                )
                self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_upload_requires_valid_mapped_user_and_sha256_header(self) -> None:
        with self.assertRaises(HubAuthenticationError):
            HubClient(self.server.base_url, "wrong-token").upload_file(
                "data", "sample.mp4", self.sample_source
            )
        with self.assertRaises(HubRemoteError) as anonymous:
            HubClient(self.server.base_url, "anonymous-token").upload_file(
                "data", "sample.mp4", self.sample_source
            )
        self.assertEqual(anonymous.exception.status, 403)

        request = Request(
            self.server.base_url + "/files/data/no-digest.mp4",
            data=b"missing digest",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
            },
            method="PUT",
        )
        with self.assertRaises(HTTPError) as missing:
            urlopen(request, timeout=3)
        error = missing.exception
        try:
            payload = json.loads(error.read().decode("utf-8"))
            self.assertEqual(error.code, 400)
            self.assertEqual(payload["error"]["code"], "sha256_required")
        finally:
            error.close()
        self.assertFalse((self.data_root / "no-digest.mp4").exists())

    def test_size_limit_is_checked_before_creating_destination(self) -> None:
        limited = HubServer(
            self.catalog,
            {TOKEN: self.actor["id"]},
            host="127.0.0.1",
            port=0,
            data_root=self.data_root,
            max_upload_bytes=4,
        ).start()
        self.addCleanup(limited.stop)
        client = HubClient(limited.base_url, TOKEN, timeout_seconds=5)

        with self.assertRaises(HubRemoteError) as caught:
            client.upload_file("data", "too-large.mp4", self.sample_source)
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(caught.exception.code, "upload_too_large")
        self.assertFalse((self.data_root / "too-large.mp4").exists())

    def test_hash_mismatch_preserves_old_file_and_success_atomically_replaces_it(self) -> None:
        destination = self.data_root / "previews" / "sample.mp4"
        destination.parent.mkdir()
        destination.write_bytes(b"old-complete-file")

        with self.assertRaises(HubRemoteError) as mismatch:
            self.client.upload_file(
                "data", "previews/sample.mp4", self.sample_source, sha256="0" * 64
            )
        self.assertEqual(mismatch.exception.status, 422)
        self.assertEqual(mismatch.exception.code, "sha256_mismatch")
        self.assertEqual(destination.read_bytes(), b"old-complete-file")
        self.assertEqual(list(destination.parent.glob(".*.upload")), [])

        result = self.client.upload_file(
            "data", "previews/sample.mp4", self.sample_source
        )
        self.assertTrue(result["replaced"])
        self.assertEqual(destination.read_bytes(), self.sample_source.read_bytes())
        self.assertEqual(list(destination.parent.glob(".*.upload")), [])

    def test_hash_mismatch_response_waits_for_temporary_cleanup(self) -> None:
        destination = self.data_root / "previews" / "sample.mp4"
        destination.parent.mkdir()
        destination.write_bytes(b"old-complete-file")
        cleanup_entered = threading.Event()
        allow_cleanup = threading.Event()
        response_started = threading.Event()
        request_errors: list[BaseException] = []
        original_unlink = Path.unlink
        original_send_failure = _HubRequestHandler._send_failure

        def gated_unlink(path: Path, *args, **kwargs):
            if path.name.endswith(".upload"):
                cleanup_entered.set()
                if not allow_cleanup.wait(3):
                    raise TimeoutError("test did not release upload cleanup")
            return original_unlink(path, *args, **kwargs)

        def observed_send_failure(handler, *args, **kwargs):
            response_started.set()
            return original_send_failure(handler, *args, **kwargs)

        def upload_bad_digest() -> None:
            try:
                self.client.upload_file(
                    "data",
                    "previews/sample.mp4",
                    self.sample_source,
                    sha256="0" * 64,
                )
            except BaseException as error:
                request_errors.append(error)

        with (
            patch.object(Path, "unlink", new=gated_unlink),
            patch.object(
                _HubRequestHandler,
                "_send_failure",
                new=observed_send_failure,
            ),
        ):
            request_thread = threading.Thread(target=upload_bad_digest, daemon=True)
            request_thread.start()
            try:
                self.assertTrue(
                    cleanup_entered.wait(2),
                    "Hub did not begin removing the rejected upload",
                )
                self.assertFalse(
                    response_started.is_set(),
                    "Hub started its reply before rejected-upload cleanup completed",
                )
            finally:
                allow_cleanup.set()
                request_thread.join(3)

        self.assertFalse(request_thread.is_alive())
        self.assertTrue(response_started.is_set())
        self.assertEqual(len(request_errors), 1)
        mismatch = request_errors[0]
        self.assertIsInstance(mismatch, HubRemoteError)
        self.assertEqual(mismatch.status, 422)  # type: ignore[attr-defined]
        self.assertEqual(mismatch.code, "sha256_mismatch")  # type: ignore[attr-defined]
        self.assertEqual(destination.read_bytes(), b"old-complete-file")
        self.assertEqual(list(destination.parent.glob(".*.upload")), [])

    def test_upload_cleanup_retries_a_transient_windows_sharing_violation(self) -> None:
        original_unlink = Path.unlink
        winerror_32 = OSError("synthetic Windows sharing violation")
        winerror_32.winerror = 32  # type: ignore[attr-defined]
        for filename, first_error in (
            (
                "permission-error.mp4",
                PermissionError("synthetic Windows permission lock"),
            ),
            ("winerror-32.mp4", winerror_32),
        ):
            with self.subTest(filename=filename):
                destination = self.data_root / "previews" / filename
                destination.parent.mkdir(exist_ok=True)
                destination.write_bytes(b"old-complete-file")
                upload_unlink_attempts = 0

                def flaky_unlink(path: Path, *args, **kwargs):
                    nonlocal upload_unlink_attempts
                    if path.name.endswith(".upload"):
                        upload_unlink_attempts += 1
                        if upload_unlink_attempts == 1:
                            raise first_error
                    return original_unlink(path, *args, **kwargs)

                with patch.object(Path, "unlink", new=flaky_unlink):
                    with self.assertRaises(HubRemoteError) as mismatch:
                        self.client.upload_file(
                            "data",
                            f"previews/{filename}",
                            self.sample_source,
                            sha256="0" * 64,
                        )

                self.assertEqual(mismatch.exception.status, 422)
                self.assertEqual(upload_unlink_attempts, 2)
                self.assertEqual(destination.read_bytes(), b"old-complete-file")
                self.assertEqual(list(destination.parent.glob(".*.upload")), [])
        self.assertEqual(self.server.last_error, "")

    def test_upload_cleanup_records_a_persistent_windows_sharing_violation(self) -> None:
        destination = self.data_root / "previews" / "sample.mp4"
        destination.parent.mkdir()
        destination.write_bytes(b"old-complete-file")
        original_unlink = Path.unlink
        upload_unlink_attempts = 0

        def locked_unlink(path: Path, *args, **kwargs):
            nonlocal upload_unlink_attempts
            if path.name.endswith(".upload"):
                upload_unlink_attempts += 1
                raise PermissionError("synthetic persistent Windows sharing violation")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=locked_unlink):
            with self.assertRaises(HubRemoteError) as mismatch:
                self.client.upload_file(
                    "data",
                    "previews/sample.mp4",
                    self.sample_source,
                    sha256="0" * 64,
                )

        self.assertEqual(mismatch.exception.status, 422)
        self.assertGreater(upload_unlink_attempts, 1)
        self.assertLess(upload_unlink_attempts, 10)
        self.assertIn("PermissionError", self.server.last_error)
        self.assertIn("persistent Windows sharing violation", self.server.last_error)

    def test_upload_reuses_root_extension_and_traversal_guards(self) -> None:
        outside = self.root / "escaped.mp4"
        outside_alignment = self.root / "escaped.ass"
        cases = (
            ("data", "../escaped.mp4", 403, "path_outside_root"),
            ("attachments", "../escaped.ass", 403, "path_outside_root"),
            ("data", str(outside.resolve()), 403, "path_outside_root"),
            ("other", "sample.mp4", 404, "download_root_not_found"),
            ("data", "catalog.sqlite3", 403, "file_type_not_allowed"),
        )
        for alias, path, status, code in cases:
            with self.subTest(alias=alias, path=path):
                with self.assertRaises(HubRemoteError) as caught:
                    self.client.upload_file(alias, path, self.sample_source)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(caught.exception.code, code)
        self.assertFalse(outside.exists())
        self.assertFalse(outside_alignment.exists())

        directory_target = self.data_root / "directory.mp4"
        directory_target.mkdir()
        with self.assertRaises(HubRemoteError) as directory:
            self.client.upload_file("data", "directory.mp4", self.sample_source)
        self.assertEqual(directory.exception.status, 409)
        self.assertEqual(directory.exception.code, "upload_target_invalid")

    def test_upload_symlink_to_outside_root_is_rejected_when_supported(self) -> None:
        outside = self.root / "outside-upload-target.mp4"
        outside.write_bytes(b"must-stay-unchanged")
        link = self.data_root / "linked-upload.mp4"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symbolic links unavailable: {error}")

        with self.assertRaises(HubRemoteError) as caught:
            self.client.upload_file("data", "linked-upload.mp4", self.sample_source)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(caught.exception.code, "path_outside_root")
        self.assertEqual(outside.read_bytes(), b"must-stay-unchanged")


class SharedProductionPresetRpcTests(HubTestCase):
    def test_presets_are_personal_and_admin_can_manage_every_owner(self) -> None:
        employee = self.catalog.save_user(
            {"username": "preset-editor", "role": "producer"}
        )
        other_employee = self.catalog.save_user(
            {"username": "preset-other", "role": "producer"}
        )
        shared = HubServer(
            self.catalog,
            {
                "admin-preset-token": self.actor["id"],
                "employee-preset-token": employee["id"],
                "other-preset-token": other_employee["id"],
            },
            host="127.0.0.1",
            port=0,
            data_root=self.data_root,
            attachment_root=self.attachment_root,
        ).start()
        self.addCleanup(shared.stop)
        admin = HubClient(shared.base_url, "admin-preset-token", timeout_seconds=5)
        producer = HubClient(
            shared.base_url, "employee-preset-token", timeout_seconds=5
        )
        other = HubClient(shared.base_url, "other-preset-token", timeout_seconds=5)

        initial = producer.call("list_production_presets", {})
        self.assertEqual(initial["total"], 0)
        saved = producer.call(
            "save_production_preset",
            {
                "value": {
                    "name": "员工共享方案",
                    "recipe": {
                        "story_mood": "romance",
                        "target_video_count": 27,
                        "production_settings": {
                            "narration_wpm": 220,
                            "output_fps": 60,
                        },
                    },
                },
                # The server must replace this forged audit actor.
                "actor_user_id": self.actor["id"],
            },
        )
        self.assertEqual(saved["owner_user_id"], employee["id"])
        self.assertEqual(saved["scope"], "personal")
        self.assertTrue(saved["editable"])
        self.assertNotIn(
            saved["id"],
            {item["id"] for item in other.call("list_production_presets", {})["items"]},
        )
        for method, params in (
            (
                "save_production_preset",
                {"value": {**saved, "description": "forged overwrite"}},
            ),
            ("delete_production_preset", {"preset_id": saved["id"]}),
        ):
            with self.subTest(method=method), self.assertRaises(HubRemoteError) as denied:
                other.call(method, params)
            self.assertEqual(denied.exception.status, 403)

        admin_personal = admin.call(
            "save_production_preset",
            {
                "value": {
                    "name": "管理员团队方案",
                    "recipe": {"production_settings": {"output_fps": 60}},
                }
            },
        )
        self.assertEqual(admin_personal["scope"], "personal")
        self.assertEqual(admin_personal["owner_user_id"], self.actor["id"])
        employee_items = producer.call("list_production_presets", {})["items"]
        self.assertNotIn(
            admin_personal["id"], {item["id"] for item in employee_items}
        )

        visible = admin.call("list_production_presets", {})
        self.assertTrue(
            any(item["id"] == saved["id"] for item in visible["items"])
        )
        self.assertTrue(
            any(item["id"] == admin_personal["id"] for item in visible["items"])
        )
        admin_updated = admin.call(
            "save_production_preset",
            {"value": {**saved, "description": "administrator support edit"}},
        )
        self.assertEqual(admin_updated["owner_user_id"], employee["id"])
        self.assertEqual(admin_updated["description"], "administrator support edit")
        deleted = admin.call(
            "delete_production_preset", {"preset_id": saved["id"]}
        )
        self.assertTrue(deleted["deleted"])
        audit = self.catalog.list_audit_events(
            entity_type="production_preset", actor_user_id=employee["id"]
        )
        self.assertTrue(
            any(item["entity_id"] == saved["id"] for item in audit["items"])
        )


if __name__ == "__main__":
    unittest.main()
