import copy
import ctypes
import importlib
import json
import math
import os
import re
import sys
import threading
import tkinter as tk
import time
from ctypes import wintypes
from contextlib import contextmanager
from pathlib import Path

from core.live2d_behavior import (
    BehaviorSelector, EMOTION_MOTION_GROUPS, INTENT_LABELS, INTENT_MOTION_GROUPS,
    finite_number, motion_matches,
)
from core.emotion import EMOTION_LABEL_ALIASES, LEGACY_EMOTIONS, PRESET_EMOTIONS, emotion_fallback_chain
from core.live2d_stage import bounded_offset, pixel_offset, validated_stage_actions
from core.live2d_models import load_model_adapter, merge_model_renderer_defaults, model_resource_scope
from core.portrait_processing import (
    IMAGE_EXTENSIONS,
)
from core.portrait_viewport import crop_viewport, resolve_viewport, viewport_crop_box

try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    Image = None
    ImageDraw = None
    ImageTk = None

STATE_LABELS = {
    "idle": "待机",
    "happy": "高兴",
    "shy": "害羞",
    "angry": "生气",
    "confused": "疑惑",
    "thinking": "认真",
    "speechless": "无语",
    "worried": "担心",
    "excited": "兴奋",
    "low_battery": "低电量",
    "found_good_thing": "发现好东西",
    "playful": "调皮",
    "awkward": "尴尬",
    "afraid": "害怕",
    "flustered": "慌张",
    "surprised": "惊讶",
    "embarrassed": "难为情",
    "passionate": "情动",
    "sad": "伤心",
    "helpless": "无奈",
    "disgusted": "厌恶",
    "confident": "自信",
}
STATE_LABELS.update({key: value["label"] for key, value in PRESET_EMOTIONS.items()})

RESIZE_HANDLE_HALF_SIZE = 14
RESIZE_HANDLE_INSET = 15


def resize_handle_center(width, height):
    """Return the fixed display-space center of the northeast resize handle."""
    width = max(1, int(width))
    height = max(1, int(height))
    return (
        max(RESIZE_HANDLE_HALF_SIZE, width - RESIZE_HANDLE_INSET),
        min(
            RESIZE_HANDLE_HALF_SIZE,
            max(0, height - RESIZE_HANDLE_INSET),
        ),
    )


def resize_handle_icon_segments(center_x, center_y):
    """Return the two fixed diagonal segments used by the handle icon."""
    return (
        (
            center_x - 8,
            center_y + 8,
            center_x + 8,
            center_y - 8,
        ),
        (
            center_x - 7,
            center_y - 7,
            center_x + 7,
            center_y + 7,
        ),
    )


def _portrait_log(message):
    """Write a small local diagnostic trail for real EXE/window failures."""
    try:
        root = (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent
        )
        log_path = root / "data" / "logs" / "portrait_renderer.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("[{}] {}\n".format(timestamp, message))
    except (OSError, TypeError, ValueError):
        pass


def _overlay_extended_style(style, mouse_passthrough=False):
    style = style | 0x00080000 | 0x00000080 | 0x08000000
    return (style | 0x20) if mouse_passthrough else (style & ~0x20)


def _overlay_frame_change_flags():
    return 0x0001 | 0x0002 | 0x0010 | 0x0020


def _overlay_insert_after(topmost):
    # HWND_NOTOPMOST is -2. Using HWND_TOP (0) here can raise a non-topmost
    # overlay above unrelated windows and break the normal z-order.
    return wintypes.HWND(-1 if bool(topmost) else -2)


class BaseRenderer(object):
    def set_mouse_passthrough(self, enabled):
        self._mouse_passthrough = bool(enabled)
        overlay = getattr(self, "_alpha_overlay", None)
        if overlay is not None:
            overlay.set_mouse_passthrough(enabled)

    def render(self, canvas, state):
        raise NotImplementedError


class Live2DRenderer(BaseRenderer):
    """在隐藏 GLFW 上下文中绘制 Live2D，再复用现有透明覆盖层显示。"""

    # live2d-py 的 C 扩展在同一进程内重复 init/dispose 后再次加载模型
    # 会触发访问冲突。运行库和隐藏 GL 上下文保留到进程结束，避免全局
    # 着色器引用已销毁的上下文；模型和透明覆盖层仍由各渲染器管理。
    _runtime_live2d = None
    _runtime_glfw = None
    _runtime_gl_initialized = False
    _runtime_glfw_initialized = False
    _runtime_window = None
    _runtime_window_size = (0, 0)
    _runtime_lock = threading.RLock()

    def __init__(
        self,
        character_dir,
        model_path,
        state_definitions=None,
        appearance=None,
        viewport_settings=None,
    ):
        self.character_dir = Path(character_dir).resolve()
        self.model_path = (self.character_dir / str(model_path)).resolve()
        from core.package_signature import authorize_character
        authorize_character(self.character_dir, require_live2d=True, model_path=self.model_path)
        self.state_definitions = (
            state_definitions if isinstance(state_definitions, dict) else {}
        )
        self.image = None
        self.image_item = None
        self.image_path = self.model_path
        self.image_bbox = None
        self.natural_size = None
        self._alpha_overlay = None
        self._topmost = False
        self._input_hit_padding = 0
        self._resize_overlay_enabled = False
        self._resize_overlay_color = "#4EA1FF"
        self._canvas_fallback = False
        self._geometry_sync_suspended = False
        self._geometry_transaction_depth = 0
        self.last_error = ""
        self._glfw = None
        self._gl = None
        self._live2d = None
        self._window = None
        self._model = None
        self._runtime_initialized = False
        self._gl_initialized = False
        self._frame_size = (0, 0)
        self._last_state = None
        self._expression_by_state = {}
        self._motion_by_state = {}
        self._expression_ids = set()
        self._expression_parameters = {}
        self._expression_files = {}
        self._expression_paths = {}
        self._motion_groups = set()
        self._motion_parameter_ids = {}
        self._motion_files = {}
        self._motion_paths = {}
        self._motion_durations = {}
        self._stage_actions = {}
        self._stage_action = None
        self._stage_elapsed = 0.0
        self._stage_neutral_height = .9
        self._stage_offset = (0.0, 0.0)
        self._stage_pixel_offset = (0, 0)
        self._pending_motion_reset = set()
        self._interaction_active = False
        self._interaction_generation = 0
        self._interaction_parameters = set()
        self._idle_motion_group = None
        self._idle_motion_index = 0
        self._idle_lively = False
        self._mouth_param_id = None
        self._voice_activity = False
        self._voice_paused = False
        self._voice_activity_started_at = 0.0
        self._appearance = {}
        self._appearance_parameters = {}
        self._appearance_expression = None
        self._appearance_motion = None
        self._appearance_applied_signature = None
        self.viewport_settings = viewport_settings or {}
        self._renderer_config = {}
        self._parameters_by_state = {}
        self._state_parameter_ids = set()
        self._parameter_ids = set()
        self._parameter_defaults = {}
        self._last_frame_at = None
        self._delta_seconds = 0.0
        self._animation_time = 0.0
        self._paused = False
        self._last_rendered_frame = None
        self._follow_position = [0.0, 0.0]
        self._follow_target = [0.0, 0.0]
        self._follow_enabled = False
        self._mouth_value = 0.0
        self._full_size = None
        self._source_crop = None
        self._frame_durations = []
        self._failed = False
        self._behavior_selector = BehaviorSelector()
        self._behavior_serial = 0
        self._behavior_state = None
        self._behavior_intent = "idle"
        self._next_idle_action_at = 20.0
        self._behavior_expression = None
        self._behavior_pose = None
        self._behavior_deadline = None
        self._behavior_expired = False
        self._behavior_preferences = {}
        self._preview_snapshot = None
        self._preview_expression = None
        self._preview_pose = None
        self._preview_motion = None
        self._reaction_release = {}
        self._reaction_release_elapsed = 0.0
        self._last_motion_values = {}
        self._physics_parameter_ids = set()
        self._idle_strength = 1.0
        self.set_appearance(appearance)
        self._configure_state_mappings()
        self._default_idle_strength = 1.0
        self._idle_strength = 1.0
        self._default_auto_behavior = bool(self._renderer_config.get("controlled_update", False))
        self._auto_behavior = self._default_auto_behavior
        self._default_idle_action_interval = finite_number(
            self._renderer_config.get("idle_action_interval", 21), 21, 8, 120)
        self._idle_action_interval = self._default_idle_action_interval

    def _configure_state_mappings(self):
        config_path = self.character_dir / "character.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            config = {}
        from core.config import get_character_dir, load_character_config
        if self.character_dir == get_character_dir(self.character_dir.name).resolve():
            config = load_character_config(self.character_dir.name)
        renderer = config.get("renderer", {}) if isinstance(config, dict) else {}
        renderer = merge_model_renderer_defaults(
            load_model_adapter(self.model_path, self.character_dir),
            renderer if isinstance(renderer, dict) else {})
        self._renderer_config = renderer if isinstance(renderer, dict) else {}
        self._follow_enabled = bool(self._renderer_config.get("mouse_follow", {}).get("enabled", False))
        self._default_follow_strength = 1.0
        self._renderer_config.setdefault("mouse_follow", {})["strength"] = 1.0
        mappings = renderer.get("state_mappings", {}) if isinstance(renderer, dict) else {}
        if isinstance(mappings, dict):
            for state, value in mappings.items():
                if isinstance(value, str):
                    self._expression_by_state[str(state)] = value
                elif isinstance(value, dict):
                    expression = value.get("expression")
                    motion = value.get("motion")
                    if expression:
                        self._expression_by_state[str(state)] = str(expression)
                    if isinstance(motion, (list, tuple)) and len(motion) >= 2:
                        self._motion_by_state[str(state)] = (str(motion[0]), int(motion[1]))
                    parameters = self._normalized_appearance(value).get("parameters", {})
                    if parameters:
                        self._parameters_by_state[str(state)] = parameters
                        self._state_parameter_ids.update(parameters)

    def _load_runtime(self):
        if self._live2d is not None:
            return
        from core.environment import live2d_runtime_roots
        runtime_root = next((p for p in live2d_runtime_roots()
                             if (p / "live2d" / "_live2d.pyd").is_file()), None)
        if runtime_root is None:
            raise RuntimeError("未安装 Live2D 组件，请使用含 Live2D 组件的本体，或改用立绘角色。")
        if runtime_root.is_dir() and str(runtime_root) not in sys.path:
            sys.path.insert(0, str(runtime_root))
        try:
            self._glfw = importlib.import_module("glfw")
            self._gl = importlib.import_module("OpenGL.GL")
            self._live2d = importlib.import_module("live2d")
        except (ImportError, OSError) as error:
            raise RuntimeError("Live2D 组件缺失或无法加载，请检查环境检测或改用立绘角色。") from error

    @classmethod
    def initialize_runtime(cls):
        """Create the hidden native window on the UI thread before worker use."""
        if cls._runtime_window is not None:
            return
        renderer = cls.__new__(cls)
        renderer._failed = False
        renderer._glfw = renderer._gl = renderer._live2d = None
        with renderer._runtime_context():
            renderer._ensure_runtime_context(16, 16)

    def _ensure_runtime_context(self, width, height):
        if self._failed:
            raise RuntimeError(self.last_error or "Live2D 渲染器已停止重试")
        self._load_runtime()
        if not self.__class__._runtime_glfw_initialized:
            if not self._glfw.init():
                raise RuntimeError("GLFW 初始化失败")
            self.__class__._runtime_glfw_initialized = True
            self.__class__._runtime_glfw = self._glfw
        runtime = self.__class__
        if runtime._runtime_window is None:
            self._glfw.window_hint(self._glfw.VISIBLE, self._glfw.FALSE)
            window = self._glfw.create_window(
                max(1, int(width)), max(1, int(height)), "desktop pet live2d", None, None
            )
            if not window:
                raise RuntimeError("无法创建 OpenGL 窗口")
            runtime._runtime_window = window
            runtime._runtime_window_size = (int(width), int(height))
        self._window = runtime._runtime_window
        self._glfw.make_context_current(self._window)
        if not self.__class__._runtime_live2d:
            self._live2d.init()
            self.__class__._runtime_live2d = self._live2d
        if not self.__class__._runtime_gl_initialized:
            self._live2d.glInit()
            self.__class__._runtime_gl_initialized = True

    def _ensure_context(self, width, height):
        self._ensure_runtime_context(width, height)
        runtime = self.__class__
        if self._model is None:
            if not self.model_path.is_file():
                raise FileNotFoundError(str(self.model_path))
            self._model = self._live2d.Model()
            self._model.LoadModelJson(str(self.model_path))
            self._model.SetAutoBreath(self._renderer_config.get("auto_breath", True))
            self._model.SetAutoBlink(self._renderer_config.get("auto_blink", True))
            self._load_model_extras()
            self._detect_runtime_parameters()
            self._apply_appearance_to_model()
            self._full_size = tuple(int(v) for v in self._model.GetCanvasSizePixel())
            self._last_frame_at = None
        if runtime._runtime_window_size != (int(width), int(height)):
            self._glfw.set_window_size(self._window, int(width), int(height))
            runtime._runtime_window_size = (int(width), int(height))
        if self._frame_size != (int(width), int(height)):
            self._model.Resize(int(width), int(height))
            self._frame_size = (int(width), int(height))

    def _detect_runtime_parameters(self):
        """识别标准口型参数；不同作者的模型可能使用不同大小写命名。"""
        self._mouth_param_id = None
        try:
            parameter_ids = [
                str(value)
                for value in self._model.GetParamIds()
                if str(value).strip()
            ]
        except Exception:
            parameter_ids = []
        by_lower = {value.lower(): value for value in parameter_ids}
        self._parameter_ids = set(parameter_ids)
        self._parameter_defaults = {}
        for parameter_id in parameter_ids:
            try:
                self._parameter_defaults[parameter_id] = float(self._model.GetParamDefaultById(parameter_id))
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass
        for candidate in (
            "ParamMouthOpenY",
            "PARAM_MOUTH_OPEN_Y",
            "MouthOpenY",
            "ParamMouthOpen",
        ):
            if candidate.lower() in by_lower:
                self._mouth_param_id = by_lower[candidate.lower()]
                break

    @staticmethod
    def _normalized_appearance(appearance):
        if not isinstance(appearance, dict):
            return {}
        parameters = appearance.get("parameters", {})
        normalized_parameters = {}
        if isinstance(parameters, dict):
            for parameter_id, value in parameters.items():
                try:
                    normalized_parameters[str(parameter_id)] = float(value)
                except (TypeError, ValueError):
                    continue
        normalized = {
            "id": str(appearance.get("id", "") or "").strip(),
            "name": str(appearance.get("name", "") or "").strip(),
            "parameters": normalized_parameters,
            "outfit_id": str(appearance.get("outfit_id") or "default"),
            "outfit_name": str(appearance.get("outfit_name") or ""),
        }
        expression = str(appearance.get("expression", "") or "").strip()
        if expression:
            normalized["expression"] = expression
        motion = appearance.get("motion")
        if isinstance(motion, (list, tuple)) and len(motion) >= 2:
            try:
                normalized["motion"] = (str(motion[0]), int(motion[1]))
            except (TypeError, ValueError):
                pass
        return normalized

    def set_appearance(self, appearance):
        """设置当前 Live2D 外观预设，实际参数在下一帧应用。"""
        if getattr(self, "_stage_action", None) is not None:
            self.end_preview() if self._preview_snapshot is not None else self._stop_reaction()
        self._appearance = self._normalized_appearance(appearance)
        self._behavior_pose = None
        self._appearance_parameters = dict(self._appearance.get("parameters", {}))
        self._appearance_expression = self._appearance.get("expression")
        self._appearance_motion = self._appearance.get("motion")
        self._appearance_applied_signature = None
        if self._model is not None:
            self._apply_appearance_to_model()
        return True

    def _apply_appearance_to_model(self):
        if self._model is None:
            return
        for parameter_id, value in self._appearance_parameters.items():
            if parameter_id in getattr(self, "_renderer_config", {}).get("discrete_parameters", []):
                value = math.floor(value + 0.5)
            try:
                minimum = float(self._model.GetParamMinById(parameter_id))
                maximum = float(self._model.GetParamMaxById(parameter_id))
                value = max(minimum, min(maximum, float(value)))
                self._model.SetParamById(parameter_id, value, 1.0)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
        signature = (
            tuple(sorted(self._appearance_parameters.items())),
            self._appearance_expression,
            self._appearance_motion,
        )
        if signature == self._appearance_applied_signature:
            return
        if self._appearance_expression:
            try:
                self._model.SetExpression(self._appearance_expression)
            except (RuntimeError, ValueError):
                pass
        self._appearance_applied_signature = signature
        if self._appearance_motion:
            try:
                group, index = self._appearance_motion
                self._model.StartMotion(
                    group,
                    index,
                    self._live2d.MotionPriority.NORMAL,
                )
            except (RuntimeError, ValueError):
                pass

    def set_voice_activity(self, active, paused=False):
        """启用轻量口型动画；不做音素级分析，也不依赖实验模型专用映射。"""
        active = bool(active)
        if active and not self._voice_activity:
            self._voice_activity_started_at = time.monotonic()
        self._voice_activity = active
        self._voice_paused = bool(paused) if active else False
        if not active or paused:
            self._mouth_value = 0.0
        if (not active or paused) and self._model is not None and self._mouth_param_id:
            try:
                self._model.SetParamById(self._mouth_param_id, 0.0, 1.0)
                if getattr(self, "_paused", False):
                    self._last_rendered_frame = None
            except (RuntimeError, ValueError):
                pass

    def _apply_voice_activity(self):
        if self._model is None or not self._mouth_param_id:
            return
        if not self._voice_activity or self._voice_paused:
            target = 0.0
        else:
            elapsed = max(0.0, time.monotonic() - self._voice_activity_started_at)
            # 轻微、非严格的开合变化，避免语音播放时角色完全静止。
            settings = getattr(self, "_renderer_config", {}).get("lip_sync", {})
            amplitude = max(0.0, min(1.0, float(settings.get("amplitude", 0.4))))
            speed = max(1.0, min(30.0, float(settings.get("speed", 16.0))))
            target = amplitude * (0.5 + 0.5 * math.sin(elapsed * speed)) ** 0.8
            smoothing = 1.0 - math.exp(-max(0.001, getattr(self, "_delta_seconds", 1 / 30)) / 0.045)
            target = getattr(self, "_mouth_value", 0.0) + smoothing * (target - getattr(self, "_mouth_value", 0.0))
        self._mouth_value = target
        try:
            minimum = float(self._model.GetParamMinById(self._mouth_param_id))
            maximum = float(self._model.GetParamMaxById(self._mouth_param_id))
            target = max(minimum, min(maximum, target))
            self._model.SetParamById(self._mouth_param_id, target, 1.0)
        except (RuntimeError, ValueError, TypeError):
            pass

    def _load_model_extras(self):
        """补充加载部分模型配置中被运行库误识别的表情和动作。"""
        try:
            self._expression_ids.update(str(item) for item in self._model.GetExpressions())
        except Exception:
            pass
        try:
            self._motion_groups.update(
                str(item) for item in self._model.GetMotionGroups().keys()
            )
        except Exception:
            pass
        try:
            model_config = json.loads(self.model_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            model_config = {}
        refs = model_config.get("FileReferences", {}) if isinstance(model_config, dict) else {}
        physics = refs.get("Physics")
        if isinstance(physics, str):
            path = (self.model_path.parent / physics).resolve()
            try:
                path.relative_to(self.model_path.parent)
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                self._physics_parameter_ids = {
                    output["Destination"]["Id"]
                    for setting in data.get("PhysicsSettings", [])
                    for output in setting.get("Output", [])
                    if isinstance(output.get("Destination", {}).get("Id"), str)
                }
            except (OSError, ValueError, TypeError, KeyError):
                self._physics_parameter_ids = set()
        expressions = refs.get("Expressions", []) if isinstance(refs, dict) else []
        if isinstance(expressions, list):
            for item in expressions:
                if not isinstance(item, dict):
                    continue
                relative = str(item.get("File", "")).strip()
                if not relative.lower().endswith(".exp3.json"):
                    continue
                path = (self.model_path.parent / relative).resolve()
                try:
                    path.relative_to(self.model_path.parent.resolve())
                except ValueError:
                    continue
                if not path.is_file():
                    continue
                expression_id = str(item.get("Name", "")).strip() or Path(relative).name[:-len(".exp3.json")]
                self._expression_paths[expression_id] = relative.replace("\\", "/")
                try:
                    expression_data = json.loads(path.read_text(encoding="utf-8-sig"))
                    parameters = []
                    for parameter in expression_data.get("Parameters", []):
                        if not isinstance(parameter, dict):
                            continue
                        parameter_id = str(parameter.get("Id", "")).strip()
                        try:
                            value = float(parameter.get("Value"))
                        except (TypeError, ValueError):
                            continue
                        if not parameter_id or not math.isfinite(value):
                            continue
                        blend = str(parameter.get("Blend", "Add")).strip().casefold()
                        if blend not in ("overwrite", "add", "multiply"):
                            blend = "add"
                        parameters.append((parameter_id, value, blend))
                    self._expression_parameters[expression_id] = parameters
                    self._expression_files[expression_id] = path.name
                    file_expression_id = Path(relative).name[:-len(".exp3.json")]
                    if file_expression_id and file_expression_id != expression_id:
                        self._expression_paths[file_expression_id] = relative.replace("\\", "/")
                        self._expression_parameters[file_expression_id] = parameters
                        self._expression_files[file_expression_id] = path.name
                except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
                    pass
                if expression_id in self._expression_ids:
                    continue
                try:
                    self._model.LoadExtraExpression(expression_id, str(path))
                    self._expression_ids.add(expression_id)
                except Exception:
                    continue
        motions = refs.get("Motions", {}) if isinstance(refs, dict) else {}
        if isinstance(motions, dict):
            for group, entries in motions.items():
                if not isinstance(entries, list):
                    continue
                for index, entry in enumerate(entries):
                    if not isinstance(entry, dict):
                        continue
                    relative = str(entry.get("File", "")).strip()
                    if not relative.lower().endswith(".motion3.json"):
                        continue
                    path = (self.model_path.parent / relative).resolve()
                    try:
                        path.relative_to(self.model_path.parent.resolve())
                    except ValueError:
                        continue
                    if not path.is_file():
                        continue
                    try:
                        motion_data = json.loads(path.read_text(encoding="utf-8-sig"))
                        self._motion_parameter_ids[(str(group), index)] = {
                            curve["Id"] for curve in motion_data.get("Curves", [])
                            if curve.get("Target") == "Parameter" and isinstance(curve.get("Id"), str)
                        }
                        self._motion_files[(str(group), index)] = path.name
                        self._motion_paths[(str(group), index)] = relative.replace("\\", "/")
                        self._motion_durations[(str(group), index)] = float(
                            motion_data.get("Meta", {}).get("Duration", 0))
                    except (OSError, ValueError, TypeError):
                        pass
                    if str(group) in self._motion_groups:
                        continue
                    try:
                        self._model.LoadExtraMotion(str(group), str(path))
                        self._motion_groups.add(str(group))
                    except Exception:
                        continue
        self._load_stage_actions()
        # Some authoring tools put an idle motion in Expressions instead of Motions.
        if isinstance(expressions, list):
            for item in expressions:
                if not isinstance(item, dict):
                    continue
                relative = str(item.get("File", "")).strip()
                if not relative.lower().endswith(".motion3.json"):
                    continue
                path = (self.model_path.parent / relative).resolve()
                if not path.is_file():
                    continue
                try:
                    self._model.LoadExtraMotion("idle", str(path))
                    self._motion_groups.add("idle")
                except Exception:
                    continue

        defaults = {
            "idle": ("豆豆眼", "A"),
            "happy": ("激动", "Hearts", "Stars"),
            "shy": ("脸红", "不悦"),
            "angry": ("黑化", "怒"),
            "confused": ("懵逼", "懵圈哭"),
            "surprised": ("Stars", "激动"),
            "sad": ("大哭", "懵圈哭"),
            "worried": ("流汗", "不悦"),
            "excited": ("激动", "Hearts"),
            "speechless": ("无语", "豆豆眼"),
            "embarrassed": ("脸红", "不悦"),
        }

        def find_expression(candidates):
            for candidate in candidates:
                for expression_id in (candidate, candidate + ".exp3"):
                    if expression_id in self._expression_ids:
                        return expression_id
            return None

        for state, candidates in defaults.items():
            if state in self._expression_by_state:
                continue
            expression_id = find_expression(candidates)
            if expression_id:
                self._expression_by_state[state] = expression_id
        for expression_id in sorted(self._expression_ids):
            self._expression_by_state.setdefault("expression:" + expression_id, expression_id)
        requested = self._renderer_config.get("idle_motion", {})
        requested_group = str(requested.get("group", "idle"))
        self._idle_motion_group = next(
            (group for group in sorted(self._motion_groups) if group.casefold() == requested_group.casefold()),
            None,
        )
        if self._idle_motion_group:
            entries = motions.get(self._idle_motion_group, [])
            index = requested.get("lively_index" if self._idle_lively else "index")
            if index is None:
                index = next((i for i, item in enumerate(entries)
                              if "idlestill" in str(item.get("File", "")).casefold()), 0)
            count = self._model.GetMotionCount(self._idle_motion_group)
            self._idle_motion_index = max(0, min(max(0, count - 1), int(index)))
            try:
                self._model.StartMotion(
                    self._idle_motion_group,
                    self._idle_motion_index,
                    self._live2d.MotionPriority.IDLE,
                )
            except Exception:
                pass

    def _load_stage_actions(self):
        self._stage_actions = {}
        relative = self._renderer_config.get("stage_motion_file")
        if not isinstance(relative, str) or not relative:
            return
        path = (self.character_dir / relative).resolve()
        try:
            path.relative_to(self.character_dir)
            pack = json.loads(path.read_text(encoding="utf-8-sig"))
            self._stage_actions = validated_stage_actions(
                pack, self.model_path.name, self._motion_files, self._motion_durations)
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            _portrait_log("live2d_stage_unavailable model={} error={}".format(
                self.model_path.name, type(error).__name__))

    def _clear_stage_action(self):
        self._stage_action = None
        self._stage_elapsed = 0.0
        self._stage_offset = (0.0, 0.0)
        self._stage_pixel_offset = (0, 0)

    def _advance_stage(self, delta):
        if self._stage_action is None:
            return
        self._stage_elapsed += delta
        if self._stage_elapsed >= self._stage_action["duration"]:
            self._clear_stage_action()
            return
        self._stage_offset = pixel_offset(
            self._stage_action["stage"], self._stage_elapsed, self._stage_neutral_height)

    def _apply_state(self, state):
        if self._model is None or state == self._last_state:
            return
        if self._preview_snapshot is None and self._behavior_state != state:
            self.notify_behavior(state)
        expression = self._expression_by_state.get(self._effective_expression_state(state))
        if expression:
            try:
                expression_id = expression
                if expression_id not in self._expression_ids:
                    candidates = (
                        expression_id + ".exp3",
                        expression_id.removesuffix(".exp3"),
                    )
                    expression_id = next(
                        (
                            candidate
                            for candidate in candidates
                            if candidate in self._expression_ids
                        ),
                        expression_id,
                    )
                self._model.SetExpression(expression_id)
            except (RuntimeError, ValueError):
                pass
        motion = (self._motion_by_state.get(state)
                  if not self._auto_behavior and self._preview_snapshot is None else None)
        if motion and motion[0] != self._idle_motion_group:
            try:
                self.play_motion(motion[0], motion[1])
            except (RuntimeError, ValueError):
                pass
        self._last_state = state

    def _advance_model(self, state):
        delta = self._next_delta()
        if self._paused:
            return
        self._expire_behavior()
        if self._idle_motion_group:
            try:
                if not self._interaction_active and self._model.IsMotionFinished():
                    self._model.StopAllMotions()
                    self._model.StartMotion(
                        self._idle_motion_group,
                        self._idle_motion_index,
                        self._live2d.MotionPriority.FORCE,
                    )
            except Exception:
                pass
        self._apply_state(state)
        self._tick_idle_action()
        if self._renderer_config.get("controlled_update", False):
            # Insert owned inputs after motion/expression evaluation, before physics.
            self._model.LoadParameters()
            # Recompute the idle target instead of feeding back last frame's blend.
            for parameter_id in self._reaction_release:
                self._write_parameter(parameter_id, self._parameter_defaults.get(parameter_id, 0.0))
            self._model.UpdateMotion(delta)
            if self._paused:
                return
            self._advance_stage(delta)
            for parameter_id in self._pending_motion_reset:
                self._write_parameter(parameter_id, self._parameter_defaults.get(parameter_id, 0.0))
            self._pending_motion_reset.clear()
            self._apply_idle_strength()
            self._apply_reaction_release(delta)
            self._model.SaveParameters()
            if self._renderer_config.get("auto_blink", True) and not self._interaction_active:
                self._model.UpdateBlink(delta)
            if self._renderer_config.get("auto_breath", True):
                self._model.UpdateBreath(delta)
            self._model.UpdateExpression(delta)
            self._apply_controllers(state, delta)
            if self._interaction_active:
                self._last_motion_values = {
                    key: float(self._model.GetParamValueById(key))
                    for key in self._interaction_parameters if key in self._parameter_ids
                }
            self._model.UpdatePhysics(delta)
            self._model.UpdatePose(delta)
        else:
            self._model.Update(delta)
            self._apply_appearance_to_model()
            self._apply_state_parameters(state)
            self._apply_voice_activity()

    def _draw_frame(self, width, height, state):
        if self._paused and self._last_rendered_frame is not None:
            self._next_delta()
            image = self._last_rendered_frame
            if image.size != (width, height):
                image = image.convert("RGBa").resize((width, height), Image.Resampling.LANCZOS).convert("RGBA")
            return image.copy()
        self._glfw.make_context_current(self._window)
        self._advance_model(state)
        # A motion's finish callback can restore a paused preview in this update.
        if self._paused and self._last_rendered_frame is not None:
            image = self._last_rendered_frame
            if image.size != (width, height):
                image = image.convert("RGBa").resize((width, height), Image.Resampling.LANCZOS).convert("RGBA")
            return image.copy()
        self._gl.glViewport(0, 0, width, height)
        self._gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        self._gl.glClear(self._gl.GL_COLOR_BUFFER_BIT)
        self._model.Draw()
        raw = self._gl.glReadPixels(
            0, 0, width, height, self._gl.GL_RGBA, self._gl.GL_UNSIGNED_BYTE
        )
        if not raw:
            raise RuntimeError("Live2D 未输出图像帧")
        # Cubism renders premultiplied RGBA; the layered window expects straight RGBA.
        image = Image.frombytes("RGBa", (width, height), raw).convert("RGBA")
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        self._last_rendered_frame = image
        return image

    def _next_delta(self):
        now = time.monotonic()
        previous = self._last_frame_at
        self._last_frame_at = now
        elapsed = max(0.0, now - previous) if previous is not None else 0.0
        self._delta_seconds = 0.0 if self._paused or elapsed > 0.5 else min(elapsed, 0.1)
        self._animation_time += self._delta_seconds
        return self._delta_seconds

    def set_paused(self, paused):
        if paused and self._stage_action is not None:
            if self._preview_snapshot is not None:
                self.end_preview()
            if not self._paused:
                parameters = set(self._interaction_parameters)
                parameters.update(self._reaction_release)
                parameters.update(self._pending_motion_reset)
                self._stop_reaction()
                for key in parameters - self._physics_parameter_ids - {
                        self._mouth_param_id, *self._renderer_config.get("discrete_parameters", [])}:
                    self._write_parameter(key, self._parameter_defaults.get(key, 0))
                self._model.SaveParameters()
                self._reaction_release.clear()
                self._last_rendered_frame = None
        self._paused = bool(paused)
        self._last_frame_at = None

    def set_mouse_follow(self, enabled, strength=None):
        self._follow_enabled = bool(enabled)
        settings = self._renderer_config.setdefault("mouse_follow", {})
        settings["strength"] = 1.0
        if not enabled:
            self._follow_target = [0.0, 0.0]

    def set_idle_mode(self, lively=False):
        if self._stage_action is not None and self._preview_snapshot is not None:
            self.end_preview()
        options = self._renderer_config.get("idle_motion", {})
        index = int(options.get("lively_index" if lively else "index", 0))
        if lively and "lively_index" not in options:
            return False
        if self._model is not None and self._idle_motion_group:
            if not 0 <= index < self._model.GetMotionCount(self._idle_motion_group):
                return False
            self._pending_motion_reset.update(self._interaction_parameters)
            self._pending_motion_reset.update(self._motion_parameter_ids.get(
                (self._idle_motion_group, self._idle_motion_index), set()))
            self._interaction_generation += 1
            self._model.StopAllMotions()
            self._interaction_active = False
            self._interaction_parameters = set()
            self._clear_stage_action()
            self._idle_motion_index = index
            # The bundled runtime retains motion priority after StopAllMotions.
            self._model.StartMotion(self._idle_motion_group, index, self._live2d.MotionPriority.FORCE)
        self._idle_lively = bool(lively)
        return True

    def set_idle_strength(self, value):
        self._idle_strength = finite_number(value, 1.0, 0, 1.5)
        self._last_rendered_frame = None

    def set_idle_action_interval(self, value):
        self._idle_action_interval = finite_number(value, self._default_idle_action_interval, 8, 120)
        self._schedule_idle_action()

    def behavior_preferences(self):
        return {"auto_behavior": self._auto_behavior,
                "idle_action_interval": self._idle_action_interval,
                **json.loads(json.dumps(self._behavior_preferences))}

    def set_behavior_preferences(self, preferences):
        self.end_preview()
        preferences = preferences if isinstance(preferences, dict) else {}
        self.set_idle_strength(1.0)
        self._auto_behavior = bool(preferences.get("auto_behavior", self._default_auto_behavior))
        self.set_idle_action_interval(preferences.get(
            "idle_action_interval", self._default_idle_action_interval))
        self._behavior_preferences = {
            key: json.loads(json.dumps(preferences[key]))
            for key in ("emotion_expressions", "emotion_motions", "intent_motions", "allowed_poses", "allowed_motions")
            if isinstance(preferences.get(key), dict if key in ("emotion_expressions", "emotion_motions", "intent_motions") else list)
        }
        if isinstance(preferences.get("automatic_motion_matching"), bool):
            self._behavior_preferences["automatic_motion_matching"] = bool(
                preferences["automatic_motion_matching"]
            )
        overrides = preferences.get("motion_scene_overrides")
        if isinstance(overrides, list):
            self._behavior_preferences["motion_scene_overrides"] = json.loads(json.dumps(overrides))
        active = self.selected_emotion_definitions()
        for name in ("emotion_expressions", "emotion_motions"):
            if name in self._behavior_preferences:
                self._behavior_preferences[name] = {
                    key: value for key, value in self._behavior_preferences[name].items() if key in active}
        for item in self._behavior_preferences.get("motion_scene_overrides", []):
            if isinstance(item, dict):
                item["emotions"] = [key for key in item.get("emotions", []) if key in active]
        self._behavior_state = None
        self._behavior_intent = "idle"
        self._schedule_idle_action()
        self._behavior_pose = None
        self._behavior_expression = None
        self._behavior_deadline = None
        self._behavior_expired = False
        self._last_state = None
        if not self._auto_behavior:
            self._stop_reaction()

    def selected_emotion_definitions(self):
        definitions = {"idle": {"label": "待机", "enabled": True}}
        for key, value in getattr(self, "state_definitions", {}).items():
            if (key in LEGACY_EMOTIONS or key.startswith(("expression_", "expression:"))
                    or not isinstance(value, dict) or not value.get("enabled", True)):
                continue
            definitions[key] = {**value, "label": PRESET_EMOTIONS.get(key, value).get("label", key)}
        return definitions

    def automatic_expression_preferences(self):
        """只按选中情绪的模型映射及明确近义回退匹配，不套用当前手动选择。"""
        options = self.expression_options()
        mapping = {}
        for state in self.selected_emotion_definitions():
            matches = []
            for candidate in emotion_fallback_chain(state):
                if candidate == "idle" and state != "idle":
                    continue
                parameters = self._parameters_by_state.get(candidate)
                expression = self._expression_by_state.get(candidate)
                matches = [key for key in options if key == candidate or (
                    parameters and self._parameters_by_state.get(key) == parameters) or (
                    expression and self._expression_by_state.get(key) == expression)]
                if matches:
                    break
            mapping[state] = matches
        return {"emotion_expressions": mapping}

    def automatic_motion_preferences(self):
        """根据当前模型的动作组和文件名生成自动动作候选表。

        这里只登记能够通过安全参数校验、且能从现有语义规则判断用途的动作。
        未识别动作仍可在动作库中预览，但不会被强行加入所有行为。
        """
        scoped = set(self.motion_resource_options())
        safe = [
            motion for motion in self.safe_motion_options()
            if motion[0] != self._idle_motion_group and motion in scoped
        ]
        intent_motions = {}
        for intent, groups in INTENT_MOTION_GROUPS.items():
            candidates = [
                motion for motion in safe
                if motion_matches(motion[0], self._motion_files.get(motion, ""), groups)
                and (intent != "inspect" or self._is_inspection_motion(motion))
            ]
            intent_motions[intent] = [list(motion) for motion in candidates]

        emotion_motions = {}
        for state, definition in self.selected_emotion_definitions().items():
            groups = EMOTION_MOTION_GROUPS.get(state, (state, definition["label"]))
            candidates = [
                motion for motion in safe
                if motion_matches(motion[0], self._motion_files.get(motion, ""), groups)
            ]
            emotion_motions[state] = [list(motion) for motion in candidates]
        overrides = self.behavior_preferences().get("motion_scene_overrides", [])
        for item in overrides:
            if not isinstance(item, dict):
                continue
            motion = item.get("motion")
            if (not isinstance(motion, list) or len(motion) != 2
                    or not isinstance(motion[0], str) or type(motion[1]) is not int
                    or tuple(motion) not in safe):
                continue
            for mapping, key in ((intent_motions, "intents"), (emotion_motions, "emotions")):
                selected = item.get(key, [])
                if not isinstance(selected, list):
                    continue
                for scene, candidates in mapping.items():
                    candidates[:] = [candidate for candidate in candidates if candidate != motion]
                    if scene in selected:
                        candidates.append(list(motion))
        return {
            "intent_motions": intent_motions,
            "emotion_motions": emotion_motions,
            "automatic_motion_matching": True,
        }

    def motion_scene_preferences(self, motion, intents, emotions):
        """Change one motion's scenes without replacing other manual mappings."""
        motion = tuple(motion)
        if motion not in self.safe_motion_options() or motion[0] == self._idle_motion_group:
            raise ValueError("该动作尚不可用于自动场景")
        if any(key not in INTENT_LABELS for key in intents) or any(
                key not in self.selected_emotion_definitions() for key in emotions):
            raise ValueError("未知动作场景")
        preferences = self.behavior_preferences()
        generated = self.automatic_motion_preferences()
        result = {"automatic_motion_matching": True}
        for name, selected in (("intent_motions", intents), ("emotion_motions", emotions)):
            mapping = generated[name]
            if preferences.get("automatic_motion_matching"):
                mapping.update({key: value for key, value in preferences.get(name, {}).items()
                                if key in mapping})
            for scene, candidates in mapping.items():
                mapping[scene] = [item for item in candidates if item != list(motion)]
                if scene in selected:
                    mapping[scene].append(list(motion))
            result[name] = mapping
        overrides = [item for item in preferences.get("motion_scene_overrides", [])
                     if isinstance(item, dict) and item.get("motion") != list(motion)]
        overrides.append({"motion": list(motion), "intents": list(dict.fromkeys(intents)),
                          "emotions": list(dict.fromkeys(emotions))})
        for item in overrides:
            item["emotions"] = [key for key in item.get("emotions", [])
                                if key in self.selected_emotion_definitions()]
        result["motion_scene_overrides"] = overrides
        return result

    def resource_scope(self, kind):
        if kind == "expression":
            paths = getattr(self, "_expression_paths", {}).values()
        else:
            paths = [getattr(self, "_motion_paths", {}).get(key, filename)
                     for key, filename in self._motion_files.items()]
        model_path = getattr(self, "model_path", None)
        return model_resource_scope(paths, kind, model_path.parent if model_path else None)

    def expression_resource_info(self, key):
        expression = self._expression_by_state.get(key)
        path = getattr(self, "_expression_paths", {}).get(expression, "")
        if path:
            return {"type": "表情文件（.exp3.json）", "path": path}
        return {"type": "模型参数预设" if not expression else "模型表情（未提供资源路径）", "path": ""}

    def motion_resource_options(self):
        allowed = set(self.resource_scope("motion")["paths"])
        return [key for key, filename in sorted(self._motion_files.items())
                if getattr(self, "_motion_paths", {}).get(key, filename) in allowed]

    def expression_options(self):
        keys = set(self._parameters_by_state) | set(self._expression_by_state)
        paths = getattr(self, "_expression_paths", {})
        scope = self.resource_scope("expression")
        if scope["restricted"]:
            allowed = set(scope["paths"])
            keys = {key for key in keys if not self._expression_by_state.get(key)
                    or paths.get(self._expression_by_state[key]) in allowed}
        else:
            # A resource placed in the opposite kind's directory is never a candidate.
            keys = {key for key in keys if not paths.get(self._expression_by_state.get(key))
                    or paths[self._expression_by_state[key]] in scope["paths"]}
        configured = self._renderer_config.get("expression_options", [])
        selected = [item["id"] for item in configured
                    if isinstance(item, dict) and item.get("id") in keys]
        if selected:
            return list(dict.fromkeys(selected))
        explicit = sorted(key for key in keys if key.startswith("expression_"))
        named = sorted(key for key in keys if key.startswith("expression:"))
        return explicit + named or sorted(keys)

    def expression_candidates(self, state):
        mapping = self._behavior_preferences.get("emotion_expressions", {})
        valid = set(self.expression_options()) | set(self._parameters_by_state) | set(self._expression_by_state)
        selected = []
        for candidate_state in emotion_fallback_chain(state):
            candidates = mapping.get(candidate_state, [candidate_state])
            if not isinstance(candidates, list):
                candidates = [candidate_state]
            selected = list(dict.fromkeys(
                item for item in candidates if isinstance(item, str) and item in valid
            ))
            if selected:
                break
        selected = selected or ["idle"]
        options = self.expression_options()
        canonical = []
        for key in selected:
            equivalent = next((option for option in options
                               if self._parameters_by_state.get(option) == self._parameters_by_state.get(key)
                               and self._expression_by_state.get(option) == self._expression_by_state.get(key)), key)
            canonical.append(key if key in options else equivalent)
        return list(dict.fromkeys(canonical))

    def _effective_expression_state(self, state):
        if getattr(self, "_preview_snapshot", None) is not None and self._preview_expression:
            return self._preview_expression
        if getattr(self, "_behavior_expired", False):
            return self._behavior_expression or "idle"
        return getattr(self, "_behavior_expression", None) or state

    def _safe_motion(self, motion):
        parameters = self._motion_parameter_ids.get(motion)
        if not parameters or not self._renderer_config.get("controlled_update", False):
            return False
        required = self._renderer_config.get("stage_motion_groups", [])
        if motion[0] in required and motion not in self._stage_actions:
            return False
        parameter_ids = {str(parameter_id).casefold() for parameter_id in parameters}
        protected = {
            str(parameter_id).casefold()
            for parameter_id in self._renderer_config.get("discrete_parameters", [])
        }
        protected.update(
            str(parameter_id).casefold()
            for parameter_id in self._physics_parameter_ids
        )
        if self._mouth_param_id:
            protected.add(str(self._mouth_param_id).casefold())
        allowed = {"ParamAngleX", "ParamAngleY", "ParamAngleZ", "ParamBodyAngleX",
                   "ParamBodyAngleY", "ParamBodyAngleZ", "ParamEyeBallX", "ParamEyeBallY",
                   "ParamEyeLOpen", "ParamEyeROpen"}
        allowed.update(
            parameter_id
            for parameter_id in parameter_ids
            if parameter_id.casefold().startswith(("paramwink", "paramblink"))
        )
        normalized_allowed = {parameter_id.casefold() for parameter_id in allowed}
        extra = self._renderer_config.get("safe_motion_parameters", [])
        if isinstance(extra, list):
            normalized_allowed.update(
                key.casefold()
                for key in extra
                if isinstance(key, str)
                and any(
                    key.casefold() == parameter.casefold()
                    for parameter in self._parameter_ids
                )
            )
        return parameter_ids <= normalized_allowed and not parameter_ids & protected

    def automatic_motion_options(self):
        configured = set(self._motion_by_state.values())
        return [motion for motion in sorted(self._motion_parameter_ids)
                if (motion in configured or motion[0].casefold() in ("nod", "shake"))
                and self._safe_motion(motion)]

    def safe_motion_options(self):
        return [motion for motion in sorted(self._motion_parameter_ids) if self._safe_motion(motion)]

    def motion_candidates(self, intent, state=None):
        if intent not in INTENT_LABELS:
            return []
        groups = INTENT_MOTION_GROUPS[intent]
        scoped = set(self.motion_resource_options())
        restricted = self.resource_scope("motion")["restricted"]

        def in_scope(motion):
            return motion in scoped or (not restricted and motion not in self._motion_files)
        safe = [
            motion for motion in self.safe_motion_options()
            if motion != (self._idle_motion_group, self._idle_motion_index)
        ]
        configured_intents = (
            self._behavior_preferences.get("intent_motions")
            if self._behavior_preferences.get("automatic_motion_matching")
            else None
        )
        if isinstance(configured_intents, dict) and intent in configured_intents:
            candidates = [
                (str(item[0]), int(item[1]))
                for item in configured_intents[intent]
                if isinstance(item, (list, tuple)) and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], int)
                and (str(item[0]), int(item[1])) in safe
            ]
        else:
            candidates = [
                motion for motion in safe
                if in_scope(motion) and motion_matches(motion[0], self._motion_files.get(motion, ""), groups)
            ]
        if intent == "inspect" and not (
                isinstance(configured_intents, dict) and intent in configured_intents):
            candidates = [motion for motion in candidates if self._is_inspection_motion(motion)]
        if candidates or state is None or intent == "idle":
            return candidates
        if state not in self.selected_emotion_definitions():
            return []
        # Emotion is a fallback, never a synonym for agreement or disagreement.
        groups = EMOTION_MOTION_GROUPS.get(state, ())
        configured_emotions = (
            self._behavior_preferences.get("emotion_motions")
            if self._behavior_preferences.get("automatic_motion_matching")
            else None
        )
        configured = self._motion_by_state.get(state)
        if (not (isinstance(configured_emotions, dict) and state in configured_emotions)
                and configured and self._safe_motion(configured)
                and configured[0] != self._idle_motion_group):
            return [configured]
        if isinstance(configured_emotions, dict) and state in configured_emotions:
            candidates = [
                (str(item[0]), int(item[1]))
                for item in configured_emotions[state]
                if isinstance(item, (list, tuple)) and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], int)
                and (str(item[0]), int(item[1])) in safe
            ]
        else:
            candidates = [
                motion for motion in safe
                if in_scope(motion) and motion_matches(motion[0], self._motion_files.get(motion, ""), groups)
            ]
        return candidates

    def _is_inspection_motion(self, motion):
        name = re.sub(r"[\s_-]", "", (
            motion[0] + " " + self._motion_files.get(motion, "")).casefold())
        return (not any(token in name for token in ("back", "后倾", "后仰"))
                and any(token in name for token in ("forward", "前倾", "轻探身", "inspect", "仔细观察")))

    def notify_behavior(self, state, intent=None):
        if self._preview_snapshot is not None:
            return
        if state != self._behavior_state:
            self._stop_reaction()
        self._behavior_serial += 1
        self._behavior_state = state
        self._behavior_intent = intent if intent in INTENT_LABELS else (
            state if state in ("idle", "thinking") else "neutral")
        self._schedule_idle_action()
        self._behavior_expired = False
        automatic = self._auto_behavior and not state.startswith("expression_")
        options = self._renderer_config.get("appearance_options", [])
        discrete_parameters = {
            str(parameter_id).casefold()
            for parameter_id in self._renderer_config.get("discrete_parameters", [])
        }
        allowed_poses = self._behavior_preferences.get("allowed_poses",
                                                       [item.get("id") for item in options])
        poses = [item["id"] for item in options
                 if item.get("id") in allowed_poses and {
                     str(parameter_id).casefold()
                     for parameter_id in item.get("parameters", {})
                 } <= discrete_parameters
                 and str(item.get("outfit_id") or "default")
                 == str(self._appearance.get("outfit_id") or "default")
                 and not item.get("expression") and not item.get("motion")
                 and "paramexpression" not in {
                     str(parameter_id).casefold()
                     for parameter_id in item.get("parameters", {})
                 }]
        motions = self.motion_candidates(self._behavior_intent, state)
        allowed_motions = self._behavior_preferences.get("allowed_motions")
        motions = [motion for motion in motions if self._safe_motion(motion) and (
            allowed_motions is None or list(motion) in allowed_motions)]
        if not automatic:
            poses, motions = [], []
        if self._behavior_intent == "idle":
            motions = []
        selection = self._behavior_selector.select(
            self._behavior_serial, state, self._animation_time,
            self.expression_candidates(state), poses, motions)
        if (selection and selection["pose"] is not None and len(poses) > 1
                and self._behavior_selector.rng.random() >= 0.65):
            selection["pose"] = None
        self._behavior_expression = selection["expression"]
        if selection["pose"] is not None:
            self._behavior_pose = next((item for item in options if item.get("id") == selection["pose"]), None)
        elif not poses:
            self._behavior_pose = None
        self._behavior_deadline = self._animation_time + 7 if automatic and state not in ("idle", "thinking") else None
        self._last_state = None
        if selection["motion"]:
            self.play_motion(*selection["motion"])

    def _schedule_idle_action(self):
        interval = self._idle_action_interval
        self._next_idle_action_at = self._animation_time + self._behavior_selector.rng.uniform(
            interval * 2 / 3, interval * 4 / 3)

    def _tick_idle_action(self):
        if (not self._auto_behavior or self._preview_snapshot is not None or self._paused
                or self._voice_activity or self._interaction_active
                or not (self._behavior_state == "idle" or self._behavior_expired)):
            self._next_idle_action_at = self._animation_time + self._idle_action_interval * 2 / 3
            return
        if self._animation_time < self._next_idle_action_at:
            return
        self._schedule_idle_action()
        options = self._renderer_config.get("appearance_options", [])
        allowed_poses = self._behavior_preferences.get(
            "allowed_poses", [item.get("id") for item in options])
        outfit_id = str(self._appearance.get("outfit_id") or "default")
        poses = [item for item in options
                 if item.get("id") in allowed_poses
                 and str(item.get("outfit_id") or "default") == outfit_id
                 and set(item.get("parameters", {}))
                 <= set(self._renderer_config.get("discrete_parameters", []))
                 and not item.get("expression") and not item.get("motion")
                 and "ParamExpression" not in item.get("parameters", {})]
        if (len(poses) > 1
                and self._animation_time - self._behavior_selector.last_pose_at >= 4
                and self._behavior_selector.rng.random() < 0.35):
            pose = self._behavior_selector.choose("pose", [item["id"] for item in poses])
            if pose is not None:
                self._behavior_pose = next(item for item in poses if item["id"] == pose)
                self._behavior_selector.last_pose_at = self._animation_time
        allowed = self._behavior_preferences.get("allowed_motions")
        candidates = [motion for motion in self.motion_candidates("idle")
                      if allowed is None or list(motion) in allowed]
        if self._animation_time - self._behavior_selector.last_motion_at < 4:
            return
        motion = self._behavior_selector.choose("motion", candidates)
        if motion and self.play_motion(*motion):
            self._behavior_selector.last_motion_at = self._animation_time

    def _expire_behavior(self):
        if (self._preview_snapshot is None and self._behavior_deadline is not None
                and self._animation_time >= self._behavior_deadline and not self._voice_activity):
            self._behavior_expression = self._behavior_selector.choose(
                "expression:idle", self.expression_candidates("idle"))
            self._behavior_deadline = None
            self._behavior_expired = True
            self._last_state = None
            self._stop_reaction()
            self._schedule_idle_action()

    def _stop_reaction(self):
        self._clear_stage_action()
        if self._interaction_active and self._model is not None:
            self._interaction_generation += 1
            self._model.StopAllMotions()
            self._begin_reaction_release(self._interaction_parameters)
            self._interaction_active = False
            self._interaction_parameters = set()

    def _begin_reaction_release(self, parameters):
        self._reaction_release = {key: self._last_motion_values[key] for key in parameters
                                  if key in self._last_motion_values and key not in
                                  self._renderer_config.get("discrete_parameters", [])
                                  and key != self._mouth_param_id and key not in self._physics_parameter_ids}
        self._reaction_release_elapsed = 0
        self._pending_motion_reset.update(parameters)

    def _apply_reaction_release(self, delta):
        if not self._reaction_release:
            return
        self._reaction_release_elapsed += delta
        weight = min(1.0, self._reaction_release_elapsed / .3)
        weight = weight * weight * (3 - 2 * weight)
        for key, previous in self._reaction_release.items():
            target = float(self._model.GetParamValueById(key))
            self._write_parameter(key, previous + (target - previous) * weight)
        if weight >= 1:
            self._reaction_release.clear()

    def _apply_idle_strength(self):
        if self._interaction_active or not self._idle_motion_group:
            return
        parameters = self._motion_parameter_ids.get((self._idle_motion_group, self._idle_motion_index), set())
        protected = set(self._renderer_config.get("discrete_parameters", [])) | self._physics_parameter_ids
        protected.add(self._mouth_param_id)
        for key in parameters - protected:
            neutral = self._parameter_defaults.get(key, 0)
            value = float(self._model.GetParamValueById(key))
            self._write_parameter(key, neutral + (value - neutral) * self._idle_strength)

    def begin_preview(self):
        if self._preview_snapshot is not None:
            return
        self._preview_snapshot = {
            "state": self._behavior_state, "expression": self._behavior_expression,
            "intent": self._behavior_intent,
            "pose": self._behavior_pose, "deadline": self._behavior_deadline,
            "expired": self._behavior_expired, "paused": self._paused,
            "started_at": self._animation_time,
            "frame": self._last_rendered_frame if self._paused else None,
            "parameters": ({key: float(self._model.GetParamValueById(key)) for key in self._parameter_ids}
                           if self._paused and self._model is not None else {}),
            "voice": (self._voice_activity, self._voice_paused, self._voice_activity_started_at),
            "mouth_value": self._mouth_value,
        }
        self._preview_pose = copy.deepcopy(self._behavior_pose)
        self._stop_reaction()
        self.set_paused(False)

    def preview_expression(self, state):
        if state not in self.expression_options() and state not in self._parameters_by_state and state not in self._expression_by_state:
            return False
        self.begin_preview()
        self._preview_expression = state
        self._last_state = None
        return True

    def preview_pose(self, appearance):
        self.begin_preview()
        self._preview_pose = self._normalized_appearance(appearance)
        self._last_rendered_frame = None

    def preview_motion(self, group, index=0):
        motion = (group, int(index))
        if not self._safe_motion(motion) or group == self._idle_motion_group:
            return False
        self.begin_preview()
        self._preview_motion = motion
        if not self.play_motion(*motion):
            self.end_preview()
            return False
        return True

    def end_preview(self):
        snapshot = self._preview_snapshot
        if snapshot is None:
            return
        self._stop_reaction()
        self._behavior_state = snapshot["state"]
        self._behavior_intent = snapshot["intent"]
        self._schedule_idle_action()
        self._behavior_expression = snapshot["expression"]
        self._behavior_pose = snapshot["pose"]
        self._behavior_deadline = snapshot["deadline"]
        if self._behavior_deadline is not None:
            self._behavior_deadline += self._animation_time - snapshot["started_at"]
        self._behavior_expired = snapshot["expired"]
        self._preview_snapshot = self._preview_expression = self._preview_pose = None
        self._preview_motion = None
        self.set_paused(snapshot["paused"])
        self._last_state = None
        self._last_rendered_frame = None
        if snapshot["paused"]:
            for key, value in snapshot["parameters"].items():
                self._write_parameter(key, value)
            save_parameters = getattr(self._model, "SaveParameters", None)
            if callable(save_parameters):
                save_parameters()
            if snapshot["voice"] == (self._voice_activity, self._voice_paused, self._voice_activity_started_at):
                self._mouth_value = snapshot["mouth_value"]
                self._last_rendered_frame = snapshot["frame"]
            else:
                self._apply_voice_activity()

    def play_motion(self, group, index=0):
        if self._model is None or group not in self._motion_groups:
            return False
        if not 0 <= int(index) < self._model.GetMotionCount(group):
            return False
        parameters = self._motion_parameter_ids.get((group, int(index)), set())
        self._pending_motion_reset.update(self._interaction_parameters)
        self._interaction_generation += 1
        generation = self._interaction_generation
        self._model.StopAllMotions()

        def finished(*_):
            if generation != self._interaction_generation:
                return
            self._begin_reaction_release(parameters)
            self._interaction_active = False
            self._interaction_parameters = set()
            self._clear_stage_action()
            if self._preview_motion == (group, int(index)):
                self.end_preview()

        self._interaction_active = True
        self._reaction_release.clear()
        self._interaction_parameters = parameters
        self._clear_stage_action()
        stage_action = self._stage_actions.get((group, int(index)))
        if stage_action and (stage_action["stage"]["Flights"]
                             or any(x for _, x in stage_action["stage"]["OffsetX"])):
            self._stage_action = stage_action
        if self._stage_action is not None:
            frame = self._last_rendered_frame
            bbox = frame.getchannel("A").getbbox() if frame is not None else None
            self._stage_neutral_height = (bbox[3] - bbox[1]) / frame.height if bbox else .9
        self._model.StartMotion(group, int(index), self._live2d.MotionPriority.FORCE,
                                onFinish=finished)
        return True

    def _write_parameter(self, parameter_id, value):
        if self._parameter_ids and parameter_id not in self._parameter_ids:
            return
        try:
            minimum = float(self._model.GetParamMinById(parameter_id))
            maximum = float(self._model.GetParamMaxById(parameter_id))
            value = float(value)
            if not math.isfinite(value):
                return
            if parameter_id in self._renderer_config.get("discrete_parameters", []):
                value = math.floor(value + 0.5)
            self._model.SetParamById(parameter_id, max(minimum, min(maximum, value)), 1.0)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return

    def _resolve_expression_id(self, expression):
        if not isinstance(expression, str):
            return None
        key = expression.strip()
        if not key:
            return None
        key = self._expression_by_state.get(key, key)
        parameters = getattr(self, "_expression_parameters", {})
        for candidate in (
            key,
            key.removesuffix(".exp3"),
            key.removesuffix(".exp3.json"),
        ):
            if candidate in parameters:
                return candidate
        return None

    def _apply_expression_parameters(self, expression):
        expression_id = self._resolve_expression_id(expression)
        if expression_id is None:
            return False
        parameters = self._expression_parameters.get(expression_id, [])
        if not parameters:
            return False
        for parameter_id, value, blend in parameters:
            if self._parameter_ids and parameter_id not in self._parameter_ids:
                continue
            try:
                current = float(self._model.GetParamValueById(parameter_id))
                minimum = float(self._model.GetParamMinById(parameter_id))
                maximum = float(self._model.GetParamMaxById(parameter_id))
                if blend == "overwrite":
                    target = value
                elif blend == "multiply":
                    target = current * value
                else:
                    target = current + value
                target = max(minimum, min(maximum, target))
                self._model.SetParamById(parameter_id, target, 1.0)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
        return True

    def _apply_state_parameters(self, state):
        state = self._effective_expression_state(state)
        values = dict(self._parameters_by_state.get("idle", {}))
        values.update(self._parameters_by_state.get(state, {}))
        for parameter_id in self._state_parameter_ids:
            self._write_parameter(parameter_id, values.get(parameter_id, self._parameter_defaults.get(parameter_id, 0.0)))

    def _update_pointer_target(self, canvas, width, height, mode, state, target_origin):
        if not self._follow_enabled:
            self._follow_target = [0.0, 0.0]
            return
        try:
            pointer_x, pointer_y = canvas.winfo_pointerxy()
            origin_x, origin_y = target_origin or (0, 0)
            origin_x += canvas.winfo_rootx()
            origin_y += canvas.winfo_rooty()
            settings = self._renderer_config.get("mouse_follow", {})
            anchor = settings.get("anchor", [0.5, 0.23])
            full_w, full_h = self._full_size
            crop = viewport_crop_box((full_w, full_h), resolve_viewport(self.viewport_settings, state)) if mode == "small" else (0, 0, full_w, full_h)
            ratio = min(width / (crop[2] - crop[0]), height / (crop[3] - crop[1]))
            offset_x = (width - (crop[2] - crop[0]) * ratio) / 2
            offset_y = (height - (crop[3] - crop[1]) * ratio) / 2
            center_x = origin_x + offset_x + (anchor[0] * full_w - crop[0]) * ratio
            center_y = origin_y + offset_y + (anchor[1] * full_h - crop[1]) * ratio
            radius = max(80.0, full_w * ratio * float(settings.get("radius", 0.8)))
            self._follow_target = [max(-1.0, min(1.0, (pointer_x - center_x) / radius)),
                                   max(-1.0, min(1.0, (center_y - pointer_y) / radius))]
        except (AttributeError, tk.TclError, TypeError, ValueError):
            self._follow_target = [0.0, 0.0]

    def _apply_mouse_follow(self, delta):
        settings = self._renderer_config.get("mouse_follow", {})
        if not settings:
            return
        weight = 1.0 - math.exp(-delta / max(0.02, float(settings.get("smoothing", 0.16))))
        dead_zone = max(0.0, min(0.5, float(settings.get("dead_zone", 0.04))))
        strength = 1.0
        for i in (0, 1):
            target = self._follow_target[i] if self._follow_enabled else 0.0
            target = 0.0 if abs(target) <= dead_zone else math.copysign((abs(target) - dead_zone) / (1 - dead_zone), target)
            self._follow_position[i] += weight * (target * strength - self._follow_position[i])
        x, y = self._follow_position
        bindings = settings.get("parameters", {"ParamEyeBallX": [0, 1], "ParamEyeBallY": [1, 1],
                                               "ParamAngleX": [0, 12], "ParamAngleY": [1, 8]})
        for parameter_id, binding in bindings.items():
            if ((self._interaction_active and parameter_id in self._interaction_parameters)
                    or parameter_id in self._reaction_release):
                continue
            axis, amplitude = binding
            base = 0.0
            if parameter_id.lower().startswith("paramangle"):
                base = float(self._model.GetParamValueById(parameter_id))
            self._write_parameter(parameter_id, base + (x, y)[int(axis)] * float(amplitude))

    def _apply_controllers(self, state, delta):
        self._apply_appearance_to_model()
        pose = self._preview_pose if self._preview_snapshot is not None else self._behavior_pose
        if pose:
            for key, value in pose.get("parameters", {}).items():
                self._write_parameter(key, value)
        self._apply_state_parameters(state)
        self._apply_mouse_follow(delta)
        blink = self._renderer_config.get("closed_eye_expressions", {})
        if blink and int(round(self._model.GetParamValueById(blink.get("parameter", "ParamExpression")))) in blink.get("values", []):
            for parameter_id in blink.get("eye_parameters", ["ParamEyeLOpen", "ParamEyeROpen"]):
                self._write_parameter(parameter_id, 0.0)
        self._apply_voice_activity()

    def parameter_specs(self):
        """Expose bounded runtime parameters for the isolated pose editor."""
        if not self.prepare("idle"):
            raise RuntimeError(self.last_error)
        names = {}
        try:
            config = json.loads(self.model_path.read_text(encoding="utf-8-sig"))
            relative = config.get("FileReferences", {}).get("DisplayInfo")
            if relative:
                path = (self.model_path.parent / relative).resolve()
                if path.is_relative_to(self.model_path.parent.resolve()):
                    display = json.loads(path.read_text(encoding="utf-8-sig"))
                    names = {row["Id"]: row.get("Name", row["Id"])
                             for row in display.get("Parameters", [])
                             if isinstance(row, dict) and "Id" in row}
        except (OSError, ValueError, TypeError):
            pass
        result = []
        for key in sorted(self._parameter_ids):
            minimum = float(self._model.GetParamMinById(key))
            maximum = float(self._model.GetParamMaxById(key))
            default = float(self._model.GetParamDefaultById(key))
            if all(math.isfinite(value) for value in (minimum, maximum, default)) and maximum > minimum:
                result.append({"id": key, "name": names.get(key) or key,
                               "minimum": minimum, "maximum": maximum, "default": default,
                               "step": 1 if key in self._renderer_config.get("discrete_parameters", []) else .01})
        return result

    @staticmethod
    def _thumbnail_target(target_size):
        if target_size is None:
            return None
        try:
            width, height = (max(1, int(value)) for value in target_size)
        except (TypeError, ValueError):
            return None
        return width, height

    def _thumbnail_render_size(self, target_size, face):
        """Choose the smallest render that still gives a sharp cropped card."""
        target = self._thumbnail_target(target_size)
        full_width, full_height = (
            max(1, int(value)) for value in (self._full_size or (1, 1))
        )
        if target is None:
            width = max(1, round(480 * full_width / full_height))
            return width, 480
        target_width, target_height = target
        if not face:
            scale = min(target_width / full_width, target_height / full_height)
            return max(1, round(full_width * scale)), max(1, round(full_height * scale))
        viewport = self._renderer_config.get(
            "expression_preview_viewport",
            resolve_viewport(self.viewport_settings, "idle"),
        )
        viewport = viewport if isinstance(viewport, dict) else {}
        try:
            crop_width = max(0.01, min(1.0, float(viewport.get("width", 1.0))))
            crop_height = max(0.01, min(1.0, float(viewport.get("height", 1.0))))
        except (TypeError, ValueError):
            crop_width, crop_height = 1.0, 1.0
        # Crop after rendering, so the source must be larger than the card by
        # the inverse of the viewport fractions.
        source_height = max(
            1,
            math.ceil(max(target_height / crop_height, target_width / crop_width)),
        )
        source_width = max(1, round(source_height * full_width / full_height))
        return source_width, source_height

    def _finish_thumbnail(self, image, target_size, face):
        if face:
            viewport = self._renderer_config.get(
                "expression_preview_viewport",
                resolve_viewport(self.viewport_settings, "idle"),
            )
            image = crop_viewport(image, viewport)
        target = self._thumbnail_target(target_size)
        if target is not None:
            image.thumbnail(target, Image.Resampling.LANCZOS)
        return image.copy()

    def _release_current_context(self):
        glfw = self.__class__._runtime_glfw
        if glfw is not None:
            glfw.make_context_current(None)

    @contextmanager
    def _runtime_context(self):
        with self._runtime_lock:
            try:
                yield
            finally:
                # The next render may run on the settings worker or UI thread.
                self._release_current_context()

    def preview_thumbnail(self, *, appearance=None, expression=None, face=False,
                          target_size=None):
        """Render a thumbnail on an isolated renderer, without a display HWND."""
        with self._runtime_context():
            if not self.prepare("idle"):
                raise RuntimeError(self.last_error)
            self.end_preview()
            self.set_paused(False)
            self.set_mouse_follow(False)
            self.set_idle_strength(0)
            self.set_behavior_preferences({"auto_behavior": False})
            if appearance is not None:
                self.preview_pose(appearance)
            if expression is not None:
                self.preview_expression(expression)
            width, height = self._thumbnail_render_size(target_size, face)
            self._ensure_context(width, height)
            image = self._draw_frame(width, height, "idle")
            return self._finish_thumbnail(image, target_size, face)

    def shared_thumbnail(self, *, appearance=None, expression=None, face=False,
                         target_size=None):
        """Render from an already loaded model without changing its runtime state."""
        with self._runtime_context():
            if self._model is None:
                return None
            full = self._full_size
            if not full or not self._parameter_ids:
                return None
            normalized = self._normalized_appearance(appearance)
            requested_expression = expression if expression is not None else normalized.get("expression")
            if requested_expression is not None and self._resolve_expression_id(requested_expression) is None:
                return None
            width, height = self._thumbnail_render_size(target_size, face)
            old_frame_size = tuple(self._frame_size)
            runtime = self.__class__
            old_window_size = tuple(runtime._runtime_window_size)
            previous = {}
            try:
                self._ensure_context(width, height)
                self._glfw.make_context_current(self._window)
                for parameter_id in self._parameter_ids:
                    try:
                        previous[parameter_id] = float(
                            self._model.GetParamValueById(parameter_id)
                        )
                    except (AttributeError, RuntimeError, TypeError, ValueError):
                        continue
                for parameter_id, value in normalized.get("parameters", {}).items():
                    self._write_parameter(parameter_id, value)
                if requested_expression is not None:
                    self._apply_expression_parameters(requested_expression)
                self._gl.glViewport(0, 0, width, height)
                self._gl.glClearColor(0.0, 0.0, 0.0, 0.0)
                self._gl.glClear(self._gl.GL_COLOR_BUFFER_BIT)
                self._model.Draw()
                raw = self._gl.glReadPixels(
                    0, 0, width, height, self._gl.GL_RGBA, self._gl.GL_UNSIGNED_BYTE
                )
                if not raw:
                    raise RuntimeError("Live2D 未输出预览图像")
                image = Image.frombytes("RGBa", (width, height), raw).convert("RGBA")
                image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            finally:
                for parameter_id, value in previous.items():
                    try:
                        self._model.SetParamById(parameter_id, value, 1.0)
                    except (AttributeError, RuntimeError, TypeError, ValueError):
                        continue
                restore_size = (
                    old_frame_size
                    if all(size > 0 for size in old_frame_size)
                    else (width, height)
                )
                if self._frame_size != restore_size:
                    try:
                        self._model.Resize(*restore_size)
                        self._frame_size = restore_size
                    except (AttributeError, RuntimeError, TypeError, ValueError):
                        pass
                restore_window_size = (
                    old_window_size
                    if all(size > 0 for size in old_window_size)
                    else restore_size
                )
                if runtime._runtime_window_size != restore_window_size:
                    try:
                        self._glfw.set_window_size(self._window, *restore_window_size)
                        runtime._runtime_window_size = restore_window_size
                    except (AttributeError, RuntimeError, TypeError, ValueError):
                        pass
                try:
                    self._gl.glViewport(0, 0, *restore_size)
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    pass
            return self._finish_thumbnail(image, target_size, face)

    @staticmethod
    def _stage_screen_bounds(canvas, bbox):
        try:
            if os.name == "nt":
                class MonitorInfo(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

                user32 = ctypes.windll.user32
                user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
                user32.MonitorFromRect.restype = wintypes.HANDLE
                user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
                user32.GetMonitorInfoW.restype = wintypes.BOOL
                rect = wintypes.RECT(*bbox)
                monitor = user32.MonitorFromRect(ctypes.byref(rect), 2)
                info = MonitorInfo()
                info.cbSize = ctypes.sizeof(info)
                if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    rect = info.rcMonitor
                    return rect.left, rect.top, rect.right, rect.bottom
            return 0, 0, canvas.winfo_screenwidth(), canvas.winfo_screenheight()
        except (AttributeError, OSError, tk.TclError, TypeError, ValueError):
            return None

    def _present(self, canvas, image, scale=1.0, target_size=None, target_origin=None):
        canvas_width = int(canvas.cget("width")) if target_size is None else int(target_size[0])
        canvas_height = int(canvas.cget("height")) if target_size is None else int(target_size[1])
        if target_size is None:
            target_size = (canvas_width, canvas_height)
        layout_width, layout_height = self.natural_size or image.size
        ratio = min(target_size[0] / float(layout_width), target_size[1] / float(layout_height))
        size = (max(1, int(round(layout_width * ratio))), max(1, int(round(layout_height * ratio))))
        source = image.convert("RGBa").resize(size, Image.Resampling.LANCZOS).convert("RGBA")
        if self._resize_overlay_enabled and ImageDraw is not None:
            source, _, _ = StaticPortraitRenderer._compose_resize_overlay(self, source)
        if target_origin is None:
            x = (canvas_width - size[0]) // 2
            y = (canvas_height - size[1]) // 2
        else:
            x, y = int(target_origin[0]), int(target_origin[1])
        self.image = source
        self.image_bbox = (x, y, x + size[0], y + size[1])
        display_x, display_y = x, y
        self._stage_pixel_offset = (0, 0)
        if self._stage_offset != (0.0, 0.0):
            # Move the complete overlay, not pixels inside a clipped model canvas.
            full_height = self._full_size[1] * ratio
            offset = tuple(value * full_height for value in self._stage_offset)
            bbox = source.getchannel("A").getbbox()
            if bbox is not None:
                root_x, root_y = canvas.winfo_rootx() + x, canvas.winfo_rooty() + y
                screen_bbox = (root_x + bbox[0], root_y + bbox[1],
                               root_x + bbox[2], root_y + bbox[3])
                offset = bounded_offset(offset, screen_bbox, self._stage_screen_bounds(canvas, screen_bbox))
            self._stage_pixel_offset = (round(offset[0]), round(offset[1]))
            display_x += self._stage_pixel_offset[0]
            display_y += self._stage_pixel_offset[1]
        if os.name == "nt" and Image is not None:
            if self._alpha_overlay is None:
                self._alpha_overlay = _WindowsAlphaOverlay(canvas)
                if self._geometry_sync_suspended:
                    self._alpha_overlay.suspend_geometry_sync()
            self._alpha_overlay.set_input_hit_padding(self._input_hit_padding)
            self._alpha_overlay.set_topmost(self._topmost)
            self._alpha_overlay.set_mouse_passthrough(
                getattr(self, "_mouse_passthrough", False))
            if not self._alpha_overlay.update(source, display_x, display_y):
                raise OSError(self._alpha_overlay._last_sync_error or "Live2D 透明帧提交失败")
        else:
            self.image_item = ImageTk.PhotoImage(source)
            canvas.delete("portrait_image")
            canvas.create_image(display_x, display_y, image=self.image_item, anchor="nw", tags="portrait_image")
        canvas.delete("portrait_placeholder")
        canvas.delete("resize_box")
        return True

    def begin_geometry_transaction(self):
        self._geometry_transaction_depth += 1
        self._geometry_sync_suspended = True
        if self._geometry_transaction_depth == 1 and self._alpha_overlay is not None:
            self._alpha_overlay.suspend_geometry_sync()

    def end_geometry_transaction(self, flush=False):
        if self._geometry_transaction_depth == 0:
            return False
        self._geometry_transaction_depth -= 1
        if self._geometry_transaction_depth:
            return False
        self._geometry_sync_suspended = False
        if self._alpha_overlay is None:
            return False
        return self._alpha_overlay.resume_geometry_sync(flush=flush)

    def move_presented(self, dx, dy):
        if self._alpha_overlay is None:
            return False
        return self._alpha_overlay.move_presented(dx, dy)

    def prepare(self, state, mode="full"):
        with self._runtime_context():
            try:
                if self._model is None:
                    self._ensure_context(16, 16)
                full = self._full_size or (1972, 2000)
                box = viewport_crop_box(full, resolve_viewport(self.viewport_settings, state)) if mode == "small" else (0, 0, *full)
                self.natural_size = (box[2] - box[0], box[3] - box[1])
                return True
            except Exception as error:
                self.last_error = "{}: {}".format(type(error).__name__, error)
                self._failed = True
                return False

    def render(self, canvas, state, mode="full", scale=1.0, preview=False, target_size=None, target_origin=None):
        if Image is None:
            self.last_error = "Pillow 不可用"
            return False
        if not self._runtime_lock.acquire(blocking=False):
            return False
        try:
            width, height = target_size or (int(canvas.cget("width")), int(canvas.cget("height")))
            started = time.monotonic()
            self._ensure_context(16, 16) if self._model is None else None
            full_w, full_h = self._full_size
            box = viewport_crop_box(self._full_size, resolve_viewport(self.viewport_settings, state)) if mode == "small" else (0, 0, full_w, full_h)
            # Layout must use model-space dimensions, not rounded raster dimensions.
            self.natural_size = (box[2] - box[0], box[3] - box[1])
            ratio = min(width / (box[2] - box[0]), height / (box[3] - box[1]))
            render_h = min(int(self._renderer_config.get("max_render_height", 1600)), max(64, int(round(full_h * ratio))))
            render_w = max(1, int(round(render_h * full_w / full_h)))
            self._ensure_context(render_w, render_h)
            self._update_pointer_target(canvas, width, height, mode, state, target_origin)
            image = self._draw_frame(render_w, render_h, state)
            if mode == "small":
                image = crop_viewport(image, resolve_viewport(self.viewport_settings, state))
            self.last_error = ""
            found = self._present(canvas, image, scale, target_size, target_origin)
            self._frame_durations.append(time.monotonic() - started)
            self._frame_durations = self._frame_durations[-120:]
            return found
        except Exception as error:
            self.last_error = "{}: {}".format(type(error).__name__, error)
            self._failed = True
            _portrait_log("live2d_renderer_failed model={} error={}".format(self.model_path, self.last_error))
            return False
        finally:
            try:
                self._release_current_context()
            finally:
                self._runtime_lock.release()

    def tick(self, canvas, state, mode="full"):
        """刷新已加载模型的一帧，避免动画只能在状态改变时更新。"""
        if self._failed or self._model is None:
            return False
        try:
            width = max(1, int(canvas.winfo_width() or canvas.cget("width")))
            height = max(1, int(canvas.winfo_height() or canvas.cget("height")))
        except (AttributeError, tk.TclError, TypeError, ValueError):
            return False
        return self.render(canvas, state, mode, target_size=(width, height))

    def hide(self, preserve_pressed=False):
        if self._stage_action is not None:
            self.end_preview() if self._preview_snapshot is not None else self._stop_reaction()
        self._last_frame_at = None
        self._follow_position = [0.0, 0.0]
        self._follow_target = [0.0, 0.0]
        if self._alpha_overlay is not None:
            self._alpha_overlay.hide(preserve_pressed=preserve_pressed)

    def close(self):
        with self._runtime_context():
            self._clear_stage_action()
            window = self._window
            glfw = self._glfw
            overlay = self._alpha_overlay
            # 先停止覆盖层回调，再在仍存活的 GL 上下文中释放当前模型。
            self._alpha_overlay = None
            if overlay is not None:
                try:
                    overlay.close()
                except (AttributeError, OSError, RuntimeError, tk.TclError, TypeError, ValueError):
                    pass
            try:
                if window is not None and glfw is not None:
                    glfw.make_context_current(window)
                if self._model is not None:
                    self._model.DestroyRenderer()
            except Exception:
                pass
            self._model = None
            # 全局着色器与隐藏窗口属于同一生命周期，不能在角色切换时销毁窗口。
            self._live2d = None
            self._glfw = None
            self._gl = None
            self._window = None
            self._model = None
            self._gl_initialized = False
            self._runtime_initialized = False
            self._failed = True
            self._voice_activity = False
            self._voice_paused = False
            self._mouth_param_id = None
            self._alpha_overlay = None
            self.image = None
            self.image_item = None
            self.image_bbox = None

    def set_topmost(self, topmost, force=False):
        self._topmost = bool(topmost)
        if self._alpha_overlay is not None:
            self._alpha_overlay.set_topmost(self._topmost, force=force)

    def set_input_hit_padding(self, padding):
        try:
            self._input_hit_padding = max(0, int(padding))
        except (TypeError, ValueError):
            self._input_hit_padding = 0
        if self._alpha_overlay is not None:
            self._alpha_overlay.set_input_hit_padding(self._input_hit_padding)

    def set_resize_overlay(self, enabled, color=None):
        self._resize_overlay_enabled = bool(enabled)
        if color:
            self._resize_overlay_color = str(color)


def _premultiplied_bgra_bytes(image):
    """Return the premultiplied BGRA byte order required by UpdateLayeredWindow."""
    premultiplied = image.convert("RGBa")
    red, green, blue, alpha = premultiplied.split()
    return Image.merge("RGBA", (blue, green, red, alpha)).tobytes()


class _Win32Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _Win32Size(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class _Win32Blend(ctypes.Structure):
    _fields_ = [
        ("operation", ctypes.c_ubyte),
        ("flags", ctypes.c_ubyte),
        ("source_constant_alpha", ctypes.c_ubyte),
        ("alpha_format", ctypes.c_ubyte),
    ]


class _Win32BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("width", wintypes.LONG),
        ("height", wintypes.LONG),
        ("planes", wintypes.WORD),
        ("bit_count", wintypes.WORD),
        ("compression", wintypes.DWORD),
        ("image_size", wintypes.DWORD),
        ("x_pixels_per_meter", wintypes.LONG),
        ("y_pixels_per_meter", wintypes.LONG),
        ("colors_used", wintypes.DWORD),
        ("colors_important", wintypes.DWORD),
    ]


class _Win32BitmapInfo(ctypes.Structure):
    _fields_ = [
        ("header", _Win32BitmapInfoHeader),
        ("colors", wintypes.DWORD * 3),
    ]


class _Win32Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _WindowsAlphaOverlay(object):
    """Draw a portrait with per-pixel alpha above Tk's color-key window."""

    _POLL_INTERVAL_MS = 16
    _BUTTONS = (
        (1, 0x01, 0x01, "ButtonPress-1", "ButtonRelease-1"),
        (2, 0x04, 0x02, "ButtonPress-2", "ButtonRelease-2"),
        (3, 0x02, 0x04, "ButtonPress-3", "ButtonRelease-3"),
    )

    def __init__(self, canvas):
        self.canvas = canvas
        self.root = canvas.winfo_toplevel()
        self.window = tk.Toplevel(self.root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        try:
            self.window.transient(self.root)
        except tk.TclError:
            pass
        self.window.geometry("1x1+0+0")
        self.window.update_idletasks()
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._configure_win32_signatures()
        self._configure_input_signatures()
        self.hwnd = None
        self._refresh_hwnd()
        _portrait_log(
            "overlay_init hwnd=0x{:X} tk_id=0x{:X}".format(
                int(self.hwnd),
                int(self.window.winfo_id()),
            )
        )

        self._user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self._user32.SetWindowPos.restype = wintypes.BOOL
        try:
            self._topmost = bool(self.root.attributes("-topmost"))
        except (AttributeError, tk.TclError, TypeError, ValueError):
            self._topmost = False
        self._ensure_window_styles()

        self._image = None
        self._presented_image = None
        self._presented_x = 0
        self._presented_y = 0
        self._x = 0
        self._y = 0
        self._visible = False
        self._last_sync_error = ""
        self._input_after_id = None
        self._input_buttons = {}
        self._input_active_buttons = set()
        self._last_click = {}
        self._geometry_sync_suspend_depth = 0
        self._geometry_sync_dirty = False
        self._geometry_sync_after_id = None
        # 覆盖层隐藏后，鼠标可能仍处于按下状态。重新显示时先等待
        # 所有物理按键释放，避免把持续按下误判成新的拖动起点。
        self._input_rearm_until_release = True
        self._input_hit_padding = 0
        self._closed = False
        self._last_applied_topmost = None
        self._root_bindings = (
            self.root.bind("<Configure>", self._on_root_configure, add="+"),
            self.root.bind("<Map>", self._on_root_map, add="+"),
            self.root.bind("<Unmap>", self._on_root_unmap, add="+"),
        )
        self._canvas_binding = self.canvas.bind(
            "<Configure>", self._on_canvas_configure, add="+"
        )

    def _refresh_hwnd(self):
        """重新读取 Tk Toplevel 当前的原生句柄。

        Windows 可能在 Tk 的 withdraw/deiconify 周期中重建 Toplevel 的
        HWND。缓存旧句柄会让 UpdateLayeredWindow 变成 WinError 1400，
        进而错误地落到 Tk Canvas 回退路径。
        """
        tk_id = int(self.window.winfo_id())
        candidate = self._user32.GetAncestor(
            wintypes.HWND(tk_id),
            2,
        )
        if not candidate or not self._user32.IsWindow(candidate):
            raise ctypes.WinError(ctypes.get_last_error() or 1400)
        previous = getattr(self, "hwnd", None)
        changed = previous is None or int(previous) != int(candidate)
        self.hwnd = candidate
        if changed:
            self._last_applied_topmost = None
            _portrait_log(
                "overlay_hwnd_refresh old={} new=0x{:X} tk_id=0x{:X}".format(
                    "none" if previous is None else "0x{:X}".format(int(previous)),
                    int(candidate),
                    tk_id,
                )
            )
        return changed

    def _configure_input_signatures(self):
        self._user32.GetCursorPos.argtypes = [ctypes.POINTER(_Win32Point)]
        self._user32.GetCursorPos.restype = wintypes.BOOL
        self._user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self._user32.GetAsyncKeyState.restype = ctypes.c_short
        self._user32.GetDoubleClickTime.restype = wintypes.UINT
        self._user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self._user32.GetSystemMetrics.restype = ctypes.c_int
        self._user32.GetWindowRect.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(_Win32Rect),
        ]
        self._user32.GetWindowRect.restype = wintypes.BOOL

    def _cancel_geometry_sync_callback(self):
        after_id = getattr(self, "_geometry_sync_after_id", None)
        self._geometry_sync_after_id = None
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass

    def _run_queued_geometry_sync(self):
        self._geometry_sync_after_id = None
        if getattr(self, "_closed", False):
            return
        if getattr(self, "_geometry_sync_suspend_depth", 0):
            self._geometry_sync_dirty = True
            return
        self._geometry_sync_dirty = False
        self.sync_position()

    def _queue_geometry_sync(self):
        if getattr(self, "_closed", False):
            return
        if getattr(self, "_geometry_sync_suspend_depth", 0):
            self._geometry_sync_dirty = True
            return
        if self._geometry_sync_after_id is None:
            self._geometry_sync_after_id = self.root.after_idle(
                self._run_queued_geometry_sync
            )

    def suspend_geometry_sync(self):
        """Freeze overlay movement while Tk changes the root geometry.

        Tk emits Configure events while a resize is still in progress. If
        each event immediately moves the independent layered window, users
        briefly see the portrait at intermediate coordinates. Keep the last
        presented frame until the final geometry is ready.
        """
        self._geometry_sync_suspend_depth += 1
        if self._geometry_sync_suspend_depth == 1:
            self._geometry_sync_dirty = True
            self._cancel_geometry_sync_callback()

    def resume_geometry_sync(self, flush=False):
        """Resume overlay movement and optionally present one final frame."""
        if self._geometry_sync_suspend_depth <= 0:
            return False
        self._geometry_sync_suspend_depth -= 1
        if self._geometry_sync_suspend_depth:
            return False
        dirty = self._geometry_sync_dirty
        self._geometry_sync_dirty = False
        if not dirty:
            return False
        if flush:
            return self.sync_position()
        self._queue_geometry_sync()
        return True

    def _schedule_input_poll(self):
        if (
            not getattr(self, "_closed", False)
            and self._input_after_id is None
            and self._visible
        ):
            self._input_after_id = self.root.after(
                self._POLL_INTERVAL_MS,
                self._poll_input,
            )

    def _cancel_input_poll(self, clear_buttons=True):
        after_id = getattr(self, "_input_after_id", None)
        self._input_after_id = None
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        # 预览缩放会在鼠标仍按住时重绘并重新显示覆盖层。此时不能把
        # `_input_buttons` 清空，否则下一轮轮询会把同一个持续按住的
        # 鼠标误判成新的 ButtonPress，并进一步误触发双击切换编辑模式。
        if clear_buttons:
            getattr(self, "_input_buttons", {}).clear()
            getattr(self, "_input_active_buttons", set()).clear()

    def _arm_input_after_hide(self):
        """让覆盖层重新显示后先完成一次全局鼠标释放同步。"""
        self._input_rearm_until_release = True
        getattr(self, "_input_buttons", {}).clear()
        getattr(self, "_input_active_buttons", set()).clear()
        getattr(self, "_last_click", {}).clear()

    def _generate_double_click(self, button, x, y, root_x, root_y, state):
        now = time.monotonic()
        previous = self._last_click.get(button)
        self._last_click[button] = (now, root_x, root_y)
        if previous is None:
            return
        try:
            interval = self._user32.GetDoubleClickTime() / 1000.0
            width = self._user32.GetSystemMetrics(36)
            height = self._user32.GetSystemMetrics(37)
        except (AttributeError, OSError, TypeError, ValueError):
            interval, width, height = 0.5, 4, 4
        previous_time, previous_x, previous_y = previous
        if (
            now - previous_time <= interval
            and abs(root_x - previous_x) <= width
            and abs(root_y - previous_y) <= height
        ):
            self._last_click.pop(button, None)
            self._input_event(
                "<<PortraitDoubleClick-{}>>".format(button),
                x,
                y,
                root_x,
                root_y,
                state,
            )

    def _pointer_position(self):
        point = _Win32Point()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    def _input_event(self, sequence, x, y, root_x, root_y, state=0):
        if getattr(self, "_mouse_passthrough", False):
            return False
        try:
            event_sequence = (
                sequence
                if sequence.startswith("<<") and sequence.endswith(">>")
                else "<{}>".format(sequence)
            )
            self.canvas.event_generate(
                event_sequence,
                x=int(x),
                y=int(y),
                rootx=int(root_x),
                rooty=int(root_y),
                state=int(state),
                when="tail",
            )
        except (tk.TclError, TypeError, ValueError):
            return False
        return True

    def _poll_input(self):
        self._input_after_id = None
        if getattr(self, "_mouse_passthrough", False):
            self._arm_input_after_hide()
            self._schedule_input_poll()
            return
        if not self._visible or self._image is None:
            self._input_buttons.clear()
            getattr(self, "_input_active_buttons", set()).clear()
            return
        try:
            # Tk redirects generated events to modal grabs, corrupting editor drags.
            grab = self.canvas.tk.call("grab", "current", str(self.canvas))
            if grab and str(grab) != str(self.canvas):
                self._arm_input_after_hide()
                self._schedule_input_poll()
                return
            pointer = self._pointer_position()
            if pointer is None:
                self._schedule_input_poll()
                return
            root_x, root_y = pointer
            canvas_x = root_x - self.canvas.winfo_rootx()
            canvas_y = root_y - self.canvas.winfo_rooty()
            image_width, image_height = self._image.size
            padding = max(0, int(getattr(self, "_input_hit_padding", 0)))
            inside = (
                self._x - padding <= canvas_x < self._x + image_width + padding
                and self._y - padding <= canvas_y < self._y + image_height + padding
            )
            # Global polling must not start a pet drag through an owned toolbar.
            input_blocked = vars(self.canvas).get("_portrait_input_blocked")
            if inside and callable(input_blocked) and input_blocked(root_x, root_y):
                inside = False
            event_x = canvas_x
            event_y = canvas_y
            current = {}
            state = 0
            for button, virtual_key, mask, _press, _release in self._BUTTONS:
                down = bool(self._user32.GetAsyncKeyState(virtual_key) & 0x8000)
                current[button] = down
                if down:
                    state |= mask << 8

            if getattr(self, "_input_rearm_until_release", False):
                # 不发送释放事件：隐藏期间没有向 Tk 发送按下事件，释放也
                # 不应被解释成一次新的交互。所有按键均释放后才重新接收。
                if not any(current.values()):
                    self._input_rearm_until_release = False
                self._input_buttons.clear()
                self._schedule_input_poll()
                return

            active = self._input_active_buttons
            for button, _virtual_key, mask, press, release in self._BUTTONS:
                was_down = self._input_buttons.get(button, False)
                is_down = current[button]
                if is_down and not was_down and inside:
                    active.add(button)
                    self._input_event(
                        press,
                        event_x,
                        event_y,
                        root_x,
                        root_y,
                        state,
                    )
                    self._generate_double_click(
                        button,
                        event_x,
                        event_y,
                        root_x,
                        root_y,
                        state,
                    )
                elif was_down and not is_down and button in active:
                    self._input_event(
                        release,
                        event_x,
                        event_y,
                        root_x,
                        root_y,
                        state,
                    )
                    active.discard(button)

            if any(current[button] for button in active):
                self._input_event(
                    "Motion",
                    event_x,
                    event_y,
                    root_x,
                    root_y,
                    state,
                )
            self._input_buttons = current
        except (AttributeError, OSError, tk.TclError, TypeError, ValueError):
            self._input_buttons.clear()
            getattr(self, "_input_active_buttons", set()).clear()
        self._schedule_input_poll()

    def set_mouse_passthrough(self, enabled):
        enabled = bool(enabled)
        if enabled == getattr(self, "_mouse_passthrough", False):
            return
        self._mouse_passthrough = enabled
        self._arm_input_after_hide()
        self._last_click.clear()
        self._ensure_window_styles()

    def set_input_hit_padding(self, padding):
        """扩大轮询命中区，用于接收画布外沿的缩放控制柄输入。"""
        try:
            self._input_hit_padding = max(0, int(padding))
        except (TypeError, ValueError):
            self._input_hit_padding = 0

    def set_topmost(self, topmost, force=False):
        """让分层窗口与桌宠主窗口使用相同的置顶状态。"""
        topmost = bool(topmost)
        changed = topmost != getattr(self, "_topmost", False)
        self._topmost = topmost
        if force or changed or getattr(self, "_last_applied_topmost", None) is None:
            self._ensure_window_styles(force_z_order=True)

    def _ensure_window_styles(self, force_z_order=False):
        self._refresh_hwnd()
        get_window_long = getattr(self._user32, "GetWindowLongPtrW", None)
        set_window_long = getattr(self._user32, "SetWindowLongPtrW", None)
        if get_window_long is None:
            get_window_long = self._user32.GetWindowLongW
            set_window_long = self._user32.SetWindowLongW
        style = get_window_long(self.hwnd, -20)
        expected_style = _overlay_extended_style(
            style, getattr(self, "_mouse_passthrough", False))
        ctypes.set_last_error(0)
        set_window_long(self.hwnd, -20, expected_style)
        error_code = ctypes.get_last_error()
        if error_code:
            raise ctypes.WinError(error_code)
        if get_window_long(self.hwnd, -20) != expected_style:
            raise OSError("Windows did not retain the layered-window styles")
        topmost = bool(getattr(self, "_topmost", False))
        needs_z_order = (
            force_z_order
            or getattr(self, "_last_applied_topmost", None) is None
            or topmost != self._last_applied_topmost
        )
        if not needs_z_order:
            return
        if not self._user32.SetWindowPos(
            self.hwnd,
            _overlay_insert_after(topmost),
            0,
            0,
            0,
            0,
            _overlay_frame_change_flags(),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        self._last_applied_topmost = topmost

    def _configure_win32_signatures(self):
        self._user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self._user32.GetAncestor.restype = wintypes.HWND
        get_window_long = getattr(self._user32, "GetWindowLongPtrW", None)
        set_window_long = getattr(self._user32, "SetWindowLongPtrW", None)
        if get_window_long is None:
            get_window_long = self._user32.GetWindowLongW
            set_window_long = self._user32.SetWindowLongW
        get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_window_long.restype = ctypes.c_ssize_t
        set_window_long.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_ssize_t,
        ]
        set_window_long.restype = ctypes.c_ssize_t
        self._user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.ShowWindow.restype = wintypes.BOOL
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.GetDC.argtypes = [wintypes.HWND]
        self._user32.GetDC.restype = wintypes.HDC
        self._user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self._user32.ReleaseDC.restype = ctypes.c_int
        self._user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND,
            wintypes.HDC,
            ctypes.POINTER(_Win32Point),
            ctypes.POINTER(_Win32Size),
            wintypes.HDC,
            ctypes.POINTER(_Win32Point),
            wintypes.COLORREF,
            ctypes.POINTER(_Win32Blend),
            wintypes.DWORD,
        ]
        self._user32.UpdateLayeredWindow.restype = wintypes.BOOL
        self._gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self._gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self._gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(_Win32BitmapInfo),
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        self._gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        self._gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        self._gdi32.SelectObject.restype = wintypes.HGDIOBJ
        self._gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self._gdi32.DeleteObject.restype = wintypes.BOOL
        self._gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self._gdi32.DeleteDC.restype = wintypes.BOOL

    def _on_root_configure(self, event):
        if not self._closed and event.widget is self.root:
            self._queue_geometry_sync()

    def _on_canvas_configure(self, _event):
        if not self._closed:
            self._queue_geometry_sync()

    def _on_root_map(self, event):
        if not self._closed and event.widget is self.root:
            self._queue_geometry_sync()

    def _on_root_unmap(self, event):
        if not self._closed and event.widget is self.root:
            self.hide()

    def update(self, image, x, y):
        if self._closed:
            return False
        previous = (
            self._image,
            self._x,
            self._y,
            self._visible,
            self._presented_image,
            self._presented_x,
            self._presented_y,
        )
        self._image = image
        self._x = int(x)
        self._y = int(y)
        if getattr(self, "_geometry_sync_suspend_depth", 0):
            self._geometry_sync_dirty = True
            return True
        presented = self.sync_position()
        if (
            not presented
            and previous[0] is not None
            and previous[3]
            and self._presented_image is not None
        ):
            # 新帧提交失败时保留上一帧及其坐标。下一次 Map/Configure 或
            # 资源刷新会再次尝试提交新帧，不让一次 Win32 瞬态错误造成空白。
            (
                self._image,
                self._x,
                self._y,
                self._visible,
                self._presented_image,
                self._presented_x,
                self._presented_y,
            ) = previous
        return presented

    def sync_position(self):
        if self._closed:
            return False
        if getattr(self, "_geometry_sync_suspend_depth", 0):
            self._geometry_sync_dirty = True
            return False
        if self._image is None or not self.canvas.winfo_ismapped():
            _portrait_log(
                "sync_deferred image={} canvas_mapped={}".format(
                    bool(self._image),
                    bool(self.canvas.winfo_ismapped()),
                )
            )
            if self._image is None or not self._visible:
                self.hide()
            return False
        try:
            x = self.canvas.winfo_rootx() + self._x
            y = self.canvas.winfo_rooty() + self._y
            width, height = self._image.size
            was_withdrawn = self.window.state() == "withdrawn"
            if was_withdrawn:
                # 隐藏状态下可以同步 Tk 的请求尺寸，避免首次显示时
                # 产生额外的几何变化。可见状态下不能先调用 geometry：
                # Tk 会先改变 Toplevel 的旧表面尺寸，而
                # UpdateLayeredWindow 还没提交新位图时，Windows 可能把
                # 未初始化的底部区域显示成黑色矩形。可见覆盖层的
                # 位置、尺寸和像素必须由一次 UpdateLayeredWindow 原子提交。
                self.window.geometry(
                    "{}x{}+{}+{}".format(width, height, x, y)
                )
                self.window.update_idletasks()
            hwnd_changed = self._refresh_hwnd()
            self._ensure_window_styles(force_z_order=hwnd_changed)
            # Present while the current HWND is alive. UpdateLayeredWindow
            # itself applies the new position and size, so a visible Toplevel
            # does not go through a separate Tk geometry transition.
            self._present(self._image, x, y)
            if was_withdrawn:
                # The new layered surface is ready before the HWND becomes
                # visible, so the first frame cannot expose a blank/black
                # client area.
                self.window.deiconify()
                self.window.update_idletasks()
                hwnd_changed = self._refresh_hwnd()
                self._ensure_window_styles(force_z_order=hwnd_changed)
                if hwnd_changed:
                    # Tk may recreate a withdrawn Toplevel during deiconify.
                    # The first presentation targeted the old handle; replay
                    # the complete surface on the new handle before checking
                    # visibility so the transition cannot expose a blank
                    # layered window.
                    self._present(self._image, x, y)
            if getattr(self, "_user32", None) is not None:
                native_exists = bool(self._user32.IsWindow(self.hwnd))
                native_visible = bool(self._user32.IsWindowVisible(self.hwnd))
                if not native_exists:
                    raise OSError("layered window no longer exists")
                if not native_visible:
                    # Tk can report "normal" before the native layered window
                    # is actually visible. Show it only for this transition.
                    self._user32.ShowWindow(self.hwnd, 4)
            if was_withdrawn:
                self.window.update_idletasks()
            if getattr(self, "_user32", None) is not None:
                native_exists = bool(self._user32.IsWindow(self.hwnd))
                native_visible = bool(self._user32.IsWindowVisible(self.hwnd))
            else:
                native_exists = True
                native_visible = self.window.state() != "withdrawn"
            actual_rect = self._get_window_rect()
            _portrait_log(
                "sync_presented hwnd=0x{:X} xy={},{} size={}x{} "
                "actual_xy={},{} actual_size={}x{} tk_state={} "
                "native_exists={} native_visible={}".format(
                    int(getattr(self, "hwnd", 0)),
                    x,
                    y,
                    self._image.size[0],
                    self._image.size[1],
                    actual_rect[0] if actual_rect else "?",
                    actual_rect[1] if actual_rect else "?",
                    actual_rect[2] if actual_rect else "?",
                    actual_rect[3] if actual_rect else "?",
                    self.window.state(),
                    native_exists,
                    native_visible,
                )
            )
            if not native_exists or not native_visible:
                raise OSError(
                    "layered window remained hidden "
                    "(exists={}, visible={})".format(
                        native_exists,
                        native_visible,
                    )
                )
            self._last_sync_error = ""
            self._visible = True
            self._presented_image = self._image
            self._presented_x = x
            self._presented_y = y
            self._schedule_input_poll()
            return True
        except (OSError, RuntimeError, tk.TclError, TypeError, ValueError) as error:
            self._last_sync_error = "{}: {}".format(type(error).__name__, error)
            _portrait_log(
                "sync_failed hwnd=0x{:X} error={}".format(
                    int(getattr(self, "hwnd", 0)),
                    self._last_sync_error,
                )
            )
            if not self._visible or self._presented_image is None:
                self.hide()
            # 保持 Tk 的 Configure/Map 回调可继续运行，不把原生窗口失败
            # 变成未处理异常，避免桌宠随后自行退出。
            return False

    def move_presented(self, dx, dy):
        """Move the already-presented layered surface without re-uploading it."""
        if (
            getattr(self, "_closed", False)
            or not getattr(self, "_visible", False)
            or self._presented_image is None
        ):
            return False
        try:
            dx = int(dx)
            dy = int(dy)
        except (TypeError, ValueError):
            return False
        if not dx and not dy:
            return True
        try:
            self._refresh_hwnd()
            x = self._presented_x + dx
            y = self._presented_y + dy
            # NOSIZE | NOZORDER | NOACTIVATE | FRAMECHANGED
            if not self._user32.SetWindowPos(
                self.hwnd,
                None,
                x,
                y,
                0,
                0,
                0x0035,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self._presented_x = x
            self._presented_y = y
            self._last_sync_error = ""
            return True
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as error:
            self._last_sync_error = "{}: {}".format(type(error).__name__, error)
            _portrait_log(
                "move_presented_failed hwnd=0x{:X} dx={} dy={} error={}".format(
                    int(getattr(self, "hwnd", 0) or 0),
                    dx,
                    dy,
                    self._last_sync_error,
                )
            )
            return False

    def _get_window_rect(self):
        """Return the native layered window rect in screen coordinates."""
        try:
            rect = _Win32Rect()
            if not self._user32.GetWindowRect(
                self.hwnd,
                ctypes.byref(rect),
            ):
                return None
            return (
                int(rect.left),
                int(rect.top),
                max(0, int(rect.right - rect.left)),
                max(0, int(rect.bottom - rect.top)),
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def get_screen_rect(self):
        """Expose the current native rect for anchor calculations/tests."""
        return self._get_window_rect()

    def hide(self, preserve_pressed=False):
        if getattr(self, "_closed", False):
            return
        self._cancel_input_poll(clear_buttons=not preserve_pressed)
        self._cancel_geometry_sync_callback()
        if not preserve_pressed:
            self._arm_input_after_hide()
        try:
            if self.window.state() != "withdrawn":
                self.window.withdraw()
        except tk.TclError:
            pass
        self._visible = False

    def close(self):
        """Stop callbacks and destroy this renderer's independent window."""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self._visible = False
        self._cancel_input_poll()
        self._cancel_geometry_sync_callback()
        for sequence, binding_id in zip(
            ("<Configure>", "<Map>", "<Unmap>"),
            getattr(self, "_root_bindings", ()),
        ):
            if binding_id:
                try:
                    self.root.unbind(sequence, binding_id)
                except tk.TclError:
                    pass
        self._root_bindings = ()
        binding_id = getattr(self, "_canvas_binding", None)
        if binding_id:
            try:
                self.canvas.unbind("<Configure>", binding_id)
            except tk.TclError:
                pass
        self._canvas_binding = None
        self._image = None
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def _present(self, image, x, y):
        width, height = image.size
        pixels = _premultiplied_bgra_bytes(image)
        screen_dc = self._user32.GetDC(None)
        if not screen_dc:
            raise ctypes.WinError(ctypes.get_last_error())
        memory_dc = self._gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            self._user32.ReleaseDC(None, screen_dc)
            raise ctypes.WinError(ctypes.get_last_error())
        bitmap_info = _Win32BitmapInfo()
        bitmap_info.header.size = ctypes.sizeof(_Win32BitmapInfoHeader)
        bitmap_info.header.width = width
        bitmap_info.header.height = -height
        bitmap_info.header.planes = 1
        bitmap_info.header.bit_count = 32
        bitmap_info.header.compression = 0
        bits = ctypes.c_void_p()
        bitmap = self._gdi32.CreateDIBSection(
            memory_dc,
            ctypes.byref(bitmap_info),
            0,
            ctypes.byref(bits),
            None,
            0,
        )
        if not bitmap or not bits.value:
            self._gdi32.DeleteDC(memory_dc)
            self._user32.ReleaseDC(None, screen_dc)
            raise ctypes.WinError(ctypes.get_last_error())
        previous_bitmap = None
        try:
            previous_bitmap = self._gdi32.SelectObject(memory_dc, bitmap)
            if not previous_bitmap:
                raise ctypes.WinError(ctypes.get_last_error())
            ctypes.memmove(bits.value, pixels, len(pixels))

            destination = _Win32Point(x, y)
            size = _Win32Size(width, height)
            source = _Win32Point(0, 0)
            blend = _Win32Blend(0, 0, 255, 1)
            ctypes.set_last_error(0)
            result = self._user32.UpdateLayeredWindow(
                self.hwnd,
                screen_dc,
                ctypes.byref(destination),
                ctypes.byref(size),
                memory_dc,
                ctypes.byref(source),
                0,
                ctypes.byref(blend),
                2,
            )
            update_error = ctypes.get_last_error()
        finally:
            if previous_bitmap:
                self._gdi32.SelectObject(memory_dc, previous_bitmap)
            self._gdi32.DeleteObject(bitmap)
            self._gdi32.DeleteDC(memory_dc)
            self._user32.ReleaseDC(None, screen_dc)
        if not result:
            error_code = update_error or 31
            _portrait_log(
                "UpdateLayeredWindow_failed hwnd=0x{:X} error_code={} "
                "size={}x{} xy={},{}".format(
                    int(self.hwnd),
                    error_code,
                    width,
                    height,
                    x,
                    y,
                )
            )
            raise ctypes.WinError(error_code)
        _portrait_log(
            "UpdateLayeredWindow_ok hwnd=0x{:X} size={}x{} xy={},{}".format(
                int(self.hwnd),
                width,
                height,
                x,
                y,
            )
        )


class StaticPortraitRenderer(BaseRenderer):
    def __init__(
        self,
        character_dir,
        state_definitions=None,
        viewport_settings=None,
        outfit_id="default",
        outfit_folder_name=None,
        root_assets=False,
    ):
        self.character_dir = character_dir
        self.outfit_id = str(outfit_id or "default")
        default_folder_name = (
            "默认" if self.outfit_id == "default" else self.outfit_id
        )
        self.outfit_folder_name = str(
            outfit_folder_name or default_folder_name
        ).strip() or default_folder_name
        if root_assets:
            self.outfit_folder_name = ""
        self.state_definitions = (
            state_definitions if isinstance(state_definitions, dict) else {}
        )
        self.viewport_settings = (
            viewport_settings if isinstance(viewport_settings, dict) else {}
        )
        self.image = None
        self.image_item = None
        self.image_path = None
        self.image_bbox = None
        self.natural_size = None
        self._source_cache = {}
        self._scaled_cache = {}
        self._scaled_cache_order = []
        self._scaled_cache_limit = 48
        self._photo_cache = {}
        self._photo_cache_order = []
        self._alpha_overlay = None
        self._topmost = False
        self._input_hit_padding = 0
        self._resize_overlay_enabled = False
        self._resize_overlay_color = "#4EA1FF"
        self._canvas_fallback = False
        self._geometry_sync_suspended = False
        self.last_error = ""

    def begin_geometry_transaction(self):
        """Freeze the native portrait while the Tk root is being resized."""
        self._geometry_sync_suspended = True
        if self._alpha_overlay is not None:
            self._alpha_overlay.suspend_geometry_sync()

    def end_geometry_transaction(self, flush=False):
        """Release a geometry transaction and present the final frame once."""
        self._geometry_sync_suspended = False
        if self._alpha_overlay is None:
            return False
        return self._alpha_overlay.resume_geometry_sync(flush=flush)

    def move_presented(self, dx, dy):
        if self._alpha_overlay is None:
            return False
        return self._alpha_overlay.move_presented(dx, dy)

    def hide(self, preserve_pressed=False):
        """隐藏 Windows 立绘覆盖层，供切换模式和失败回滚使用。"""
        if self._alpha_overlay is not None:
            self._alpha_overlay.hide(preserve_pressed=preserve_pressed)

    def close(self):
        """释放 renderer 持有的原生窗口和图像缓存。"""
        overlay = self._alpha_overlay
        self._alpha_overlay = None
        if overlay is not None:
            close_overlay = getattr(overlay, "close", None)
            if callable(close_overlay):
                close_overlay()
            else:
                overlay.hide()
        self.image = None
        self.image_item = None
        self.image_path = None
        self.image_bbox = None
        self.natural_size = None
        self._source_cache.clear()
        self._scaled_cache.clear()
        self._scaled_cache_order.clear()
        self._photo_cache.clear()
        self._photo_cache_order.clear()

    def set_topmost(self, topmost, force=False):
        """同步 Windows 立绘覆盖层的置顶状态。"""
        self._topmost = bool(topmost)
        if self._alpha_overlay is not None:
            self._alpha_overlay.set_topmost(self._topmost, force=force)

    def set_input_hit_padding(self, padding):
        """同步缩放编辑时控制柄所需的额外鼠标命中范围。"""
        try:
            self._input_hit_padding = max(0, int(padding))
        except (TypeError, ValueError):
            self._input_hit_padding = 0
        if self._alpha_overlay is not None:
            self._alpha_overlay.set_input_hit_padding(self._input_hit_padding)

    def set_resize_overlay(self, enabled, color=None):
        """在 Windows 分层立绘中显示或隐藏临时缩放编辑框。"""
        self._resize_overlay_enabled = bool(enabled)
        if color:
            self._resize_overlay_color = str(color)

    def _compose_resize_overlay(self, source):
        """把编辑框合成到临时副本，不修改原始立绘或缩放缓存。"""
        if (
            not self._resize_overlay_enabled
            or Image is None
            or ImageDraw is None
            or source is None
        ):
            return source, 0, 0
        width, height = source.size
        if width < 2 or height < 2:
            return source, 0, 0
        # 不要为控制柄创建透明外扩画布。该区域会随 UpdateLayeredWindow
        # 一起提交，在部分 Windows 合成路径中会变成立绘下方的黑色块。
        # 控制柄只绘制在源图内部，实际命中范围由 overlay 的 hit padding
        # 提供，因此不会牺牲鼠标可操作性。
        composed = source.copy()
        draw = ImageDraw.Draw(composed)
        x1, y1 = 0, 0
        x2, y2 = width - 1, height - 1
        color = self._resize_overlay_color
        draw.rectangle((x1, y1, x2, y2), outline=color, width=2)

        # Keep the visible handle fully inside the existing image. Its logical
        # hit point uses the same fixed inset as the Canvas fallback.
        handle_x, handle_y = resize_handle_center(width, height)
        draw.rectangle(
            (
                handle_x - RESIZE_HANDLE_HALF_SIZE,
                handle_y - RESIZE_HANDLE_HALF_SIZE,
                handle_x + RESIZE_HANDLE_HALF_SIZE,
                handle_y + RESIZE_HANDLE_HALF_SIZE,
            ),
            fill=color,
            outline="#ffffff",
            width=2,
        )
        for segment in resize_handle_icon_segments(handle_x, handle_y):
            draw.line(segment, fill="#ffffff", width=2)
        return composed, 0, 0

    def _render_on_canvas(
        self,
        canvas,
        source,
        image_path,
        target_size,
        x,
        y,
        preview,
    ):
        """Use Tk's image path when native layered presentation is unavailable."""
        if not hasattr(canvas, "create_image"):
            raise OSError(self.last_error or "Tk canvas fallback is unavailable")
        cache_key = str(image_path.resolve())
        photo_key = (cache_key, target_size, bool(preview))
        photo = self._photo_cache.get(photo_key)
        if photo is None:
            master = None
            try:
                master = canvas.winfo_toplevel()
            except (AttributeError, tk.TclError):
                pass
            photo = ImageTk.PhotoImage(source, master=master)
            self._photo_cache[photo_key] = photo
            self._photo_cache_order.append(photo_key)
            if len(self._photo_cache_order) > self._scaled_cache_limit:
                expired = self._photo_cache_order.pop(0)
                self._photo_cache.pop(expired, None)
        pending_item = canvas.create_image(
            x + source.width // 2,
            y + source.height // 2,
            image=photo,
            tags="portrait_image_pending",
        )
        # 新 Canvas 项已经创建成功后再隐藏旧的分层窗口，避免 PhotoImage
        # 或 Canvas 创建失败时同时失去原生覆盖层中的上一帧。
        self.hide()
        # 只有新 PhotoImage 和 Canvas item 都创建成功后才替换旧图，
        # 避免资源或绘制失败时把上一帧一起删掉。
        canvas.delete("portrait_image")
        try:
            canvas.itemconfigure(
                pending_item,
                tags=("portrait_image",),
            )
        except (AttributeError, tk.TclError):
            # 极简测试画布或兼容实现可能没有 itemconfigure；真实 Tk
            # Canvas 始终支持它，无法改标签时仍保留已创建的新项。
            pass
        self.image = photo
        self.image_item = pending_item
        self._canvas_fallback = True
        return True

    @staticmethod
    def _canvas_fallback_allowed(canvas):
        """Tk 回退只允许用于不透明的普通画布。

        桌宠主窗口使用 color-key 透明背景。ImageTk 在这种画布上会先
        把半透明像素合成到背景色，再由 Windows 删除背景色，边缘会出现
        黑色/深色虚线。因此透明桌宠画布必须等待或修复分层窗口，不能走
        这个回退路径。
        """
        try:
            root = canvas.winfo_toplevel()
            transparent_color = root.wm_attributes("-transparentcolor")
            canvas_color = canvas.cget("bg")
            if transparent_color and str(canvas_color).lower() == str(
                transparent_color
            ).lower():
                return False
        except (AttributeError, tk.TclError, TypeError, ValueError):
            pass
        return True

    def _iter_image_candidates(self, state, mode):
        portraits = self.character_dir / "portraits"
        selected_states = [state] if state == "idle" else [state, "idle"]
        if self.outfit_folder_name:
            outfit_portraits = portraits / "outfits" / self.outfit_folder_name
            seen_candidates = set()
            for selected_state in selected_states:
                definition = self.state_definitions.get(selected_state, {})
                assets = (
                    definition.get("portrait_assets", {})
                    if isinstance(definition, dict)
                    else {}
                )
                relative_path = assets.get("full") if isinstance(assets, dict) else None
                if not relative_path:
                    continue
                try:
                    candidate = (portraits / str(relative_path)).resolve()
                    candidate.relative_to(portraits.resolve())
                    candidate.relative_to(outfit_portraits.resolve())
                except (OSError, ValueError):
                    continue
                key = str(candidate)
                if (
                    key not in seen_candidates
                    and candidate.is_file()
                    and candidate.suffix.lower() in IMAGE_EXTENSIONS
                ):
                    seen_candidates.add(key)
                    yield candidate
            return

        seen_candidates = set()
        full_directories = (portraits / "outfits" / "默认",)
        for selected_state in selected_states:
            definition = self.state_definitions.get(selected_state, {})
            assets = (
                definition.get("portrait_assets", {})
                if isinstance(definition, dict)
                else {}
            )
            relative_path = assets.get("full") if isinstance(assets, dict) else None
            if relative_path:
                try:
                    candidate = (portraits / str(relative_path)).resolve()
                    candidate.relative_to(portraits.resolve())
                except (OSError, ValueError):
                    candidate = None
                if (
                    candidate is not None
                    and candidate.is_file()
                    and candidate.suffix.lower() in IMAGE_EXTENSIONS
                ):
                    key = str(candidate)
                    if key not in seen_candidates:
                        seen_candidates.add(key)
                        yield candidate

            label = str(definition.get("label", "")).strip()
            aliases = tuple(name for name, key in EMOTION_LABEL_ALIASES.items()
                            if key == selected_state)
            for stem in (label, selected_state, *aliases):
                if not stem:
                    continue
                for full_directory in full_directories:
                    for extension in IMAGE_EXTENSIONS:
                        candidate = full_directory / (stem + extension)
                        key = str(candidate.resolve())
                        if candidate.is_file() and key not in seen_candidates:
                            seen_candidates.add(key)
                            yield candidate

    def _find_image(self, state, mode):
        return next(self._iter_image_candidates(state, mode), None)

    def _load_first_source(self, state, mode):
        load_errors = []
        for image_path in self._iter_image_candidates(state, mode):
            try:
                return image_path, self._load_source(image_path)
            except (OSError, ValueError) as error:
                load_errors.append(
                    "{}: {}".format(image_path.name, error)
                )
        if load_errors:
            raise OSError("；".join(load_errors))
        return None

    def _load_source(self, image_path):
        cache_key = str(image_path.resolve())
        source = self._source_cache.get(cache_key)
        if source is None:
            source = Image.open(str(image_path)).convert("RGBA")
            original_size = source.size
            alpha = source.getchannel("A")
            bbox = alpha.getbbox()
            if bbox:
                source = source.crop(bbox)
            self._source_cache[cache_key] = source.copy()
            _portrait_log(
                "source_loaded path={} original={}x{} cropped={}x{}".format(
                    image_path.resolve(),
                    original_size[0],
                    original_size[1],
                    source.size[0],
                    source.size[1],
                )
            )
        return source

    def _scale_source(self, source, cache_key, target_size, preview):
        scaled_key = (cache_key, target_size, bool(preview))
        scaled = self._scaled_cache.get(scaled_key)
        if scaled is not None:
            return scaled
        # Resize once from the source; premultiplied alpha prevents edge bleed.
        scaled = source.convert("RGBa").resize(
            target_size,
            Image.Resampling.LANCZOS,
        ).convert("RGBA")
        self._scaled_cache[scaled_key] = scaled
        self._scaled_cache_order.append(scaled_key)
        if len(self._scaled_cache_order) > self._scaled_cache_limit:
            expired = self._scaled_cache_order.pop(0)
            self._scaled_cache.pop(expired, None)
        return scaled

    def prepare(self, state, mode="full"):
        """预加载当前立绘并更新有效尺寸，供窗口调整前计算目标画布。"""
        if Image is None:
            self.last_error = "Pillow is unavailable"
            return bool(self.image is not None and self.image_path is not None)
        try:
            loaded = self._load_first_source(state, mode)
        except (OSError, ValueError) as error:
            self.last_error = "{}: {}".format(type(error).__name__, error)
            loaded = None
        if loaded is None:
            # 保留上一帧的尺寸和路径，让窗口仍可显示并允许下一次刷新恢复。
            return bool(self.image is not None and self.image_path is not None)
        image_path, source = loaded
        if mode == "small":
            source = crop_viewport(
                source,
                resolve_viewport(self.viewport_settings, state),
            )
        self.image_path = image_path
        self.natural_size = source.size
        return True

    def render(
        self,
        canvas,
        state,
        mode="full",
        scale=1.0,
        preview=False,
        target_size=None,
        target_origin=None,
    ):
        previous = {
            "image": self.image,
            "image_item": self.image_item,
            "image_path": self.image_path,
            "image_bbox": self.image_bbox,
            "natural_size": self.natural_size,
            "canvas_fallback": self._canvas_fallback,
        }
        previous_available = bool(
            previous["image"] is not None
            and previous["image_path"] is not None
        )
        self.last_error = ""
        try:
            loaded = self._load_first_source(state, mode)
        except (OSError, ValueError) as error:
            self.last_error = "{}: {}".format(type(error).__name__, error)
            loaded = None
        if loaded:
            image_path, loaded_source = loaded
            try:
                if Image is not None and ImageTk is not None:
                    source = loaded_source
                    local_view = mode == "small"
                    if local_view:
                        viewport = resolve_viewport(self.viewport_settings, state)
                        source = crop_viewport(source, viewport)
                        cache_key = "{}|local|{}|{}|{}|{}".format(
                            image_path.resolve(),
                            viewport["center_x"],
                            viewport["center_y"],
                            viewport.get("width", viewport["zoom"]),
                            viewport.get("height", viewport["zoom"]),
                        )
                    else:
                        cache_key = str(image_path.resolve())
                    natural_size = source.size
                    if target_size is None:
                        canvas_width = int(canvas.cget("width"))
                        canvas_height = int(canvas.cget("height"))
                    else:
                        canvas_width = max(1, int(target_size[0]))
                        canvas_height = max(1, int(target_size[1]))
                    ratio = min(
                        canvas_width / float(source.width),
                        canvas_height / float(source.height),
                    )
                    target_size = (
                        max(1, int(round(source.width * ratio))),
                        max(1, int(round(source.height * ratio))),
                    )
                    source = self._scale_source(
                        source,
                        cache_key,
                        target_size,
                        preview,
                    )
                    image_width, image_height = source.size
                    width = int(canvas.cget("width"))
                    height = int(canvas.cget("height"))
                else:
                    self.image = tk.PhotoImage(file=str(image_path))
                    image_width = self.image.width()
                    image_height = self.image.height()
                    natural_size = (image_width, image_height)
                    width = int(canvas.cget("width"))
                    height = int(canvas.cget("height"))
                if target_origin is None:
                    x = (width - image_width) // 2
                    y = (height - image_height) // 2
                else:
                    x = int(target_origin[0])
                    y = int(target_origin[1])
                image_bbox = (x, y, x + image_width, y + image_height)
                if os.name == "nt" and Image is not None:
                    if self._alpha_overlay is None:
                        self._alpha_overlay = _WindowsAlphaOverlay(canvas)
                        if self._geometry_sync_suspended:
                            self._alpha_overlay.suspend_geometry_sync()
                    set_input_hit_padding = getattr(
                        self._alpha_overlay,
                        "set_input_hit_padding",
                        None,
                    )
                    if callable(set_input_hit_padding):
                        set_input_hit_padding(self._input_hit_padding)
                    set_passthrough = getattr(
                        self._alpha_overlay, "set_mouse_passthrough", None)
                    if callable(set_passthrough):
                        set_passthrough(getattr(self, "_mouse_passthrough", False))
                    try:
                        self._topmost = bool(
                            canvas.winfo_toplevel().attributes("-topmost")
                        )
                        self._alpha_overlay.set_topmost(
                            self._topmost
                        )
                    except (AttributeError, tk.TclError, TypeError, ValueError):
                        pass
                    present_source, present_x, present_y = (
                        self._compose_resize_overlay(source)
                    )
                    presented = self._alpha_overlay.update(
                        present_source,
                        x + present_x,
                        y + present_y,
                    )
                    if not presented:
                        # 首次启动时 Tk 可能还没有完成主窗口映射。覆盖层
                        # 会保留 image，待 <Map>/启动后的刷新回调重新提交；
                        # 不能把这个时序状态当成资源或 Win32 渲染失败。
                        if not canvas.winfo_ismapped():
                            self.image = source
                            self.image_path = image_path
                            self.image_bbox = image_bbox
                            self.natural_size = natural_size
                            return True
                        self.last_error = (
                            self._alpha_overlay._last_sync_error
                            or "layered portrait presentation failed"
                        )
                        _portrait_log(
                            "renderer_canvas_fallback path={} size={}x{} "
                            "error={}".format(
                                image_path,
                                image_width,
                                image_height,
                                self.last_error,
                            )
                        )
                        if self._canvas_fallback_allowed(canvas):
                            return self._render_on_canvas(
                                canvas,
                                source,
                                image_path,
                                target_size,
                                x,
                                y,
                                preview,
                            )
                        _portrait_log(
                            "renderer_canvas_fallback_blocked path={} "
                            "reason=transparent_color_canvas".format(
                                image_path,
                            )
                        )
                        raise OSError(self.last_error)
                    self.image = source
                    self._canvas_fallback = False
                else:
                    self._render_on_canvas(
                        canvas,
                        source,
                        image_path,
                        target_size,
                        x,
                        y,
                        preview,
                    )
                self.image_path = image_path
                self.image_bbox = image_bbox
                self.natural_size = natural_size
                canvas.delete("portrait_placeholder")
                canvas.delete("resize_box")
                if not self._canvas_fallback:
                    canvas.delete("portrait_image")
                    self.image_item = None
                return True
            except (OSError, ValueError, tk.TclError) as error:
                self.last_error = "{}: {}".format(type(error).__name__, error)
                _portrait_log(
                    "renderer_failed path={} error={}".format(
                        image_path if loaded else None,
                        self.last_error,
                    )
                )
                self.image = previous["image"]

        # 资源加载或提交失败时保留上一帧和其元数据。下一次状态刷新、
        # Map 或 Configure 事件仍可继续尝试恢复。
        self.image = previous["image"]
        self.image_item = previous["image_item"]
        self.image_path = previous["image_path"]
        self.image_bbox = previous["image_bbox"]
        self.natural_size = previous["natural_size"]
        self._canvas_fallback = previous["canvas_fallback"]
        if previous_available:
            return True
        return False


