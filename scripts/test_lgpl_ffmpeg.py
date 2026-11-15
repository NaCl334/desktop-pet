"""Exercise archive integrity, GPL rejection and transactional replacement."""
import copy
import hashlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError
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

    def test_official_api_failure_falls_back_to_direct_without_losing_partial(self):
        (self.root / "setup_components/fixture.zip").unlink()
        partial = self.root / "temp/setup_downloads/fixture.zip.download"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"partial")
        calls = []
        def download(url, target, digest, **kwargs):
            calls.append((url, kwargs))
            self.assertEqual(partial.read_bytes(), b"partial")
            if len(calls) == 1:
                raise URLError("timeout")
            kwargs['progress'](10, 20)
            return target
        progress = []
        with patch("core.installer.download_verified", side_effect=download):
            result = tools.obtain_lgpl_archive(progress.append)
        self.assertEqual(result.name, "fixture.zip")
        self.assertTrue(calls[0][0].startswith(self.package['api_url'] + '?'))
        self.assertEqual(calls[0][1]['request_headers']['Accept'], 'application/octet-stream')
        self.assertEqual(calls[1][0], self.package['url'])
        self.assertTrue(any('MB' in e['text'] for e in progress))

    def test_all_sources_fail_with_actionable_error_and_preserve_main_pack(self):
        (self.root / "setup_components/fixture.zip").unlink()
        main_pack = self.root / "temp/setup_downloads/voice.7z"
        main_pack.parent.mkdir(parents=True)
        main_pack.write_bytes(b"verified main pack")
        with patch("core.installer.download_verified", side_effect=URLError('timeout')) as download:
            with self.assertRaisesRegex(SetupError, 'FFmpeg.*3 次.*无需重新下载主包'):
                tools.obtain_lgpl_archive()
        self.assertEqual(download.call_count, 3)
        self.assertNotEqual(download.call_args_list[0].args[0], download.call_args_list[2].args[0])
        self.assertEqual(main_pack.read_bytes(), b"verified main pack")

    def test_cancellation_does_not_try_more_sources(self):
        (self.root / "setup_components/fixture.zip").unlink()
        event = threading.Event()
        def download(*args, **kwargs):
            event.set()
            raise URLError('cancelled connection')
        with patch("core.installer.download_verified", side_effect=download) as mock:
            with self.assertRaisesRegex(SetupError, '已取消配置'):
                tools.obtain_lgpl_archive(cancel_event=event)
        self.assertEqual(mock.call_count, 1)

    def test_verified_download_cache_does_not_require_network(self):
        original = self.root / "setup_components/fixture.zip"
        cached = self.root / "temp/setup_downloads/fixture.zip"
        cached.parent.mkdir(parents=True)
        original.replace(cached)
        progress = []
        with patch('core.installer.urllib_request.urlopen') as network:
            self.assertEqual(tools.obtain_lgpl_archive(progress.append), cached)
        network.assert_not_called()
        self.assertTrue(all(e['percent'] is None for e in progress))


if __name__ == "__main__":
    unittest.main()
