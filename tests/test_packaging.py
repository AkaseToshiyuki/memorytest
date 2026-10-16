"""Installer cleanup contracts without mounting volumes or launching loads."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("artifact_check", ROOT / "packaging/check_artifact.py")
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)


class InstallerCleanupTests(unittest.TestCase):
    def test_busy_disk_image_is_retried_without_force(self):
        command = ["hdiutil", "detach", "/Volumes/MemoryTest"]
        busy = subprocess.CalledProcessError(16, command, stderr="Resource busy")
        with patch.object(artifact.subprocess, "run", side_effect=[busy, busy, None]) as run, \
                patch.object(artifact.time, "sleep") as sleep:
            artifact.detach_disk_image(command[-1])
        self.assertEqual(run.call_args_list, [call(command, check=True,
                         capture_output=True, text=True, timeout=15)] * 3)
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])

    def test_persistent_busy_and_other_failures_remain_failures(self):
        for code, attempts in ((16, 5), (1, 1)):
            with self.subTest(returncode=code):
                error = subprocess.CalledProcessError(code, ["hdiutil", "detach", "fixture"])
                with patch.object(artifact.subprocess, "run", side_effect=error) as run, \
                        patch.object(artifact.time, "sleep") as sleep:
                    with self.assertRaises(subprocess.CalledProcessError) as result:
                        artifact.detach_disk_image("fixture")
                self.assertIs(result.exception, error)
                self.assertEqual(run.call_count, attempts)
                self.assertEqual(sleep.call_count, attempts - 1)

    def test_detach_timeout_is_not_retried(self):
        error = subprocess.TimeoutExpired(["hdiutil", "detach", "fixture"], 15)
        with patch.object(artifact.subprocess, "run", side_effect=error) as run, \
                patch.object(artifact.time, "sleep") as sleep:
            with self.assertRaises(subprocess.TimeoutExpired):
                artifact.detach_disk_image("fixture")
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
