"""Exercise archive integrity, GPL rejection and transactional replacement."""
import copy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from core import lgpl_ffmpeg as tools
from core.installer import SetupError


class LGPLToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = self.root / "engine"
        self.engine.mkdir()
        self.package = copy.deepcopy(tools.PACKAGE)
        self.package["filename"] = "fixture.zip"
        self.payload = {"ffmpeg.exe": b"verified ffmpeg fixture", "ffprobe.exe": b"verified ffprobe fixture"}
        self.package["files"] = {n: hashlib.sha256(b).hexdigest() for n, b in self.payload.items()}
        archive = self.root / "setup_components/fixture.zip"
        archive.parent.mkdir()
        with zipfile.ZipFile(archive, "w") as z:
            for name, data in self.payload.items():
                z.writestr("build/bin/" + name, data)
            z.writestr("build/LICENSE.txt", "LGPL fixture")
        self.package.update(sha256=tools._digest(archive), size=archive.stat().st_size)
        self.addCleanup(patch.stopall)
        patch.object(tools, "ROOT", self.root).start()
        patch.object(tools, "PACKAGE", self.package).start()
        self.run = patch.object(tools.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="configuration: --enable-version3")).start()

    def test_existing_aliases_and_canonical_tools_replaced_together(self):
        for relative in ["ffmpeg.exe", "runtime/ffmpeg.exe", "ffmpeg/ffprobe.exe"]:
            p = self.engine / relative
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(b"old GPL tool")
        target = tools.install_lgpl_tools(self.engine)
        for relative in ["ffmpeg.exe", "runtime/ffmpeg.exe", "ffmpeg/ffmpeg.exe", "ffmpeg/ffprobe.exe"]:
            p = self.engine / relative
            self.assertEqual(p.read_bytes(), self.payload[p.name])
        self.assertTrue((target / "licenses/SOURCE.json").is_file())

    def test_corrupt_archive_never_changes_old_environment(self):
        original = self.engine / "ffmpeg.exe"
        original.write_bytes(b"original")
        (self.root / "setup_components/fixture.zip").write_bytes(b"corrupt")
        with self.assertRaises(SetupError):
            tools.install_lgpl_tools(self.engine)
        self.assertEqual(original.read_bytes(), b"original")

    def test_gpl_flag_rejected_before_any_replacement(self):
        original = self.engine / "ffmpeg.exe"
        original.write_bytes(b"original")
        self.run.return_value.stdout = "configuration: --enable-gpl"
        with self.assertRaises(SetupError):
            tools.install_lgpl_tools(self.engine)
        self.assertEqual(original.read_bytes(), b"original")

    def test_failure_restores_all_prior_tools(self):
        original = self.engine / "ffmpeg.exe"
        original.write_bytes(b"original")
        real_replace = tools.os.replace
        calls = 0
        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("simulated file lock")
            return real_replace(source, target)
        with patch.object(tools.os, "replace", side_effect=fail_second):
            with self.assertRaises(PermissionError):
                tools.install_lgpl_tools(self.engine)
        self.assertEqual(original.read_bytes(), b"original")
        self.assertFalse((self.engine / "ffmpeg/ffmpeg.exe").exists())
        self.assertFalse(list(self.engine.rglob("*.lgpl-new")))

    def test_identical_verified_tools_need_no_network(self):
        target = self.engine / "ffmpeg"
        target.mkdir()
        for name, data in self.payload.items():
            (target / name).write_bytes(data)
        with patch("core.installer.download_verified") as download:
            self.assertEqual(tools.install_lgpl_tools(self.engine), target)
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
