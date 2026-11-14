"""Isolated route-B regression tests. Never read personal signing keys/data."""
import copy
import ctypes
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import core.package_signature as sig
import core.config as config
import core.backup as backup
import core.body_location as body
from core.tray import decode_character_request, COPYDATASTRUCT, WM_COPYDATA, WM_TRAY_SWITCH_CHARACTER, WindowsTrayIcon


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class SignedPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "prepared"
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        for obj, name, value in [(sig, "OFFICIAL_PUBLIC_KEY", public),
                                  (config, "ROOT", self.root / "body"),
                                  (config, "CHARACTERS_DIR", self.root / "body/characters")]:
            p = patch.object(obj, name, value);p.start();self.addCleanup(p.stop)
        write(self.package / "character.json", {"id": "sample", "display_name": "Sample", "renderer": {
            "current": "live2d", "model": "model/role.model3.json", "state_mappings": {}}})
        write(self.package / "voice/voice.json", {"model": {}, "reference": {}})
        write(self.package / "model/role.model3.json", {"FileReferences": {
            "Moc": "role.moc3", "Textures": [], "Expressions": [{"Name": "Happy", "File": "expression/Happy.exp3.json"}],
            "Motions": {"Idle": [{"File": "motion/Idle.motion3.json"}]}}})
        write(self.package / "model/expression/Happy.exp3.json", {"Parameters": []})
        write(self.package / "model/motion/Idle.motion3.json", {"Curves": []})
        (self.package / "model/role.moc3").write_bytes(b"test fixture, not a real model")

    def sign(self):
        manifest = sig.build_manifest(self.package)
        write(self.package / sig.SIGNATURE_FILE, {"manifest": manifest,
              "signature": self.key.sign(sig.DOMAIN + sig.canonical(manifest)).hex()})
        return manifest

    def test_valid_package_and_recheck(self):
        self.assertEqual(self.sign(), sig.verify_package(self.package))
        (self.package / "model/role.moc3").write_bytes(b"tampered")
        with self.assertRaises(sig.PackageSignatureError):sig.verify_package(self.package)

    def test_unsigned_live2d_rejected(self):
        with self.assertRaises(sig.PackageSignatureError):sig.authorize_character(self.package)

    def test_unsigned_static_allowed(self):
        write(self.package / "character.json", {"id": "sample", "renderer": {"current": "static"}})
        self.assertIsNone(sig.authorize_character(self.package))

    def test_renderer_tamper_cannot_bypass(self):
        self.sign()
        write(self.package / "character.json", {"id": "sample", "renderer": {"current": "static"}})
        with self.assertRaises(sig.PackageSignatureError):sig.authorize_character(self.package)

    def test_wrong_key(self):
        self.sign()
        other = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        with patch.object(sig, "OFFICIAL_PUBLIC_KEY", other), self.assertRaises(sig.PackageSignatureError):
            sig.verify_package(self.package)

    def test_added_and_removed_resources_rejected(self):
        self.sign()
        extra = self.package / "extra.txt";extra.write_text("extra")
        with self.assertRaises(sig.PackageSignatureError):sig.verify_package(self.package)
        extra.unlink();(self.package / "model/role.moc3").unlink()
        with self.assertRaises(sig.PackageSignatureError):sig.verify_package(self.package)

    def test_outside_model_resource_rejected(self):
        write(self.package / "model/role.model3.json", {"FileReferences": {"Moc": "../../outside.moc3"}})
        with self.assertRaises(sig.PackageSignatureError):self.sign()

    def test_malformed_model_resources_rejected(self):
        for refs in ({"Moc": "role.moc3", "Textures": 1}, {"Moc": "role.moc3", "Expressions": [1]},
                     {"Moc": "role.moc3", "Motions": {"Idle": 1}}, {}):
            with self.subTest(refs=refs):
                write(self.package / "model/role.model3.json", {"FileReferences": refs})
                with self.assertRaises(sig.PackageSignatureError):self.sign()

    def test_invalid_character_json_rejected(self):
        for value in ([], {"id": "../bad"}, {"id": "ok", "renderer": []}):
            write(self.package / "character.json", value)
            with self.assertRaises(sig.PackageSignatureError):sig.authorize_character(self.package)

    def test_model_path_must_be_signed_member(self):
        self.sign()
        sig.authorize_character(self.package, True, self.package / "model/role.model3.json")
        for path in (self.root / "outside.model3.json", self.package / "voice/voice.json"):
            with self.assertRaises(sig.PackageSignatureError):sig.authorize_character(self.package, True, path)

    def test_import_idempotent_preserves_bytes_and_id(self):
        manifest = self.sign()
        result = backup.import_signed_directory(self.package)
        self.assertEqual(result[0], "sample")
        self.assertEqual(result, backup.import_signed_directory(self.package))
        self.assertEqual(sig.verify_package(config.get_character_dir("sample")), manifest)

    def test_conflicting_existing_id_never_overwritten(self):
        self.sign();backup.import_signed_directory(self.package)
        (self.package / "model/role.moc3").write_bytes(b"new official model");self.sign()
        with self.assertRaises(ValueError):backup.import_signed_directory(self.package)
        self.assertEqual((config.get_character_dir("sample") / "model/role.moc3").read_bytes(), b"test fixture, not a real model")

    def test_export_import_preserves_signature(self):
        self.sign();backup.import_signed_directory(self.package)
        archive = backup.export_character("sample", self.root / "signed.zip")
        self.assertEqual(backup.import_character(archive)[0], "sample")
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(z.read(sig.SIGNATURE_FILE), (self.package / sig.SIGNATURE_FILE).read_bytes())

    def test_overrides_preserve_signed_payload(self):
        manifest = self.sign();backup.import_signed_directory(self.package)
        value = config.load_character_config("sample")
        value["personality"] = "custom"
        value["renderer"]["state_mappings"] = {"happy": {"expression": "Happy"}}
        config.save_character_config("sample", value)
        config.save_character_voice_config("sample", {"enabled": True, "generation": {"speed": 0.9}})
        self.assertEqual(config.load_character_config("sample")["personality"], "custom")
        self.assertEqual(config.load_character_config("sample")["renderer"]["state_mappings"], value["renderer"]["state_mappings"])
        self.assertTrue(config.load_voice_config("sample")["enabled"])
        self.assertEqual(sig.verify_package(config.get_character_dir("sample")), manifest)

    def test_override_cannot_change_renderer_type(self):
        self.sign();backup.import_signed_directory(self.package)
        value = config.load_character_config("sample");value["renderer"]["current"] = "static"
        config.save_character_config("sample", value)
        self.assertEqual(config.load_character_config("sample")["renderer"]["current"], "live2d")

    def test_static_character_id_safety(self):
        for value in ("../bad", "C:/bad", "a/b", None):
            with self.assertRaises(ValueError):config.get_character_dir(value)
        self.assertEqual(config.get_character_dir(""), config.CHARACTERS_DIR)

    def test_zip_unsafe_duplicates_and_links(self):
        for entries in (["../bad"], ["a.txt", "A.txt"], ["C:bad"], ["folder\\bad"], ["NUL.txt"]):
            archive = self.root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("character.json", '{"id":"test"}');z.writestr("voice/voice.json", '{}')
                for name in entries:
                    entry = zipfile.ZipInfo("placeholder");entry.filename = name
                    z.writestr(entry, "bad")
            with self.assertRaises(ValueError):backup.import_character(archive)

    def test_missing_components_falls_back_to_static(self):
        self.sign();backup.import_signed_directory(self.package)
        write(config.CHARACTERS_DIR / "static/character.json", {"id": "static"})
        with patch("core.environment.live2d_present", return_value=False):
            selected, errors = sig.choose_startup_character("sample", "")
        self.assertEqual(selected, "static");self.assertTrue(errors)

    def test_empty_body_is_valid_startup(self):
        self.assertEqual(sig.choose_startup_character("", ""), ("", []))

    def test_reselecting_same_character_still_checks_signature(self):
        self.sign();backup.import_signed_directory(self.package)
        from ui.app import DesktopPetApp
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.root = Mock();app.character_id = "sample"
        app._clear_character_theme_preview = Mock()
        (config.get_character_dir("sample") / "voice/voice.json").write_text("{} ")
        with patch("ui.app.messagebox.showerror") as error:
            self.assertFalse(app.switch_character("sample"))
        error.assert_called_once();app._clear_character_theme_preview.assert_not_called()

    def test_save_never_repairs_a_tampered_package_by_accident(self):
        self.sign();backup.import_signed_directory(self.package)
        directory = config.get_character_dir("sample")
        (directory / "voice/voice.json").write_text("{} ")
        with self.assertRaises(ValueError):config.save_character_config("sample", {"id": "sample"})

    def test_all_rejected_characters_leave_empty_safe_startup(self):
        self.sign();backup.import_signed_directory(self.package)
        with patch("core.environment.live2d_present", return_value=False):
            result, errors = sig.choose_startup_character("sample", {"corrupt": True})
        self.assertEqual(result, "");self.assertEqual(len(errors), 2)

    def test_json_duplicate_fields_rejected(self):
        (self.package / "character.json").write_text('{"id":"sample","id":"other"}')
        with self.assertRaises(ValueError):sig.authorize_character(self.package)

    def test_unsigned_creation_and_model_replacement_blocked_before_writes(self):
        from core.live2d_models import create_live2d_character, ModelUpdateTransaction
        with self.assertRaises(ValueError):create_live2d_character("new", "New", "", self.package)
        with self.assertRaises(ValueError):ModelUpdateTransaction("sample", self.package)
        self.assertFalse(config.CHARACTERS_DIR.exists())

    def test_invalid_requested_character_falls_back(self):
        write(config.CHARACTERS_DIR / "static/character.json", {"id": "static"})
        selected, errors = sig.choose_startup_character("../bad", "static")
        self.assertEqual(selected, "static");self.assertTrue(errors)

    def test_static_renderer_cannot_load_live2d_model(self):
        write(self.package / "character.json", {"id": "sample", "renderer": {"current": "static"}})
        from core.rendering import Live2DRenderer
        with self.assertRaises(ValueError):Live2DRenderer(self.package, "model/role.model3.json")


class LauncherProtocolTests(unittest.TestCase):
    def test_real_cross_process_copydata_without_visible_window(self):
        import os
        import subprocess
        import sys
        import threading
        from core.tray import request_switch_character
        if os.name != "nt":
            self.skipTest("Windows IPC")
        event = threading.Event()
        received = []
        def accept(request):
            received.append(request);event.set()
        title = "DesktopPetRouteBTest_" + str(os.getpid())
        tray = WindowsTrayIcon(title, Mock(), Mock(), Mock(), Mock(), on_switch_character=accept)
        self.assertTrue(tray.start())
        self.addCleanup(tray.stop)
        command = "from core.tray import request_switch_character; import sys; sys.exit(0 if request_switch_character(sys.argv[1], 'sample', request_id='cross-process') else 1)"
        child = subprocess.run([sys.executable, "-c", command, title], capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(child.returncode, 0, child.stderr.decode(errors="replace"))
        self.assertTrue(event.wait(1));self.assertEqual(received[0]["character_id"], "sample")

    def test_request_validation(self):
        valid = {"character_id": "角色_1", "package": "", "request_id": "abc"}
        self.assertEqual(decode_character_request(json.dumps(valid).encode()), valid)
        for changed in ({"character_id": "../bad"}, {"package": "relative"}, {"request_id": ""}):
            with self.assertRaises(ValueError):decode_character_request(json.dumps(dict(valid, **changed)).encode())
        with self.assertRaises(ValueError):decode_character_request(b"x" * 32769)

    def test_copydata_dispatches_valid_request(self):
        callback = Mock()
        tray = WindowsTrayIcon("test", Mock(), Mock(), Mock(), Mock(), on_switch_character=callback)
        payload = json.dumps({"character_id": "sample", "package": "", "request_id": "a"}).encode()
        buffer = ctypes.create_string_buffer(payload)
        data = COPYDATASTRUCT(WM_TRAY_SWITCH_CHARACTER, len(payload), ctypes.cast(buffer, ctypes.c_void_p))
        self.assertEqual(tray._window_proc(0, WM_COPYDATA, 0, ctypes.addressof(data)), 1)
        callback.assert_called_once()
        data.cbData = 32769
        self.assertEqual(tray._window_proc(0, WM_COPYDATA, 0, ctypes.addressof(data)), 0)

    def test_executable_and_cache_location(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d);exe = root / "我的桌宠.exe";exe.write_bytes(b"MZfixture")
            self.assertEqual(body.executable_path(root), exe.resolve())
            exe.write_bytes(b"invalid");self.assertIsNone(body.executable_path(exe))
            self.assertFalse(body.cache_path(root).is_relative_to(root))

    def test_launcher_exact_arguments(self):
        from scripts import character_launcher as launcher
        with tempfile.TemporaryDirectory() as d:
            root = Path(d);write(root / "character.json", {"id": "sample"});(root / "signature.ed25519").write_text("fixture")
            exe = root.parent / "我的桌宠.exe"
            with patch.object(launcher, "locate_body", return_value=exe), patch.object(launcher.subprocess, "Popen") as spawn:
                self.assertTrue(launcher.launch(root, Mock()))
            self.assertEqual(spawn.call_args.args[0], [str(exe), "--character", "sample", "--character-package", str(root.resolve())])


if __name__ == "__main__":
    unittest.main()
