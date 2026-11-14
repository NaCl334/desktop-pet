import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from core.config import save_json
from core.rendering import Live2DRenderer
from ui.app import DesktopPetApp


class ParameterModel:
    def __init__(self):
        self.values = {"ParamExpression": 1.0, "ParamPose": 1.0, "ParamEyeBallX": 0.0,
                       "ParamEyeBallY": 0.0, "ParamAngleX": 0.0, "ParamAngleY": 0.0,
                       "ParamEyeLOpen": 1.0, "ParamEyeROpen": 1.0, "ParamMouthOpenY": 0.0}

    def GetParamIds(self):
        return list(self.values)

    def GetParamDefaultById(self, key):
        return 1.0 if key in ("ParamExpression", "ParamPose", "ParamEyeLOpen", "ParamEyeROpen") else 0.0

    def GetParamMinById(self, key):
        return 1.0 if key in ("ParamExpression", "ParamPose") else -30.0 if key.startswith("ParamAngle") else -1.0 if key.startswith("ParamEyeBall") else 0.0

    def GetParamMaxById(self, key):
        return 14.0 if key == "ParamExpression" else 3.0 if key == "ParamPose" else 30.0 if key.startswith("ParamAngle") else 1.0

    def GetParamValueById(self, key):
        return self.values[key]

    def SetParamById(self, key, value, weight=1):
        self.values[key] = value


class AdapterTests(unittest.TestCase):
    def make_renderer(self, options=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        save_json(Path(directory.name) / "character.json", {"renderer": options or {}})
        # These fixtures exercise parameter math without native model resources.
        # Real package admission is covered by test_signed_character_packages.
        with patch("core.package_signature.authorize_character"):
            renderer = Live2DRenderer(directory.name, "model.model3.json")
        renderer._model = ParameterModel()
        renderer._detect_runtime_parameters()
        return renderer

    def test_elapsed_time_and_long_resume_gap(self):
        renderer = self.make_renderer()
        with patch("core.rendering.time.monotonic", side_effect=[10, 10.033, 10.1, 20, 20.034]):
            self.assertEqual(renderer._next_delta(), 0)
            self.assertAlmostEqual(renderer._next_delta(), .033)
            self.assertAlmostEqual(renderer._next_delta(), .067)
            self.assertEqual(renderer._next_delta(), 0)
            self.assertAlmostEqual(renderer._next_delta(), .034)

    def test_pause_and_hide_reset_clock(self):
        renderer = self.make_renderer()
        renderer.set_paused(True)
        with patch("core.rendering.time.monotonic", side_effect=[0, .05]):
            self.assertEqual(renderer._next_delta(), 0)
            self.assertEqual(renderer._next_delta(), 0)
        renderer.hide()
        self.assertIsNone(renderer._last_frame_at)
        renderer.set_paused(False)
        self.assertFalse(renderer._paused)

    def test_pause_skips_native_updates(self):
        renderer = self.make_renderer({"controlled_update": True})
        renderer._model = Mock()
        renderer.set_paused(True)
        renderer._advance_model("idle")
        renderer._model.UpdateMotion.assert_not_called()
        renderer._model.UpdatePhysics.assert_not_called()

    def test_state_parameters_restore_idle_and_defaults(self):
        renderer = self.make_renderer({"state_mappings": {
            "idle": {"parameters": {"ParamExpression": 1}},
            "angry": {"parameters": {"ParamExpression": 13, "ParamEyeBallX": .5}},
        }})
        renderer._apply_state_parameters("angry")
        self.assertEqual(renderer._model.values["ParamExpression"], 13)
        renderer._apply_state_parameters("idle")
        self.assertEqual(renderer._model.values["ParamExpression"], 1)
        self.assertEqual(renderer._model.values["ParamEyeBallX"], 0)

    def test_discrete_selection_is_quantized_and_clamped(self):
        renderer = self.make_renderer({"discrete_parameters": ["ParamExpression", "ParamPose"]})
        renderer._write_parameter("ParamExpression", 2.6)
        renderer._write_parameter("ParamPose", 8)
        self.assertEqual(renderer._model.values["ParamExpression"], 3)
        self.assertEqual(renderer._model.values["ParamPose"], 3)
        renderer._write_parameter("ParamExpression", float("nan"))
        self.assertEqual(renderer._model.values["ParamExpression"], 3)

    def test_pose_does_not_own_expression(self):
        renderer = self.make_renderer({"state_mappings": {"happy": {"parameters": {"ParamExpression": 2}}}})
        renderer.set_appearance({"parameters": {"ParamPose": 3}})
        renderer._apply_controllers("happy", .033)
        self.assertEqual(renderer._model.values["ParamExpression"], 2)
        self.assertEqual(renderer._model.values["ParamPose"], 3)

    def test_global_pointer_direction_and_small_viewport(self):
        renderer = self.make_renderer({"mouse_follow": {"enabled": True, "anchor": [.5, .25]}})
        renderer._full_size = (1000, 2000)
        canvas = Mock()
        canvas.winfo_rootx.return_value = -500
        canvas.winfo_rooty.return_value = 0
        canvas.winfo_pointerxy.return_value = (-450, 250)
        renderer._update_pointer_target(canvas, 500, 1000, "full", "idle", (0, 0))
        self.assertLess(renderer._follow_target[0], 0)
        self.assertEqual(renderer._follow_target[1], 0)
        canvas.winfo_pointerxy.return_value = (-250, 600)
        renderer._update_pointer_target(canvas, 500, 1000, "full", "idle", (0, 0))
        self.assertLess(renderer._follow_target[1], 0)

    def test_follow_smooths_and_returns_to_neutral(self):
        renderer = self.make_renderer({"mouse_follow": {"enabled": True}})
        renderer._follow_target = [1, -1]
        renderer._apply_mouse_follow(.033)
        self.assertTrue(0 < renderer._model.values["ParamEyeBallX"] < 1)
        self.assertTrue(-1 < renderer._model.values["ParamEyeBallY"] < 0)
        renderer.set_mouse_follow(False)
        for _ in range(100):
            renderer._model.values["ParamAngleX"] = 0
            renderer._model.values["ParamAngleY"] = 0
            renderer._apply_mouse_follow(.033)
        self.assertAlmostEqual(renderer._model.values["ParamEyeBallX"], 0, places=6)

    def test_closed_eye_expression_and_voice_pause(self):
        renderer = self.make_renderer({
            "state_mappings": {"closed": {"parameters": {"ParamExpression": 2}}},
            "closed_eye_expressions": {"values": [2]},
        })
        renderer._apply_controllers("closed", .033)
        self.assertEqual(renderer._model.values["ParamEyeLOpen"], 0)
        renderer.set_voice_activity(True)
        renderer._delta_seconds = .033
        with patch("core.rendering.time.monotonic", return_value=renderer._voice_activity_started_at + .2):
            renderer._apply_voice_activity()
        self.assertGreater(renderer._model.values["ParamMouthOpenY"], 0)
        renderer.set_voice_activity(True, paused=True)
        self.assertEqual(renderer._model.values["ParamMouthOpenY"], 0)
        renderer.set_voice_activity(False)
        self.assertEqual(renderer._model.values["ParamMouthOpenY"], 0)

    def test_voice_stop_invalidates_paused_open_mouth_frame(self):
        renderer = self.make_renderer()
        renderer._mouth_param_id = "ParamMouthOpenY"
        renderer._model.values["ParamMouthOpenY"] = .4
        renderer._last_rendered_frame = object()
        renderer.set_paused(True)
        renderer.set_voice_activity(False)
        self.assertEqual(renderer._model.values["ParamMouthOpenY"], 0)
        self.assertIsNone(renderer._last_rendered_frame)

    def test_idle_preference_can_be_set_before_native_load(self):
        renderer = self.make_renderer({"idle_motion": {"index": 1, "lively_index": 0}})
        renderer._model = None
        self.assertTrue(renderer.set_idle_mode(True))
        self.assertTrue(renderer._idle_lively)
        self.assertEqual(renderer._renderer_config["idle_motion"]["index"], 1)
        self.assertTrue(renderer.set_idle_mode(False))
        self.assertFalse(renderer._idle_lively)

    def test_unconfigured_lively_mode_is_rejected(self):
        renderer = self.make_renderer()
        self.assertFalse(renderer.set_idle_mode(True))
        self.assertFalse(renderer._idle_lively)

    def test_prepare_keeps_loaded_context_size(self):
        renderer = self.make_renderer()
        renderer._ensure_context = Mock()
        renderer._full_size = (1972, 2000)
        self.assertTrue(renderer.prepare("idle"))
        renderer._ensure_context.assert_not_called()
        renderer._model = None
        self.assertTrue(renderer.prepare("idle"))
        renderer._ensure_context.assert_called_once_with(16, 16)

    def test_raster_presentation_does_not_change_model_dimensions(self):
        renderer = self.make_renderer()
        renderer.natural_size = (1645, 1460)
        renderer._alpha_overlay = Mock()
        canvas = Mock()
        with patch("core.rendering.os.name", "nt"):
            for size in ((325, 288), (326, 289), (400, 355)):
                renderer._present(canvas, Image.new("RGBA", size), target_size=size)
                self.assertEqual(renderer.natural_size, (1645, 1460))

    def test_local_crop_rounding_does_not_shift_preview_anchor(self):
        renderer = self.make_renderer()
        renderer.natural_size = (1380, 900)
        renderer._alpha_overlay = Mock()
        with patch("core.rendering.os.name", "nt"):
            for raster in ((323, 210), (325, 213)):
                renderer._present(Mock(), Image.new("RGBA", raster),
                                  target_size=(460, 300), target_origin=(0, -50))
                self.assertEqual(renderer.image.size, (460, 300))
                self.assertEqual(renderer.image_bbox, (0, -50, 460, 250))

    def test_nested_geometry_transaction_flushes_only_outer_release(self):
        renderer = self.make_renderer()
        overlay = renderer._alpha_overlay = Mock()
        renderer.begin_geometry_transaction()
        renderer.begin_geometry_transaction()
        overlay.suspend_geometry_sync.assert_called_once()
        self.assertFalse(renderer.end_geometry_transaction(flush=True))
        self.assertTrue(renderer._geometry_sync_suspended)
        overlay.resume_geometry_sync.assert_not_called()
        renderer.end_geometry_transaction(flush=True)
        self.assertFalse(renderer._geometry_sync_suspended)
        overlay.resume_geometry_sync.assert_called_once_with(flush=True)
        self.assertFalse(renderer.end_geometry_transaction(flush=True))
        self.assertEqual(overlay.resume_geometry_sync.call_count, 1)

    def test_first_overlay_respects_pending_nested_transaction(self):
        renderer = self.make_renderer()
        canvas = Mock()
        canvas.cget.return_value = 64
        renderer.begin_geometry_transaction()
        renderer.begin_geometry_transaction()
        with patch("core.rendering.os.name", "nt"), patch("core.rendering._WindowsAlphaOverlay") as factory:
            renderer._present(canvas, Image.new("RGBA", (64, 64)), target_size=(64, 64))
            overlay = factory.return_value
            overlay.suspend_geometry_sync.assert_called_once()
            renderer.end_geometry_transaction(flush=True)
            overlay.resume_geometry_sync.assert_not_called()
            renderer.end_geometry_transaction(flush=True)
            overlay.resume_geometry_sync.assert_called_once_with(flush=True)


class Live2DResizeTickTests(unittest.TestCase):
    make_renderer = AdapterTests.make_renderer

    def make_app(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.renderer = self.make_renderer()
        app.root = Mock()
        app.root.winfo_exists.return_value = True
        app.portrait_canvas = Mock()
        app.portrait_canvas.winfo_ismapped.return_value = True
        app.pet_only = True
        app.state, app.display_mode, app.portrait_scale = "idle", "full", 1.2
        app.renderer.tick = Mock(return_value=True)
        app.renderer.render = Mock(return_value=True)
        app._portrait_preview_render_geometry = Mock(return_value=((360, 480), (-60, -80)))
        app._sync_pet_topmost = Mock()
        app._schedule_live2d_tick = Mock()
        return app

    def test_drag_ticks_keep_preview_size_and_anchor(self):
        app = self.make_app()
        app._portrait_resize_interaction_active = True
        for _ in range(3):
            app._tick_live2d()
        app.renderer.tick.assert_not_called()
        self.assertEqual(app.renderer.render.call_count, 3)
        app.renderer.render.assert_called_with(
            app.portrait_canvas, "idle", "full", scale=1.2, preview=True,
            target_size=(360, 480), target_origin=(-60, -80),
        )
        self.assertEqual(app._schedule_live2d_tick.call_count, 3)

    def test_commit_skips_tick_until_final_geometry_is_stable(self):
        app = self.make_app()
        app._portrait_resize_commit_pending = True
        app._tick_live2d()
        app.renderer.tick.assert_not_called()
        app.renderer.render.assert_not_called()
        app._schedule_live2d_tick.assert_called_once()
        app._portrait_resize_commit_pending = False
        app._tick_live2d()
        app.renderer.tick.assert_called_once_with(app.portrait_canvas, "idle", "full")

    def test_missing_preview_geometry_uses_normal_tick(self):
        app = self.make_app()
        app._portrait_resize_interaction_active = True
        app._portrait_preview_render_geometry.return_value = None
        app._tick_live2d()
        app.renderer.render.assert_not_called()
        app.renderer.tick.assert_called_once()


class Live2DControlTests(unittest.TestCase):
    make_renderer = AdapterTests.make_renderer
    def make_app(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.renderer = self.make_renderer({"mouse_follow": {"enabled": True, "strength": .85},
                                           "idle_motion": {"index": 1, "lively_index": 0}})
        app.renderer._model = None
        app.character_id = "anna"
        app.store = SimpleNamespace(settings={}, save_settings=Mock())
        app._sync_live2d_settings_controls = Mock()
        app._capture_control_snapshot = Mock(return_value={"marker": "before change"})
        return app

    def test_idle_mode_is_not_exposed_or_loaded_from_legacy_preferences(self):
        app = self.make_app()
        app.character_config = {"states": {}}
        self.assertNotIn("lively", app._live2d_control_values())
        self.assertFalse(app._set_live2d_control("lively", True))
        app.store.settings["live2d_controls"] = {"anna": {"lively": True}}
        app._apply_live2d_preferences(app.renderer)
        self.assertFalse(app.renderer._idle_lively)

    def test_controls_are_saved_per_character_and_reapplied(self):
        app = self.make_app()
        self.assertFalse(app._set_live2d_control("strength", .25))
        app._set_live2d_control("paused", True)
        self.assertNotIn("strength", app.store.settings["live2d_controls"]["anna"])
        self.assertEqual(app.store.save_settings.call_count, 1)
        app._apply_live2d_preferences(app.renderer)
        self.assertTrue(app.renderer._paused)
        self.assertEqual(app.renderer._renderer_config["mouse_follow"]["strength"], 1)
        app.character_id = "other"
        app._apply_live2d_preferences(app.renderer)
        self.assertFalse(app.renderer._paused)
        self.assertEqual(app.renderer._renderer_config["mouse_follow"]["strength"], 1)

    def test_draft_does_not_write_until_settings_save(self):
        app = self.make_app()
        app._control_draft_active = True
        app._set_live2d_control("follow", False)
        self.assertFalse(app.renderer._follow_enabled)
        app.store.save_settings.assert_not_called()
        app.store.settings = {}
        app._apply_live2d_preferences(app.renderer)
        self.assertTrue(app.renderer._follow_enabled)
        self.assertEqual(app.renderer._renderer_config["mouse_follow"]["strength"], 1)

    def test_preview_uses_target_character_preferences(self):
        app = self.make_app()
        app.store.settings["live2d_controls"] = {
            "anna": {"strength": .25, "paused": True},
            "other": {"strength": .5, "lively": True},
        }
        app._apply_live2d_preferences(app.renderer, "other")
        self.assertEqual(app.renderer._renderer_config["mouse_follow"]["strength"], 1)
        self.assertFalse(app.renderer._idle_lively)
        self.assertFalse(app.renderer._paused)

    def test_edit_after_save_starts_new_snapshot(self):
        app = self.make_app()
        app._control_dialog = Mock()
        app._control_dialog.winfo_exists.return_value = True
        app._control_draft_active = False
        app._set_live2d_control("paused", True)
        self.assertTrue(app._control_draft_active)
        self.assertEqual(app._control_snapshot, {"marker": "before change"})
        app.store.save_settings.assert_not_called()

    def test_invalid_strength_does_not_change_controls(self):
        app = self.make_app()
        self.assertFalse(app._set_live2d_control("strength", float("nan")))
        app.store.save_settings.assert_not_called()
        self.assertEqual(app.renderer._renderer_config["mouse_follow"]["strength"], 1)

if __name__ == "__main__":
    unittest.main()
