import json
import hashlib
import re
import shutil
from pathlib import Path
import sys

from core.emotion import EMOTION_LABEL_ALIASES, LEGACY_EMOTIONS, PRESET_EMOTIONS, STATE_KEYWORDS
from core.portrait_processing import (
    IMAGE_EXTENSIONS,
    enhance_portrait,
    enhance_portrait_folder,
)

ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
CONFIG_DIR = ROOT / "config"
CHARACTERS_DIR = ROOT / "characters"
CHARACTER_DIR = CHARACTERS_DIR / "huadan"
DEFAULT_VOICE_DIRECTORY = "data/voices"
ADDITIONAL_PORTRAIT_LABELS = {
    key: LEGACY_EMOTIONS[key]["label"] for key in ("playful", "passionate")
}


def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    temporary.replace(path)


def load_app_config():
    value = load_json(CONFIG_DIR / "app_config.json", {})
    if isinstance(value, dict):
        value.setdefault("voice_directory", DEFAULT_VOICE_DIRECTORY)
        legacy_autonomous = value.pop("companionship", None)
        legacy_screen_watch = value.pop("screen_watch", None)
        autonomous = value.get("autonomous_conversation")
        if (
            isinstance(legacy_autonomous, dict)
            or isinstance(legacy_screen_watch, dict)
            or isinstance(autonomous, dict)
        ):
            migrated = (
                dict(legacy_autonomous)
                if isinstance(legacy_autonomous, dict)
                else {}
            )
            screen_watch = (
                dict(legacy_screen_watch)
                if isinstance(legacy_screen_watch, dict)
                else {}
            )
            legacy_interval = migrated.pop("idle_minutes", None)
            current = dict(autonomous) if isinstance(autonomous, dict) else {}
            current_legacy_interval = current.pop("idle_minutes", None)
            current.pop("interval_seconds", None)
            if "interval_minutes" not in migrated:
                migrated["interval_minutes"] = (
                    legacy_interval
                    if legacy_interval is not None
                    else current_legacy_interval
                    if current_legacy_interval is not None
                    else max(
                        1,
                        int(
                            round(
                                float(
                                    screen_watch.get(
                                        "interval_seconds",
                                        300,
                                    )
                                )
                                / 60.0
                            )
                        ),
                    )
                    if screen_watch.get("interval_seconds") is not None
                    else 60
                )
            migrated.update(current)
            if "enabled" not in migrated:
                migrated["enabled"] = bool(screen_watch.get("enabled", False))
            if "vision_enabled" not in migrated:
                migrated["vision_enabled"] = bool(
                    screen_watch.get("enabled", False)
                )
            migrated.setdefault("enabled", False)
            value["autonomous_conversation"] = migrated

        value.pop("speech_recognition", None)
        value.get("shortcuts", {}).pop("voice_input", None)
    return value


def get_character_dir(character_id):
    from core.package_signature import valid_character_id
    if character_id != "" and not valid_character_id(character_id):
        raise ValueError("角色 ID 无效。")
    return CHARACTERS_DIR / character_id


def list_characters():
    characters = []
    if not CHARACTERS_DIR.exists():
        return characters
    for directory in sorted(CHARACTERS_DIR.iterdir()):
        if not directory.is_dir():
            continue
        config = load_json(directory / "character.json", {})
        from core.package_signature import valid_character_id
        if isinstance(config, dict) and config.get("id") == directory.name and valid_character_id(directory.name):
            characters.append(load_character_config(directory.name))
    return characters


def generate_character_id(display_name):
    """根据角色名称生成不会覆盖已有目录的内部 ID。"""
    name = (display_name or "").strip()
    normalized = re.sub(r"\s+", "_", name)
    safe_name = "".join(
        character for character in normalized if character.isalnum() or character in ("_", "-")
    ).strip("_-")
    base = safe_name or "role_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    candidate = base
    suffix = 2
    while get_character_dir(candidate).exists():
        candidate = "{}_{}".format(base, suffix)
        suffix += 1
    return candidate


def load_character_config(character_id="huadan"):
    value = load_json(get_character_dir(character_id) / "character.json", {})
    if character_id and _signed_character(character_id):
        override = load_json(_override_path(character_id, "character.json"), {})
        if isinstance(override, dict) and override:
            base_renderer = value.get("renderer", {})
            renderer = override.get("renderer", base_renderer)
            if not isinstance(renderer, dict):
                renderer = base_renderer
            value = dict(value, **override)
            value["id"] = character_id
            value["renderer"] = dict(renderer, current=base_renderer.get("current", "static"))
    return value


def _signed_character(character_id):
    return (get_character_dir(character_id) / "signature.ed25519").exists()


def _override_path(character_id, filename):
    get_character_dir(character_id)  # Validate before using an ID as a path component.
    return ROOT / "data" / "character_overrides" / character_id / filename


def load_voice_config(character_id="huadan"):
    value = load_json(get_character_dir(character_id) / "voice" / "voice.json", {})
    if character_id and _signed_character(character_id):
        value = load_json(_override_path(character_id, "voice.json"), value)
    if isinstance(value, dict):
        # 旧版本曾为 GPT-SoVITS 预留远程接口字段；本项目使用本地模型文件。
        value.pop("api", None)
        for key in ("companionship_generation", "companionship_generation_by_language",
                    "companionship_parameter_mode", "companionship_parameter_language"):
            value.pop(key, None)
        reference = value.get("reference")
        if isinstance(reference, dict):
            reference.pop("language", None)
            language_references = value.setdefault("language_references", {})
            if not isinstance(language_references, dict):
                language_references = {}
                value["language_references"] = language_references
            # 英语参考音频暂不使用，清理旧版本留下的空占位。
            language_references.pop("en", None)
            default_audio = str(reference.get("default_audio", "")).strip()
            default_text = str(reference.get("text", "")).strip()
            if default_audio and not language_references:
                inferred = "ja" if any(
                    "\u3040" <= char <= "\u30ff" for char in default_text
                ) else "zh"
                language_references[inferred] = {
                    "audio": default_audio,
                    "text": default_text,
                }
            for language in ("zh", "ja"):
                item = language_references.setdefault(language, {})
                if not isinstance(item, dict):
                    language_references[language] = item = {}
                item.setdefault("audio", "")
                item.setdefault("text", "")
    return value


def save_app_config(value):
    save_json(CONFIG_DIR / "app_config.json", value)


def save_voice_config(value):
    save_character_voice_config("huadan", value)


def save_character_voice_config(character_id, value):
    if _signed_character(character_id):
        from core.package_signature import authorize_character
        authorize_character(get_character_dir(character_id))
        save_json(_override_path(character_id, "voice.json"), value)
    else:
        save_json(get_character_dir(character_id) / "voice" / "voice.json", value)


def save_character_config(character_id, value):
    if _signed_character(character_id):
        from core.package_signature import authorize_character
        authorize_character(get_character_dir(character_id))
        save_json(_override_path(character_id, "character.json"), value)
    else:
        if value.get("renderer", {}).get("current") == "live2d":
            raise ValueError("Live2D 角色仅支持官方签名角色包，请导入已签名的角色包。")
        save_json(get_character_dir(character_id) / "character.json", value)


def _copy_optional_file(source, destination):
    if not source:
        return ""
    source_path = Path(source)
    if not source_path.is_file():
        raise FileNotFoundError(str(source_path))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source_path), str(destination))
    return destination.name


def _copy_optional_folder(source, destination):
    if not source:
        return False
    source_path = Path(source)
    if not source_path.is_dir():
        raise FileNotFoundError(str(source_path))
    destination.mkdir(parents=True, exist_ok=True)
    for item in source_path.iterdir():
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(str(item), str(target), dirs_exist_ok=True)
        else:
            shutil.copy2(str(item), str(target))
    return True


def _copy_image_folder(source, destination):
    if not source:
        return []
    return enhance_portrait_folder(source, destination, mode="full")


def _copy_character_portrait_folder(source, destination, require_idle=True, recursive=True):
    source_path = Path(source)
    if not source_path.is_dir():
        raise ValueError("角色立绘来源必须是文件夹。")

    images = [
        item
        for item in sorted(source_path.rglob("*") if recursive else source_path.iterdir())
        if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS
    ]
    idle_image = next((item for item in images if item.stem == "待机"), None)
    if require_idle and idle_image is None:
        raise ValueError("角色立绘文件夹中必须包含一张文件名为“待机”的图片。")

    emotion_names = {
        definition["label"]: definition["label"]
        for state, definition in PRESET_EMOTIONS.items()
    }
    emotion_names.update(
        {
            state: definition["label"]
            for state, definition in PRESET_EMOTIONS.items()
        }
    )
    emotion_names.update(
        {
            label: label
            for label in ADDITIONAL_PORTRAIT_LABELS.values()
        }
    )
    emotion_names.update(ADDITIONAL_PORTRAIT_LABELS)
    emotion_names.update({
        label: PRESET_EMOTIONS[key]["label"]
        for label, key in EMOTION_LABEL_ALIASES.items()
    })
    emotion_names["待机"] = "待机"
    emotion_names["idle"] = "待机"
    destination = Path(destination)
    copied = []
    for image in images:
        stem = emotion_names.get(image.stem)
        if not stem:
            continue
        target = destination / "{}.png".format(stem)
        if target.exists():
            continue
        enhance_portrait(image, target, mode="full")
        copied.append(target)
    return copied


def _character_portrait_metadata(portrait_directory, character_directory=None):
    portrait_directory = Path(portrait_directory)
    state_by_label = {
        definition["label"]: state
        for state, definition in PRESET_EMOTIONS.items()
    }
    state_by_label.update(
        {
            label: state
            for state, label in ADDITIONAL_PORTRAIT_LABELS.items()
        }
    )
    state_by_label.update(EMOTION_LABEL_ALIASES)
    state_by_label["待机"] = "idle"
    asset_paths = {}
    for image in portrait_directory.glob("*.png"):
        state = image.stem
        if (
            state not in PRESET_EMOTIONS
            and state not in ADDITIONAL_PORTRAIT_LABELS
            and state != "idle"
        ):
            state = state_by_label.get(state, "")
        if not state or state in asset_paths:
            continue
        asset_paths[state] = {
            "full": image.relative_to(character_directory or portrait_directory.parents[1]).as_posix()
        }

    ordered_states = [
        state
        for state in [
            "idle",
            *PRESET_EMOTIONS,
            *ADDITIONAL_PORTRAIT_LABELS,
        ]
        if state in asset_paths
    ]
    states = {}
    ordered_assets = {}
    for state in ordered_states:
        assets = asset_paths[state]
        ordered_assets[state] = assets
        preset = PRESET_EMOTIONS.get(state)
        states[state] = {
            "label": (
                "待机"
                if state == "idle"
                else preset["label"]
                if preset
                else ADDITIONAL_PORTRAIT_LABELS[state]
            ),
            "enabled": True,
            "keywords": list(
                preset["keywords"] if preset else STATE_KEYWORDS.get(state, ())
            ),
            "portrait_assets": assets,
            "category": (
                "special"
                if state == "idle"
                else "preset"
                if preset
                else "custom"
            ),
        }
    return states, ordered_assets


def _portrait_folder_plan(source):
    source = Path(source)
    def images(folder):
        return [item for item in folder.iterdir()
                if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS]
    direct = images(source)
    children = [item for item in sorted(source.iterdir()) if item.is_dir()
                and any(path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
                        for path in item.rglob("*"))]
    # Flat folders remain a single default outfit. Named children are outfits.
    folders = ([("默认", source)] if direct else []) + [(item.name, item) for item in children]
    if not folders:
        raise ValueError("所选文件夹中没有可用立绘。")
    for name, folder in folders:
        candidates = images(folder) if folder == source else list(folder.rglob("*"))
        if not any(item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS
                   and item.stem in ("待机", "idle") for item in candidates):
            raise ValueError("服装“{}”至少需要一张名为“待机”的立绘；不要求每个动作都有。".format(name))
    return folders


def _import_portrait_outfits(source, character_dir, plan):
    outfits, all_states = [], {}
    for index, (name, folder) in enumerate(plan):
        destination = character_dir / "portraits/outfits" / name
        destination.mkdir(parents=True, exist_ok=True)
        _copy_character_portrait_folder(folder, destination, require_idle=False, recursive=False)
        states, assets = _character_portrait_metadata(destination, character_dir / "portraits")
        all_states.update({key: value for key, value in states.items() if key not in all_states})
        actions = []
        if folder != Path(source):
            for action_index, action_folder in enumerate(item for item in sorted(folder.iterdir()) if item.is_dir()):
                if not any(item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS
                           for item in action_folder.rglob("*")):
                    continue
                action_target = destination / "actions" / action_folder.name
                action_target.mkdir(parents=True, exist_ok=True)
                _copy_character_portrait_folder(action_folder, action_target, require_idle=False)
                action_states, action_assets = _character_portrait_metadata(action_target, character_dir / "portraits")
                all_states.update({key: value for key, value in action_states.items() if key not in all_states})
                actions.append({"id": "action_{}".format(action_index + 1), "name": action_folder.name,
                                "folder_name": action_folder.name, "portrait_assets": action_assets,
                                "local_viewport": {}})
        # A sole image wrapper is a default pose rather than an extra empty pose.
        if not assets and len(actions) == 1:
            assets = actions[0]["portrait_assets"]
            actions = []
        outfits.append({"id": "default" if index == 0 else "outfit_{}".format(index + 1),
                        "name": name, "folder_name": name, "portrait_assets": assets,
                        "local_viewport": {}, "actions": actions,
                        "active_action_id": (next((item["id"] for item in actions
                            if "idle" in item["portrait_assets"]), "") if not assets else "")})
    return all_states, outfits


def create_character(
    character_id,
    display_name,
    personality="",
    full_portrait="",
    small_portrait="",
    ckpt_file="",
    pth_file="",
    default_reference_audio="",
    portrait_folder="",
    reference_audio_folder="",
    derive_small_from_full=True,
    require_idle_portrait=False,
    normalize_emotion_names=False,
    card_avatar="",
):
    character_id = character_id.strip()
    display_name = display_name.strip() or character_id
    full_portrait = str(full_portrait or "").strip()
    small_portrait = str(small_portrait or "").strip()
    card_avatar = str(card_avatar or "").strip()
    if card_avatar:
        avatar_source = Path(card_avatar)
        if (
            not avatar_source.is_file()
            or avatar_source.suffix.lower() not in IMAGE_EXTENSIONS
        ):
            raise ValueError("请选择有效的头像图片文件。")
    full_source = full_portrait or small_portrait
    portrait_plan = None
    if require_idle_portrait or normalize_emotion_names:
        source_path = Path(full_source) if full_source else None
        if source_path is None or not source_path.is_dir():
            raise ValueError("请选择有效的角色立绘文件夹。")
        portrait_plan = _portrait_folder_plan(source_path)
    character_dir = get_character_dir(character_id)
    character_dir.mkdir(parents=True, exist_ok=False)
    full_portrait_directory = (
        character_dir / "portraits" / "outfits" / "默认"
    )
    if not normalize_emotion_names:
        full_portrait_directory.mkdir(parents=True, exist_ok=True)
    if normalize_emotion_names:
        states, imported_outfits = _import_portrait_outfits(full_source, character_dir, portrait_plan)
        portrait_assets = imported_outfits[0]["portrait_assets"]
    else:
        _copy_image_folder(
            full_source,
            full_portrait_directory,
        )
        states, portrait_assets = _character_portrait_metadata(full_portrait_directory)
    copied_ckpt = _copy_optional_file(
        ckpt_file,
        character_dir / "voice" / "models" / Path(ckpt_file).name if ckpt_file else character_dir / "voice" / "models" / "unused",
    )
    copied_pth = _copy_optional_file(
        pth_file,
        character_dir / "voice" / "models" / Path(pth_file).name if pth_file else character_dir / "voice" / "models" / "unused",
    )
    copied_reference = _copy_optional_file(
        default_reference_audio,
        character_dir / "voice" / "references" / Path(default_reference_audio).name
        if default_reference_audio
        else character_dir / "voice" / "references" / "unused",
    )
    _copy_optional_folder(reference_audio_folder, character_dir / "voice" / "references" / "imported")
    copied_avatar = (
        _copy_optional_file(
            card_avatar,
            character_dir / "ui" / ("avatar" + Path(card_avatar).suffix.lower()),
        )
        if card_avatar
        else ""
    )
    character = {
        "id": character_id,
        "display_name": display_name,
        "personality": personality.strip(),
        "description": "用户创建的桌宠角色",
        "card_avatar": (
            "ui/{}".format(copied_avatar) if copied_avatar else ""
        ),
        "renderer": {"current": "static", "live2d_ready": True},
        "states": states,
        "portrait_rules": {
            "directory": "portraits",
            "extensions": [".png", ".gif", ".ppm", ".jpg", ".jpeg"],
            "fallback": "programmatic_placeholder",
            "active_outfit_id": "default",
        },
        "portrait_outfits": imported_outfits if normalize_emotion_names else [
            {
                "id": "default",
                "name": "默认",
                "folder_name": "默认",
                "portrait_assets": portrait_assets,
                "local_viewport": {},
            }
        ],
    }
    save_json(
        character_dir / "character.json",
        character,
    )
    save_json(
        character_dir / "voice" / "voice.json",
        {
            "provider": "GPT-SoVITS",
            "enabled": False,
            "preload_on_startup": False,
            "model": {
                "ckpt_file": "models/{}".format(copied_ckpt) if copied_ckpt else "",
                "pth_file": "models/{}".format(copied_pth) if copied_pth else "",
            },
            "reference": {
                "default_audio": "references/{}".format(copied_reference) if copied_reference else "",
                "text": "",
            },
            "generation": {"speed": 1.0, "temperature": 1.0, "top_k": 5},
            "emotion_reference_audio_groups": {"平静": []},
        },
    )
    return character_dir
