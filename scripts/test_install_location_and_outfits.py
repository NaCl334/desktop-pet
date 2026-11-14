"""Installation destination, multi-outfit import and character package regressions."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import config, install_location
from ui.app import DesktopPetApp


class OutfitImportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.target = self.root / "sample"

    def image(self, name, content):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def create(self):
        def copy_image(source, target, **kwargs):
            Path(target).write_bytes(Path(source).read_bytes())
        with patch.object(config, "get_character_dir", return_value=self.target), \
                patch.object(config, "enhance_portrait", side_effect=copy_image):
            config.create_character("sample", "Sample", full_portrait=str(self.source),
                                    normalize_emotion_names=True, require_idle_portrait=True)
        return json.loads((self.target / "character.json").read_text(encoding="utf-8"))

    def test_named_outfits_keep_same_emotions_separate(self):
        for outfit in ("校服", "私服"):
            self.image(outfit + "/待机.png", outfit.encode())
            self.image(outfit + "/高兴.png", (outfit + "happy").encode())
        character = self.create()
        self.assertEqual({item["name"] for item in character["portrait_outfits"]}, {"校服", "私服"})
        self.assertFalse((self.target / "portraits/outfits/默认").exists())
        for outfit in DesktopPetApp._portrait_outfits(character):
            path = self.target / "portraits" / outfit["portrait_assets"]["happy"]["full"]
            self.assertEqual(path.read_bytes(), (outfit["name"] + "happy").encode())

    def test_actions_only_require_one_idle_per_outfit(self):
        self.image("校服/站立/待机.png", b"idle")
        self.image("校服/站立/高兴.png", b"standing")
        self.image("校服/侧身/高兴.png", b"side")
        character = self.create()
        outfit = DesktopPetApp._portrait_outfits(character)[0]
        self.assertEqual(len(outfit["actions"]), 2)
        self.assertTrue(outfit["active_action_id"])
        for action in outfit["actions"]:
            for assets in action["portrait_assets"].values():
                self.assertTrue((self.target / "portraits" / assets["full"]).is_file())
        side = next(item for item in outfit["actions"] if item["name"] == "侧身")
        self.assertNotIn("idle", side["portrait_assets"])

    def test_outfit_without_idle_is_rejected_before_character_creation(self):
        self.image("校服/待机.png", b"idle")
        self.image("私服/高兴.png", b"happy")
        with self.assertRaisesRegex(ValueError, "私服"):
            self.create()
        self.assertFalse(self.target.exists())

    def test_flat_folder_and_single_image_wrapper(self):
        self.image("校服/立绘/待机.png", b"idle")
        self.image("校服/立绘/高兴.png", b"happy")
        outfit = self.create()["portrait_outfits"][0]
        self.assertEqual(outfit["name"], "校服")
        self.assertEqual(outfit["actions"], [])
        self.assertIn("idle", outfit["portrait_assets"])

    def test_export_uses_card_character_rather_than_active_character(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.character_id = "active"
        app._control_status = Mock()
        with patch("ui.app.load_character_config", return_value={"display_name": "Selected"}), \
                patch("ui.app.filedialog.asksaveasfilename", return_value="sample.zip"), \
                patch("ui.app.export_character") as export:
            app._export_current_character_package("selected")
        export.assert_called_once_with("selected", "sample.zip")

    def test_import_refreshes_current_role_page_without_destroying_it(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app._control_current_page = "characters"
        app._refresh_character_manager_tab = Mock()
        app._invalidate_control_page = Mock()
        app._control_status = Mock()
        with patch("ui.app.filedialog.askopenfilename", return_value="role.zip"), \
                patch("ui.app.import_character", return_value=("new", "New")), \
                patch("ui.app.messagebox.showinfo"):
            app._import_character_package()
        app._refresh_character_manager_tab.assert_called_once_with(create_mode=False)
        app._invalidate_control_page.assert_not_called()


class InstallDestinationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.target = self.root / "install"
        self.patch = patch.object(install_location, "ROOT", self.source)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_nonempty_directory_is_never_overwritten(self):
        self.target.mkdir()
        original = self.target / "document.txt"
        original.write_text("keep")
        with patch.object(install_location.sys, "frozen", True, create=True):
            with self.assertRaisesRegex(install_location.SetupError, "不为空"):
                install_location.install_in_directory(self.target, "desktop")
        self.assertEqual(original.read_text(), "keep")

    def test_new_destination_is_recognized_by_uninstaller(self):
        from core.uninstaller import is_release_install
        (self.source / "config").mkdir()
        (self.source / "config/app_config.json").write_text("{}")
        executable = self.source / "我的桌宠.exe"
        executable.write_bytes(b"fixture")

        def spawn(command, **kwargs):
            Path(command[-1]).write_text(json.dumps({"kind": "result", "success": True}) + "\n")
            return SimpleNamespace(poll=lambda: 0, wait=lambda **kwargs: 0, returncode=0)

        with patch.object(install_location.sys, "frozen", True, create=True), \
                patch.object(install_location.sys, "executable", str(executable)), \
                patch.object(install_location.subprocess, "Popen", side_effect=spawn):
            install_location.install_in_directory(self.target, "desktop")
        self.assertTrue(is_release_install(self.target))
        self.assertFalse((self.target / ".desktop_pet_setup.json").exists())

    def test_worker_marks_only_destination_completed(self):
        (self.source / "temp").mkdir()
        (self.source / "data").mkdir()
        path = self.source / "temp/status.jsonl"
        with patch("core.installer.install_profile", return_value=None) as install, \
                patch("core.config.ROOT", self.source):
            code = install_location.run_setup_worker("desktop", path)
        self.assertEqual(code, 0)
        install.assert_called_once()
        settings = json.loads((self.source / "data/settings.json").read_text())
        self.assertTrue(settings["setup_wizard_completed"])
        self.assertFalse(settings["voice_output"])
        self.assertTrue(json.loads(path.read_text().splitlines()[-1])["success"])

    def test_worker_failure_does_not_mark_setup_completed(self):
        (self.source / "temp").mkdir()
        (self.source / "data").mkdir()
        with patch("core.installer.install_profile", side_effect=RuntimeError("failed")):
            code = install_location.run_setup_worker("full", self.source / "temp/status.jsonl")
        self.assertEqual(code, 1)
        self.assertFalse((self.source / "data/settings.json").exists())

    def test_worker_rejects_progress_outside_installation_directory(self):
        with self.assertRaises(install_location.SetupError):
            install_location.run_setup_worker("desktop", self.root / "outside.jsonl")


if __name__ == "__main__":
    unittest.main()
