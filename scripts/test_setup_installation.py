"""Exercise real downloads, archive extraction and isolated installation transactions."""
import ctypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from core import environment, installer
from core.config import ROOT
from core.setup_packages import PACKAGES
from scripts.build_clean_release import build_release

PAYLOAD = b"desktop-pet-download-test" * 500
READY = {"components": {key: {"available": True} for key in
         ("python", "torch", "compute", "ffmpeg", "gpt_sovits_runtime", "gpt_sovits_model")}}


class DownloadHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        offset = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
        self.server.offsets.append(offset)
        if self.path == "/ignore":
            offset = 0
        self.send_response(206 if offset else 200)
        if offset:
            start = offset + (1 if self.path == "/bad-range" else 0)
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, len(PAYLOAD)-1, len(PAYLOAD)))
        self.send_header("Content-Length", str(len(PAYLOAD) - offset))
        self.end_headers()
        self.wfile.write(PAYLOAD[offset:])

    def log_message(self, *args):
        pass


class SetupDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DownloadHandler)
        cls.server.offsets = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=ROOT / "temp")
        self.addCleanup(self.folder.cleanup)
        self.target = Path(self.folder.name) / "voice.7z"
        self.partial = self.target.with_suffix(".7z.download")
        self.digest = hashlib.sha256(PAYLOAD).hexdigest()
        self.url = "http://127.0.0.1:{}/".format(self.server.server_port)
        self.server.offsets.clear()

    def download(self, endpoint="ok", **kwargs):
        return installer.download_verified(self.url + endpoint, self.target,
                                          kwargs.pop("sha", self.digest),
                                          expected_size=len(PAYLOAD), **kwargs)

    def test_real_http_download_and_verified_cache(self):
        progress = Mock()
        self.download(progress=progress)
        self.assertEqual(self.target.read_bytes(), PAYLOAD)
        self.assertFalse(self.partial.exists())
        self.download()
        self.assertEqual(self.server.offsets, [0])
        progress.assert_called()

    def test_real_http_resumes_partial_file(self):
        self.partial.write_bytes(PAYLOAD[:127])
        self.download()
        self.assertEqual(self.target.read_bytes(), PAYLOAD)
        self.assertEqual(self.server.offsets, [127])

    def test_server_ignoring_range_restarts_without_duplicate_bytes(self):
        self.partial.write_bytes(PAYLOAD[:127])
        self.download("ignore")
        self.assertEqual(self.target.read_bytes(), PAYLOAD)

    def test_wrong_range_does_not_corrupt_partial(self):
        self.partial.write_bytes(PAYLOAD[:127])
        with self.assertRaisesRegex(installer.SetupError, "续传范围"):
            self.download("bad-range")
        self.assertEqual(self.partial.read_bytes(), PAYLOAD[:127])

    def test_hash_mismatch_cannot_be_deployed(self):
        with self.assertRaisesRegex(installer.SetupError, "校验失败"):
            self.download(sha="0" * 64)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.partial.exists())

    def test_completed_partial_is_verified_without_network(self):
        self.partial.write_bytes(PAYLOAD)
        self.download()
        self.assertEqual(self.target.read_bytes(), PAYLOAD)
        self.assertEqual(self.server.offsets, [])

    def test_cancel_preserves_partial_and_does_not_start_network(self):
        self.partial.write_bytes(PAYLOAD[:127])
        event = threading.Event()
        event.set()
        with self.assertRaises(installer.SetupError):
            self.download(cancel_event=event)
        self.assertEqual(self.partial.read_bytes(), PAYLOAD[:127])
        self.assertEqual(self.server.offsets, [])


class SetupTransactionTests(unittest.TestCase):
    def setUp(self):
        # Tool integrity/rollback has its own real-archive tests.
        external_extractor = os.environ.get("DESKTOP_PET_TEST_7ZA", "")
        if external_extractor:
            original_setup_file = installer.bundled_setup_file
            extractor_patch = patch.object(installer, "bundled_setup_file", side_effect=lambda name:
                Path(external_extractor) if name == "7zip/7za.exe" else original_setup_file(name))
            extractor_patch.start();self.addCleanup(extractor_patch.stop)
        tool_patch = patch.object(installer, "install_lgpl_tools")
        self.lgpl_install = tool_patch.start()
        self.addCleanup(tool_patch.stop)
        self.folder = tempfile.TemporaryDirectory(dir=ROOT / "temp")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.engine = self.root / "engines/gpt_sovits"
        self.package = self.root / "fixture.zip"
        with zipfile.ZipFile(self.package, "w") as archive:
            archive.writestr("fixture/api_v2.py", "# fixture")
            archive.writestr("fixture/GPT_SoVITS/marker.txt", "fixture")
        self.root_patch = patch.object(installer, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_native_windows_archive_extraction(self):
        if sys.platform != "win32":
            self.skipTest("Windows tar")
        result = installer._unpack_voice(self.package, self.root / "unpack", None, None)
        self.assertEqual(result.name, "fixture")
        self.assertTrue((result / "GPT_SoVITS/marker.txt").is_file())

    def test_native_windows_7z_extraction(self):
        if sys.platform != "win32":
            self.skipTest("Windows tar")
        try:
            import py7zr
        except ImportError:
            self.skipTest("py7zr is used only to generate the test archive")
        archive_path = self.root / "fixture.7z"
        with py7zr.SevenZipFile(archive_path, "w") as archive:
            archive.writestr("# fixture", "fixture/api_v2.py")
            archive.writestr("fixture", "fixture/GPT_SoVITS/marker.txt")
        result = installer._unpack_voice(archive_path, self.root / "unpack7z", None, None)
        self.assertEqual((result / "GPT_SoVITS/marker.txt").read_text(), "fixture")

    def test_unsafe_archive_paths_are_rejected(self):
        for name in ("../escape", "C:/outside", "/absolute", "a/../../escape", "a\\..\\escape"):
            with self.subTest(name=name), self.assertRaises(installer.SetupError):
                installer._archive_parts(name)

    def test_extractor_is_verified_before_running_archive(self):
        altered = self.root / "7za.exe"
        altered.write_bytes(b"altered-extractor")
        with patch.object(installer, "bundled_setup_file", return_value=altered), \
                patch.object(installer, "_run") as run:
            with self.assertRaisesRegex(installer.SetupError, "校验失败"):
                installer._unpack_voice(self.package, self.root / "unpack", None, None)
        run.assert_not_called()

    def test_offline_package_is_verified_without_network_and_preserved(self):
        package = {"filename": "offline.7z", "size": len(PAYLOAD),
                   "sha256": hashlib.sha256(PAYLOAD).hexdigest(), "urls": []}
        local = self.root / "setup_components/offline.7z"
        local.parent.mkdir()
        local.write_bytes(PAYLOAD)
        with patch.object(installer, "download_verified") as download:
            self.assertEqual(installer._obtain_package(package, None, None), local)
        download.assert_not_called()
        self.assertEqual(local.read_bytes(), PAYLOAD)

    def test_cleanup_handles_windows_paths_longer_than_260_characters(self):
        if sys.platform != "win32":
            self.skipTest("Windows extended paths")
        directory = self.root / "long_paths"
        nested = directory.joinpath(*(["long_component_name_" * 2] * 9))
        native = "\\\\?\\" + str(nested.resolve())
        os.makedirs(native)
        with open(native + "\\fixture.txt", "w") as handle:
            handle.write("fixture")
        installer._safe_remove(directory)
        self.assertFalse(directory.exists())

    def test_new_install_is_validated_before_and_after_relocation(self):
        if sys.platform != "win32":
            self.skipTest("Windows installer")
        with patch.object(installer, "detect_environment", side_effect=[{}, READY]), \
                patch.object(installer.shutil, "disk_usage", return_value=Mock(free=100*1024**3)), \
                patch.object(installer, "voice_package", return_value={}), \
                patch.object(installer, "_obtain_package", return_value=self.package), \
                patch.object(installer, "_validate_engine") as validate:
            result = installer.install_profile("full")
        self.assertIs(result, READY)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(validate.call_args.args[0], self.engine)
        self.assertFalse(validate.call_args.kwargs["load_models"])
        self.assertTrue((self.engine / "start_api.py").exists())
        config = json.loads((self.engine / "tts_infer_project.yaml").read_text())
        self.assertNotIn("huadan", str(config))
        self.assertEqual(config["custom"]["version"], "v2Pro")
        self.assertFalse(any((self.root / "temp/setup_staging").glob("voice_*")))

    def test_failure_after_relocation_restores_previous_directory(self):
        if sys.platform != "win32":
            self.skipTest("Windows installer")
        self.engine.mkdir(parents=True)
        (self.engine / "user-file.txt").write_text("preserve")
        with patch.object(installer, "detect_environment", return_value={}), \
                patch.object(installer.shutil, "disk_usage", return_value=Mock(free=100*1024**3)), \
                patch.object(installer, "voice_package", return_value={}), \
                patch.object(installer, "_obtain_package", return_value=self.package), \
                patch.object(installer, "_validate_engine", side_effect=[None, installer.SetupError("failed")]):
            with self.assertRaisesRegex(installer.SetupError, "failed"):
                installer.install_profile("full")
        self.assertEqual((self.engine / "user-file.txt").read_text(), "preserve")
        self.assertFalse((self.engine / "start_api.py").exists())

    def test_existing_ready_environment_is_checked_without_download(self):
        with patch.object(installer, "detect_environment", return_value=READY), \
                patch.object(installer, "_obtain_package") as obtain, \
                patch.object(installer, "_validate_engine") as validate:
            installer.install_profile("full")
        obtain.assert_not_called()
        validate.assert_called_once()

    def test_forced_repair_failure_restores_previously_ready_environment(self):
        self.engine.mkdir(parents=True)
        (self.engine / "user-file.txt").write_text("preserve")
        with patch.object(installer, "detect_environment", return_value=READY), \
                patch.object(installer.shutil, "disk_usage", return_value=Mock(free=100*1024**3)), \
                patch.object(installer, "voice_package", return_value={}), \
                patch.object(installer, "_obtain_package", return_value=self.package), \
                patch.object(installer, "_validate_engine", side_effect=[None, installer.SetupError("failed")]) as validate:
            with self.assertRaisesRegex(installer.SetupError, "failed"):
                installer.install_profile("full", force_repair=True)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual((self.engine / "user-file.txt").read_text(), "preserve")

    def test_only_desktop_mode_needs_no_voice_environment_or_network(self):
        with patch.object(installer, "detect_environment") as detect:
            installer.install_profile("desktop")
        detect.assert_not_called()
        self.assertFalse(self.engine.exists())

    def test_disk_space_failure_happens_before_download(self):
        with patch.object(installer, "detect_environment", return_value={}), \
                patch.object(installer.shutil, "disk_usage", return_value=Mock(free=1024)), \
                patch.object(installer, "_obtain_package") as obtain:
            with self.assertRaisesRegex(installer.SetupError, "32 GiB"):
                installer.install_profile("full")
        obtain.assert_not_called()

    def test_retries_are_bounded_and_never_install_on_failure(self):
        package = dict(PACKAGES["standard"], urls=["http://127.0.0.1/a"]*3)
        with patch.object(installer, "download_verified", side_effect=OSError("unavailable")) as download:
            with self.assertRaisesRegex(installer.SetupError, "下载失败"):
                installer._obtain_package(package, None, None)
        self.assertEqual(download.call_count, 3)
        self.assertFalse(self.engine.exists())

    def test_silent_child_can_be_cancelled(self):
        event = threading.Event()
        timer = threading.Timer(.25, event.set)
        timer.start()
        start = time.monotonic()
        try:
            with self.assertRaises(installer.SetupError):
                installer._run([sys.executable, "-c", "import time;time.sleep(30)"],
                               self.root, cancel_event=event)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - start, 3)

    def test_frozen_environment_never_runs_gui_as_python_probe(self):
        with patch.object(environment.sys, "frozen", True, create=True), \
                patch.object(environment, "gpt_python_path", return_value=Path(sys.executable)), \
                patch.object(environment, "_run_python_probe") as probe, \
                patch.object(environment, "_gpt_runtime_present", return_value=False):
            result = environment.detect_environment()
        probe.assert_not_called()
        self.assertTrue(result["components"]["python"]["available"])
        self.assertFalse(environment.profile_ready(result, "full"))

    def test_character_model_files_cannot_replace_missing_base_models(self):
        (self.root / "models/gpt_sovits/characters/test").mkdir(parents=True)
        (self.root / "models/gpt_sovits/characters/test/weights.pth").write_bytes(b"weights")
        self.assertFalse(environment.base_models_present(self.engine))

    def test_clean_distribution_has_no_personal_config_or_characters(self):
        project = self.root / "source"
        (project / "assets/branding").mkdir(parents=True)
        (project / "config").mkdir()
        (project / "characters/private-role").mkdir(parents=True)
        (project / "我的桌宠.exe").write_bytes(b"fixture")
        example = {"autostart": {"enabled": True}, "conversation_model": {"api_key": "template"}}
        (project / "config/app_config.example.json").write_text(json.dumps(example))
        (project / "config/app_config.json").write_text('"private-settings"')
        target = build_release(project / "release/clean_0.1.0", project, archive=True)
        self.assertTrue((target.parent / "clean_0.1.0.zip").is_file())
        self.assertFalse((target.parent / "clean_0.1.zip").exists())
        self.assertEqual(list((target / "characters").iterdir()), [])
        app = json.loads((target / "config/app_config.json").read_text(encoding="utf-8"))
        self.assertEqual(app["conversation_model"]["api_key"], "")
        self.assertFalse(app["autostart"]["enabled"])
        voice = json.loads((target / "config/gpt_sovits.json").read_text(encoding="utf-8"))
        self.assertEqual(voice["characters"], {})
        from core.uninstaller import is_release_install
        self.assertTrue(is_release_install(target))


if __name__ == "__main__":
    unittest.main()
