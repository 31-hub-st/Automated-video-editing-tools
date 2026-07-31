from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storyforge.windows_bootstrap import unblock_frozen_installation


class WindowsBootstrapTests(unittest.TestCase):
    def test_non_frozen_runtime_does_not_touch_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "StoryForge Studio.exe"
            executable.write_bytes(b"exe")
            with patch(
                "storyforge.windows_bootstrap._remove_zone_identifier"
            ) as remove:
                result = unblock_frozen_installation(
                    root / "data",
                    frozen=False,
                    executable=executable,
                    platform_name="nt",
                )
        self.assertTrue(result["skipped"])
        remove.assert_not_called()

    def test_frozen_runtime_checks_only_loadable_bundle_files_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "StoryForge Studio.exe"
            executable.write_bytes(b"exe")
            internal = root / "_internal"
            (internal / "pythonnet" / "runtime").mkdir(parents=True)
            runtime = internal / "pythonnet" / "runtime" / "Python.Runtime.dll"
            runtime.write_bytes(b"dll")
            ignored = internal / "large-model.pth"
            ignored.write_bytes(b"model")
            data = root / "StoryForgeData"

            with patch(
                "storyforge.windows_bootstrap._remove_zone_identifier",
                return_value=True,
            ) as remove:
                first = unblock_frozen_installation(
                    data,
                    frozen=True,
                    executable=executable,
                    platform_name="nt",
                )
                second = unblock_frozen_installation(
                    data,
                    frozen=True,
                    executable=executable,
                    platform_name="nt",
                )

            checked = {call.args[0] for call in remove.call_args_list}
            self.assertEqual(checked, {executable, runtime})
            self.assertEqual(first["zone_markers_removed"], 2)
            self.assertEqual(second["reason"], "already_checked")
            self.assertEqual(remove.call_count, 2)


if __name__ == "__main__":
    unittest.main()
