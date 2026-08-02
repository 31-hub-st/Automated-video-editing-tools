from __future__ import annotations

import http.client
import hashlib
import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from storyforge.catalog import CatalogRepository, installation_id_sha256
from storyforge.credentials import hash_password
from storyforge.hub import HubServer
from storyforge.library_service import LibraryService
from storyforge.models import AppSettings
from storyforge.updater import UpdateRepository
from storyforge.web import (
    MAX_FILE_REFERENCES_PER_SESSION,
    MAX_UPLOAD_BYTES,
    WEB_RPC_PERMISSIONS,
)
from scripts.build_update_package import write_release_validation


class _ApiStub:
    def __init__(self, media: Path, catalog: CatalogRepository) -> None:
        self.media = media
        self.saved_settings: dict = {}
        self.saved_drafts: list[dict] = []
        self.queued_drafts: list[dict] = []
        self.created_backups = 0
        self.deleted_shared_records: list[tuple[str, str]] = []
        data_dir = media.parent / "api-data"
        data_dir.mkdir()
        allowed_root = data_dir / "authorized"
        allowed_root.mkdir()
        self.allowed_root = allowed_root
        settings = AppSettings()
        settings.hub.web_allowed_roots = [str(allowed_root)]
        self._repository = SimpleNamespace(data_dir=data_dir)
        self._state = SimpleNamespace(settings=settings, batches=[])
        self._library = LibraryService(catalog, lambda: self._state.settings, data_dir)
        self.library_payload: dict = {
            "novels": [],
            "production_records": [],
            "users": [],
        }

    def get_bootstrap(self) -> dict:
        return {
            "ok": True,
            "data": {
                "preview_uri": self.media.as_uri(),
                "local_path": str(self.media),
            },
        }

    def save_settings(self, value: dict) -> dict:
        self.saved_settings = dict(value)
        return {"ok": True, "data": self.saved_settings}

    def read_text_document(self, file_path: str) -> dict:
        return {"ok": True, "data": {"text": Path(file_path).read_text("utf-8")}}

    def get_library_bootstrap(self) -> dict:
        return {"ok": True, "data": self.library_payload}

    def get_novel(self, novel_id: str) -> dict:
        novel = next(
            (item for item in self.library_payload["novels"] if item["id"] == novel_id),
            {"id": novel_id, "episodes": [], "draft": {}},
        )
        return {"ok": True, "data": novel}

    def save_production_draft(self, value: dict) -> dict:
        payload = dict(value)
        self.saved_drafts.append(payload)
        return {"ok": True, "data": payload}

    def queue_production_draft(self, value: dict) -> dict:
        payload = dict(value)
        self.queued_drafts.append(payload)
        return {"ok": True, "data": payload}

    def check_for_updates(self) -> dict:
        return {"ok": True, "data": {"available": False}}

    def _backup_status_value(self, *, include_error: bool = False) -> dict:
        status = {
            "available": True,
            "enabled": True,
            "running": True,
            "state": "ready",
            "last_backup_id": "backup-1" if self.created_backups else "",
            "last_backup_at": "2026-07-28T00:00:00Z" if self.created_backups else "",
            "last_backup_reason": "manual" if self.created_backups else "",
            "last_daily_at": "",
            "next_check_at": "2026-07-29T00:00:00Z",
            "retention_hours": 72,
        }
        if include_error:
            status["last_error"] = ""
        else:
            status["has_error"] = False
        return status

    def get_hub_backup_status(self) -> dict:
        return {"ok": True, "data": self._backup_status_value(include_error=True)}

    def list_hub_backups(self) -> dict:
        return {
            "ok": True,
            "data": {
                "items": [],
                "total": 0,
                "status": self._backup_status_value(include_error=True),
            },
        }

    def create_hub_backup(self) -> dict:
        self.created_backups += 1
        return {
            "ok": True,
            "data": {
                "snapshot": {"id": "backup-1", "reason": "manual"},
                "status": self._backup_status_value(include_error=True),
            },
        }

    def delete_novel(self, value_id: str) -> dict:
        self.deleted_shared_records.append(("novel", value_id))
        return {"ok": True, "data": {"deleted": True, "id": value_id}}

    def delete_promo_code(self, value_id: str) -> dict:
        self.deleted_shared_records.append(("promo_code", value_id))
        return {"ok": True, "data": {"deleted": True, "id": value_id}}

    def delete_publishing_account(self, value_id: str) -> dict:
        self.deleted_shared_records.append(("publishing_account", value_id))
        return {"ok": True, "data": {"deleted": True, "id": value_id}}

    def choose_file(self, _kind: str = "novel") -> dict:
        raise AssertionError("desktop chooser must never be exposed")


class WebApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ui = self.root / "ui"
        self.ui.mkdir()
        (self.ui / "index.html").write_text("<!doctype html><title>Web UI</title>", "utf-8")
        (self.ui / "app.js").write_text("window.webUi = true;", "utf-8")
        (self.ui / "styles.css").write_text("body{color:#123}", "utf-8")
        self.media = self.root / "proof.mp4"
        self.media.write_bytes(b"0123456789abcdef")
        self.catalog = CatalogRepository(
            self.root / "catalog.sqlite3", site_id="web-test", site_name="Web Test"
        )
        self.admin_password = "Owner123!"
        self.producer_password = "Worker123!"
        self.admin = self.catalog.save_user(
            {
                "username": "owner",
                "display_name": "Owner",
                "role": "admin",
                "password_hash": hash_password(self.admin_password),
            }
        )
        self.producer = self.catalog.save_user(
            {
                "username": "worker",
                "display_name": "Worker",
                "role": "producer",
                "password_hash": hash_password(self.producer_password),
            }
        )
        self.admin_token = self.catalog.issue_hub_access_token(
            self.admin["id"], label="browser"
        )
        self.producer_token = self.catalog.issue_hub_access_token(
            self.producer["id"], label="browser"
        )
        self.data = self.root / "data"
        self.attachments = self.root / "attachments"
        self.data.mkdir()
        self.attachments.mkdir()
        self.server = HubServer(
            self.catalog,
            {"host-bootstrap-token": self.admin["id"]},
            host="127.0.0.1",
            port=0,
            data_root=self.data,
            attachment_root=self.attachments,
        ).start()
        self.addCleanup(self.server.stop)
        self.api = _ApiStub(self.media, self.catalog)
        self.server.attach_web_application(
            self.api, ui_root=self.ui, upload_root=self.root / "uploads"
        )

    def _restart_web_application(self):
        return self.server.attach_web_application(
            self.api, ui_root=self.ui, upload_root=self.root / "uploads"
        )

    @staticmethod
    def _json(response) -> dict:
        return json.loads(response.read().decode("utf-8"))

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ):
        return urlopen(
            Request(
                self.server.base_url + path,
                method=method,
                data=data,
                headers=headers or {},
            ),
            timeout=5,
        )

    def _login(
        self, username: str, password: str, *, remember: bool = False
    ) -> tuple[str, str, dict]:
        raw = json.dumps(
            {"username": username, "password": password, "remember": remember}
        ).encode()
        with self._request(
            "/web/api/session/login",
            method="POST",
            data=raw,
            headers={"Content-Type": "application/json"},
        ) as response:
            payload = self._json(response)
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        self.assertTrue(payload["ok"])
        return cookie, payload["data"]["csrf_token"], payload

    def _rpc(self, cookie: str, csrf: str, method: str, args: list | None = None):
        raw = json.dumps({"method": method, "args": args or []}).encode()
        return self._request(
            "/web/api/rpc",
            method="POST",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "X-StoryForge-CSRF": csrf,
            },
        )

    def _upload(
        self,
        cookie: str,
        csrf: str,
        *,
        kind: str,
        filename: str,
        content: bytes,
        form_kind: str | None = None,
    ):
        boundary = "----StoryForgeTestBoundary"
        body = bytearray()
        body.extend(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\n"
                f"{form_kind or kind}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            ).encode()
        )
        body.extend(content)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        return self._request(
            f"/web/api/upload?kind={kind}",
            method="POST",
            data=bytes(body),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Cookie": cookie,
                "X-StoryForge-CSRF": csrf,
            },
        )

    def _publish_update(self, version: str = "0.4.0-rc3") -> tuple[bytes, dict]:
        package = self.root / f"StoryForge-{version}.zip"
        build = self.root / f"update-build-{version}"
        build.mkdir(parents=True, exist_ok=True)
        (build / "ui").mkdir(exist_ok=True)
        (build / "StoryForge Studio.exe").write_bytes(
            b"employee update executable"
        )
        (build / "ui" / "index.html").write_bytes(b"updated workstation UI")
        (build / "BUILD_STARTUP_VALIDATION.json").write_text(
            json.dumps({"ok": True, "frozen": True, "app_version": version}),
            encoding="utf-8",
        )
        write_release_validation(
            build,
            entrypoint="StoryForge Studio.exe",
            requested_version=version,
            with_local_ai=False,
        )
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr(
                "storyforge-update.json",
                json.dumps(
                    {"version": version, "entrypoint": "StoryForge Studio.exe"}
                ),
            )
            for path in sorted(build.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(build).as_posix())
        package_bytes = package.read_bytes()
        repository = UpdateRepository(self.root / "published-updates")
        manifest = repository.publish(package, version, "Employee render recovery")
        self.server.update_repository = repository
        return package_bytes, manifest

    def test_root_serves_real_ui_and_path_traversal_is_not_static(self) -> None:
        with self._request("/") as response:
            self.assertIn("Web UI", response.read().decode())
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        with self.assertRaises(HTTPError) as missing:
            self._request("/../storyforge/web.py")
        self.assertEqual(missing.exception.code, 404)

    def test_static_ui_is_frozen_until_web_application_restarts(self) -> None:
        original = {
            "/": "<!doctype html><title>Web UI</title>",
            "/app.js": "window.webUi = true;",
            "/styles.css": "body{color:#123}",
        }
        replacement = {
            "/": "<!doctype html><title>Updated UI</title>",
            "/app.js": "window.webUi = 'updated';",
            "/styles.css": "body{color:#456}",
        }

        for request_path, expected in original.items():
            with self._request(request_path) as response:
                self.assertEqual(response.read().decode("utf-8"), expected)

        for filename, content in (
            ("index.html", replacement["/"]),
            ("app.js", replacement["/app.js"]),
            ("styles.css", replacement["/styles.css"]),
        ):
            (self.ui / filename).write_text(content, encoding="utf-8")

        for request_path, expected in original.items():
            with self._request(request_path) as response:
                self.assertEqual(response.read().decode("utf-8"), expected)

        self._restart_web_application()

        for request_path, expected in replacement.items():
            with self._request(request_path) as response:
                self.assertEqual(response.read().decode("utf-8"), expected)

    def test_employee_web_contract_can_read_library_bootstrap(self) -> None:
        cookie, csrf, _ = self._login("worker", self.producer_password)

        with self._rpc(cookie, csrf, "get_library_bootstrap") as response:
            payload = self._json(response)

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(
            WEB_RPC_PERMISSIONS["get_library_bootstrap"],
            ("library.view",),
        )
        self.assertEqual(payload["data"]["novels"], [])

    def test_shared_library_deletes_require_an_administrator(self) -> None:
        methods = (
            ("delete_novel", "novel-1"),
            ("delete_promo_code", "code-1"),
            ("delete_publishing_account", "account-1"),
        )
        employee_cookie, employee_csrf, _ = self._login(
            "worker", self.producer_password
        )
        for method, value_id in methods:
            with self.subTest(role="producer", method=method):
                with self.assertRaises(HTTPError) as denied:
                    self._rpc(employee_cookie, employee_csrf, method, [value_id])
                self.assertEqual(denied.exception.code, 403)
                denied.exception.close()

        admin_cookie, admin_csrf, _ = self._login("owner", self.admin_password)
        for method, value_id in methods:
            with self.subTest(role="admin", method=method):
                with self._rpc(
                    admin_cookie, admin_csrf, method, [value_id]
                ) as response:
                    payload = self._json(response)
                self.assertTrue(payload["data"]["deleted"])
        self.assertEqual(
            self.api.deleted_shared_records,
            [
                ("novel", "novel-1"),
                ("promo_code", "code-1"),
                ("publishing_account", "account-1"),
            ],
        )

    def test_login_accepts_only_account_password_not_hub_token(self) -> None:
        for username in ("worker", "owner"):
            wrong = json.dumps(
                {"username": username, "password": self.admin_token["token"]}
            ).encode()
            with self.subTest(username=username):
                with self.assertRaises(HTTPError) as rejected:
                    self._request(
                        "/web/api/session/login",
                        method="POST",
                        data=wrong,
                        headers={"Content-Type": "application/json"},
                    )
                self.assertEqual(rejected.exception.code, 401)
        cookie, _csrf, payload = self._login("owner", self.admin_password)
        self.assertIn("storyforge_session=", cookie)
        self.assertTrue(payload["data"]["password_configured"])
        self.assertFalse(payload["data"]["must_set_password"])

    def test_employee_password_session_can_download_only_published_update(self) -> None:
        package_bytes, manifest = self._publish_update()

        with self.assertRaises(HTTPError) as anonymous_status:
            self._request("/web/api/update")
        self.assertEqual(anonymous_status.exception.code, 401)
        with self.assertRaises(HTTPError) as anonymous_package:
            self._request("/web/api/update/package")
        self.assertEqual(anonymous_package.exception.code, 401)

        cookie, _csrf, login = self._login("worker", self.producer_password)
        self.assertNotIn("hub.manage", login["data"]["permissions"])
        with self._request(
            "/web/api/update", headers={"Cookie": cookie}
        ) as response:
            status = self._json(response)
        self.assertTrue(status["ok"])
        self.assertTrue(status["data"]["available"])
        self.assertEqual(status["data"]["version"], manifest["version"])
        self.assertEqual(status["data"]["size_bytes"], len(package_bytes))
        self.assertEqual(
            status["data"]["sha256"], hashlib.sha256(package_bytes).hexdigest()
        )
        self.assertNotIn(str(self.root), json.dumps(status))
        self.assertEqual(
            status["data"]["download_url"],
            "/web/api/update/package?version=0.4.0-rc3",
        )

        with self._request(
            status["data"]["download_url"], headers={"Cookie": cookie}
        ) as response:
            downloaded = response.read()
            headers = response.headers
        self.assertEqual(downloaded, package_bytes)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertEqual(headers["X-Content-SHA256"], manifest["sha256"])
        self.assertIn(manifest["filename"], headers["Content-Disposition"])

        with self._request(
            "/web/api/update/package?version=0.4.0-rc3",
            method="HEAD",
            headers={"Cookie": cookie},
        ) as response:
            self.assertEqual(response.read(), b"")
            self.assertEqual(int(response.headers["Content-Length"]), len(package_bytes))

        with self._request(
            "/web/api/update/package?version=0.4.0-rc3",
            headers={"Cookie": cookie, "Range": "bytes=2-8"},
        ) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), package_bytes[2:9])
            self.assertEqual(
                response.headers["Content-Range"], f"bytes 2-8/{len(package_bytes)}"
            )

        with self.assertRaises(HTTPError) as stale:
            self._request(
                "/web/api/update/package?version=0.4.0-rc2",
                headers={"Cookie": cookie},
            )
        self.assertEqual(stale.exception.code, 404)

    def test_browser_update_status_is_safe_when_nothing_is_published(self) -> None:
        cookie, _csrf, _login = self._login("worker", self.producer_password)
        with self._request(
            "/web/api/update", headers={"Cookie": cookie}
        ) as response:
            payload = self._json(response)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["data"]["available"])
        with self.assertRaises(HTTPError) as missing:
            self._request("/web/api/update/package", headers={"Cookie": cookie})
        self.assertEqual(missing.exception.code, 404)

    def test_remembered_password_session_survives_hub_web_restart(self) -> None:
        cookie, csrf, _payload = self._login(
            "worker", self.producer_password, remember=True
        )
        raw_cookie = cookie.split("=", 1)[1]
        session_path = self.api._repository.data_dir / "web-sessions.json"
        persisted = session_path.read_text(encoding="utf-8")
        self.assertNotIn(raw_cookie, persisted)
        self.assertNotIn(self.producer_password, persisted)
        self.assertNotIn(self.admin_password, persisted)

        application = self._restart_web_application()
        self.assertEqual({}, application._sessions)
        with self._request("/web/api/session", headers={"Cookie": cookie}) as response:
            restored = self._json(response)
        self.assertTrue(restored["data"]["authenticated"])
        self.assertEqual("worker", restored["data"]["user"]["username"])
        self.assertEqual(csrf, restored["data"]["csrf_token"])
        with self._rpc(cookie, csrf, "get_bootstrap") as response:
            self.assertTrue(self._json(response)["ok"])

    def test_logout_revokes_remembered_session_across_restart(self) -> None:
        cookie, csrf, _payload = self._login(
            "worker", self.producer_password, remember=True
        )
        with self._request(
            "/web/api/session",
            method="DELETE",
            headers={"Cookie": cookie, "X-StoryForge-CSRF": csrf},
        ) as response:
            self.assertFalse(self._json(response)["data"]["authenticated"])
        self._restart_web_application()
        with self.assertRaises(HTTPError) as revoked:
            self._request("/web/api/session", headers={"Cookie": cookie})
        self.assertEqual(401, revoked.exception.code)

    def test_disabled_account_cannot_restore_remembered_session(self) -> None:
        cookie, _csrf, _payload = self._login(
            "worker", self.producer_password, remember=True
        )
        current = self.catalog._web_user_by_id(self.producer["id"])
        self.catalog.save_user(
            {
                "id": self.producer["id"],
                "username": current["username"],
                "display_name": current["display_name"],
                "role": current["role"],
                "active": False,
                "row_version": current["row_version"],
            },
            actor_user_id=self.admin["id"],
        )
        application = self._restart_web_application()
        self.assertEqual({}, application._persistent_sessions)
        with self.assertRaises(HTTPError) as revoked:
            self._request("/web/api/session", headers={"Cookie": cookie})
        self.assertEqual(401, revoked.exception.code)

    def test_expired_remembered_session_is_not_restored(self) -> None:
        cookie, _csrf, _payload = self._login(
            "worker", self.producer_password, remember=True
        )
        application = self.server.web_application
        raw_cookie = cookie.split("=", 1)[1]
        cookie_hash = application._cookie_hash(raw_cookie)
        with application._lock:
            application._persistent_sessions[cookie_hash].expires_at = time.time() - 1
            application._save_persistent_sessions_locked()
        restored = self._restart_web_application()
        self.assertEqual({}, restored._persistent_sessions)
        with self.assertRaises(HTTPError) as expired:
            self._request("/web/api/session", headers={"Cookie": cookie})
        self.assertEqual(401, expired.exception.code)

    def test_rpc_requires_csrf_and_rejects_non_allowlisted_and_employee_admin_calls(self) -> None:
        cookie, csrf, _payload = self._login("worker", self.producer_password)
        raw = json.dumps({"method": "get_bootstrap", "args": []}).encode()
        with self.assertRaises(HTTPError) as no_csrf:
            self._request(
                "/web/api/rpc",
                method="POST",
                data=raw,
                headers={"Content-Type": "application/json", "Cookie": cookie},
            )
        self.assertEqual(no_csrf.exception.code, 403)
        with self.assertRaises(HTTPError) as non_ascii_csrf:
            self._request(
                "/web/api/rpc",
                method="POST",
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "Cookie": cookie,
                    "X-StoryForge-CSRF": "é",
                },
            )
        self.assertEqual(non_ascii_csrf.exception.code, 403)
        with self.assertRaises(HTTPError) as private:
            self._rpc(cookie, csrf, "choose_file", ["novel"])
        self.assertEqual(private.exception.code, 403)
        with self.assertRaises(HTTPError) as denied:
            self._rpc(cookie, csrf, "save_settings", [{"hub": {}}])
        self.assertEqual(denied.exception.code, 403)

    def test_browser_rejects_media_execution_even_for_an_administrator(self) -> None:
        cookie, csrf, _payload = self._login("owner", self.admin_password)
        calls = {
            "generate_voice_candidates": ["novel-1", "suspense"],
            "queue_production_draft": [{"draft_id": "draft-1"}],
            "start_queue": [],
            "cancel_queue": [],
            "approve_preview": ["job-1"],
            "regenerate_preview": ["job-1"],
            "retry_failed": ["job-1"],
            "get_local_runtime_snapshot": [],
            "get_local_self_check": [],
        }
        for method, args in calls.items():
            with self.subTest(method=method):
                with self.assertRaises(HTTPError) as rejected:
                    self._rpc(cookie, csrf, method, args)
                self.assertEqual(rejected.exception.code, 403)
                payload = json.loads(rejected.exception.read().decode("utf-8"))
                self.assertEqual(payload["error"], "请在制作电脑客户端执行")

    def test_logged_in_hub_page_can_request_only_its_own_worker_ticket(self) -> None:
        device = self.catalog.register_hub_device(
            {
                "installation_id_hash": installation_id_sha256("web-worker-device"),
                "name": "Employee Browser PC",
                "app_version": "0.3.3",
                "capabilities": {"local_render": True, "local_tts": True},
            },
            actor_user_id=self.producer["id"],
        )["device"]
        cookie, csrf, _payload = self._login("worker", self.producer_password)
        body = json.dumps(
            {
                "device_id": device["id"],
                "worker_nonce": "browser-worker-nonce-0123456789",
            }
        ).encode("utf-8")
        with self._request(
            "/web/api/local-worker-ticket",
            method="POST",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "X-StoryForge-CSRF": csrf,
                "Origin": self.server.base_url,
            },
        ) as response:
            issued = self._json(response)
        self.assertTrue(issued["ok"])
        self.assertEqual(issued["data"]["device_id"], device["id"])
        self.assertGreater(len(issued["data"]["ticket"]), 40)

    def test_media_reference_is_opaque_session_bound_and_supports_range(self) -> None:
        cookie, csrf, _payload = self._login("owner", self.admin_password)
        with self._rpc(cookie, csrf, "get_bootstrap") as response:
            payload = self._json(response)
        self.assertEqual(payload["data"]["local_path"], "proof.mp4")
        media_url = payload["data"]["preview_uri"]
        self.assertTrue(media_url.startswith("/web/api/media?ref="))
        self.assertNotIn(str(self.root), json.dumps(payload))
        with self._request(
            media_url, headers={"Cookie": cookie, "Range": "bytes=4-8"}
        ) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], "bytes 4-8/16")
            self.assertEqual(response.read(), b"45678")

        second_cookie, _second_csrf, _ = self._login("owner", self.admin_password)
        with self.assertRaises(HTTPError) as cross_session:
            self._request(media_url, headers={"Cookie": second_cookie})
        self.assertEqual(cross_session.exception.code, 404)

    def test_media_references_are_reused_capped_and_zero_byte_range_is_valid(self) -> None:
        cookie, csrf, _payload = self._login("owner", self.admin_password)
        with self._rpc(cookie, csrf, "get_bootstrap") as response:
            first_url = self._json(response)["data"]["preview_uri"]
        with self._rpc(cookie, csrf, "get_bootstrap") as response:
            second_url = self._json(response)["data"]["preview_uri"]
        self.assertEqual(first_url, second_url)

        application = self.server.web_application
        session_id = cookie.split("=", 1)[1]
        session = application._sessions[session_id]
        for index in range(MAX_FILE_REFERENCES_PER_SESSION - 1):
            path = self.root / f"ref-{index}.bin"
            path.write_bytes(str(index).encode())
            application._register_media(session, path)
        overflow = self.root / "ref-overflow.bin"
        overflow.write_bytes(b"overflow")
        with self.assertRaisesRegex(ValueError, "引用过多"):
            application._register_media(session, overflow)

        empty = self.root / "empty.mp4"
        empty.write_bytes(b"")
        # Reuse a fresh browser session so the reference cap above does not
        # obscure the zero-byte transport behavior.
        empty_cookie, _empty_csrf, _ = self._login("owner", self.admin_password)
        empty_session = application._sessions[empty_cookie.split("=", 1)[1]]
        empty_ref = application._register_media(empty_session, empty)
        empty_url = f"/web/api/media?ref={empty_ref}"
        with self._request(empty_url, headers={"Cookie": empty_cookie}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Length"], "0")
            self.assertEqual(response.read(), b"")
        with self.assertRaises(HTTPError) as invalid_range:
            self._request(
                empty_url,
                headers={"Cookie": empty_cookie, "Range": "bytes=0-0"},
            )
        self.assertEqual(invalid_range.exception.code, 416)
        self.assertEqual(invalid_range.exception.headers["Content-Range"], "bytes */0")

    def test_multipart_upload_returns_handle_and_only_controlled_rpc_resolves_it(self) -> None:
        cookie, csrf, _payload = self._login("owner", self.admin_password)
        boundary = "----StoryForgeBoundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\nnovel\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"story.txt\"\r\n"
            "Content-Type: text/plain\r\n\r\nA browser-uploaded story.\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        with self._request(
            "/web/api/upload?kind=novel",
            method="POST",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Cookie": cookie,
                "X-StoryForge-CSRF": csrf,
            },
        ) as response:
            uploaded = self._json(response)["data"]
        self.assertTrue(uploaded["file_path"].startswith("upload:"))
        self.assertNotIn(str(self.root), json.dumps(uploaded))
        with self._rpc(
            cookie, csrf, "read_text_document", [uploaded["file_path"]]
        ) as response:
            result = self._json(response)
        self.assertEqual(result["data"]["text"], "A browser-uploaded story.")

    def test_upload_limit_is_rejected_before_body_read(self) -> None:
        cookie, csrf, _payload = self._login("owner", self.admin_password)
        parsed = urlsplit(self.server.base_url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        connection.putrequest("POST", "/web/api/upload?kind=update_package")
        connection.putheader("Content-Type", "multipart/form-data; boundary=x")
        connection.putheader("Content-Length", str(MAX_UPLOAD_BYTES + 1))
        connection.putheader("Cookie", cookie)
        connection.putheader("X-StoryForge-CSRF", csrf)
        connection.endheaders()
        response = connection.getresponse()
        try:
            self.assertEqual(response.status, 413)
        finally:
            response.read()
            connection.close()

    def test_upload_kind_permissions_mismatch_and_docx_bomb_are_rejected(self) -> None:
        producer_cookie, producer_csrf, _ = self._login(
            "worker", self.producer_password
        )
        parsed = urlsplit(self.server.base_url)
        for upload_kind in ("update_package", "component_package"):
            with self.subTest(upload_kind=upload_kind):
                connection = http.client.HTTPConnection(
                    parsed.hostname, parsed.port, timeout=5
                )
                connection.putrequest(
                    "POST", f"/web/api/upload?kind={upload_kind}"
                )
                connection.putheader(
                    "Content-Type", "multipart/form-data; boundary=x"
                )
                connection.putheader("Content-Length", str(2 * 1024 * 1024 * 1024))
                connection.putheader("Cookie", producer_cookie)
                connection.putheader("X-StoryForge-CSRF", producer_csrf)
                connection.endheaders()
                response = connection.getresponse()
                try:
                    self.assertEqual(response.status, 403)
                finally:
                    response.read()
                    connection.close()

        cookie, csrf, _ = self._login("owner", self.admin_password)
        with self.assertRaises(HTTPError) as mismatch:
            self._upload(
                cookie,
                csrf,
                kind="novel",
                form_kind="cover",
                filename="story.txt",
                content=b"safe text",
            )
        self.assertEqual(mismatch.exception.code, 400)

        with self._upload(
            cookie,
            csrf,
            kind="novel",
            filename="story.txt",
            content=b"safe text",
        ) as response:
            upload_ref = self._json(response)["data"]["file_path"]
        with self.assertRaises(HTTPError) as wrong_rpc_kind:
            self._rpc(
                cookie,
                csrf,
                "save_novel",
                [{"id": "novel", "cover_path": upload_ref}],
            )
        self.assertEqual(wrong_rpc_kind.exception.code, 400)

        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "A" * (2 * 1024 * 1024))
        with self.assertRaises(HTTPError) as compressed_bomb:
            self._upload(
                cookie,
                csrf,
                kind="docx",
                filename="bomb.docx",
                content=archive_bytes.getvalue(),
            )
        self.assertEqual(compressed_bomb.exception.code, 400)

    def test_employee_drafts_are_isolated_for_library_create_and_queue(self) -> None:
        colleague_password = "Colleague123!"
        colleague = self.catalog.save_user(
            {
                "username": "worker-b",
                "display_name": "Worker B",
                "role": "producer",
                "password_hash": hash_password(colleague_password),
            }
        )
        novel = self.catalog.import_novel(
            {
                "title": "Shared Story",
                "body": "Chapter one. A shared story for two creators.",
                "episodes": [{"ordinal": 1, "title": "Opening"}],
            }
        )["novel"]
        binding = self.catalog.save_novel_binding(
            {"novel_id": novel["id"], "platform_name": "GoodNovel"}
        )
        code = self.catalog.add_promo_code(
            {"binding_id": binding["id"], "code": "AB123"}
        )
        folders = {}
        for key in ("video_folder", "music_folder", "output_folder"):
            path = self.api.allowed_root / key
            path.mkdir()
            folders[key] = str(path)
        base = {
            "novel_id": novel["id"],
            "binding_id": binding["id"],
            "promo_code_id": code["id"],
            "creative_line_count": 1,
            "episode_ids": [novel["current_revision"]["episodes"][0]["id"]],
            "metadata": folders,
        }
        own_draft = self.catalog.save_draft(
            base, actor_user_id=self.producer["id"]
        )
        colleague_draft = self.catalog.save_draft(
            base, actor_user_id=colleague["id"]
        )
        own_record = self.catalog.save_production_record(
            {"draft_id": own_draft["id"], "job_id": "own-browser-record"},
            actor_user_id=self.producer["id"],
        )
        colleague_record = self.catalog.save_production_record(
            {
                "draft_id": colleague_draft["id"],
                "job_id": "colleague-browser-record",
            },
            actor_user_id=colleague["id"],
        )
        self.api.library_payload = {
            "novels": [self.api._library.novel_for_ui(novel["id"])],
            "production_records": [own_record, colleague_record],
            "users": [],
        }

        own_cookie, own_csrf, _ = self._login("worker", self.producer_password)
        other_cookie, other_csrf, _ = self._login("worker-b", colleague_password)
        with self._rpc(own_cookie, own_csrf, "get_library_bootstrap") as response:
            own_payload = self._json(response)["data"]
            own_novel = own_payload["novels"][0]
        with self._rpc(other_cookie, other_csrf, "get_library_bootstrap") as response:
            other_payload = self._json(response)["data"]
            other_novel = other_payload["novels"][0]
        self.assertEqual(own_novel["draft"]["id"], own_draft["id"])
        self.assertEqual(other_novel["draft"]["id"], colleague_draft["id"])
        self.assertEqual(
            [item["id"] for item in own_payload["production_records"]],
            [own_record["id"]],
        )
        self.assertEqual(
            [item["id"] for item in other_payload["production_records"]],
            [colleague_record["id"]],
        )
        self.catalog.set_user_permission(
            self.producer["id"],
            "records.view_all",
            True,
            actor_user_id=self.admin["id"],
        )
        with self._rpc(own_cookie, own_csrf, "get_library_bootstrap") as response:
            record_viewer_payload = self._json(response)["data"]
            record_viewer_novel = record_viewer_payload["novels"][0]
        self.assertEqual(record_viewer_novel["draft"]["id"], own_draft["id"])
        self.assertCountEqual(
            [item["id"] for item in record_viewer_payload["production_records"]],
            [own_record["id"], colleague_record["id"]],
        )

        new_payload = {
            "novel_id": novel["id"],
            "created_by_user_id": colleague["id"],
            **folders,
        }
        with self._rpc(
            own_cookie, own_csrf, "save_production_draft", [new_payload]
        ) as response:
            saved = self._json(response)["data"]
        self.assertEqual(saved["created_by_user_id"], self.producer["id"])
        with self.assertRaises(HTTPError) as edit_other:
            self._rpc(
                own_cookie,
                own_csrf,
                "save_production_draft",
                [{"id": colleague_draft["id"], **folders}],
            )
        self.assertEqual(edit_other.exception.code, 403)
        with self.assertRaises(HTTPError) as queue_other:
            self._rpc(
                own_cookie,
                own_csrf,
                "queue_production_draft",
                [{"draft_id": colleague_draft["id"]}],
            )
        self.assertEqual(queue_other.exception.code, 403)
        with self.assertRaises(HTTPError) as desktop_only:
            self._rpc(
                own_cookie,
                own_csrf,
                "queue_production_draft",
                [{"draft_id": own_draft["id"]}],
            )
        self.assertEqual(desktop_only.exception.code, 403)
        self.assertIn(
            "请在制作电脑客户端执行",
            desktop_only.exception.read().decode("utf-8"),
        )

    def test_hub_browser_never_persists_hub_host_paths_as_worker_paths(self) -> None:
        cookie, csrf, _ = self._login("owner", self.admin_password)
        allowed = self.api.allowed_root / "nested"
        allowed.mkdir()
        payload = {
            "video_folder": str(allowed),
            "music_folder": str(allowed),
            "output_folder": str(allowed),
        }
        with self._rpc(cookie, csrf, "save_production_draft", [payload]) as response:
            # Host filesystem values are replaced with logical workstation
            # markers and then hidden from the browser projection.
            self.assertEqual(self._json(response)["data"]["video_folder"], "")
        self.assertEqual(
            self.api.saved_drafts[-1]["video_folder"],
            "worker://local/videos",
        )

        outside = self.root / "outside"
        outside.mkdir()
        for ignored in (str(outside), r"\\server\share", r"\\?\C:\device"):
            with self._rpc(
                cookie,
                csrf,
                "save_production_draft",
                [{**payload, "video_folder": ignored}],
            ) as response:
                self.assertEqual(self._json(response)["data"]["video_folder"], "")
            self.assertEqual(
                self.api.saved_drafts[-1]["video_folder"],
                "worker://local/videos",
            )
        # Hub settings remain untouched; they are simply no longer treated as
        # a rendering workstation's local filesystem.
        self.assertEqual(self.api._state.settings.hub.web_allowed_roots, [str(self.api.allowed_root)])

    def test_update_methods_require_hub_manage(self) -> None:
        cookie, csrf, _ = self._login("worker", self.producer_password)
        for method, arguments in (
            ("check_for_updates", []),
            ("download_update", []),
            ("schedule_update_on_restart", []),
            ("cancel_scheduled_update", []),
            ("save_local_update_preferences", [{"auto_update_enabled": False}]),
        ):
            with self.subTest(method=method), self.assertRaises(HTTPError) as denied:
                self._rpc(cookie, csrf, method, arguments)
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()

    def test_backup_health_is_safe_and_backup_rpc_requires_hub_manage(self) -> None:
        with self._request("/web/api/health") as response:
            health = self._json(response)["data"]["backup"]
        self.assertTrue(health["available"])
        self.assertEqual(health["state"], "ready")
        self.assertNotIn("last_error", health)

        employee_cookie, employee_csrf, _ = self._login(
            "worker", self.producer_password
        )
        for method in (
            "get_hub_backup_status",
            "list_hub_backups",
            "create_hub_backup",
        ):
            with self.subTest(method=method):
                with self.assertRaises(HTTPError) as denied:
                    self._rpc(employee_cookie, employee_csrf, method)
                self.assertEqual(denied.exception.code, 403)

        admin_cookie, admin_csrf, _ = self._login("owner", self.admin_password)
        with self._rpc(admin_cookie, admin_csrf, "create_hub_backup") as response:
            created = self._json(response)
        self.assertTrue(created["ok"])
        self.assertEqual(created["data"]["snapshot"]["id"], "backup-1")

    def test_employee_cannot_call_fleet_admin_methods(self) -> None:
        cookie, csrf, _ = self._login("worker", self.producer_password)
        for method, arguments in (
            ("list_managed_devices", []),
            ("rename_managed_device", ["other-device", "Forged name"]),
            (
                "create_managed_device_config",
                [
                    {
                        "target_mode": "all",
                        "device_ids": [],
                        "config": {"output_fps": 30},
                    }
                ],
            ),
        ):
            with self.subTest(method=method):
                with self.assertRaises(HTTPError) as denied:
                    self._rpc(cookie, csrf, method, arguments)
                self.assertEqual(denied.exception.code, 403)

    def test_employee_bootstrap_hides_host_commands_endpoints_and_legacy_batches(self) -> None:
        original = self.api.get_bootstrap
        self.api.get_bootstrap = lambda: {
            "ok": True,
            "data": {
                "settings": {
                    "providers": {
                        "text_provider": "local",
                        "text_endpoint": "http://10.0.0.1:9999/private",
                        "text_api_key": "********",
                        "tts_provider": "local_kokoro",
                        "tts_endpoint": "http://10.0.0.2:8888/private",
                        "tts_api_key": "********",
                        "kokoro_endpoint": "http://127.0.0.1:5000",
                        "kokoro_command": r"C:\private\kokoro.exe --secret",
                        "has_text_api_key": True,
                    },
                    "hub": {
                        "mode": "host",
                        "endpoint": "http://10.0.0.3:8765",
                        "access_token": "********",
                        "listen_host": "0.0.0.0",
                        "listen_port": 8765,
                    },
                },
                "system": {
                    "ffmpeg_ready": True,
                    "ffmpeg_path": r"C:\private\ffmpeg\bin\ffmpeg.exe",
                },
                "batches": [{"video_folder": r"C:\Users\owner"}],
                "jobs": [{"id": "hub-host-private-job", "status": "running"}],
                "archived_jobs": [
                    {"id": "hub-host-private-archive", "status": "failed"}
                ],
            },
        }
        self.addCleanup(setattr, self.api, "get_bootstrap", original)
        cookie, csrf, _ = self._login("worker", self.producer_password)
        with self._rpc(cookie, csrf, "get_bootstrap") as response:
            data = self._json(response)["data"]
        providers = data["settings"]["providers"]
        self.assertEqual(data["batches"], [])
        self.assertEqual(data["jobs"], [])
        self.assertEqual(data["archived_jobs"], [])
        self.assertEqual(data["system"]["ffmpeg_path"], "ffmpeg.exe")
        self.assertEqual(providers["text_endpoint"], "")
        self.assertEqual(providers["tts_endpoint"], "")
        self.assertEqual(providers["kokoro_command"], "")
        self.assertEqual(providers["kokoro_endpoint"], "configured")
        self.assertNotIn("text_api_key", providers)
        self.assertNotIn("access_token", data["settings"]["hub"])

    def test_internal_device_token_revocation_does_not_revoke_password_session(self) -> None:
        cookie, csrf, _payload = self._login("worker", self.producer_password)
        self.catalog.revoke_hub_access_token(self.producer_token["id"])
        with self._rpc(cookie, csrf, "get_bootstrap") as response:
            self.assertTrue(self._json(response)["ok"])

    def test_user_token_management_rpc_is_not_exposed_to_browser(self) -> None:
        cookie, csrf, _payload = self._login("owner", self.admin_password)
        for method, args in (
            ("list_hub_user_tokens", [self.admin["id"]]),
            ("issue_hub_user_token", [self.admin["id"], "Browser"]),
            ("revoke_hub_user_token", [self.admin_token["id"]]),
        ):
            with self.subTest(method=method):
                with self.assertRaises(HTTPError) as rejected:
                    self._rpc(cookie, csrf, method, args)
                self.assertEqual(rejected.exception.code, 403)

    def test_password_change_requires_current_password_and_does_not_store_plaintext(self) -> None:
        cookie, csrf, _payload = self._login("owner", self.admin_password)
        old_cookie, old_csrf, _ = self._login("owner", self.admin_password)
        token_attempt = json.dumps(
            {
                "current_password": self.admin_token["token"],
                "new_password": "NoToken1!",
            }
        ).encode()
        with self.assertRaises(HTTPError) as token_rejected:
            self._request(
                "/web/api/session/password",
                method="POST",
                data=token_attempt,
                headers={
                    "Content-Type": "application/json",
                    "Cookie": cookie,
                    "X-StoryForge-CSRF": csrf,
                },
            )
        self.assertEqual(token_rejected.exception.code, 400)
        raw = json.dumps(
            {
                "current_password": self.admin_password,
                "new_password": "Ab1!cD2?",
            }
        ).encode()
        with self._request(
            "/web/api/session/password",
            method="POST",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "X-StoryForge-CSRF": csrf,
            },
        ) as response:
            changed = self._json(response)
        self.assertTrue(changed["data"]["password_configured"])
        self.assertNotIn(
            "Ab1!cD2?",
            str(self.catalog._web_user_by_id(self.admin["id"])["password_hash"]),
        )
        self._restart_web_application()
        with self._rpc(cookie, csrf, "get_bootstrap") as response:
            self.assertTrue(self._json(response)["ok"])
        with self.assertRaises(HTTPError) as old_session_after_restart:
            self._rpc(old_cookie, old_csrf, "get_bootstrap")
        self.assertEqual(old_session_after_restart.exception.code, 401)
        _new_cookie, _new_csrf, password_login = self._login(
            "owner", "Ab1!cD2?"
        )
        self.assertFalse(password_login["data"]["must_set_password"])
        with self.assertRaises(HTTPError) as old_session:
            self._rpc(old_cookie, old_csrf, "get_bootstrap")
        self.assertEqual(old_session.exception.code, 401)

    def test_login_attempt_and_session_caps_are_enforced(self) -> None:
        with patch("storyforge.web.MAX_LOGIN_ATTEMPTS_PER_IP_MINUTE", 2):
            for index in range(2):
                raw = json.dumps(
                    {"username": f"missing-{index}", "password": "wrong-password"}
                ).encode()
                with self.assertRaises(HTTPError) as failed:
                    self._request(
                        "/web/api/session/login",
                        method="POST",
                        data=raw,
                        headers={"Content-Type": "application/json"},
                    )
                self.assertEqual(failed.exception.code, 401)
            raw = json.dumps(
                {"username": "owner", "password": self.admin_password}
            ).encode()
            with self.assertRaises(HTTPError) as limited:
                self._request(
                    "/web/api/session/login",
                    method="POST",
                    data=raw,
                    headers={"Content-Type": "application/json"},
                )
            self.assertEqual(limited.exception.code, 429)

        application = self.server.web_application
        application._login_attempts_by_ip.clear()
        with patch("storyforge.web.MAX_SESSIONS_PER_IP", 1):
            self._login("owner", self.admin_password)
            with self.assertRaises(HTTPError) as session_limited:
                self._login("owner", self.admin_password)
            self.assertEqual(session_limited.exception.code, 429)

    def test_permission_changes_apply_to_an_existing_session(self) -> None:
        cookie, csrf, _payload = self._login("worker", self.producer_password)
        self.catalog.set_user_permission(
            self.producer["id"], "library.view", False, actor_user_id=self.admin["id"]
        )
        with self.assertRaises(HTTPError) as denied:
            self._rpc(cookie, csrf, "get_novel", ["not-used"])
        self.assertEqual(denied.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
