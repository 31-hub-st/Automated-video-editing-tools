from __future__ import annotations

import importlib.util
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storyforge import __version__
from storyforge.api import StoryForgeApi
from storyforge.catalog import CatalogRepository, installation_id_sha256
from storyforge.component_updater import (
    ComponentPackageBuilder,
    ComponentRepository,
    ComponentRepositoryError,
    ComponentUpdater,
    component_file_sha256,
    sign_component_catalog,
    validate_component_catalog,
    verify_component_catalog_signature,
)
from storyforge.hub import HubClient, HubServer
from storyforge.config import SettingsRepository
from storyforge.models import AppSettings


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _build_component(
    root: Path,
    *,
    component_id: str = "kokoro.language.ja",
    version: str = "1.0.0",
    value: str = "first",
) -> Path:
    source = root / f"source-{version}-{value}"
    package = root / f"{component_id}-{version}-{value}.zip"
    module = source / "storyforge_component_probe"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text(
        f"VALUE = {value!r}\n", encoding="utf-8"
    )
    (module / "resource.txt").write_text(value, encoding="utf-8")
    # Merely installing/activating a component must never run a package script.
    (source / "installer.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('installer-ran').write_text('unsafe')\n",
        encoding="utf-8",
    )
    ComponentPackageBuilder.build(
        source,
        package,
        component_id=component_id,
        version=version,
        min_app_version=__version__,
    )
    return package


class ComponentRepositoryTests(unittest.TestCase):
    def test_repository_publishes_validated_multi_component_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = ComponentRepository(root / "published")
            first_package = _build_component(root, value="first")
            first = repository.publish(first_package, "Japanese language pack")
            second = repository.publish(
                _build_component(
                    root,
                    component_id="kokoro.language.zh",
                    value="second",
                )
            )

            catalog = repository.get_catalog()
            self.assertEqual(
                [item["component_id"] for item in catalog["components"]],
                ["kokoro.language.ja", "kokoro.language.zh"],
            )
            self.assertEqual(
                component_file_sha256(repository.resolve_package(first)),
                first["sha256"],
            )
            self.assertEqual(repository.get_manifest("kokoro.language.zh"), second)
            signature = sign_component_catalog("existing-device-token", catalog)
            verify_component_catalog_signature(
                "existing-device-token", catalog, signature
            )
            with self.assertRaises(ValueError):
                verify_component_catalog_signature("different-token", catalog, signature)

    def test_same_identity_and_version_cannot_be_republished_with_new_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = ComponentRepository(root / "published")
            repository.publish(_build_component(root, value="first"))
            with self.assertRaisesRegex(
                ComponentRepositoryError, "same component version"
            ):
                repository.publish(_build_component(root, value="different"))
            self.assertEqual(len(repository.get_catalog()["components"]), 1)


class ComponentRuntimeTests(unittest.TestCase):
    def test_install_activates_module_and_resource_without_running_installer(self) -> None:
        original_path = list(sys.path)
        original_environment = os.environ.get("STORYFORGE_COMPONENT_PATHS")
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                package = _build_component(root)
                updater = ComponentUpdater(root / "StoryForgeData" / "components")
                installed = updater.install(package)
                activated = updater.activate_runtime()

                self.assertEqual(activated[0].version, "1.0.0")
                spec = importlib.util.find_spec("storyforge_component_probe")
                self.assertIsNotNone(spec)
                self.assertIn(str(installed.root), str(spec.origin))
                self.assertEqual(
                    updater.resolve(
                        "kokoro.language.ja",
                        "storyforge_component_probe/resource.txt",
                    ).read_text(encoding="utf-8"),
                    "first",
                )
                self.assertFalse((installed.root / "installer-ran").exists())
                self.assertFalse((installed.root.parent / "installer-ran").exists())
        finally:
            sys.path[:] = original_path
            if original_environment is None:
                os.environ.pop("STORYFORGE_COMPONENT_PATHS", None)
            else:
                os.environ["STORYFORGE_COMPONENT_PATHS"] = original_environment

    def test_failed_install_keeps_previous_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            updater = ComponentUpdater(root / "components")
            first = updater.install(_build_component(root, version="1.0.0"))
            damaged = _build_component(root, version="2.0.0", value="second")
            damaged.write_bytes(damaged.read_bytes()[:-7])

            with self.assertRaises(ValueError):
                updater.install(damaged)

            current = updater.current("kokoro.language.ja")
            self.assertIsNotNone(current)
            self.assertEqual(current.version, first.version)
            self.assertEqual(current.package_sha256, first.package_sha256)

    def test_api_rollback_restores_state_when_activation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = SettingsRepository(root / "client")
            api = StoryForgeApi(repository=repository)
            try:
                updater = api._component_updater
                updater.install(_build_component(root, version="1.0.0", value="first"))
                updater.install(_build_component(root, version="2.0.0", value="second"))
                recovered_runtime = updater.list_installed()
                with patch.object(
                    updater,
                    "activate_runtime",
                    side_effect=[RuntimeError("activation failed"), recovered_runtime],
                ):
                    result = api.rollback_component_update("kokoro.language.ja")

                self.assertFalse(result["ok"])
                current = updater.current("kokoro.language.ja")
                self.assertIsNotNone(current)
                assert current is not None
                self.assertEqual(current.version, "2.0.0")
                self.assertEqual(
                    current.resolve(
                        "storyforge_component_probe/resource.txt"
                    ).read_text(encoding="utf-8"),
                    "second",
                )
            finally:
                api._shutdown()

    def test_api_install_restores_exact_state_when_activation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = SettingsRepository(root / "client")
            api = StoryForgeApi(repository=repository)
            try:
                updater = api._component_updater
                updater.install(
                    _build_component(root, version="1.0.0", value="first")
                )
                state_path = (
                    repository.data_dir
                    / "components"
                    / "kokoro.language.ja"
                    / "state.json"
                )
                state_before = state_path.read_bytes()
                recovered_runtime = updater.list_installed()
                api._runtime_hub_mode = "host"
                api._component_repository.publish(
                    _build_component(root, version="2.0.0", value="second")
                )

                with patch.object(
                    updater,
                    "activate_runtime",
                    side_effect=[
                        RuntimeError("candidate activation failed"),
                        recovered_runtime,
                    ],
                ) as activate_runtime:
                    result = api.install_component_update(
                        "kokoro.language.ja", "2.0.0"
                    )

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "candidate activation failed")
                self.assertEqual(activate_runtime.call_count, 2)
                self.assertEqual(state_path.read_bytes(), state_before)
                current = updater.current("kokoro.language.ja")
                self.assertIsNotNone(current)
                assert current is not None
                self.assertEqual(current.version, "1.0.0")
            finally:
                api._shutdown()

    def test_api_install_reports_recovery_failure_after_restoring_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = SettingsRepository(root / "client")
            api = StoryForgeApi(repository=repository)
            try:
                updater = api._component_updater
                updater.install(
                    _build_component(root, version="1.0.0", value="first")
                )
                state_path = (
                    repository.data_dir
                    / "components"
                    / "kokoro.language.ja"
                    / "state.json"
                )
                state_before = state_path.read_bytes()
                api._runtime_hub_mode = "host"
                api._component_repository.publish(
                    _build_component(root, version="2.0.0", value="second")
                )

                with patch.object(
                    updater,
                    "activate_runtime",
                    side_effect=[
                        RuntimeError("candidate activation failed"),
                        RuntimeError("previous runtime recovery failed"),
                    ],
                ):
                    result = api.install_component_update(
                        "kokoro.language.ja", "2.0.0"
                    )

                self.assertFalse(result["ok"])
                self.assertIn("previous runtime recovery failed", result["error"])
                self.assertEqual(state_path.read_bytes(), state_before)
                current = updater.current("kokoro.language.ja")
                self.assertIsNotNone(current)
                assert current is not None
                self.assertEqual(current.version, "1.0.0")
            finally:
                api._shutdown()


class ComponentHubTransportTests(unittest.TestCase):
    def test_existing_hub_session_downloads_and_installs_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = CatalogRepository(root / "catalog.sqlite3")
            actor = catalog.save_user({"username": "owner", "role": "admin"})
            repository = ComponentRepository(root / "published")
            publication = repository.publish(_build_component(root))
            data = root / "data"
            data.mkdir()
            server = HubServer(
                catalog,
                {"existing-device-token": actor["id"]},
                host="127.0.0.1",
                port=0,
                data_root=data,
                component_repository=repository,
            ).start()
            self.addCleanup(server.stop)
            client = HubClient(
                server.base_url, "existing-device-token", timeout_seconds=5
            )

            remote = client.get_component_manifests()
            self.assertEqual(remote, [publication])
            destination = root / "download" / publication["filename"]
            downloaded = client.download_component_package(
                remote[0], destination=destination
            )
            self.assertEqual(downloaded["sha256"], publication["sha256"])
            installed = ComponentUpdater(root / "components").install(
                destination,
                expected_package_sha256=publication["sha256"],
            )
            self.assertEqual(installed.version, publication["version"])

    def test_empty_component_repository_is_backward_compatible(self) -> None:
        self.assertEqual(
            validate_component_catalog(None),
            {"schema_version": 1, "components": []},
        )

    def test_storyforge_api_installs_and_rolls_back_from_hub(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            port = _free_port()
            host_repository = SettingsRepository(root / "host")
            host_settings = AppSettings()
            host_settings.hub.mode = "host"
            host_settings.hub.listen_host = "127.0.0.1"
            host_settings.hub.listen_port = port
            host_settings.hub.auto_update_enabled = False
            host_repository.save(host_settings, [], [])
            host = StoryForgeApi(repository=host_repository)
            client: StoryForgeApi | None = None
            try:
                first = host.publish_component_update(
                    str(_build_component(root, version="1.0.0", value="first"))
                )
                self.assertTrue(first["ok"], first)

                client_repository = SettingsRepository(root / "client")
                client_settings = AppSettings()
                client_settings.hub.mode = "client"
                client_settings.hub.endpoint = f"http://127.0.0.1:{port}"
                client_settings.hub.access_token = host._state.settings.hub.access_token
                client_settings.hub.auto_update_enabled = False
                client_repository.save(client_settings, [], [])
                client = StoryForgeApi(repository=client_repository)

                installed_first = client.install_component_update(
                    "kokoro.language.ja"
                )
                self.assertTrue(installed_first["ok"], installed_first)
                self.assertEqual(
                    installed_first["data"]["component"]["version"], "1.0.0"
                )

                second = host.publish_component_update(
                    str(_build_component(root, version="2.0.0", value="second"))
                )
                self.assertTrue(second["ok"], second)
                installed_second = client.install_component_update(
                    "kokoro.language.ja", "2.0.0"
                )
                self.assertTrue(installed_second["ok"], installed_second)
                self.assertEqual(
                    installed_second["data"]["component"]["version"], "2.0.0"
                )

                restored = client.rollback_component_update(
                    "kokoro.language.ja"
                )
                self.assertTrue(restored["ok"], restored)
                self.assertEqual(
                    restored["data"]["component"]["version"], "1.0.0"
                )
            finally:
                if client is not None:
                    client._shutdown()
                host._shutdown()


class DeviceDeletionTransportTests(unittest.TestCase):
    def test_admin_rpc_deletes_only_a_disabled_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = CatalogRepository(root / "catalog.sqlite3")
            owner = catalog.save_user({"username": "owner", "role": "admin"})
            worker = catalog.save_user({"username": "worker", "role": "producer"})
            device = catalog.register_hub_device(
                {
                    "installation_id_hash": installation_id_sha256("device-one"),
                    "name": "Old workstation",
                    "app_version": __version__,
                },
                actor_user_id=worker["id"],
            )["device"]
            catalog.set_hub_device_active(
                device["id"], False, actor_user_id=owner["id"]
            )
            data = root / "data"
            data.mkdir()
            server = HubServer(
                catalog,
                {"admin-token": owner["id"]},
                host="127.0.0.1",
                port=0,
                data_root=data,
            ).start()
            self.addCleanup(server.stop)

            result = HubClient(server.base_url, "admin-token").call(
                "device_delete", {"device_id": device["id"]}
            )

            self.assertTrue(result["deleted"])
            self.assertEqual(catalog.list_hub_devices()["total"], 0)


if __name__ == "__main__":
    unittest.main()
