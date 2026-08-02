from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from storyforge.component_updater import (
    COMPONENT_MANIFEST_FILENAME,
    ComponentCompatibilityError,
    ComponentIntegrityError,
    ComponentNotInstalledError,
    ComponentPackageBuilder,
    ComponentPackageError,
    ComponentSecurityError,
    ComponentUpdater,
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _manifest(
    *,
    version: str = "1.0.0",
    min_app_version: str = "0.4.0",
    max_app_version: str | None = "0.9.0",
    path: str = "models/voice.bin",
    payload: bytes = b"voice model",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "component_id": "kokoro.language.ja",
        "version": version,
        "app_compatibility": {
            "min_version": min_app_version,
            "max_version": max_app_version,
        },
        "files": [
            {"path": path, "size": len(payload), "sha256": _digest(payload)}
        ],
    }


def _write_package(
    path: Path,
    manifest: dict[str, object],
    entries: list[tuple[str | zipfile.ZipInfo, bytes]],
) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(COMPONENT_MANIFEST_FILENAME, json.dumps(manifest))
        for name, payload in entries:
            archive.writestr(name, payload)
    return path


class ComponentPackageBuilderTests(unittest.TestCase):
    def test_builder_creates_verified_deterministic_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "dict").mkdir(parents=True)
            (source / "dict" / "ja.txt").write_text("電話", encoding="utf-8")
            (source / "model.bin").write_bytes(b"model")

            first = ComponentPackageBuilder.build(
                source,
                root / "first.zip",
                component_id="kokoro.language.ja",
                version="1.2.0",
                min_app_version="0.4.0",
                max_app_version="0.5.0",
            )
            second = ComponentPackageBuilder.build(
                source,
                root / "second.zip",
                component_id="kokoro.language.ja",
                version="1.2.0",
                min_app_version="0.4.0",
                max_app_version="0.5.0",
            )

            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(first.manifest.component_id, "kokoro.language.ja")
            self.assertEqual(first.manifest.version, "1.2.0")
            self.assertEqual(
                first.manifest.app_compatibility,
                {"min_version": "0.4.0", "max_version": "0.5.0"},
            )
            self.assertEqual(
                {item.path for item in first.manifest.files},
                {"dict/ja.txt", "model.bin"},
            )
            inspection = ComponentUpdater.inspect_package(
                first.path,
                app_version="0.4.5",
                expected_package_sha256=first.sha256,
            )
            self.assertEqual(inspection.manifest, first.manifest)

    def test_builder_rejects_empty_source_and_destination_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            with self.assertRaises(ComponentPackageError):
                ComponentPackageBuilder.build(
                    source,
                    Path(temporary) / "empty.zip",
                    component_id="kokoro.language.ja",
                    version="1.0.0",
                )
            (source / "value.bin").write_bytes(b"value")
            with self.assertRaises(ComponentPackageError):
                ComponentPackageBuilder.build(
                    source,
                    source / "component.zip",
                    component_id="kokoro.language.ja",
                    version="1.0.0",
                )


class ComponentPackageSecurityTests(unittest.TestCase):
    def test_rejects_package_and_payload_digest_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _write_package(
                root / "bad.zip",
                _manifest(payload=b"expected"),
                [("payload/models/voice.bin", b"tampered")],
            )
            with self.assertRaises(ComponentIntegrityError):
                ComponentUpdater.inspect_package(package, app_version="0.4.5")
            with self.assertRaises(ComponentIntegrityError):
                ComponentUpdater.inspect_package(
                    package,
                    app_version="0.4.5",
                    expected_package_sha256="0" * 64,
                )

    def test_rejects_traversal_absolute_windows_and_symlink_entries(self) -> None:
        unsafe_names = [
            "payload/../../outside.txt",
            "/payload/outside.txt",
            "payload/C:/outside.txt",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, unsafe_name in enumerate(unsafe_names):
                package = _write_package(
                    root / f"unsafe-{index}.zip",
                    _manifest(path="outside.txt", payload=b"unsafe"),
                    [(unsafe_name, b"unsafe")],
                )
                with self.subTest(name=unsafe_name):
                    with self.assertRaises(ComponentSecurityError):
                        ComponentUpdater.inspect_package(
                            package, app_version="0.4.5"
                        )

            # Python's Windows zipfile writer normalises backslashes before it
            # writes an entry, so cover the same unsafe spelling in the
            # manifest where it remains byte-for-byte observable.
            package = _write_package(
                root / "backslash.zip",
                _manifest(path="models\\voice.bin"),
                [("payload/models/voice.bin", b"voice model")],
            )
            with self.assertRaises(ComponentSecurityError):
                ComponentUpdater.inspect_package(package, app_version="0.4.5")

            link = zipfile.ZipInfo("payload/models/voice.bin")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            package = _write_package(
                root / "symlink.zip",
                _manifest(),
                [(link, b"../../outside")],
            )
            with self.assertRaises(ComponentSecurityError):
                ComponentUpdater.inspect_package(package, app_version="0.4.5")
            self.assertFalse((root / "outside.txt").exists())

    def test_rejects_case_insensitive_duplicates_and_undeclared_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _write_package(
                root / "duplicate.zip",
                _manifest(),
                [
                    ("payload/models/voice.bin", b"voice model"),
                    ("payload/MODELS/VOICE.BIN", b"voice model"),
                ],
            )
            with self.assertRaises(ComponentSecurityError):
                ComponentUpdater.inspect_package(package, app_version="0.4.5")

            package = _write_package(
                root / "extra.zip",
                _manifest(),
                [
                    ("payload/models/voice.bin", b"voice model"),
                    ("payload/unlisted.bin", b"extra"),
                ],
            )
            with self.assertRaises(ComponentIntegrityError):
                ComponentUpdater.inspect_package(package, app_version="0.4.5")

    def test_enforces_app_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            future = _write_package(
                root / "future.zip",
                _manifest(min_app_version="0.5.0", max_app_version=None),
                [("payload/models/voice.bin", b"voice model")],
            )
            with self.assertRaises(ComponentCompatibilityError):
                ComponentUpdater.inspect_package(future, app_version="0.4.5")

            obsolete = _write_package(
                root / "obsolete.zip",
                _manifest(min_app_version="0.1.0", max_app_version="0.4.4"),
                [("payload/models/voice.bin", b"voice model")],
            )
            with self.assertRaises(ComponentCompatibilityError):
                ComponentUpdater.inspect_package(obsolete, app_version="0.4.5")


class ComponentUpdaterTests(unittest.TestCase):
    @staticmethod
    def _build(root: Path, version: str, content: bytes):
        source = root / f"source-{version}"
        source.mkdir()
        (source / "models").mkdir()
        (source / "models" / "voice.bin").write_bytes(content)
        return ComponentPackageBuilder.build(
            source,
            root / f"component-{version}.zip",
            component_id="kokoro.language.ja",
            version=version,
            min_app_version="0.4.0",
            max_app_version="0.5.0",
        )

    def test_installs_under_selected_components_root_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            components = root / "chosen" / "StoryForgeData" / "components"
            artifact = self._build(root, "1.0.0", b"version one")
            updater = ComponentUpdater(components, app_version="0.4.5")

            installed = updater.install(
                artifact.path, expected_package_sha256=artifact.sha256
            )
            installed_again = updater.install(artifact.path)

            self.assertTrue(installed.root.is_relative_to(components))
            self.assertEqual(installed.root, installed_again.root)
            self.assertEqual(
                installed.resolve("models/voice.bin").read_bytes(), b"version one"
            )
            self.assertEqual(updater.current("kokoro.language.ja"), installed)
            self.assertEqual(updater.list_installed(), (installed,))
            state = json.loads(
                (components / "kokoro.language.ja" / "state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIsNone(state["previous"])

    def test_reinstall_same_package_repairs_damaged_active_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            components = root / "components"
            artifact = self._build(root, "1.0.0", b"version one")
            updater = ComponentUpdater(components, app_version="0.4.5")
            installed = updater.install(artifact.path)
            state_path = components / "kokoro.language.ja" / "state.json"
            state_before = state_path.read_bytes()
            installed.resolve("models/voice.bin").write_bytes(b"version bad")

            repaired = updater.install(
                artifact.path, expected_package_sha256=artifact.sha256
            )

            self.assertEqual(repaired.root, installed.root)
            self.assertEqual(
                repaired.resolve("models/voice.bin").read_bytes(), b"version one"
            )
            self.assertEqual(state_path.read_bytes(), state_before)

    def test_failed_repair_restore_keeps_the_only_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release_dir = root / "release"
            release_dir.mkdir()
            (release_dir / "value.bin").write_bytes(b"damaged original")
            staged = root / "staged"
            staged.mkdir()
            (staged / "value.bin").write_bytes(b"verified repair")
            real_replace = os.replace

            def fail_repair_and_restore(source: object, destination: object) -> None:
                source_path = Path(source)
                if source_path == staged or source_path.name.startswith(
                    ".repair-backup-"
                ):
                    raise OSError("injected replace failure")
                real_replace(source, destination)

            with patch(
                "storyforge.component_updater.os.replace",
                side_effect=fail_repair_and_restore,
            ):
                with self.assertRaises(OSError):
                    ComponentUpdater._replace_damaged_release(staged, release_dir)

            backups = list(root.glob(".repair-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(
                (backups[0] / "value.bin").read_bytes(), b"damaged original"
            )

    def test_update_and_one_step_rollback_atomically_swap_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            updater = ComponentUpdater(root / "components", app_version="0.4.5")
            version_one = self._build(root, "1.0.0", b"version one")
            version_two = self._build(root, "2.0.0", b"version two")

            first = updater.install(version_one.path)
            second = updater.install(version_two.path)
            self.assertEqual(second.version, "2.0.0")
            self.assertEqual(
                updater.resolve("kokoro.language.ja", "models/voice.bin").read_bytes(),
                b"version two",
            )

            restored = updater.rollback("kokoro.language.ja")
            self.assertEqual(restored.version, "1.0.0")
            self.assertEqual(restored.root, first.root)
            self.assertEqual(
                updater.resolve("kokoro.language.ja", "models/voice.bin").read_bytes(),
                b"version one",
            )
            # Rollback is a swap, so one more rollback returns to version two.
            self.assertEqual(updater.rollback("kokoro.language.ja").version, "2.0.0")

    def test_failed_update_preserves_active_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            updater = ComponentUpdater(root / "components", app_version="0.4.5")
            first = self._build(root, "1.0.0", b"version one")
            updater.install(first.path)

            bad = _write_package(
                root / "bad-update.zip",
                _manifest(version="2.0.0", payload=b"expected"),
                [("payload/models/voice.bin", b"tampered")],
            )
            with self.assertRaises(ComponentIntegrityError):
                updater.install(bad)

            current = updater.current("kokoro.language.ja")
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current.version, "1.0.0")
            self.assertEqual(current.resolve("models/voice.bin").read_bytes(), b"version one")

    def test_rollback_requires_previous_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            updater = ComponentUpdater(root / "components", app_version="0.4.5")
            updater.install(self._build(root, "1.0.0", b"version one").path)
            with self.assertRaises(ComponentNotInstalledError):
                updater.rollback("kokoro.language.ja")

    def test_rollback_rejects_damaged_target_before_state_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            updater = ComponentUpdater(root / "components", app_version="0.4.5")
            first = updater.install(
                self._build(root, "1.0.0", b"version one").path
            )
            updater.install(self._build(root, "2.0.0", b"version two").path)
            first.resolve("models/voice.bin").write_bytes(b"damaged")

            with self.assertRaises(ComponentIntegrityError):
                updater.rollback("kokoro.language.ja")

            current = updater.current("kokoro.language.ja")
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current.version, "2.0.0")
            self.assertEqual(
                current.resolve("models/voice.bin").read_bytes(), b"version two"
            )


if __name__ == "__main__":
    unittest.main()
