import copy
import json
import threading
import tempfile
import time
import tkinter as tk
import unittest
from contextlib import ExitStack
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from core.config import _character_portrait_metadata, _copy_character_portrait_folder
from core.conversation import ConversationClient
from core.emotion import (
    EMOTION_FALLBACKS, EMOTION_GROUPS, PRESET_EMOTIONS,
    emotion_fallback_chain, select_state, state_from_model_result,
)
from core.rendering import Live2DRenderer, StaticPortraitRenderer
from ui.app import BRAND_COLORS, DesktopPetApp
from ui.emotion_selector import EmotionSelector
from main import _configure_tk_runtime


class EmotionCatalogTests(unittest.TestCase):
    def test_catalog_and_groups_are_complete_disjoint_and_ordered(self):
        self.assertEqual(len(PRESET_EMOTIONS), 39)
        self.assertEqual(tuple(EMOTION_GROUPS), ("正向", "中性", "负向"))
        self.assertEqual([len(keys) for keys in EMOTION_GROUPS.values()], [10, 13, 16])
        keys = [key for group in EMOTION_GROUPS.values() for key in group]
        self.assertEqual(keys, list(PRESET_EMOTIONS))
        self.assertEqual(set(EMOTION_FALLBACKS), set(keys))
        self.assertNotIn("idle", keys)
        self.assertEqual(PRESET_EMOTIONS["sparkling_eyes"]["label"], "眼睛发光")

    def test_specific_emotions_are_not_collapsed(self):
        cases = {
            "我感到愉悦": "pleased", "我很高兴": "happy", "我很兴奋": "excited",
            "我好喜欢你": "affectionate", "我眼睛发光了": "sparkling_eyes",
            "我很期待明天": "expectant", "我感到不满": "displeased",
            "我生气了": "angry", "我愤怒了": "furious", "我觉得意外": "unexpected",
            "我很惊讶": "surprised", "我震惊了": "shocked",
            "我担心你": "worried", "我很不安": "uneasy",
            "我伤心地哭泣": "crying", "我很认真": "thinking",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(select_state(text), expected)

    def test_pretend_crying_and_actual_crying_are_separate(self):
        self.assertEqual(select_state("呜呜，你居然不理我了！"), "fake_crying")
        self.assertEqual(select_state("我只是在假哭啦"), "fake_crying")
        self.assertEqual(select_state("呜呜，我真的伤心到泪流满面"), "crying")
        self.assertEqual(select_state("呜呜"), "idle")

    def test_quotes_other_people_and_negated_emotions_do_not_drive_face(self):
        for text in ("你哭泣的时候可以找我", "她很伤心", "如果你害怕，我可以陪你",
                     "“哭泣”是一个词", "我并不生气", "我没有感到伤心", "今天是周二"):
            with self.subTest(text=text):
                self.assertEqual(select_state(text), "idle")

    def test_model_markers_custom_states_and_explicit_idle(self):
        self.assertEqual(state_from_model_result("[emotion:idle]我很高兴", []), "idle")
        self.assertEqual(state_from_model_result("[emotion:fake_crying]"), "fake_crying")
        self.assertIsNone(state_from_model_result("[emotion:unknown]"))
        self.assertEqual(select_state("我有点发懵", ["custom"], {"custom": ["发懵"]}), "custom")
        self.assertEqual(state_from_model_result("[emotion:playful]", ["playful"]), "playful")
        self.assertNotIn("playful", PRESET_EMOTIONS)
        self.assertNotIn("passionate", PRESET_EMOTIONS)

    def test_custom_emotion_prompt_contains_matching_words(self):
        client = ConversationClient({}, runtime_context=SimpleNamespace(format_for_model=lambda: "test"))
        prompt = client.build_messages([], {"states": {"emotion_dazed": {
            "label": "发懵", "keywords": ["脑袋空白", "没反应过来"],
        }}})[0]["content"]
        self.assertIn("emotion_dazed（发懵）", prompt)
        self.assertIn("脑袋空白、没反应过来", prompt)

    def test_custom_scenario_is_sent_to_reply_model_after_json_roundtrip(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        config = {"states": {"emotion_dazed": {"label": "发懵",
            "usage_scenario": "角色面对突如其来的信息，一时没有反应过来时使用。"}}}
        definitions = app._configured_state_definitions(json.loads(json.dumps(config)))
        self.assertEqual(definitions["emotion_dazed"]["usage_scenario"],
                         config["states"]["emotion_dazed"]["usage_scenario"])
        client = ConversationClient({}, runtime_context=SimpleNamespace(format_for_model=lambda: "test"))
        prompt = client.build_messages([], {"states": definitions})[0]["content"]
        self.assertIn("使用场景：角色面对突如其来的信息", prompt)

    def test_scenario_analysis_uses_current_model_and_character_without_keywords(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.app_config = {"conversation_model": {"model": "sample"}}
        with patch("ui.app.ConversationClient") as client:
            client.return_value._complete_messages.return_value = "角色难以理解眼前的反常举动时使用。"
            result = app._generate_emotion_usage_scenario("无语", {"personality": "克制直接"})
            client.assert_called_once_with({"model": "sample"})
            messages = client.return_value._complete_messages.call_args.args[0]
            self.assertIn("角色自身", messages[0]["content"])
            self.assertIn("克制直接", messages[1]["content"])
            self.assertEqual(result, "角色难以理解眼前的反常举动时使用。")

    def test_deliberate_non_transitive_fallbacks(self):
        self.assertEqual(emotion_fallback_chain("crying"), ("crying", "sad", "dejected", "idle"))
        self.assertEqual(emotion_fallback_chain("fake_crying"), ("fake_crying", "idle"))
        self.assertEqual(emotion_fallback_chain("thinking"), ("thinking", "idle"))
        self.assertEqual(emotion_fallback_chain("playful"), ("playful", "idle"))

    def test_prompt_has_all_emotions_and_own_emotion_rules(self):
        client = ConversationClient({}, runtime_context=SimpleNamespace(
            format_for_model=lambda: "test runtime"))
        config = {"states": {"playful": {"label": "调皮"}}}
        original = copy.deepcopy(config)
        prompt = client.build_messages([], config)[0]["content"]
        for key, definition in PRESET_EMOTIONS.items():
            self.assertIn("{}（{}）".format(key, definition["label"]), prompt)
        self.assertIn("idle（待机）", prompt)
        self.assertIn("playful（调皮）", prompt)
        self.assertIn("你自身此刻表达", prompt)
        self.assertIn("假哭", prompt)
        self.assertEqual(config, original)

    def test_old_named_images_import_without_losing_retired_emotions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "portraits" / "outfits" / "默认"
            source.mkdir()
            target.mkdir(parents=True)
            for label in ("待机", "担心", "调皮", "情动", "眼睛发光", "假哭"):
                Image.new("RGBA", (8, 12), "red").save(source / (label + ".png"))
            with patch("core.config.enhance_portrait",
                       side_effect=lambda src, dst, **kw: Image.open(src).save(dst)):
                _copy_character_portrait_folder(source, target)
            states, assets = _character_portrait_metadata(target)
            self.assertEqual(set(states), {"idle", "worried", "playful", "passionate",
                                           "sparkling_eyes", "fake_crying"})
            self.assertEqual(states["worried"]["label"], "担忧")
            self.assertEqual(states["playful"]["category"], "custom")
            self.assertEqual(states["passionate"]["category"], "custom")
            self.assertTrue((source / "担心.png").is_file())
            self.assertEqual(set(assets), set(states))


class ReplyEmotionResourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = DesktopPetApp.__new__(DesktopPetApp)
        self.app.character_dir = self.root
        self.app.character_id = "test"
        self.app.root = Mock()
        self.app.set_state = Mock()
        self.app.renderer = StaticPortraitRenderer(self.root)
        self.app._refresh_state_buttons = Mock()
        self.app.character_config = {
            "states": {}, "portrait_rules": {},
            "portrait_outfits": [{
                "id": "default", "name": "默认", "folder_name": "默认",
                "portrait_assets": {}, "actions": [
                    {"id": "wave", "name": "挥手", "folder_name": "挥手", "portrait_assets": {}}
                ],
            }],
        }
        self.outfit = self.app.character_config["portrait_outfits"][0]
        self.add_asset(self.outfit, "idle")

    def add_asset(self, item, state):
        relative = "outfits/默认/{}/{}.png".format(item.get("id", ""), state)
        path = self.root / "portraits" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (8, 12), "red").save(path)
        item["portrait_assets"][state] = {"full": relative}
        return path

    def test_missing_model_emotion_is_recognized_then_substituted(self):
        self.add_asset(self.outfit, "sad")
        self.assertEqual(self.app._state_from_reply("[emotion:crying]文本"), "crying")
        original = copy.deepcopy(self.app.character_config)
        self.app._apply_reply_behavior("[emotion:crying]文本")
        self.assertEqual(self.app.last_reply_emotion, "crying")
        self.app.set_state.assert_called_once_with("sad")
        self.assertEqual(self.app.character_config, original)
        self.assertNotIn("crying", self.outfit["portrait_assets"])

    def test_saved_custom_emotion_switches_its_image_from_marker_or_keyword(self):
        key = "emotion_dazed"
        self.app.character_config["states"][key] = {
            "label": "发懵", "enabled": True, "category": "custom", "keywords": ["脑袋空白"],
        }
        image = self.add_asset(self.outfit, key)
        self.app.character_config = json.loads(json.dumps(self.app.character_config))
        for reply in ("[emotion:emotion_dazed]一时没反应过来。", "我脑袋空白了。"):
            self.app.set_state.reset_mock()
            self.app._apply_reply_behavior(reply)
            self.app.set_state.assert_called_once_with(key)
            self.assertEqual(self.app.last_reply_emotion, key)
        definition = self.app._configured_state_definitions()[key]
        self.assertEqual(self.app._portrait_mode_asset_path(key, "full", self.root,
                         definition=definition), image.resolve())

    def test_auto_prefers_exact_in_another_action_over_current_similar(self):
        self.app.character_config["portrait_rules"]["auto_actions"] = True
        self.add_asset(self.outfit, "sad")
        self.add_asset(self.outfit["actions"][0], "crying")
        with patch("ui.app.save_character_config") as save:
            self.app._apply_reply_behavior("[emotion:crying]文本")
        self.assertEqual(self.app._active_portrait_action_id(self.app.character_config), "wave")
        self.app.set_state.assert_called_once_with("crying")
        save.assert_not_called()

    def test_auto_can_choose_similar_when_all_exact_images_are_missing(self):
        self.app.character_config["portrait_rules"]["auto_actions"] = True
        self.add_asset(self.outfit["actions"][0], "sad")
        self.app._apply_reply_behavior("[emotion:crying]文本")
        self.app.set_state.assert_called_once_with("sad")
        self.assertEqual(self.app._active_portrait_action_id(self.app.character_config), "wave")

    def test_manual_action_never_uses_default_action_emotion(self):
        self.outfit["active_action_id"] = "wave"
        self.add_asset(self.outfit, "crying")
        self.add_asset(self.outfit["actions"][0], "idle")
        self.app._apply_reply_behavior("[emotion:crying]文本")
        self.app.set_state.assert_called_once_with("idle")
        self.assertEqual(self.app._active_portrait_action_id(self.app.character_config), "wave")

    def test_fake_crying_does_not_fall_back_to_a_sad_face(self):
        self.add_asset(self.outfit, "sad")
        self.add_asset(self.outfit, "crying")
        self.app._apply_reply_behavior("[emotion:fake_crying]呜呜")
        self.app.set_state.assert_called_once_with("idle")

    def test_missing_or_outside_outfit_files_cannot_satisfy_fallback(self):
        self.outfit["portrait_assets"]["crying"] = {"full": "missing.png"}
        other = copy.deepcopy(self.outfit)
        other.update(id="school", name="校服")
        self.add_asset(other, "crying")
        self.app.character_config["portrait_outfits"].append(other)
        self.app._apply_reply_behavior("[emotion:crying]文本")
        self.app.set_state.assert_called_once_with("idle")

    def test_legacy_label_state_key_and_assets_survive(self):
        self.app.character_config["states"]["old_worry"] = {"label": "担心"}
        self.add_asset(self.outfit, "old_worry")
        self.app._apply_reply_behavior("[emotion:worried]文本")
        self.app.set_state.assert_called_once_with("old_worry")

    def test_model_idle_overrides_emotional_words(self):
        self.add_asset(self.outfit, "happy")
        self.app._apply_reply_behavior("[emotion:idle]这里提到了高兴")
        self.app.set_state.assert_called_once_with("idle")

    def test_live2d_expression_fallback_keeps_explicit_mappings_and_legacy(self):
        renderer = Live2DRenderer.__new__(Live2DRenderer)
        renderer._behavior_preferences = {"emotion_expressions": {"crying": ["expression_cry"]}}
        renderer._parameters_by_state = {}
        renderer._expression_by_state = {
            "idle": "idle", "sad": "sad", "playful": "playful", "expression_cry": "cry"
        }
        renderer._renderer_config = {}
        self.assertEqual(renderer.expression_candidates("crying"), ["expression_cry"])
        renderer._behavior_preferences = {}
        self.assertEqual(renderer.expression_candidates("crying"), ["sad"])
        self.assertEqual(renderer.expression_candidates("playful"), ["playful"])
        self.assertEqual(renderer.expression_candidates("fake_crying"), ["idle"])


class HiddenEmotionSelectorTests(unittest.TestCase):
    def setUp(self):
        _configure_tk_runtime()
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.variable = tk.StringVar(self.root)

    def selector(self, keys=None):
        widget = EmotionSelector(
            self.root, self.variable, PRESET_EMOTIONS if keys is None else keys,
            BRAND_COLORS, width=400,
        )
        widget.pack(fill="x")
        self.root.update_idletasks()
        return widget

    def test_default_group_three_slots_and_cyclic_navigation(self):
        widget = self.selector()
        self.assertEqual(widget.group, "正向")
        self.assertEqual(self.variable.get(), "愉悦")
        self.assertEqual([slot.cget("text") for slot in widget.slots], ["得瑟", "愉悦", "高兴"])
        widget.previous.invoke()
        self.assertEqual(self.variable.get(), "得瑟")
        widget.next.invoke()
        self.assertEqual(self.variable.get(), "愉悦")
        widget._wheel(SimpleNamespace(delta=-120))
        self.assertEqual(self.variable.get(), "高兴")
        self.assertEqual(widget._wheel(SimpleNamespace(delta=120)), "break")
        self.assertEqual(self.variable.get(), "愉悦")

    def test_group_selection_filters_and_remembers_position(self):
        widget = self.selector()
        widget.step(5)
        widget.tabs["中性"].invoke()
        widget.step(2)
        self.assertEqual(self.variable.get(), "假哭")
        widget.tabs["负向"].invoke()
        self.assertEqual(self.variable.get(), "不满")
        widget.tabs["正向"].invoke()
        self.assertEqual(self.variable.get(), "期待")
        widget.tabs["中性"].invoke()
        self.assertEqual(self.variable.get(), "假哭")

    def test_filtered_empty_group_does_not_auto_change_default(self):
        widget = self.selector(["crying"])
        self.assertEqual(widget.group, "正向")
        self.assertEqual(self.variable.get(), "")
        self.assertEqual(widget.slots[1].cget("text"), "本组已全部添加")
        self.assertEqual(widget.next.cget("state"), "disabled")
        widget.tabs["负向"].invoke()
        self.assertEqual(self.variable.get(), "哭泣")
        widget.step(1)
        self.assertEqual(self.variable.get(), "哭泣")

    def test_labels_fit_carousel_at_supported_dialog_widths(self):
        host = tk.Frame(self.root)
        host.place(x=0, y=0, width=360, height=180)
        widget = EmotionSelector(host, self.variable, PRESET_EMOTIONS, BRAND_COLORS)
        widget.place(x=0, y=0, width=360, height=130)
        for width in (360, 432, 520):
            host.place_configure(width=width)
            widget.place_configure(width=width)
            self.root.update_idletasks()
            # Withdrawn windows do not deliver the normal map/configure cycle.
            widget.carousel.grid_rowconfigure(0, weight=1)
            self.root.update_idletasks()
            for group in EMOTION_GROUPS:
                widget.select_group(group)
                for position in range(len(widget.groups[group])):
                    widget.step(1)
                    self.root.update_idletasks()
                    # Geometry remains measurable on a withdrawn toplevel.
                    for slot in widget.slots:
                        self.assertGreater(slot.winfo_width(), 50)
                        self.assertLessEqual(
                            slot.winfo_x() + slot.winfo_width(),
                            widget.carousel.winfo_width(),
                        )
                        self.assertGreaterEqual(slot.winfo_height(), slot.winfo_reqheight())
                        self.assertLessEqual(slot.winfo_reqheight(), widget.carousel.winfo_reqheight())

    def test_natural_dialog_width_displays_long_emotion_in_full(self):
        from tkinter import font as tkfont
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", 0)
        self.root.geometry("+20000+20000")
        self.root.deiconify()
        for scale in (1, 1.25, 1.5, 2):
            self.root.tk.call("tk", "scaling", scale * 96 / 72)
            widget = self.selector()
            widget.step(4)
            self.root.update()
            label = widget.slots[1]
            font = tkfont.Font(root=self.root, font=label.cget("font"))
            self.assertEqual(label.cget("text"), "眼睛发光")
            self.assertGreaterEqual(label.winfo_width(), font.measure("眼睛发光") + 4)
            self.assertGreaterEqual(label.winfo_height(), label.winfo_reqheight())
            widget.destroy()

    def descendants(self, parent):
        result = []
        for child in parent.winfo_children():
            result.append(child)
            result.extend(self.descendants(child))
        return result

    def button(self, parent, label):
        return next(child for child in self.descendants(parent)
                    if isinstance(child, tk.Button) and child.cget("text") == label)

    def test_custom_emotion_adds_without_model_and_survives_runtime_recognition(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.root = self.root
        app._center_toplevel = Mock()
        app._refresh_portrait_manager = Mock()
        app._control_status = Mock()
        context = {"portrait_state_rows": []}
        original_toplevel = tk.Toplevel
        dialogs = []
        def make_dialog(parent):
            dialog = original_toplevel(parent)
            dialog.withdraw()
            dialog.grab_set = Mock()
            dialogs.append(dialog)
            return dialog
        with tempfile.TemporaryDirectory() as folder, patch(
                "ui.app.tk.Toplevel", side_effect=make_dialog), patch.object(
                app, "_generate_portrait_emotion_keywords") as generate:
            image = Path(folder) / "dazed.png"
            Image.new("RGBA", (8, 12), "red").save(image)
            with patch("ui.app.filedialog.askopenfilename", return_value=str(image)), \
                    patch.object(app, "_portrait_ratio_info", return_value={}), \
                    patch.object(app, "_portrait_ratio_text", return_value=""), \
                    patch.object(app, "_portrait_ratio_status_color", return_value="#000000"):
                app._add_portrait_manager_state(context)
                dialog = dialogs[-1]
                texts = [widget.cget("text") for widget in self.descendants(dialog)
                         if isinstance(widget, tk.Label)]
                self.assertIn("立绘", texts)
                self.assertNotIn("全身立绘", texts)
                entries = [widget for widget in self.descendants(dialog) if isinstance(widget, tk.Entry)]
                entries[0].insert(0, "发懵")
                usage = next(widget for widget in self.descendants(dialog) if isinstance(widget, tk.Text))
                usage.insert("1.0", "角色面对突发情况，脑袋空白、没反应过来时使用。")
                self.button(dialog, "选择图片").invoke()
                self.button(dialog, "确认添加").invoke()
            generate.assert_not_called()
            row = context["portrait_state_rows"][0]
            row["portrait_assets"] = {"full": "dazed.png"}
            app._append_portrait_manager_state(context, "idle", {
                "label": "待机", "keywords": [], "category": "special"})
            states = app._collect_portrait_states(context["portrait_state_rows"])
            key = row["key"].get()
            self.assertIn("角色面对突发情况", states[key]["usage_scenario"])
            app.character_config = {"states": states, "portrait_outfits": [{
                "id": "default", "name": "默认", "portrait_assets": {key: {"full": "dazed.png"}},
            }]}
            app.character_dir = Path(folder)
            self.assertEqual(app._state_from_reply("[emotion:{}]一时没反应过来。".format(key)), key)
            self.assertEqual(app._state_from_reply("我有点发懵。"), key)
            self.assertEqual(row["pending"]["full"], image)

    def wait_for_analysis(self, button):
        deadline = time.monotonic() + 3
        while str(button.cget("state")) == "disabled" and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(str(button.cget("state")), "normal")

    def test_analyze_button_returns_editable_scenario_and_retry_keeps_manual_text(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        name = tk.StringVar(self.root, value="发懵")
        panel, editor = app._emotion_usage_editor(self.root, name, {"personality": "直接"}, "手写内容")
        panel.pack()
        button = self.button(panel, "分析使用场景")
        with patch.object(app, "_generate_emotion_usage_scenario", side_effect=RuntimeError("failed")):
            button.invoke()
            self.wait_for_analysis(button)
        self.assertEqual(editor.get("1.0", "end-1c"), "手写内容")
        with patch.object(app, "_generate_emotion_usage_scenario", return_value="角色尚未反应过来时使用。") as analyze:
            button.invoke()
            self.wait_for_analysis(button)
            analyze.assert_called_once_with("发懵", {"personality": "直接"})
        self.assertEqual(editor.get("1.0", "end-1c"), "角色尚未反应过来时使用。")
        editor.insert("end", "不等同于害怕。")
        self.assertTrue(editor.get("1.0", "end-1c").endswith("不等同于害怕。"))

    def test_pending_analysis_does_not_overwrite_renamed_or_edited_content_and_can_close(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        for change in ("name", "text", "close"):
            name = tk.StringVar(self.root, value="发懵")
            panel, editor = app._emotion_usage_editor(self.root, name, {}, "原说明")
            panel.pack()
            button = self.button(panel, "分析使用场景")
            ready, release = threading.Event(), threading.Event()
            def generate(*_args):
                ready.set()
                release.wait(2)
                return "旧请求结果"
            with patch.object(app, "_generate_emotion_usage_scenario", side_effect=generate):
                button.invoke()
                self.assertTrue(ready.wait(1))
                if change == "name":
                    name.set("无语")
                elif change == "text":
                    editor.insert("end", "手动修改")
                else:
                    panel.destroy()
                release.set()
                if change != "close":
                    self.wait_for_analysis(button)
                    self.assertNotIn("旧请求结果", editor.get("1.0", "end-1c"))
                    panel.destroy()
                self.root.update()

    def test_modified_scenario_survives_outfit_and_action_draft_caches(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        context = {"portrait_state_rows": []}
        row = app._append_portrait_manager_state(context, "emotion_dazed", {
            "label": "发懵", "keywords": ["发懵"], "usage_scenario": "旧说明"})
        clone = app._clone_portrait_state_row_for_outfit(row, None)
        self.assertEqual(clone["usage_scenario"], "旧说明")
        context["portrait_action_rows"] = {("default", "wave"): [clone]}
        app._set_portrait_emotion_usage(context, row, "用户修改后的说明")
        self.assertEqual(clone["usage_scenario"], "用户修改后的说明")
        self.assertEqual(context["character_config"]["states"]["emotion_dazed"]["usage_scenario"],
                         "用户修改后的说明")

    def test_existing_custom_emotion_can_reopen_edit_and_apply_scenario(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.root = self.root
        app._center_toplevel = Mock()
        app._refresh_portrait_manager = Mock()
        app._change_portrait_manager_emotion = Mock()
        context = {"portrait_state_rows": []}
        row = app._append_portrait_manager_state(context, "emotion_dazed", {
            "label": "发懵", "keywords": ["发懵"], "usage_scenario": "旧场景"})
        original = tk.Toplevel
        dialogs = []
        def make_dialog(parent):
            dialog = original(parent)
            dialog.withdraw()
            dialog.grab_set = Mock()
            dialogs.append(dialog)
            return dialog
        with patch("ui.app.tk.Toplevel", side_effect=make_dialog):
            app._edit_portrait_manager_emotion(context, row)
            editor = next(widget for widget in self.descendants(dialogs[-1]) if isinstance(widget, tk.Text))
            self.assertEqual(editor.get("1.0", "end-1c"), "旧场景")
            editor.delete("1.0", "end")
            editor.insert("1.0", "修改后的场景")
            self.button(dialogs[-1], "确认修改").invoke()
        self.assertEqual(row["usage_scenario"], "修改后的场景")
        app._append_portrait_manager_state(context, "idle", {"label": "待机"})
        self.assertEqual(app._collect_portrait_states(context["portrait_state_rows"])[
            "emotion_dazed"]["usage_scenario"], "修改后的场景")

    def test_add_dialog_filters_cancel_and_confirm_update_only_draft(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.root = self.root
        app._center_toplevel = Mock()
        app._refresh_portrait_manager = Mock()
        app._restore_portrait_state = Mock()
        context = {"portrait_state_rows": []}
        original_toplevel = tk.Toplevel
        dialogs = []

        def make_dialog(parent):
            dialog = original_toplevel(parent)
            dialog.withdraw()
            dialog.grab_set = Mock()
            dialog.grab_release = Mock()
            dialogs.append(dialog)
            return dialog

        with tempfile.TemporaryDirectory() as temp, patch(
            "ui.app.tk.Toplevel", side_effect=make_dialog
        ):
            image = Path(temp) / "face.png"
            Image.new("RGBA", (20, 30)).save(image)
            app._add_common_portrait_emotions(context)
            self.button(dialogs[-1], "取消").invoke()
            self.assertEqual(context["portrait_state_rows"], [])
            app._add_common_portrait_emotions(context)
            dialog = dialogs[-1]
            selector = next(child for child in self.descendants(dialog)
                            if isinstance(child, EmotionSelector))
            selector.tabs["中性"].invoke()
            selector.step(2)
            with patch("ui.app.filedialog.askopenfilename", return_value=str(image)):
                self.button(dialog, "选择图片").invoke()
            self.button(dialog, "确认添加").invoke()
            rows = context["portrait_state_rows"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["key"].get(), "fake_crying")
            self.assertEqual(rows[0]["pending"]["full"], image)
            app._refresh_portrait_manager.assert_called_once_with(context)
            app._add_common_portrait_emotions(context)
            selector = next(child for child in self.descendants(dialogs[-1])
                            if isinstance(child, EmotionSelector))
            self.assertNotIn("fake_crying", selector.groups["中性"])
            self.button(dialogs[-1], "取消").invoke()
        self.assertEqual(self.root.state(), "withdrawn")

    def test_add_dialog_empty_positive_group_disables_confirmation(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.root = self.root
        app._center_toplevel = Mock()
        context = {"portrait_state_rows": [
            {"key": tk.StringVar(self.root, value=key)}
            for key in EMOTION_GROUPS["正向"]
        ]}
        original_toplevel = tk.Toplevel
        dialogs = []

        def make_dialog(parent):
            dialog = original_toplevel(parent)
            dialog.withdraw()
            dialog.grab_set = Mock()
            dialogs.append(dialog)
            return dialog

        with patch("ui.app.tk.Toplevel", side_effect=make_dialog):
            app._add_common_portrait_emotions(context)
            dialog = dialogs[-1]
            confirm = self.button(dialog, "确认添加")
            self.assertEqual(confirm.cget("state"), "disabled")
            selector = next(child for child in self.descendants(dialog)
                            if isinstance(child, EmotionSelector))
            selector.tabs["负向"].invoke()
            self.assertEqual(confirm.cget("state"), "normal")
            self.button(dialog, "取消").invoke()

    def test_batch_emotion_editor_cancel_and_confirm(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.root = self.root
        app._center_toplevel = Mock()
        app._update_treeview_scrollbar = Mock()
        app._apply_brand_ttk_style()
        context = {"portrait_state_rows": []}
        entries = [{"label": "高兴", "assets": {"full": Path("happy.png")}}]
        dialogs = []
        callbacks = {}
        original_toplevel = tk.Toplevel
        original_bind = ttk.Treeview.bind

        def make_dialog(parent):
            dialog = original_toplevel(parent)
            dialog.withdraw()
            dialog.grab_set = Mock()
            dialog.wait_window = Mock()
            dialogs.append(dialog)
            return dialog

        def bind(table, sequence=None, func=None, add=None):
            if sequence == "<Button-1>":
                callbacks["click"] = func
            return original_bind(table, sequence, func, add)

        with ExitStack() as stack:
            stack.enter_context(patch("ui.app.tk.Toplevel", side_effect=make_dialog))
            stack.enter_context(patch("ui.app.filedialog.askdirectory", return_value="unused"))
            stack.enter_context(patch.object(app, "_scan_portrait_batch_folder", return_value=(entries, [])))
            stack.enter_context(patch.object(app, "_filter_portrait_batch_entries", return_value=(entries, set())))
            stack.enter_context(patch.object(app, "_portrait_batch_existing_state"))
            stack.enter_context(patch.object(app, "_portrait_idle_asset_modes", return_value={"full"}))
            stack.enter_context(patch.object(ttk.Treeview, "bind", bind))
            app._batch_add_portrait_manager_states(context)
            preview = dialogs[0]
            table = next(child for child in self.descendants(preview)
                         if isinstance(child, ttk.Treeview))
            item = table.get_children()[0]
            with patch.object(table, "identify_column", return_value="#1"), patch.object(
                table, "identify_row", return_value=item
            ), patch.object(table, "bbox", return_value=(0, 0, 200, 24)):
                callbacks["click"](SimpleNamespace(x=1, y=1))
                editor = dialogs[-1]
                selector = next(child for child in self.descendants(editor)
                                if isinstance(child, EmotionSelector))
                self.assertEqual(selector.group, "正向")
                selector.tabs["负向"].invoke()
                self.button(editor, "取消").invoke()
                self.assertEqual(entries[0]["label"], "高兴")
                callbacks["click"](SimpleNamespace(x=1, y=1))
                editor = dialogs[-1]
                selector = next(child for child in self.descendants(editor)
                                if isinstance(child, EmotionSelector))
                selector.tabs["中性"].invoke()
                selector.step(2)
                self.button(editor, "确认").invoke()
                self.assertEqual(entries[0]["label"], "假哭")
                self.assertEqual(table.item(table.get_children()[0], "values")[0], "假哭")
            self.button(preview, "取消").invoke()
        self.assertEqual(context["portrait_state_rows"], [])


if __name__ == "__main__":
    unittest.main()
