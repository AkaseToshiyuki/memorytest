"""Pinned source integrity and packaging policy; no downloads or device reads."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("smart_build", ROOT / "packaging/build_smartctl.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class SmartPackagingTests(unittest.TestCase):
    def test_source_cache_is_verified_even_when_already_present(self):
        with tempfile.TemporaryDirectory(prefix="memorytest-smart-source-") as directory:
            cache = Path(directory)
            target = cache / build.ARCHIVE
            target.write_bytes(b"modified source fixture")
            with patch.object(build.urllib.request, "urlopen") as network:
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    build.source_archive(cache)
                network.assert_not_called()
            with patch.object(build, "SOURCE_SHA256", hashlib.sha256(target.read_bytes()).hexdigest()):
                self.assertEqual(build.source_archive(cache), target)

    def test_platform_builds_have_no_updater_service_or_runtime_download(self):
        for windows, architecture, compiler, host in [(False, "arm64", "clang", None),
                (True, "AMD64", "gcc", "x86_64-w64-mingw32"), (True, "ARM64", "clang", "aarch64-w64-mingw32")]:
            with patch.object(build.shutil, "which", return_value=None):
                options, environment = build.build_options(windows, architecture, compiler)
            self.assertIn("--with-update-smart-drivedb=no", options)
            self.assertIn("--with-drivedbdir=no", options)
            self.assertNotIn("--disable-drivedb", options)
            self.assertEqual(environment["CXX"], "clang++" if compiler == "clang" else "g++")
            self.assertEqual([option for option in options if option.startswith("--host=")], ["--host=" + host] if host else [])

    def test_bundled_reader_is_preferred_to_system_path(self):
        import os
        import hardware_inspector
        with tempfile.TemporaryDirectory(prefix="memorytest-smart-path-") as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            binary = root / "bin" / ("smartctl.exe" if os.name == "nt" else "smartctl")
            binary.write_bytes(b"fixture")
            binary.chmod(0o755)
            with patch.object(hardware_inspector, "ROOT", root), patch.object(hardware_inspector.shutil, "which", return_value="system-smartctl"):
                self.assertEqual(hardware_inspector.find_tool("smartctl"), str(binary))


if __name__ == "__main__":
    unittest.main()
