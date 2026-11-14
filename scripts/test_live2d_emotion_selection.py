import copy
import unittest
import tkinter as tk
from unittest.mock import Mock

import main  # noqa: F401
from core.emotion import PRESET_EMOTIONS
from core.live2d_models import _default_live2d_states, select_live2d_emotions
from ui.app import DesktopPetApp


class Live2DEmotionSelectionTests(unittest.TestCase):
    def test_dialog_lists_all_presets_and_applies_only_checked_emotions(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.root = root
        app._center_toplevel = Mock()
        app._brand_button = lambda parent, text, command, **kwargs: tk.Button(
            parent, text=text, command=command)
        app._brand_checkbutton = lambda parent, **kwargs: tk.Checkbutton(parent, **kwargs)
        context = {"character_config": {"states": _default_live2d_states()}}
        changed = Mock()
        app._show_common_emotions(context, changed)
        dialog = root.winfo_children()[0]
        checks = [widget for section in dialog.winfo_children()
                  for widget in section.winfo_children() if isinstance(widget, tk.Checkbutton)]
        self.assertEqual(len(checks), len(PRESET_EMOTIONS))
        next(widget for widget in checks if widget.cget("text") == "高兴").invoke()
        next(widget for widget in dialog.winfo_children()
             if isinstance(widget, tk.Button) and widget.cget("text") == "应用选择").invoke()
        self.assertEqual(set(context["character_config"]["states"]), {"idle", "happy"})
        changed.assert_called_once_with()
        app._show_common_emotions()
        dialog = root.winfo_children()[0]
        labels = [widget.cget("text") for section in dialog.winfo_children()
                  for widget in section.winfo_children() if isinstance(widget, tk.Label)]
        self.assertEqual(set(labels), {value["label"] for value in PRESET_EMOTIONS.values()})

    def test_new_states_only_include_recognized_new_presets(self):
        states = _default_live2d_states(("happy", "playful", "sad"))
        self.assertEqual(set(states), {"idle", "happy", "sad"})
        self.assertEqual(states["happy"]["label"], PRESET_EMOTIONS["happy"]["label"])

    def test_empty_selection_keeps_idle(self):
        self.assertEqual(set(_default_live2d_states()), {"idle"})

    def test_selection_preserves_custom_legacy_and_mappings_without_mutation(self):
        config = {"states": {"happy": {"label": "开心"},
                             "playful": {"label": "调皮"},
                             "custom": {"label": "自定义"}},
                  "renderer": {"state_mappings": {"happy": ["smile"]}}}
        original = copy.deepcopy(config)
        selected = select_live2d_emotions(config, ["sad", "worried"])
        self.assertEqual(set(selected), {"idle", "sad", "worried", "playful", "custom"})
        self.assertEqual(config, original)
        config["states"] = selected
        app = DesktopPetApp.__new__(DesktopPetApp)
        config["renderer"]["current"] = "live2d"
        definitions = app._configured_state_definitions(config)
        self.assertEqual(set(definitions), set(selected) - {"playful"})
        self.assertNotIn("happy", definitions)
        self.assertEqual(config["renderer"]["state_mappings"], original["renderer"]["state_mappings"])

    def test_disabled_presets_are_not_reinserted(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        config = {"renderer": {"current": "live2d"},
                  "states": {"happy": {"enabled": False},
                             "thinking": {"label": "思考", "enabled": True}}}
        states = app._configured_state_definitions(config)
        self.assertEqual(set(states), {"idle", "thinking"})
        self.assertEqual(states["thinking"]["label"], "认真")


if __name__ == "__main__":
    unittest.main()
