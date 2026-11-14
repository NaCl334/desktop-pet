"""Live2D 模型包校验、导入和配置辅助。"""

import json
import copy
import math
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath

from core.config import generate_character_id, get_character_dir, save_json
from core.emotion import PRESET_EMOTIONS, STATE_KEYWORDS


MODEL_SUFFIX = ".model3.json"
RESOURCE_DIRECTORIES = {
    "expression": ("表情", "expressions", "expression"),
    "motion": ("动作", "motions", "motion"),
}
RESOURCE_SUFFIXES = {"expression": ".exp3.json", "motion": ".motion3.json"}


def model_resource_scope(paths, kind, model_root=None):
    """按目录和扩展名限定候选；不要求中文文件名，也不改写模型引用。"""
    suffix = RESOURCE_SUFFIXES[kind]
    directories = RESOURCE_DIRECTORIES[kind]
    other_directories = RESOURCE_DIRECTORIES["motion" if kind == "expression" else "expression"]
    normalized = []
    folders = set()
    for value in paths:
        path = PurePosixPath(str(value or "").replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or ":" in str(path):
            continue
        parents = {part.casefold() for part in path.parts[:-1]}
        folders.update(parents.intersection(directories))
        if str(path).casefold().endswith(suffix) and not parents.intersection(other_directories):
            normalized.append((str(value).strip().replace("\\", "/"), parents))
    if model_root is not None:
        for name in directories:
            if (Path(model_root) / name).is_dir():
                folders.add(name)
    selected = [path for path, parents in normalized
                if not folders or parents.intersection(directories)]
    return {"paths": list(dict.fromkeys(selected)), "folders": sorted(folders),
            "restricted": bool(folders), "suffix": suffix}


_RESOURCE_KEYS = (
    "Moc",
    "Physics",
    "PhysicsV2",
    "Pose",
    "DisplayInfo",
)

_STATE_NAME_ALIASES = {
    "idle": ("idle", "待机", "默认", "平静", "循环", "循环动画"),
    "happy": ("happy", "高兴", "开心", "快乐", "喜悦"),
    "shy": ("shy", "害羞", "脸红", "羞"),
    "sad": ("sad", "伤心", "难过", "悲伤", "哭"),
    "angry": ("angry", "生气", "愤怒", "怒", "黑化"),
    "confused": ("confused", "疑惑", "困惑", "懵", "懵逼", "懵圈"),
    "surprised": ("surprised", "惊讶", "震惊", "惊喜"),
    "worried": ("worried", "担心", "忧虑", "不安", "焦虑", "流汗"),
    "excited": ("excited", "兴奋", "激动", "期待"),
    "speechless": ("speechless", "无语", "无言", "沉默"),
    "embarrassed": ("embarrassed", "难为情", "尴尬"),
    "playful": ("playful", "调皮"),
    "afraid": ("afraid", "害怕", "恐惧"),
    "thinking": ("thinking", "认真", "思考", "专注"),
}
_STATE_LABELS = {
    "idle": "待机",
    "happy": "高兴",
    "shy": "害羞",
    "sad": "伤心",
    "angry": "生气",
    "confused": "疑惑",
    "worried": "担心",
    "excited": "兴奋",
    "speechless": "无语",
    "embarrassed": "难为情",
    "playful": "调皮",
    "afraid": "害怕",
    "thinking": "认真",
    "surprised": "惊讶",
}
_STATE_LABELS.update({key: definition["label"] for key, definition in PRESET_EMOTIONS.items()})
_NAME_SEPARATORS = re.compile(r"[\s._\-\[\]()（）【】「」]+")


def _asset_stem(relative, suffix):
    """去掉 Live2D 双扩展名，保留用户为资源取的中文/英文名称。"""
    name = Path(str(relative or "")).name
    suffix = suffix.lower()
    if name.lower().endswith(suffix):
        name = name[: -len(suffix)]
    return name.strip()


def _name_tokens(name):
    normalized = str(name or "").strip().casefold()
    return [
        token for token in _NAME_SEPARATORS.split(normalized) if token
    ]


def _match_state_name(name):
    """按完整名称、分隔词和中文包含关系返回状态 ID。"""
    normalized = str(name or "").strip().casefold()
    tokens = set(_name_tokens(normalized))
    for state, definition in PRESET_EMOTIONS.items():
        if normalized in (state, definition["label"]) or state in tokens or definition["label"] in tokens:
            return state
    for state, definition in sorted(
        PRESET_EMOTIONS.items(), key=lambda item: len(item[1]["label"]), reverse=True
    ):
        if definition["label"] in normalized:
            return state
    for state, aliases in _STATE_NAME_ALIASES.items():
        for alias in aliases:
            alias = str(alias).casefold()
            if normalized == alias or alias in tokens:
                return state
    for state, aliases in _STATE_NAME_ALIASES.items():
        for alias in aliases:
            alias = str(alias).casefold()
            if len(alias) >= 2 and alias in normalized:
                return state
    return None


def _merge_state_mapping(mappings, state, **values):
    if not state:
        return
    mapping = mappings.setdefault(state, {})
    for key, value in values.items():
        if value not in (None, "", []):
            mapping[key] = value


def scan_model_mappings(model_config, model_root=None):
    """扫描模型资源，生成可确认的状态映射候选。

    文件名可以使用中文或英文情绪名；动作始终保存为 [动作组, 序号]。
    该函数只读配置，不修改模型文件。
    """
    refs = (
        model_config.get("FileReferences", {})
        if isinstance(model_config, dict)
        else {}
    )
    mappings = {}
    recognized = []
    unrecognized = []

    expressions = refs.get("Expressions", []) if isinstance(refs, dict) else []
    motions = refs.get("Motions", {}) if isinstance(refs, dict) else {}
    expression_paths = [item.get("File", "") for item in expressions
                        if isinstance(item, dict)] if isinstance(expressions, list) else []
    motion_paths = [item.get("File", "") for entries in motions.values()
                    if isinstance(entries, list) for item in entries if isinstance(item, dict)
                    ] if isinstance(motions, dict) else []
    expression_scope = model_resource_scope(expression_paths, "expression", model_root)
    motion_scope = model_resource_scope(motion_paths + [path for path in expression_paths
        if str(path).lower().endswith(".motion3.json")], "motion", model_root)
    if isinstance(expressions, list):
        for item in expressions:
            if not isinstance(item, dict):
                continue
            relative = str(item.get("File", "")).strip()
            if not relative.lower().endswith(".exp3.json"):
                continue
            if relative.replace("\\", "/") not in expression_scope["paths"]:
                continue
            expression_id = _asset_stem(relative, ".exp3.json")
            semantic_name = str(item.get("Name", "")).strip() or expression_id
            state = _match_state_name(semantic_name)
            if state:
                _merge_state_mapping(mappings, state, expression=expression_id)
                recognized.append(
                    {
                        "kind": "expression",
                        "file": relative,
                        "name": semantic_name,
                        "state": state,
                    }
                )
            else:
                unrecognized.append(
                    {"kind": "expression", "file": relative, "name": semantic_name}
                )

    if isinstance(motions, dict):
        for group, entries in motions.items():
            if not isinstance(entries, list):
                continue
            for index, item in enumerate(entries):
                if not isinstance(item, dict):
                    continue
                relative = str(item.get("File", "")).strip()
                if not relative.lower().endswith(".motion3.json"):
                    continue
                if relative.replace("\\", "/") not in motion_scope["paths"]:
                    continue
                motion_name = _asset_stem(relative, ".motion3.json")
                state = _match_state_name(motion_name)
                if state:
                    _merge_state_mapping(
                        mappings,
                        state,
                        motion=[str(group), index],
                    )
                    recognized.append(
                        {
                            "kind": "motion",
                            "file": relative,
                            "state": state,
                            "group": str(group),
                            "index": index,
                        }
                    )
                else:
                    unrecognized.append(
                        {
                            "kind": "motion",
                            "file": relative,
                            "group": str(group),
                            "index": index,
                        }
                    )

    # 兼容把循环动作错误放在 Expressions 列表中的旧模型。
    if isinstance(expressions, list):
        for item in expressions:
            if not isinstance(item, dict):
                continue
            relative = str(item.get("File", "")).strip()
            if not relative.lower().endswith(".motion3.json"):
                continue
            if relative.replace("\\", "/") not in motion_scope["paths"]:
                continue
            motion_name = _asset_stem(relative, ".motion3.json")
            state = _match_state_name(motion_name)
            if state:
                _merge_state_mapping(
                    mappings,
                    state,
                    motion=["idle", 0],
                )
                recognized.append(
                    {
                        "kind": "motion",
                        "file": relative,
                        "state": state,
                        "group": "idle",
                        "index": 0,
                    }
                )
            else:
                unrecognized.append(
                    {
                        "kind": "motion",
                        "file": relative,
                        "group": "idle",
                        "index": 0,
                    }
                )

    return {
        "state_mappings": mappings,
        "recognized": recognized,
        "unrecognized": unrecognized,
        "recognized_count": len(recognized),
        "unrecognized_count": len(unrecognized),
        "labels": {state: _STATE_LABELS.get(state, state) for state in mappings},
        "resource_scopes": {"expression": expression_scope, "motion": motion_scope},
    }


def format_mapping_preview(scan_result):
    """生成创建界面可直接展示的短摘要。"""
    if not isinstance(scan_result, dict):
        return "未发现可自动匹配的情绪表情或动作。"
    lines = [
        "已识别 {} 项，待手动确认 {} 项。".format(
            scan_result.get("recognized_count", 0),
            scan_result.get("unrecognized_count", 0),
        )
    ]
    for item in scan_result.get("recognized", [])[:12]:
        kind = "表情" if item.get("kind") == "expression" else "动作"
        label = _STATE_LABELS.get(item.get("state", ""), item.get("state", ""))
        target = (
            _asset_stem(item.get("file", ""), ".motion3.json") or "未命名动作"
            if kind == "动作"
            else item.get("name") or _asset_stem(item.get("file", ""), ".exp3.json")
        )
        lines.append("{}：{} -> {}".format(kind, label, target))
    if scan_result.get("unrecognized_count", 0):
        lines.append("未识别项目不会被强行绑定，可稍后在 Live2D 外观设置中手动填写。")
    return "\n".join(lines)


def _default_live2d_states(selected=()):
    """只加入选定的新版常用情绪，待机始终保留。"""
    states = {
        "idle": {
            "label": "待机",
            "enabled": True,
            "keywords": [],
            "category": "special",
        }
    }
    for state, preset in PRESET_EMOTIONS.items():
        if state not in selected:
            continue
        states[state] = {
            "label": preset["label"],
            "enabled": True,
            "keywords": list(preset["keywords"]),
            "category": "preset",
        }
    return states


def select_live2d_emotions(config, selected):
    """更新预设选择，保留已有自定义及旧情绪，不更改表情动作映射。"""
    states = {key: value for key, value in config.get("states", {}).items()
              if key not in PRESET_EMOTIONS and key != "idle"}
    states.update(_default_live2d_states(selected))
    return states


def _safe_path(root, relative):
    value = str(relative or "").strip()
    if not value:
        return None
    candidate = (Path(root) / value).resolve()
    try:
        candidate.relative_to(Path(root).resolve())
    except ValueError:
        return None
    return candidate


def _resource_values(file_references):
    for key in _RESOURCE_KEYS:
        value = file_references.get(key)
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            nested = value.get("File")
            if isinstance(nested, str):
                yield nested
    textures = file_references.get("Textures", [])
    if isinstance(textures, list):
        yield from (item for item in textures if isinstance(item, str))
    motions = file_references.get("Motions", {})
    if isinstance(motions, dict):
        for entries in motions.values():
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                for key in ("File", "Sound"):
                    value = item.get(key)
                    if isinstance(value, str):
                        yield value
    expressions = file_references.get("Expressions", [])
    if isinstance(expressions, list):
        for item in expressions:
            if isinstance(item, dict):
                value = item.get("File")
                if isinstance(value, str):
                    yield value


def _find_model_avatar(model_directory, model_name):
    """Prefer the model's own icon; fall back to a nearby image if present."""
    candidates = [
        model_directory / (Path(model_name).stem + "_ico.png"),
        model_directory / (Path(model_name).stem + ".png"),
    ]
    candidates.extend(
        path
        for path in sorted(model_directory.glob("*_ico.png"))
        if path not in candidates
    )
    return next((path for path in candidates if path.is_file()), None)


def _normalize_imported_model(model_json):
    """Normalize legacy references in the imported copy, never the source."""
    try:
        config = json.loads(model_json.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("无法读取导入的 Live2D 配置：{}".format(error))
    refs = config.get("FileReferences", {}) if isinstance(config, dict) else {}
    if not isinstance(refs, dict):
        return

    expressions = refs.get("Expressions", [])
    normalized_expressions = []
    migrated_motions = []
    if isinstance(expressions, list):
        for item in expressions:
            if not isinstance(item, dict):
                continue
            relative = str(item.get("File", "")).strip()
            if relative.lower().endswith(".exp3.json"):
                normalized_expressions.append(item)
            elif relative.lower().endswith(".motion3.json"):
                migrated_motions.append(
                    {
                        "File": relative,
                        **{
                            key: item[key]
                            for key in ("FadeInTime", "FadeOutTime", "Sound")
                            if key in item
                        },
                    }
                )

    motions = refs.get("Motions", {})
    normalized_motions = {}
    if isinstance(motions, dict):
        for group, entries in motions.items():
            if not isinstance(entries, list):
                continue
            valid = [
                entry
                for entry in entries
                if isinstance(entry, dict)
                and isinstance(entry.get("File"), str)
                and entry["File"].strip()
            ]
            if valid:
                normalized_motions[group] = valid
    if migrated_motions:
        normalized_motions.setdefault("idle", []).extend(migrated_motions)

    refs["Expressions"] = normalized_expressions
    refs["Motions"] = normalized_motions
    model_json.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def discover_model(model_source):
    """返回目录中唯一的 model3 配置；多个模型时要求用户明确选择。"""
    source = Path(model_source).expanduser().resolve()
    if source.is_file() and source.name.lower().endswith(MODEL_SUFFIX):
        return source
    if not source.is_dir():
        raise ValueError("Live2D 模型必须是 .model3.json 文件或包含该文件的文件夹。")
    candidates = sorted(source.rglob("*" + MODEL_SUFFIX))
    if not candidates:
        raise ValueError("所选文件夹中没有找到 .model3.json 模型配置。")
    if len(candidates) > 1:
        raise ValueError("所选文件夹包含多个 .model3.json，请只选择一个模型文件夹。")
    return candidates[0]


def validate_model(model_source):
    model_json = discover_model(model_source)
    try:
        config = json.loads(model_json.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("无法读取 Live2D 配置：{}".format(error))
    if not isinstance(config, dict) or config.get("Version") != 3:
        raise ValueError("当前只支持 Cubism 3/4 的 .model3.json 模型。")
    refs = config.get("FileReferences")
    if not isinstance(refs, dict):
        raise ValueError("模型缺少 FileReferences。")
    missing = []
    for relative in _resource_values(refs):
        path = _safe_path(model_json.parent, relative)
        if path is None or not path.is_file():
            missing.append(str(relative))
    if missing:
        raise ValueError("模型缺少资源：{}".format("、".join(missing[:8])))
    mapping_scan = scan_model_mappings(config, model_json.parent)
    return {
        "model_json": model_json,
        "config": config,
        "expressions": list(config.get("FileReferences", {}).get("Expressions", [])),
        "motions": config.get("FileReferences", {}).get("Motions", {}),
        "mapping_scan": mapping_scan,
        "adapter_defaults": load_model_adapter(model_json),
    }


def model_resource_options(config):
    """Describe model resources using the same IDs as the runtime."""
    refs = config.get("FileReferences", {})
    motions = []
    expressions = []
    for group, items in refs.get("Motions", {}).items():
        for index, item in enumerate(items):
            relative = item.get("File", "")
            motions.append({"name": _asset_stem(relative, ".motion3.json"),
                            "file": relative.replace("\\", "/"),
                            "group": str(group), "index": index})
    for item in refs.get("Expressions", []):
        relative = item.get("File", "")
        if not relative.lower().endswith(".exp3.json"):
            continue
        expression = str(item.get("Name", "")).strip() or _asset_stem(relative, ".exp3.json")
        expressions.append({"id": "expression:" + expression, "name": expression,
                            "expression": expression, "file": relative.replace("\\", "/")})
    return {"motions": motions, "expressions": expressions}


def model_resource_additions(previous_config, config):
    previous = model_resource_options(previous_config)
    current = model_resource_options(config)
    result = {}
    for kind in ("motions", "expressions"):
        def identity(item):
            return ((item["group"], item["file"].casefold()) if kind == "motions"
                    else (item["expression"], item["file"].casefold()))
        known = {identity(item) for item in previous[kind]}
        result[kind] = []
        for item in current[kind]:
            key = identity(item)
            if key not in known:
                result[kind].append(item)
                known.add(key)
    return result


def merge_model_expression_options(existing, config):
    """Keep custom entries while exposing every expression in the updated model."""
    result = copy.deepcopy(existing)
    known = {item["id"] for item in result}
    for item in model_resource_options(config)["expressions"]:
        if item["id"] not in known:
            result.append({key: value for key, value in item.items() if key != "file"})
            known.add(item["id"])
    return result


def load_model_adapter(model_json, character_dir=None):
    """Read optional, model-scoped desktop defaults without accepting arbitrary paths."""
    model_json = Path(model_json).resolve()
    sidecar = model_json.with_name(model_json.name.removesuffix(MODEL_SUFFIX) + ".pet3.json")
    pack = _read_json_file(sidecar)
    if (not isinstance(pack, dict) or pack.get("Version") != 1
            or pack.get("Model") != model_json.name):
        return {}
    source = pack.get("Renderer", {})
    if not isinstance(source, dict):
        return {}
    result = {}
    for key in ("controlled_update", "auto_blink", "auto_breath"):
        if type(source.get(key)) is bool:
            result[key] = source[key]
    for key in ("discrete_parameters", "safe_motion_parameters", "stage_motion_groups"):
        values = source.get(key)
        if isinstance(values, list) and all(isinstance(item, str) for item in values):
            result[key] = list(dict.fromkeys(values))
    config = _read_json_file(model_json) or {}
    refs = config.get("FileReferences", {})
    expression_ids = {
        _asset_stem(item.get("File"), ".exp3.json")
        for item in refs.get("Expressions", []) if isinstance(item, dict)
    }
    mappings = source.get("state_mappings", {})
    if isinstance(mappings, dict):
        result["state_mappings"] = {}
        for state, mapping in mappings.items():
            if not isinstance(mapping, dict):
                continue
            cleaned = {}
            if mapping.get("expression") in expression_ids:
                cleaned["expression"] = mapping["expression"]
            parameters = mapping.get("parameters", {})
            if isinstance(parameters, dict):
                parameters = {key: value for key, value in parameters.items()
                              if type(value) in (int, float) and math.isfinite(value)}
                if parameters:
                    cleaned["parameters"] = parameters
            if cleaned:
                result["state_mappings"][state] = cleaned
    for key in ("expression_options", "appearance_options"):
        options = source.get(key)
        if not isinstance(options, list):
            continue
        cleaned = []
        for option in options:
            if not isinstance(option, dict) or not isinstance(option.get("id"), str):
                continue
            item = {"id": option["id"], "name": str(option.get("name", option["id"]))}
            if key == "expression_options":
                if option.get("expression") not in expression_ids:
                    continue
                item["expression"] = option["expression"]
            else:
                parameters = option.get("parameters", {})
                if not isinstance(parameters, dict) or not all(
                        isinstance(parameter, str) and type(value) in (int, float)
                        and math.isfinite(value) for parameter, value in parameters.items()):
                    continue
                item["parameters"] = dict(parameters)
                # Clothing groups are explicit metadata, not inferred from pose names.
                for field in ("outfit_id", "outfit_name"):
                    value = option.get(field)
                    if isinstance(value, str) and value.strip():
                        item[field] = value.strip()
                if option.get("expression") in expression_ids:
                    item["expression"] = option["expression"]
                motion = option.get("motion")
                if (isinstance(motion, (list, tuple)) and len(motion) == 2
                        and isinstance(motion[0], str) and type(motion[1]) is int):
                    entries = refs.get("Motions", {}).get(motion[0], [])
                    if isinstance(entries, list) and 0 <= motion[1] < len(entries):
                        item["motion"] = list(motion)
            cleaned.append(item)
        if cleaned:
            result[key] = cleaned
    idle = source.get("idle_motion", {})
    if isinstance(idle, dict) and isinstance(idle.get("group"), str):
        entries = refs.get("Motions", {}).get(idle["group"], [])
        cleaned = {"group": idle["group"]}
        for key in ("index", "lively_index"):
            if type(idle.get(key)) is int and 0 <= idle[key] < len(entries):
                cleaned[key] = idle[key]
        if "index" in cleaned:
            result["idle_motion"] = cleaned
    blink = source.get("closed_eye_expressions", {})
    if (isinstance(blink, dict) and isinstance(blink.get("parameter"), str)
            and isinstance(blink.get("values"), list)
            and all(type(value) is int for value in blink["values"])):
        result["closed_eye_expressions"] = {
            "parameter": blink["parameter"], "values": blink["values"]}
    follow = source.get("mouse_follow", {})
    if isinstance(follow, dict) and type(follow.get("enabled")) is bool:
        cleaned = {"enabled": follow["enabled"]}
        for key, lower, upper in (
                ("strength", 0, 1), ("dead_zone", 0, .5),
                ("smoothing", .02, 2), ("radius", .1, 2)):
            value = follow.get(key)
            if type(value) in (int, float) and math.isfinite(value):
                cleaned[key] = max(lower, min(upper, value))
        anchor = follow.get("anchor")
        if (isinstance(anchor, list) and len(anchor) == 2
                and all(type(value) in (int, float) and math.isfinite(value)
                        and 0 <= value <= 1 for value in anchor)):
            cleaned["anchor"] = anchor
        bindings = follow.get("parameters", {})
        if isinstance(bindings, dict):
            cleaned["parameters"] = {
                key: value for key, value in bindings.items()
                if (key in ("ParamEyeBallX", "ParamEyeBallY", "ParamAngleX", "ParamAngleY")
                    and isinstance(value, list) and len(value) == 2
                    and type(value[0]) is int and value[0] in (0, 1)
                    and type(value[1]) in (int, float) and math.isfinite(value[1])
                    and abs(value[1]) <= 30)}
        result["mouse_follow"] = cleaned
    lip = source.get("lip_sync", {})
    if isinstance(lip, dict):
        cleaned = {}
        for key, upper in (("amplitude", 1), ("speed", 30)):
            value = lip.get(key)
            if type(value) in (int, float) and math.isfinite(value):
                cleaned[key] = max(0, min(upper, value))
        if cleaned:
            result["lip_sync"] = cleaned
    relative = source.get("stage_motion_file")
    path = _safe_path(model_json.parent, relative) if isinstance(relative, str) else None
    if path is not None and path.is_file():
        base = Path(character_dir).resolve() if character_dir else model_json.parent
        try:
            result["stage_motion_file"] = path.relative_to(base).as_posix()
        except ValueError:
            pass
    return result


def merge_model_renderer_defaults(defaults, renderer):
    """Keep character choices while supplying missing portable model settings."""
    result = {**defaults, **renderer}
    mappings = dict(defaults.get("state_mappings", {}))
    for state, mapping in renderer.get("state_mappings", {}).items():
        previous = mappings.get(state)
        if isinstance(previous, dict) and isinstance(mapping, dict):
            mappings[state] = {**previous, **mapping}
        else:
            mappings[state] = mapping
    result["state_mappings"] = mappings
    for key in ("expression_options", "appearance_options"):
        if not renderer.get(key) and defaults.get(key):
            result[key] = defaults[key]
    return result


def _read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        return None


def _numeric_pose_values(value):
    values = []
    if isinstance(value, bool):
        return values
    if isinstance(value, (int, float)) and math.isfinite(value):
        values.append(float(value))
    elif isinstance(value, (list, tuple)):
        for item in value:
            values.extend(_numeric_pose_values(item))
    elif isinstance(value, dict):
        for item in value.values():
            values.extend(_numeric_pose_values(item))
    return values


def _collect_pose_switch_values(value, parameter_id, output):
    if isinstance(value, dict):
        parameter = str(value.get("parameter", "")).casefold()
        if parameter == str(parameter_id).casefold():
            output.extend(_numeric_pose_values(value.get("switchId")))
        for child in value.values():
            _collect_pose_switch_values(child, parameter_id, output)
    elif isinstance(value, list):
        for child in value:
            _collect_pose_switch_values(child, parameter_id, output)


def _collect_parameter_values(value, parameter_id, output):
    """从模型附带的外观元数据中收集指定参数的离散值。"""
    target = str(parameter_id).casefold()
    if isinstance(value, dict):
        parameters = value.get("parameters", {})
        if isinstance(parameters, dict):
            for key, child in parameters.items():
                if str(key).casefold() == target and type(child) in (int, float):
                    output.extend(_numeric_pose_values(child))
        if (str(value.get("Id", "")).casefold() == target
                and value.get("Blend", "Add") == "Overwrite"):
            output.extend(_numeric_pose_values(value.get("Value")))
        for child in value.values():
            _collect_parameter_values(child, parameter_id, output)
    elif isinstance(value, list):
        for child in value:
            _collect_parameter_values(child, parameter_id, output)


def _motion_endpoint_values(segments):
    """Read keyframe values, never timestamps, segment tags or Bezier handles."""
    if not isinstance(segments, list) or len(segments) < 2:
        return []
    values = _numeric_pose_values(segments[1])
    index = 2
    while index < len(segments):
        kind = segments[index]
        if type(kind) not in (int, float) or kind not in (0, 1, 2, 3):
            return []
        size = 7 if kind == 1 else 3
        if index + size > len(segments):
            return []
        values.extend(_numeric_pose_values(segments[index + size - 1]))
        index += size
    return values


def scan_model_pose_candidates(model_source, existing_appearances=None):
    """扫描模型中可确认的 ParamPose 候选值。

    Cubism 的 .moc3 是二进制文件，不能仅凭 .model3.json 推断自定义参数
    的离散数量。因此这里只收集能从现有外观、模型元数据、动作/表情资源中
    观察到的值，不把连续参数的最小值/最大值直接当作姿势数量。
    """
    result = validate_model(model_source)
    model_json = result["model_json"]
    config = result["config"]
    refs = config.get("FileReferences", {})
    parameter_ids = set()
    display_info = refs.get("DisplayInfo") if isinstance(refs, dict) else None
    if isinstance(display_info, str):
        display_config = _read_json_file(model_json.parent / display_info)
        if isinstance(display_config, dict):
            for item in display_config.get("Parameters", []):
                if isinstance(item, dict) and item.get("Id"):
                    parameter_ids.add(str(item["Id"]))
    pose_parameter_id = next(
        (value for value in parameter_ids if value.casefold() == "parampose"),
        "ParamPose",
    )
    observed = []
    for appearance in existing_appearances or ():
        if not isinstance(appearance, dict):
            continue
        parameters = appearance.get("parameters", {})
        if isinstance(parameters, dict):
            for key, value in parameters.items():
                if str(key).casefold() == pose_parameter_id.casefold():
                    observed.extend(_numeric_pose_values(value))

    resource_paths = []
    if isinstance(refs, dict):
        expressions = refs.get("Expressions", [])
        if isinstance(expressions, list):
            resource_paths.extend(
                item.get("File")
                for item in expressions
                if isinstance(item, dict)
            )
        motions = refs.get("Motions", {})
        if isinstance(motions, dict):
            for entries in motions.values():
                if isinstance(entries, list):
                    resource_paths.extend(
                        item.get("File")
                        for item in entries
                        if isinstance(item, dict)
                    )
    for relative in resource_paths:
        path = _safe_path(model_json.parent, relative)
        if path is None or path.suffix.lower() not in {".json", ".motion3"}:
            continue
        resource = _read_json_file(path)
        if resource is not None:
            _collect_parameter_values(resource, pose_parameter_id, observed)
        curves = resource.get("Curves", []) if isinstance(resource, dict) else []
        if not isinstance(curves, list):
            continue
        for curve in curves:
            if not isinstance(curve, dict):
                continue
            if str(curve.get("Id", "")).casefold() != pose_parameter_id.casefold():
                continue
            if curve.get("Target", "Parameter") == "Parameter":
                observed.extend(_motion_endpoint_values(curve.get("Segments", [])))
    # 一些模型会额外携带 PSD/导出工具的映射 JSON，其中 switchId 记录了
    # ParamPose 的离散值；它不是 Cubism 运行时标准文件，但可作为候选来源。
    # 同时兼容桌宠自己的 .pet3.json 或其他外观元数据，其中姿势通常写成
    # {"parameters": {"ParamPose": 4}}。这些值是明确的离散预设，不是参数范围猜测。
    for path in model_json.parent.rglob("*.json"):
        if path.name == model_json.name or path.name.casefold().endswith(
                (".model3.json", ".exp3.json", ".motion3.json")):
            continue
        if (path.name.casefold().endswith(".pet3.json")
                and path != model_json.with_name(model_json.name.removesuffix(MODEL_SUFFIX) + ".pet3.json")):
            continue
        resource = _read_json_file(path)
        if isinstance(resource, dict) and resource.get("Model") not in (None, model_json.name):
            continue
        if resource is not None:
            _collect_pose_switch_values(resource, pose_parameter_id, observed)
            _collect_parameter_values(resource, pose_parameter_id, observed)

    unique = []
    for value in observed:
        if not any(abs(value - previous) < 1e-6 for previous in unique):
            unique.append(value)
    values = [int(round(value)) if abs(value - round(value)) < 1e-6 else value
              for value in unique]
    values = sorted(set(values))
    return {
        "parameter_id": pose_parameter_id,
        "values": values,
        "observed_values": unique,
        "source": "现有姿势配置、模型外观元数据、动作和表情资源",
        "message": (
            "已发现 {} 个可确认的 ParamPose 值。".format(len(values))
            if values
            else "没有从资源中确认可用的 ParamPose 离散姿势值。"
        ),
        "model_json": model_json,
    }


def model_pose_additions(existing, proposals, active_id=None):
    """Merge approved presets into the active garment without replacing user choices."""
    active = next((item for item in existing if item.get("id") == active_id),
                  existing[0] if existing else {})
    outfit_id = active.get("outfit_id") or "default"
    ids = {item.get("id") for item in existing}
    additions = []
    for proposal in proposals:
        parameters = proposal.get("parameters", {})
        if not isinstance(parameters, dict) or not all(
                isinstance(key, str) and type(value) in (int, float) and math.isfinite(value)
                for key, value in parameters.items()):
            continue
        expression = proposal.get("expression")
        expression = expression.strip() if isinstance(expression, str) else ""
        motion = proposal.get("motion")
        motion = (list(motion) if isinstance(motion, (list, tuple)) and len(motion) == 2
                  and isinstance(motion[0], str) and type(motion[1]) is int and motion[1] >= 0 else [])
        if not parameters and not expression and not motion:
            continue
        target_outfit_id = str(proposal.get("outfit_id") or outfit_id).strip() or outfit_id
        same_group = [item for item in existing + additions
                      if (item.get("outfit_id") or "default") == target_outfit_id]
        names = {str(item.get("name", "")).casefold() for item in same_group}
        if any((item.get("expression") or "") == expression
                   and list(item.get("motion") or []) == motion
                   and all(key in item.get("parameters", {})
                   and abs(item["parameters"][key] - value) < 1e-6
                   for key, value in parameters.items()) for item in same_group):
            continue
        name = str(proposal.get("name") or "新姿势").strip() or "新姿势"
        base, number = name, 2
        while name.casefold() in names:
            name = "{} ({})".format(base, number)
            number += 1
        number = 1
        while "pose_{}".format(number) in ids:
            number += 1
        item = {"id": "pose_{}".format(number), "name": name,
                "parameters": dict(parameters), "outfit_id": target_outfit_id,
                "outfit_name": (proposal.get("outfit_name")
                                or next((entry.get("outfit_name") for entry in same_group
                                         if entry.get("outfit_name")), None)
                                or (active.get("outfit_name") if target_outfit_id == outfit_id else None)
                                or "默认服装")}
        viewport_source = same_group[0] if same_group else (active if target_outfit_id == outfit_id else {})
        if isinstance(viewport_source.get("local_viewport"), dict):
            item["local_viewport"] = copy.deepcopy(viewport_source["local_viewport"])
        if expression:
            item["expression"] = expression
        if motion:
            item["motion"] = motion
        additions.append(item)
        ids.add(item["id"])
        names.add(name.casefold())
    return additions


class ModelUpdateTransaction:
    """Keep the old package and exact config bytes until runtime activation succeeds."""

    def __init__(self, character_id, model_source):
        raise ValueError("Live2D 模型资源由官方签名角色包提供，请导入完整角色包；不能单独替换模型文件。")
        self.source = validate_model(model_source)["model_json"]
        self.character_dir = get_character_dir(character_id).resolve()
        self.config_path = self.character_dir / "character.json"
        self.original_config = self.config_path.read_bytes()
        self.destination = self.character_dir / "live2d" / "model"
        token = uuid.uuid4().hex
        self.staging = self.destination.parent / (".model-import-" + token)
        self.backup = self.destination.parent / (".model-backup-" + token)
        self.config_backup = self.character_dir / (".character-backup-" + token + ".json")
        self.config_pending = self.character_dir / (".character-update-" + token + ".json")
        self.staged_model = self.staging / self.source.name
        self.relative_model = (Path("live2d") / "model" / self.source.name).as_posix()
        self.installed = False
        self.config_replaced = False
        self.cleanup_warnings = []

    def __enter__(self):
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        # Avoid recursively copying the staging directory into itself.
        if self.destination.parent.is_relative_to(self.source.parent):
            raise ValueError("请选择独立的模型文件夹，不能从角色父目录导入。")
        try:
            shutil.copytree(self.source.parent, self.staging)
            _normalize_imported_model(self.staged_model)
            self.imported = validate_model(self.staged_model)
            self.imported["adapter_defaults"] = load_model_adapter(
                self.staged_model, self.staging)
            self.imported["pose_scan"] = scan_model_pose_candidates(self.staged_model)
            return self
        except Exception:
            self._cleanup_temporary_files()
            raise

    def _cleanup_temporary_files(self):
        for path, action in (
                (self.staging, lambda: self._remove_directory(self.staging)),
                (self.config_pending, lambda: self.config_pending.unlink(missing_ok=True))):
            try:
                action()
            except OSError as error:
                self.cleanup_warnings.append("更新临时文件未清理：{} ({})".format(path, error))

    def _remove_directory(self, path):
        path = path.resolve()
        if path.parent != self.destination.parent.resolve() or path not in {
                self.staging.resolve(), self.backup.resolve(), self.destination.resolve()}:
            raise ValueError("拒绝清理模型更新目录之外的路径。")
        if path.exists():
            shutil.rmtree(path)

    def install(self, config=None):
        if self.installed:
            raise RuntimeError("模型更新已提交。")
        if self.config_path.read_bytes() != self.original_config:
            raise RuntimeError("角色配置已被其他操作修改，请重新打开更新模型。")
        if config is not None:
            self.config_pending.write_text(json.dumps(config, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
            self.config_backup.write_bytes(self.original_config)
        if self.destination.exists():
            shutil.move(str(self.destination), str(self.backup))
        shutil.move(str(self.staging), str(self.destination))
        self.installed = True
        if config is not None:
            self.config_pending.replace(self.config_path)
            self.config_replaced = True
        self.imported["model_json"] = self.destination / self.source.name
        self.imported["pose_scan"]["model_json"] = self.imported["model_json"]
        self.imported["adapter_defaults"] = load_model_adapter(
            self.destination / self.source.name, self.character_dir)

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is not None:
                if self.installed:
                    self._remove_directory(self.destination)
                if self.backup.exists():
                    shutil.move(str(self.backup), str(self.destination))
                if self.config_replaced:
                    self.config_backup.replace(self.config_path)
            else:
                try:
                    self._remove_directory(self.backup)
                except OSError as error:
                    self.cleanup_warnings.append("旧模型备份未清理：{} ({})".format(self.backup, error))
        except OSError as error:
            raise RuntimeError("模型更新清理或回退失败，备份保留在 {} 和 {}：{}".format(
                self.backup, self.config_backup, error)) from exc
        finally:
            self._cleanup_temporary_files()
        try:
            self.config_backup.unlink(missing_ok=True)
        except OSError as error:
            self.cleanup_warnings.append("角色配置备份未清理：{} ({})".format(self.config_backup, error))
        return False


def import_model_into_character(character_id, model_source, display_name=None):
    """Compatibility entry point for callers that only replace the model package."""
    with ModelUpdateTransaction(character_id, model_source) as update:
        update.install()
        return update.relative_model, update.imported


def create_live2d_character(
    character_id,
    display_name,
    personality,
    model_source,
    card_avatar="",
):
    """Runtime users import signed packages rather than arbitrary Live2D models."""
    raise ValueError("Live2D 角色仅支持官方签名角色包，请在角色管理中选择“导入角色包”。")


def prepare_live2d_package(character_id, display_name, personality, model_source, card_avatar=""):
    """Authoring helper: output remains untrusted until signed outside the app."""
    result = validate_model(model_source)
    character_id = character_id or generate_character_id(display_name)
    target = get_character_dir(character_id)
    target.mkdir(parents=True, exist_ok=False)
    try:
        model_dir = target / "live2d" / "model"
        shutil.copytree(str(result["model_json"].parent), str(model_dir))
        _normalize_imported_model(model_dir / result["model_json"].name)
        relative_model = (Path("live2d") / "model" / result["model_json"].name).as_posix()
        imported_config = json.loads(
            (model_dir / result["model_json"].name).read_text(encoding="utf-8-sig")
        )
        mapping_scan = scan_model_mappings(imported_config)
        pose_scan = scan_model_pose_candidates(model_dir / result["model_json"].name)
        appearance_options = [
            {
                "id": "pose_{}".format(index + 1),
                "name": "姿势 {}".format(value),
                "parameters": {pose_scan["parameter_id"]: value},
            }
            for index, value in enumerate(pose_scan["values"])
        ]
        if not appearance_options:
            appearance_options = [
                {"id": "default", "name": "默认", "parameters": {}}
            ]
        expression_options = []
        refs = imported_config.get("FileReferences", {})
        expressions = refs.get("Expressions", []) if isinstance(refs, dict) else []
        if isinstance(expressions, list):
            for item in expressions:
                if not isinstance(item, dict):
                    continue
                relative = str(item.get("File", "")).strip()
                if not relative.lower().endswith(".exp3.json"):
                    continue
                expression_id = _asset_stem(relative, ".exp3.json")
                expression_options.append(
                    {
                        "id": "expression:{}".format(expression_id),
                        "name": str(item.get("Name", "")).strip() or expression_id,
                        "expression": expression_id,
                    }
                )
        avatar_source = Path(str(card_avatar or "").strip()) if card_avatar else None
        if avatar_source is None or not avatar_source.is_file():
            avatar_source = _find_model_avatar(model_dir, result["model_json"].name)
        copied_avatar = ""
        if avatar_source is not None and avatar_source.is_file():
            avatar_target = target / "ui" / "avatar.png"
            avatar_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(avatar_source), str(avatar_target))
            copied_avatar = "ui/avatar.png"
        character = {
            "id": character_id,
            "display_name": str(display_name).strip() or character_id,
            "personality": str(personality or "").strip(),
            "description": "用户创建的 Live2D 桌宠角色",
            "card_avatar": copied_avatar,
            "renderer": {
                "current": "live2d",
                "live2d_ready": True,
                "model": relative_model,
                "controlled_update": True,
                "discrete_parameters": ["ParamExpression", "ParamPose"],
                "appearance_options": appearance_options,
                "expression_options": expression_options,
                "state_mappings": mapping_scan["state_mappings"],
            },
            "states": _default_live2d_states(mapping_scan["state_mappings"]),
            "portrait_rules": {
                "fallback": "static_placeholder",
                "active_appearance_id": appearance_options[0]["id"],
            },
            "portrait_outfits": [
                {
                    "id": appearance["id"],
                    "name": appearance["name"],
                    "parameters": dict(appearance["parameters"]),
                }
                for appearance in appearance_options
            ],
        }
        defaults = load_model_adapter(model_dir / result["model_json"].name, target)
        character["renderer"] = merge_model_renderer_defaults(defaults, character["renderer"])
        if defaults.get("appearance_options"):
            character["renderer"]["appearance_options"] = defaults["appearance_options"]
            character["portrait_rules"]["active_appearance_id"] = defaults["appearance_options"][0]["id"]
            character["portrait_outfits"] = defaults["appearance_options"]
        save_json(target / "character.json", character)
        save_json(
            target / "voice" / "voice.json",
            {
                "provider": "GPT-SoVITS",
                "enabled": False,
                "preload_on_startup": False,
                "model": {"ckpt_file": "", "pth_file": ""},
                "reference": {"default_audio": "", "text": ""},
                "generation": {"speed": 1.0, "temperature": 1.0, "top_k": 5},
                "emotion_reference_audio_groups": {"平静": []},
            },
        )
        return target, character
    except Exception:
        shutil.rmtree(str(target), ignore_errors=True)
        raise
