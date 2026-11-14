import json
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from core.live2d_behavior import BehaviorSelector, finite_number, intent_from_reply
from scripts import test_annalise_adapter as adapter


class SelectorTests(unittest.TestCase):
    def test_intent_is_not_an_emotion_and_ignores_questions_and_quotes(self):
        examples = {
            "[emotion:happy] 今天真开心。": "neutral",
            "[emotion:sad] 我同意你的建议。": "agree",
            "我不同意，这里要再检查。": "disagree",
            "我想仔细看看。": "inspect",
            "我不想仔细看看。": "neutral",
            "“我同意”是一个例句。": "neutral",
            "你是否同意？": "neutral",
            "I agree?": "neutral",
            "[intent:inspect] 我来看看。": "inspect",
            "[intent:missing] 普通说明。": "neutral",
        }
        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(intent_from_reply(text), expected)

    def test_intent_marker_never_shown_or_spoken(self):
        from ui.app import DesktopPetApp
        from core.gpt_sovits import _clean_tts_text
        text = "[emotion:happy] [intent:agree] 我同意。"
        self.assertEqual(DesktopPetApp._strip_bracketed_emotion_markers(text), "我同意。")
        self.assertEqual(_clean_tts_text(text), "我同意。")

    def test_lean_labels_do_not_call_backward_motions_forward(self):
        from ui.app import DesktopPetApp
        label = DesktopPetApp._live2d_motion_label
        self.assertEqual(label("Lean", 1, "model.leanBack.motion3.json"), "后倾")
        self.assertEqual(label("Lean", 2, "model.peekForward.motion3.json"), "轻探身")
        self.assertEqual(label("Lean", 0), "倾身")

    def test_annalise_motion_names_have_no_display_index(self):
        from ui.app import DesktopPetApp
        path = Path(__file__).resolve().parents[1] / "characters" / "annalise_live2d"
        if not (path / "character.json").is_file():
            self.skipTest("本机角色素材不随公开源码分发；通用行为测试仍执行。")
        config = json.loads((path / "character.json").read_text(encoding="utf-8"))
        model = json.loads((path / config["renderer"]["model"]).read_text(encoding="utf-8"))
        names = []
        for group, motions in model["FileReferences"]["Motions"].items():
            if group.casefold() == "idle":
                continue
            for index, motion in enumerate(motions):
                with self.subTest(group=group, index=index):
                    name = DesktopPetApp._live2d_motion_label(group, index, motion["File"])
                    self.assertNotRegex(name, r"\s+\d+$")
                    names.append(name)
        self.assertTrue(names)
        self.assertEqual(len(names), len(set(names)))

    def test_live2d_prompt_requests_intent_but_static_does_not(self):
        from core.conversation import ConversationClient
        client = ConversationClient({})
        live = client.build_messages([], {"renderer": {"current": "live2d"}})[0]["content"]
        static = client.build_messages([], {"renderer": {"current": "static"}})[0]["content"]
        self.assertIn("[intent:", live)
        self.assertNotIn("[intent:", static)

    def test_nonfinite_strength_and_bounds(self):
        self.assertEqual(finite_number(float("nan"), 1, 0, 1.5), 1)
        self.assertEqual(finite_number("bad", 1, 0, 1.5), 1)
        self.assertEqual(finite_number(10, 1, 0, 1.5), 1.5)

    def test_no_consecutive_repetition(self):
        selector = BehaviorSelector()
        previous = None
        for _ in range(30):
            choice = selector.choose("expression", ["a", "b"])
            self.assertNotEqual(choice, previous)
            previous = choice

    def test_duplicate_event_does_not_resample(self):
        selector = BehaviorSelector()
        first = selector.select(1, "happy", 0, ["a"], ["pose"], [("Nod", 0)])
        self.assertEqual(first["pose"], "pose")
        self.assertIsNone(selector.select(1, "happy", 10, ["a"]))

    def test_cooldown_does_not_block_new_emotion(self):
        selector = BehaviorSelector()
        selector.select(1, "happy", 0, ["a"], ["pose"], [("Nod", 0)])
        second = selector.select(2, "angry", 1, ["b"], ["pose2"], [("Shake", 0)])
        self.assertEqual(second["expression"], "b")
        self.assertIsNone(second["pose"])
        self.assertIsNone(second["motion"])


class RendererBehaviorTests(unittest.TestCase):
    make_renderer = adapter.AdapterTests.make_renderer

    def make_behavior_renderer(self):
        from core.emotion import PRESET_EMOTIONS
        renderer = self.make_renderer({
            "controlled_update": True,
            "discrete_parameters": ["ParamExpression", "ParamPose"],
            "state_mappings": {
                "idle": {"parameters": {"ParamExpression": 1}},
                "happy": {"parameters": {"ParamExpression": 2}},
                "expression_01": {"parameters": {"ParamExpression": 1}},
                "expression_02": {"parameters": {"ParamExpression": 2}},
                "expression_03": {"parameters": {"ParamExpression": 3}},
            },
            "appearance_options": [
                {"id": "pose1", "parameters": {"ParamPose": 1}},
                {"id": "pose2", "parameters": {"ParamPose": 2}},
                {"id": "pose3", "parameters": {"ParamPose": 3}},
            ],
        })
        renderer.state_definitions = {key: dict(value) for key, value in PRESET_EMOTIONS.items()}
        return renderer

    def test_custom_expression_candidates_and_reset(self):
        renderer = self.make_behavior_renderer()
        renderer.set_behavior_preferences({"emotion_expressions": {"happy": ["expression_03"]}})
        renderer.notify_behavior("happy")
        renderer._apply_state_parameters("happy")
        self.assertEqual(renderer._model.values["ParamExpression"], 3)
        renderer.set_behavior_preferences({})
        renderer.notify_behavior("happy")
        renderer._apply_state_parameters("happy")
        self.assertEqual(renderer._model.values["ParamExpression"], 2)

    def test_legacy_manual_mapping_is_preserved_but_does_not_override_auto_selection(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_groups = {"Nod", "Shake", "Wave"}
        renderer._motion_parameter_ids = {
            ("Nod", 0): {"ParamAngleY"},
            ("Wave", 0): {"ParamAngleX"},
            ("Unsafe", 0): {"ParamMouthOpenY"},
        }
        renderer.play_motion = lambda *motion: setattr(renderer, "_test_played", motion)
        renderer.set_behavior_preferences({
            "intent_motions": {"greet": [["Wave", 0]], "agree": [["Wave", 0]]}})
        renderer.notify_behavior("idle", "greet")
        self.assertEqual(renderer._test_played, ("Wave", 0))
        renderer._animation_time = 5
        renderer.notify_behavior("happy", "agree")
        self.assertEqual(renderer._test_played, ("Nod", 0))
        self.assertEqual(renderer.motion_candidates("agree"), [("Nod", 0)])
        renderer.set_behavior_preferences({"intent_motions": {"agree": []}})
        self.assertEqual(renderer.motion_candidates("agree"), [("Nod", 0)])
        self.assertEqual(renderer.behavior_preferences()["intent_motions"], {"agree": []})

    def test_automatic_motion_preferences_match_safe_actions_and_enable_them(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_parameter_ids = {
            ("Nod", 0): {"ParamAngleY"},
            ("Jump", 0): {"ParamAngleY"},
            ("Lean", 0): {"ParamBodyAngleY"},
            ("Lean", 1): {"ParamBodyAngleY"},
            ("Unsafe", 0): {"ParamMouthOpenY"},
        }
        renderer._motion_files = {
            ("Nod", 0): "model.nod.motion3.json",
            ("Jump", 0): "model.hopBack.motion3.json",
            ("Lean", 0): "model.leanForward.motion3.json",
            ("Lean", 1): "model.leanBack.motion3.json",
            ("Unsafe", 0): "model.unsafe.motion3.json",
        }
        preferences = renderer.automatic_motion_preferences()
        self.assertTrue(preferences["automatic_motion_matching"])
        self.assertIn(["Nod", 0], preferences["intent_motions"]["agree"])
        self.assertIn(["Jump", 0], preferences["emotion_motions"]["excited"])
        self.assertIn(["Lean", 1], preferences["emotion_motions"]["afraid"])
        self.assertNotIn(["Lean", 1], preferences["intent_motions"]["inspect"])
        self.assertNotIn(["Unsafe", 0], preferences["intent_motions"]["agree"])

        renderer.set_behavior_preferences(preferences)
        self.assertEqual(renderer.motion_candidates("agree"), [("Nod", 0)])
        self.assertEqual(renderer.motion_candidates("neutral", "excited"), [("Jump", 0)])

    def test_automatic_motion_preferences_match_wink_as_idle_and_light_feedback(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_files = {
            ("Wink", 0): "左眼wink.motion3.json",
            ("Wink", 1): "右眼wink.motion3.json",
        }
        renderer._motion_parameter_ids = {
            ("Wink", 0): {"ParamWinkL", "ParamWinkPose", "ParamWinkSmile"},
            ("Wink", 1): {"ParamWinkR", "ParamWinkPose", "ParamWinkSmile"},
        }
        renderer._parameter_ids.update(
            {"ParamWinkL", "ParamWinkR", "ParamWinkPose", "ParamWinkSmile"}
        )
        renderer._renderer_config["safe_motion_parameters"] = [
            "ParamWinkL", "ParamWinkR", "ParamWinkPose", "ParamWinkSmile"
        ]

        preferences = renderer.automatic_motion_preferences()

        self.assertEqual(
            preferences["intent_motions"]["idle"],
            [["Wink", 0], ["Wink", 1]],
        )
        self.assertEqual(
            preferences["emotion_motions"]["happy"],
            [["Wink", 0], ["Wink", 1]],
        )
        self.assertTrue(preferences["automatic_motion_matching"])

    def test_manual_motion_scenes_survive_rematching_and_reload(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_parameter_ids = {
            ("Nod", 0): {"ParamAngleY"}, ("Wave", 0): {"ParamAngleX"},
            ("Unsafe", 0): {"ParamMouthOpenY"}}
        renderer._motion_files = {("Nod", 0): "nod.motion3.json",
                                  ("Wave", 0): "wave.motion3.json"}
        preferences = renderer.motion_scene_preferences(("Nod", 0), ["greet", "inspect"], ["shy"])
        renderer.set_behavior_preferences(preferences)
        self.assertEqual(renderer.motion_candidates("agree"), [])
        self.assertIn(("Nod", 0), renderer.motion_candidates("inspect"))
        self.assertEqual(renderer.motion_candidates("neutral", "shy"), [("Nod", 0)])
        self.assertIn(("Wave", 0), renderer.motion_candidates("greet"))
        saved = renderer.behavior_preferences()
        renderer.set_behavior_preferences(saved)
        renderer.set_behavior_preferences({**saved, **renderer.automatic_motion_preferences()})
        self.assertEqual(renderer.motion_candidates("agree"), [])
        self.assertIn(("Nod", 0), renderer.motion_candidates("greet"))
        self.assertEqual(renderer.motion_candidates("neutral", "shy"), [("Nod", 0)])
        with self.assertRaises(ValueError):
            renderer.motion_scene_preferences(("Unsafe", 0), ["greet"], [])
        with self.assertRaises(ValueError):
            renderer.motion_scene_preferences(("Nod", 0), ["unknown"], [])

    def test_empty_manual_scenes_disable_legacy_emotion_and_preserve_other_actions(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_parameter_ids = {("Nod", 0): {"ParamAngleY"}, ("Wave", 0): {"ParamAngleX"}}
        renderer._motion_files = {("Nod", 0): "nod.motion3.json", ("Wave", 0): "wave.motion3.json"}
        renderer._motion_by_state["happy"] = ("Nod", 0)
        renderer.set_behavior_preferences(renderer.motion_scene_preferences(("Wave", 0), ["comfort"], []))
        renderer.set_behavior_preferences(renderer.motion_scene_preferences(("Nod", 0), [], []))
        self.assertEqual(renderer.motion_candidates("neutral", "happy"), [])
        self.assertEqual(renderer.motion_candidates("comfort"), [("Wave", 0)])
        generated = renderer.automatic_motion_preferences()
        self.assertNotIn(["Wave", 0], generated["intent_motions"]["greet"])
        self.assertIn(["Wave", 0], generated["intent_motions"]["comfort"])
        self.assertNotIn(["Nod", 0], generated["intent_motions"]["agree"])

    def test_reply_emotion_selects_semantic_action_without_implying_agreement(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_parameter_ids = {
            ("Nod", 0): {"ParamAngleY"},
            ("Gesture", 0): {"ParamAngleX"},
            ("Gesture", 1): {"ParamAngleY"},
            ("Lean", 0): {"ParamBodyAngleY"},
            ("Glance", 0): {"ParamEyeBallY"},
            ("Tilt", 0): {"ParamAngleZ"},
        }
        renderer._motion_files = {
            ("Gesture", 0): "model.bashful.motion3.json",
            ("Gesture", 1): "model.swayForward.motion3.json",
            ("Lean", 0): "model.leanBack.motion3.json",
            ("Glance", 0): "model.glanceDown.motion3.json",
            ("Tilt", 0): "model.tiltLeft.motion3.json",
        }
        cases = {"happy": [("Gesture", 1)], "shy": [("Gesture", 0)],
                 "surprised": [("Lean", 0)], "sad": [("Glance", 0)],
                 "confused": [("Tilt", 0)], "angry": [], "custom_unknown": []}
        for state, expected in cases.items():
            with self.subTest(state=state):
                self.assertEqual(renderer.motion_candidates("neutral", state), expected)
        self.assertEqual(renderer.motion_candidates("agree", "shy"), [("Nod", 0)])
        renderer.play_motion = lambda *motion: setattr(renderer, "_test_played", motion)
        renderer.notify_behavior("shy", "neutral")
        self.assertEqual(renderer._test_played, ("Gesture", 0))
        renderer._animation_time = 5
        renderer.notify_behavior("happy", "neutral")
        self.assertEqual(renderer._test_played, ("Gesture", 1))

    def test_model_state_motion_is_safe_fallback_for_custom_emotions(self):
        renderer = self.make_behavior_renderer()
        renderer.state_definitions['custom'] = {'label': '自定义', 'enabled': True}
        renderer._motion_by_state = {"custom": ("Special", 0)}
        renderer._motion_parameter_ids = {("Special", 0): {"ParamAngleY"}}
        self.assertEqual(renderer.motion_candidates("neutral", "custom"), [("Special", 0)])
        renderer._motion_parameter_ids[("Special", 0)] = {"ParamMouthOpenY"}
        self.assertEqual(renderer.motion_candidates("neutral", "custom"), [])

    def test_happy_and_legacy_emotion_mapping_do_not_imply_agreement(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_parameter_ids = {("Nod", 0): {"ParamAngleY"}}
        played = []
        renderer.play_motion = lambda *motion: played.append(motion)
        renderer.set_behavior_preferences({"emotion_motions": {"happy": [["Nod", 0]]}})
        renderer.notify_behavior("happy")
        self.assertEqual(played, [])
        renderer._animation_time = 5
        renderer.notify_behavior("happy", "agree")
        self.assertEqual(played, [("Nod", 0)])

    def test_missing_lean_does_not_use_look_as_inspection(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_parameter_ids = {("Look", 0): {"ParamAngleX"},
                                          ("LeanForward", 0): {"ParamBodyAngleY"}}
        self.assertEqual(renderer.motion_candidates("inspect"), [("LeanForward", 0)])
        del renderer._motion_parameter_ids[("LeanForward", 0)]
        self.assertEqual(renderer.motion_candidates("inspect"), [])

    def test_inspection_excludes_backward_and_ambiguous_lean_defaults(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_parameter_ids = {
            ("Lean", index): {"ParamBodyAngleY"} for index in range(4)}
        renderer._motion_files = {
            ("Lean", 0): "model.leanForward.motion3.json",
            ("Lean", 1): "model.leanBack.motion3.json",
            ("Lean", 2): "model.peekForward.motion3.json",
            ("Lean", 3): "model.lean.motion3.json",
        }
        self.assertEqual(renderer.motion_candidates("inspect"), [("Lean", 0), ("Lean", 2)])
        renderer.set_behavior_preferences({"intent_motions": {"inspect": [["Lean", 1]]}})
        self.assertEqual(renderer.motion_candidates("inspect"), [("Lean", 0), ("Lean", 2)])

    def test_idle_action_timer_respects_preview_voice_auto_and_empty_candidates(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_parameter_ids = {("Look", 0): {"ParamAngleX"}}
        played = []
        renderer.play_motion = lambda *motion: played.append(motion) or True
        renderer.notify_behavior("idle")
        self.assertEqual(played, [])
        renderer._animation_time = 30
        renderer._tick_idle_action()
        self.assertEqual(played, [("Look", 0)])
        self.assertGreater(renderer._next_idle_action_at, 30)
        for flag in ("_voice_activity", "_interaction_active", "_paused"):
            setattr(renderer, flag, True)
            renderer._animation_time += 40
            renderer._tick_idle_action()
            setattr(renderer, flag, False)
        renderer.begin_preview()
        renderer._animation_time += 40
        renderer._tick_idle_action()
        renderer.end_preview()
        renderer.set_behavior_preferences({"intent_motions": {"idle": []}})
        renderer.notify_behavior("idle")
        renderer._animation_time += 40
        renderer._tick_idle_action()
        self.assertEqual(played, [("Look", 0), ("Look", 0)])
        renderer.set_behavior_preferences({"auto_behavior": False})
        renderer.notify_behavior("idle")
        renderer._animation_time += 40
        renderer._tick_idle_action()
        self.assertEqual(played, [("Look", 0), ("Look", 0)])

    def test_idle_frequency_is_bounded_rescheduled_and_saved_per_character(self):
        renderer = self.make_behavior_renderer()
        renderer._animation_time = 10
        renderer.set_behavior_preferences({"idle_action_interval": 60})
        self.assertEqual(renderer.behavior_preferences()["idle_action_interval"], 60)
        self.assertTrue(50 <= renderer._next_idle_action_at <= 90)
        renderer.set_idle_action_interval(float("nan"))
        self.assertEqual(renderer._idle_action_interval, 21)
        self.assertTrue(24 <= renderer._next_idle_action_at <= 38)
        renderer.set_idle_action_interval(0)
        self.assertEqual(renderer._idle_action_interval, 8)
        renderer.set_idle_action_interval(1000)
        self.assertEqual(renderer._idle_action_interval, 120)

    def test_reply_motion_and_release_own_their_gaze_parameters(self):
        renderer = self.make_behavior_renderer()
        renderer._renderer_config["mouse_follow"] = {"enabled": True}
        renderer._follow_enabled = True
        renderer._follow_target = [1, 1]
        renderer._interaction_active = True
        renderer._interaction_parameters = {"ParamEyeBallX", "ParamAngleY"}
        renderer._model.values.update(ParamEyeBallX=-.7, ParamAngleY=-5)
        renderer._apply_mouse_follow(.1)
        self.assertEqual(renderer._model.values["ParamEyeBallX"], -.7)
        self.assertEqual(renderer._model.values["ParamAngleY"], -5)
        self.assertGreater(renderer._model.values["ParamEyeBallY"], 0)
        renderer._interaction_active = False
        renderer._reaction_release = {"ParamEyeBallX": -.7}
        renderer._apply_mouse_follow(.1)
        self.assertEqual(renderer._model.values["ParamEyeBallX"], -.7)
        renderer._reaction_release.clear()
        renderer._apply_mouse_follow(.1)
        self.assertGreater(renderer._model.values["ParamEyeBallX"], 0)

    def test_motion_library_preview_finishes_and_restores_paused_state(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_groups = {"Nod"}
        renderer._motion_parameter_ids = {("Nod", 0): {"ParamAngleY"}}
        renderer._model.GetMotionCount = lambda group: 1
        renderer._model.StopAllMotions = lambda: None
        renderer._model.StartMotion = lambda *args, **kwargs: setattr(
            renderer, "_test_finish", kwargs["onFinish"])
        renderer._live2d = SimpleNamespace(MotionPriority=SimpleNamespace(FORCE=3))
        renderer._behavior_selector.rng.random = lambda: 0
        renderer.notify_behavior("happy")
        original_pose = renderer._behavior_pose
        preferences = renderer.behavior_preferences()
        renderer.set_paused(True)
        self.assertTrue(renderer.preview_motion("Nod", 0))
        self.assertFalse(renderer._paused)
        self.assertIsNotNone(renderer._preview_snapshot)
        self.assertEqual(renderer._preview_pose, original_pose)
        renderer._apply_controllers("happy", .033)
        self.assertEqual(renderer._model.values["ParamPose"], original_pose["parameters"]["ParamPose"])
        renderer._animation_time = 2
        renderer._test_finish()
        self.assertIsNone(renderer._preview_snapshot)
        self.assertTrue(renderer._paused)
        self.assertIs(renderer._behavior_pose, original_pose)
        self.assertEqual(renderer._behavior_deadline, 9)
        self.assertEqual(renderer.behavior_preferences(), preferences)

    def test_motion_library_rejects_unadapted_actions_without_starting_preview(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_parameter_ids = {("Jump", 0): {"ParamBreeze"}}
        self.assertFalse(renderer.preview_motion("Jump", 0))
        self.assertIsNone(renderer._preview_snapshot)

    def test_frames_never_resample(self):
        renderer = self.make_behavior_renderer()
        renderer.notify_behavior("happy")
        choice = renderer._behavior_pose
        serial = renderer._behavior_serial
        for _ in range(20):
            renderer._apply_controllers("happy", .033)
        self.assertIs(renderer._behavior_pose, choice)
        self.assertEqual(renderer._behavior_serial, serial)

    def test_pose_allowlist_and_nonpersistent_selection(self):
        renderer = self.make_behavior_renderer()
        renderer.set_appearance({"parameters": {"ParamPose": 1}})
        renderer.set_behavior_preferences({"allowed_poses": ["pose3"]})
        renderer._behavior_selector.rng.random = lambda: 0
        renderer.notify_behavior("happy")
        renderer._apply_controllers("happy", .033)
        self.assertEqual(renderer._model.values["ParamPose"], 3)
        self.assertEqual(renderer._appearance_parameters["ParamPose"], 1)

    def test_behavior_pose_candidates_are_limited_to_current_outfit(self):
        renderer = self.make_behavior_renderer()
        options = renderer._renderer_config["appearance_options"]
        options[0]["outfit_id"] = "daily"
        options[1]["outfit_id"] = "formal"
        options[2]["outfit_id"] = "daily"
        renderer.set_appearance({"id": "pose1", "outfit_id": "daily",
                                 "parameters": {"ParamPose": 1}})
        renderer.set_behavior_preferences({"allowed_poses": ["pose1", "pose2", "pose3"]})
        renderer._behavior_selector.rng.random = lambda: 0
        renderer.notify_behavior("thinking")
        self.assertIn(renderer._behavior_pose["id"], {"pose1", "pose3"})

    def test_idle_timer_can_change_pose_but_can_also_keep_it(self):
        renderer = self.make_behavior_renderer()
        renderer.set_appearance({"id": "pose1", "parameters": {"ParamPose": 1}})
        renderer.notify_behavior("idle")
        renderer._behavior_pose = None
        renderer._behavior_selector.last_pose_at = float("-inf")
        renderer._behavior_selector.last_choices["pose"] = "pose1"
        renderer._behavior_selector.rng.random = lambda: 0
        renderer._animation_time = 20
        renderer._next_idle_action_at = 0
        renderer._tick_idle_action()
        self.assertIn(renderer._behavior_pose["id"], {"pose2", "pose3"})

        previous = renderer._behavior_pose
        renderer._behavior_selector.rng.random = lambda: 1
        renderer._animation_time = 25
        renderer._next_idle_action_at = 0
        renderer._tick_idle_action()
        self.assertIs(renderer._behavior_pose, previous)

    def test_selected_pose_persists_after_behavior_expires(self):
        renderer = self.make_behavior_renderer()
        renderer.set_appearance({"id": "pose1", "parameters": {"ParamPose": 1}})
        renderer._behavior_pose = {"id": "pose2", "parameters": {"ParamPose": 2}}
        renderer._behavior_deadline = 1
        renderer._animation_time = 2
        renderer._expire_behavior()
        self.assertEqual(renderer._behavior_pose["id"], "pose2")

    def test_pose_stays_fixed_during_cooldown(self):
        renderer = self.make_behavior_renderer()
        renderer.notify_behavior("happy")
        previous = renderer._behavior_pose
        renderer.notify_behavior("happy")
        self.assertIs(renderer._behavior_pose, previous)

    def test_disabled_auto_keeps_custom_emotion_mapping(self):
        renderer = self.make_behavior_renderer()
        renderer.set_behavior_preferences({"auto_behavior": False,
            "emotion_expressions": {"happy": ["expression_03"]}})
        renderer.notify_behavior("happy")
        self.assertIsNone(renderer._behavior_pose)
        self.assertIsNone(renderer._behavior_deadline)
        renderer._apply_controllers("happy", .033)
        self.assertEqual(renderer._model.values["ParamExpression"], 3)

    def test_expiration_waits_for_voice_then_restores_neutral(self):
        renderer = self.make_behavior_renderer()
        renderer.set_appearance({"parameters": {"ParamPose": 1}})
        renderer._behavior_selector.rng.random = lambda: 1
        renderer.notify_behavior("happy")
        renderer._animation_time = 8
        renderer._voice_activity = True
        renderer._expire_behavior()
        self.assertFalse(renderer._behavior_expired)
        renderer._voice_activity = False
        renderer._expire_behavior()
        renderer._apply_controllers("happy", .033)
        self.assertTrue(renderer._behavior_expired)
        self.assertEqual(renderer._model.values["ParamExpression"], 1)
        self.assertEqual(renderer._model.values["ParamPose"], 1)

    def test_idle_strength_does_not_change_discrete_or_mouth(self):
        renderer = self.make_behavior_renderer()
        renderer._idle_motion_group = "Idle"
        renderer._motion_parameter_ids[("Idle", 0)] = {"ParamAngleX", "ParamExpression", "ParamMouthOpenY"}
        renderer._model.values.update(ParamAngleX=10, ParamExpression=2, ParamMouthOpenY=.5)
        renderer.set_idle_strength(.5)
        renderer._apply_idle_strength()
        self.assertEqual(renderer._model.values["ParamAngleX"], 5)
        self.assertEqual(renderer._model.values["ParamExpression"], 2)
        self.assertEqual(renderer._model.values["ParamMouthOpenY"], .5)

    def test_active_reaction_is_not_scaled_as_idle(self):
        renderer = self.make_behavior_renderer()
        renderer._idle_motion_group = "Idle"
        renderer._motion_parameter_ids[("Idle", 0)] = {"ParamAngleX"}
        renderer._model.values["ParamAngleX"] = 10
        renderer._interaction_active = True
        renderer.set_idle_strength(0)
        renderer._apply_idle_strength()
        self.assertEqual(renderer._model.values["ParamAngleX"], 10)

    def test_unsafe_motions_are_not_automatic_candidates(self):
        renderer = self.make_behavior_renderer()
        for key in ("ParamPose", "ParamMouthOpenY", "ParamHair", "ParamUnknown"):
            renderer._motion_parameter_ids[("Nod", 0)] = {key}
            self.assertFalse(renderer._safe_motion(("Nod", 0)))
        renderer._motion_parameter_ids[("Nod", 0)] = {"ParamAngleY"}
        self.assertTrue(renderer._safe_motion(("Nod", 0)))
        renderer._motion_parameter_ids[("Nod", 0)] = {"paramangley"}
        self.assertTrue(renderer._safe_motion(("Nod", 0)))
        renderer._motion_parameter_ids[("Nod", 0)] = {"parampose"}
        self.assertFalse(renderer._safe_motion(("Nod", 0)))

    def test_custom_continuous_parameter_allowlist_cannot_override_protection(self):
        renderer = self.make_behavior_renderer()
        renderer._parameter_ids.update({"ParamBodyLean", "ParamPose", "ParamMouthOpenY", "ParamHair"})
        renderer._physics_parameter_ids = {"ParamHair"}
        renderer._motion_parameter_ids[("Lean", 0)] = {"ParamBodyLean"}
        self.assertFalse(renderer._safe_motion(("Lean", 0)))
        renderer._renderer_config["safe_motion_parameters"] = [
            "ParamBodyLean", "ParamPose", "ParamMouthOpenY", "ParamHair", "ParamUnknown"]
        self.assertTrue(renderer._safe_motion(("Lean", 0)))
        for key in ("ParamPose", "ParamMouthOpenY", "ParamHair", "ParamUnknown"):
            renderer._motion_parameter_ids[("Lean", 0)] = {key}
            self.assertFalse(renderer._safe_motion(("Lean", 0)))

    def test_automatic_candidates_are_compatible_and_support_multiple_indices(self):
        renderer = self.make_behavior_renderer()
        renderer._motion_parameter_ids = {
            ("nod", 0): {"ParamAngleY"}, ("nod", 1): {"ParamAngleY"},
            ("Shake", 0): {"ParamPose"}, ("Blink", 0): {"ParamEyeLOpen"},
        }
        self.assertEqual(renderer.automatic_motion_options(), [("nod", 0), ("nod", 1)])
        played = []
        renderer.play_motion = lambda *motion: played.append(motion)
        renderer.notify_behavior("happy", "agree")
        renderer._animation_time = 5
        renderer.notify_behavior("happy", "agree")
        self.assertEqual(set(played), {("nod", 0), ("nod", 1)})

    def test_reaction_blends_back_without_interpolating_discrete_ids(self):
        renderer = self.make_behavior_renderer()
        renderer._last_motion_values = {"ParamAngleX": 10, "ParamPose": 3, "ParamMouthOpenY": .5}
        renderer._begin_reaction_release(renderer._last_motion_values)
        self.assertEqual(set(renderer._reaction_release), {"ParamAngleX"})
        renderer._model.values["ParamAngleX"] = 0
        renderer._apply_reaction_release(.15)
        self.assertEqual(renderer._model.values["ParamAngleX"], 5)
        renderer._model.values["ParamAngleX"] = 0
        renderer._apply_reaction_release(.15)
        self.assertEqual(renderer._model.values["ParamAngleX"], 0)
        self.assertFalse(renderer._reaction_release)

    def test_reaction_release_does_not_feed_previous_blend_back_into_idle(self):
        renderer = self.make_behavior_renderer()
        model = renderer._model
        model.LoadParameters = lambda: None
        model.SaveParameters = lambda: None
        model.UpdateMotion = lambda delta: None
        model.UpdateBreath = lambda delta: None
        model.UpdateBlink = lambda delta: None
        model.UpdateExpression = lambda delta: None
        model.UpdatePhysics = lambda delta: None
        model.UpdatePose = lambda delta: None
        renderer.notify_behavior("idle")
        renderer._last_motion_values = {"ParamAngleX": 10}
        model.values["ParamAngleX"] = 10
        renderer._begin_reaction_release({"ParamAngleX"})
        with patch.object(renderer, "_next_delta", return_value=.1):
            for _ in range(3):
                renderer._advance_model("idle")
        self.assertAlmostEqual(model.values["ParamAngleX"], 0)
        self.assertFalse(renderer._reaction_release)

    def test_preview_restores_pose_emotion_deadline_and_pause(self):
        renderer = self.make_behavior_renderer()
        renderer.set_appearance({"parameters": {"ParamPose": 1}})
        renderer.notify_behavior("happy")
        before_pose = renderer._behavior_pose
        renderer._apply_controllers("happy", .033)
        before_values = dict(renderer._model.values)
        renderer.set_paused(True)
        renderer.preview_expression("expression_03")
        renderer.preview_pose({"parameters": {"ParamPose": 3}})
        renderer._animation_time = 2
        renderer._apply_controllers("happy", .033)
        self.assertEqual(renderer._model.values["ParamExpression"], 3)
        self.assertEqual(renderer._model.values["ParamPose"], 3)
        renderer.end_preview()
        self.assertTrue(renderer._paused)
        self.assertIs(renderer._behavior_pose, before_pose)
        self.assertEqual(renderer._behavior_deadline, 9)
        self.assertIsNone(renderer._preview_snapshot)
        self.assertEqual(renderer._model.values, before_values)


class PreferenceTests(adapter.Live2DControlTests):
    def test_frequency_control_is_saved_and_reloaded(self):
        app = self.make_app()
        self.assertTrue(app._set_live2d_control("idle_action_interval", 40))
        self.assertEqual(app.store.settings["live2d_controls"]["anna"]["idle_action_interval"], 40)
        self.assertFalse(app._set_live2d_control("idle_action_interval", float("nan")))
        app.renderer.set_idle_action_interval(21)
        app._apply_live2d_preferences(app.renderer, "anna")
        self.assertEqual(app.renderer._idle_action_interval, 40)

    def test_behavior_configuration_is_saved_per_character(self):
        app = self.make_app()
        app.renderer.state_definitions = {"happy": {"label": "高兴", "enabled": True}}
        app.renderer._parameters_by_state = {"idle": {}, "happy": {}, "expression_02": {}}
        app._set_live2d_behavior_preferences({"emotion_expressions": {"happy": ["expression_02"]}})
        self.assertEqual(app.store.settings["live2d_controls"]["anna"]["emotion_expressions"],
                         {"happy": ["expression_02"]})
        self.assertFalse(app._set_live2d_control("idle_strength", .4))
        self.assertNotIn("idle_strength", app.store.settings["live2d_controls"]["anna"])

    def test_invalid_mapping_does_not_save(self):
        app = self.make_app()
        self.assertFalse(app._set_live2d_behavior_preferences({"emotion_expressions": {"happy": ["missing"]}}))
        app.store.save_settings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
