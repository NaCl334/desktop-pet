import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest.mock import Mock, patch

import main  # noqa: F401
from core.config import create_character
from ui.app import DesktopPetApp


class CharacterCreationAvatarTests(unittest.TestCase):
    def test_import_and_create_share_bottom_row_after_initial_load_and_refresh(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.root = root
        app._control_page_parent = root
        app._character_manager_items = lambda: []
        app._setup_brand_canvas_scroll = Mock()
        app._control_characters()
        for refresh in (False, True):
            if refresh:
                app._refresh_character_manager_tab()
            row = app._character_manager_content.winfo_children()[-1]
            buttons = row.winfo_children()
            self.assertEqual({button.cget("text") for button in buttons},
                             {"导入角色包", "+新建角色"})
            self.assertTrue(all(button.pack_info()["side"] == "right" for button in buttons))

    def test_live2d_creation_dialog_does_not_expose_runtime_motion_ids(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.root = Mock()
        scan = {
            "recognized_count": 1,
            "unrecognized_count": 0,
            "recognized": [{"kind": "motion", "state": "idle",
                            "group": "Idle", "index": 0,
                            "file": "motions/待机.motion3.json"}],
        }
        with patch("ui.app.generate_character_id", return_value="sample"), patch(
            "ui.app.validate_model", return_value={"mapping_scan": scan}
        ), patch("ui.app.messagebox.askyesno", return_value=False) as confirm, patch(
            "ui.app.create_live2d_character"
        ) as create:
            result = app._create_character_from_form(
                "", "Sample", "", "", "", "", "", "", "", "",
                character_mode="live2d", live2d_model="sample.model3.json",
            )

        self.assertEqual(result, "")
        create.assert_not_called()
        text = confirm.call_args.args[1]
        self.assertIn("动作：待机 -> 待机", text)
        self.assertNotIn("动作组 + 序号", text)
        self.assertNotIn("Idle /", text)

    def test_source_mode_switch_hides_and_restores_static_portrait_help(self):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest("Tk unavailable: {}".format(error))
        root.withdraw()
        self.addCleanup(root.destroy)
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.root = root
        app._add_tooltip = Mock()
        app._brand_button = lambda parent, text, command, **kwargs: tk.Button(
            parent, text=text, command=command
        )

        def upload_field(parent, row, variable, spec, **kwargs):
            field = tk.Frame(parent)
            field.grid(row=row, column=1, columnspan=2)
            return field

        app._add_upload_field = upload_field
        app._build_inline_character_create_form(root)
        form = root.winfo_children()[0]
        mode = next(widget for widget in form.winfo_children()
                    if isinstance(widget, ttk.Combobox))
        hint = next(widget for widget in form.winfo_children()
                    if isinstance(widget, tk.Label)
                    and widget.cget("text").startswith("图片按情绪名"))
        self.assertEqual(mode.winfo_manager(), "")
        type_label = next(widget for widget in form.winfo_children()
                          if isinstance(widget, tk.Label) and widget.cget("text") == "角色类型")
        self.assertEqual(type_label.winfo_manager(), "")
        title = form.grid_slaves(row=0)[0]
        help_label = next(widget for widget in title.winfo_children()
                          if widget.cget("text") == "常用情绪？")
        self.assertEqual(hint.winfo_manager(), "grid")
        self.assertEqual(help_label.winfo_manager(), "pack")

        root.attributes("-alpha", 0)
        root.geometry("980x710+20000+20000")
        root.deiconify()
        root.update()
        field = next(widget for widget in form.grid_slaves(row=3)
                     if isinstance(widget, tk.Frame))
        personality = next(widget for widget in field.winfo_children()
                           if isinstance(widget, tk.Text))
        def field_bounds():
            root.update()
            return tuple((widget.winfo_rootx(), widget.winfo_rooty(),
                          widget.winfo_width(), widget.winfo_height())
                         for widget in (mode, personality))
        initial_bounds = field_bounds()

        for selected, hint_manager, help_manager in (
            ("Live2D 模型", "", "pack"),
            ("静态立绘", "grid", "pack"),
            ("Live2D 模型", "", "pack"),
        ):
            mode.set(selected)
            root.tk.call("event", "generate", mode._w, "<<ComboboxSelected>>")
            self.assertEqual(hint.winfo_manager(), hint_manager)
            self.assertEqual(help_label.winfo_manager(), help_manager)
            self.assertEqual(field_bounds(), initial_bounds)

    def _create_through_app(self, switch_after_create=None):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.switch_character = Mock()
        kwargs = {}
        if switch_after_create is not None:
            kwargs["switch_after_create"] = switch_after_create

        with patch("ui.app.generate_character_id", return_value="sample"), patch(
            "ui.app.create_character"
        ) as create, patch("ui.app.messagebox.showinfo") as showinfo:
            created_id = app._create_character_from_form(
                "",
                "Sample",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                **kwargs
            )
        return created_id, app.switch_character, create, showinfo

    def test_inline_creation_can_leave_current_character_unchanged(self):
        created_id, switch_character, create, showinfo = self._create_through_app(
            switch_after_create=False
        )

        self.assertEqual(created_id, "sample")
        switch_character.assert_not_called()
        create.assert_called_once()
        self.assertIn("已创建。", showinfo.call_args.args[1])

    def test_existing_creation_flow_still_switches_by_default(self):
        created_id, switch_character, create, showinfo = self._create_through_app()

        self.assertEqual(created_id, "sample")
        switch_character.assert_called_once_with("sample")
        create.assert_called_once()
        self.assertIn("已创建并切换。", showinfo.call_args.args[1])

    def test_card_avatar_is_copied_and_recorded_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "portrait-card.png"
            source.write_bytes(b"avatar")

            with patch(
                "core.config.get_character_dir",
                side_effect=lambda character_id: root / character_id,
            ):
                character_dir = create_character(
                    "sample",
                    "Sample",
                    card_avatar=str(source),
                )

            avatar_path = character_dir / "ui" / "avatar.png"
            config = json.loads(
                (character_dir / "character.json").read_text(encoding="utf-8")
            )
            self.assertEqual(avatar_path.read_bytes(), b"avatar")
            self.assertEqual(config["card_avatar"], "ui/avatar.png")
            self.assertFalse(
                (character_dir / "portraits" / "mode_assets" / "small").exists()
            )
            self.assertFalse(
                (character_dir / "portraits" / "candidates").exists()
            )
            self.assertFalse((character_dir / "portraits" / "faces").exists())
            self.assertFalse((character_dir / "voice" / "models").exists())
            self.assertFalse(
                (character_dir / "voice" / "references").exists()
            )
            self.assertEqual(config["portrait_rules"]["active_outfit_id"], "default")
            self.assertEqual(
                config["portrait_outfits"],
                [
                    {
                        "id": "default",
                        "name": "默认",
                        "folder_name": "默认",
                        "portrait_assets": {},
                        "local_viewport": {},
                    }
                ],
            )
            self.assertNotIn(
                "full_portrait_folder",
                config["portrait_rules"],
            )
            self.assertNotIn(
                "window_portrait_folder",
                config["portrait_rules"],
            )

    def test_creation_does_not_copy_legacy_portrait_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "candidate-source"
            source.mkdir()
            (source / "legacy.png").write_bytes(b"candidate")

            with patch(
                "core.config.get_character_dir",
                side_effect=lambda character_id: root / character_id,
            ):
                character_dir = create_character(
                    "sample",
                    "Sample",
                    portrait_folder=str(source),
                )

            self.assertFalse(
                (character_dir / "portraits" / "candidates").exists()
            )

    def test_creation_writes_default_outfit_and_portrait_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "待机.png").write_bytes(b"idle")
            (source / "高兴.png").write_bytes(b"happy")
            (source / "无语.png").write_bytes(b"speechless")
            (source / "低电量.png").write_bytes(b"event-only")
            (source / "发现好东西.png").write_bytes(b"event-only")

            def copy_image(source_path, destination_path, mode="full"):
                destination_path = Path(destination_path)
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                destination_path.write_bytes(Path(source_path).read_bytes())
                return destination_path

            with patch(
                "core.config.get_character_dir",
                side_effect=lambda character_id: root / character_id,
            ), patch("core.config.enhance_portrait", side_effect=copy_image):
                character_dir = create_character(
                    "sample",
                    "Sample",
                    full_portrait=str(source),
                    require_idle_portrait=True,
                    normalize_emotion_names=True,
                )

            config = json.loads(
                (character_dir / "character.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                list(config["states"]),
                ["idle", "happy", "speechless"],
            )
            self.assertFalse(
                (
                    character_dir
                    / "portraits"
                    / "mode_assets"
                    / "full"
                    / "低电量.png"
                ).exists()
            )
            self.assertFalse(
                (
                    character_dir
                    / "portraits"
                    / "mode_assets"
                    / "full"
                    / "发现好东西.png"
                ).exists()
            )
            self.assertEqual(
                config["states"]["idle"]["portrait_assets"],
                {"full": "outfits/默认/待机.png"},
            )
            self.assertEqual(
                config["portrait_outfits"][0]["portrait_assets"],
                {
                    "idle": {"full": "outfits/默认/待机.png"},
                    "happy": {"full": "outfits/默认/高兴.png"},
                    "speechless": {"full": "outfits/默认/无语.png"},
                },
            )
            self.assertTrue(
                (
                    character_dir
                    / "portraits"
                    / "outfits"
                    / "默认"
                    / "待机.png"
                ).is_file()
            )
            self.assertFalse((character_dir / "ui").exists())
            self.assertFalse(
                (character_dir / "portraits" / "candidates").exists()
            )
            self.assertFalse((character_dir / "portraits" / "faces").exists())

    def test_invalid_card_avatar_is_rejected_before_creating_character(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "avatar.txt"
            source.write_text("not an image", encoding="utf-8")
            character_dir = root / "sample"

            with patch(
                "core.config.get_character_dir",
                side_effect=lambda character_id: character_dir,
            ):
                with self.assertRaises(ValueError):
                    create_character(
                        "sample",
                        "Sample",
                        card_avatar=str(source),
                    )

            self.assertFalse(character_dir.exists())


if __name__ == "__main__":
    unittest.main()
