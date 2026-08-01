from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from storyforge.tts_components import (
    KOKORO_LANGUAGE_COMPONENTS,
    kokoro_component_manifest,
    kokoro_language_component_health,
    probe_kokoro_language_runtime,
)


class TTSComponentTests(unittest.TestCase):
    @staticmethod
    def _finder(unidic_root: Path):
        def find(module: str):
            if module == "unidic_lite":
                return SimpleNamespace(
                    origin=str(unidic_root / "__init__.py"),
                    submodule_search_locations=[str(unidic_root)],
                )
            return SimpleNamespace(origin="built-in", submodule_search_locations=[])

        return find

    def test_japanese_health_rejects_module_without_dictionary_payload(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "unidic_lite"
            (root / "dicdir").mkdir(parents=True)
            (root / "__init__.py").write_text("", encoding="utf-8")

            health = kokoro_language_component_health(
                "j", module_finder=self._finder(root)
            )

        self.assertFalse(health.ready)
        self.assertIn(
            "tts_component_resource_missing",
            {issue.code for issue in health.issues},
        )
        self.assertTrue(
            any(issue.subject.endswith("dicdir/version") for issue in health.issues)
        )

    def test_japanese_health_accepts_nonempty_verified_dictionary_files(self) -> None:
        required = KOKORO_LANGUAGE_COMPONENTS["j"]["resources"]["unidic_lite"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "unidic_lite"
            root.mkdir(parents=True)
            (root / "__init__.py").write_text("", encoding="utf-8")
            for relative in required:
                path = root.joinpath(*relative.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"verified")

            health = kokoro_language_component_health(
                "j", module_finder=self._finder(root)
            )

        self.assertTrue(health.ready)
        self.assertEqual(health.issues, ())

    def test_component_manifest_is_small_serializable_inventory(self) -> None:
        manifest = kokoro_component_manifest()
        self.assertEqual(manifest["schema"], 1)
        packs = {item["language"]: item for item in manifest["language_packs"]}
        self.assertIn("ja", packs)
        self.assertIn("dicdir/version", packs["ja"]["resources"]["unidic_lite"])

    def test_runtime_probe_reports_broken_japanese_g2p_as_structured_issue(self) -> None:
        required = KOKORO_LANGUAGE_COMPONENTS["j"]["resources"]["unidic_lite"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "unidic_lite"
            root.mkdir(parents=True)
            (root / "__init__.py").write_text("", encoding="utf-8")
            for relative in required:
                path = root.joinpath(*relative.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"verified")
            broken_module = SimpleNamespace(
                JAG2P=lambda: (lambda _text: ("", []))
            )
            health = probe_kokoro_language_runtime(
                "j",
                module_finder=self._finder(root),
                module_importer=lambda _name: broken_module,
            )

        self.assertFalse(health.ready)
        self.assertEqual(
            health.error_code,
            "tts_component_runtime_probe_failed",
        )


if __name__ == "__main__":
    unittest.main()
