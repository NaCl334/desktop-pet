import json
import hashlib
import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from core.config import generate_character_id, get_character_dir, load_json, save_json
from core.package_signature import authorize_character, verify_package, safe_relative, SIGNATURE_FILE


def _package_voice_references(source, voice):
    assets = {}
    references = {}

    def collect(value):
        text = str(value or "").strip()
        if not text:
            return value
        candidate = Path(text).expanduser()
        candidates = [candidate] if candidate.is_absolute() else [
            source / base / candidate for base in ("voice", "voice/references", "")
        ]
        path = next((item.resolve() for item in candidates if item.is_file()), None)
        if path is None:
            raise FileNotFoundError("参考音频不存在：{}".format(text))
        if path not in references:
            # Hash the resolved path to avoid collisions between same-named clips.
            digest = hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
            relative = "references/packaged/{}_{}".format(digest, path.name)
            references[path] = relative
            assets["voice/" + relative] = path
        return references[path]

    reference = voice.get("reference", {})
    if isinstance(reference, dict) and reference.get("default_audio"):
        reference["default_audio"] = collect(reference["default_audio"])
    languages = voice.get("language_references", {})
    if isinstance(languages, dict):
        for item in languages.values():
            if isinstance(item, dict) and item.get("audio"):
                item["audio"] = collect(item["audio"])
    groups = voice.get("emotion_reference_audio_groups", {})
    if isinstance(groups, dict):
        for name, items in groups.items():
            if isinstance(items, list):
                groups[name] = [collect(item) for item in items]
    return assets


def _package_voice_models(source, voice, character_id):
    model = voice.get("model", {})
    if not isinstance(model, dict):
        raise ValueError("角色语音模型配置格式不正确")
    values = [model.get("ckpt_file"), model.get("pth_file")]
    if not any(values):
        from core.gpt_sovits import load_gpt_config, resolve_project_path
        legacy = load_gpt_config().get("characters", {}).get(character_id, {})
        values = [legacy.get("gpt_weights"), legacy.get("sovits_weights")]
        if not any(values):
            return {}
        values = [resolve_project_path(value) if value else None for value in values]
    if not all(values):
        raise ValueError("角色语音权重配置不完整，请同时配置 GPT 和 SoVITS 模型")
    assets = {}
    packaged = dict(model)
    for key, value in zip(("ckpt_file", "pth_file"), values):
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = source / "voice" / path
        if not path.is_file():
            raise FileNotFoundError("语音权重不存在：{}".format(value))
        relative = "models/" + path.name
        if "voice/" + relative in assets:
            raise ValueError("GPT 和 SoVITS 权重文件名重复")
        assets["voice/" + relative] = path.resolve()
        packaged[key] = relative
    voice["model"] = packaged
    return assets


def export_character(character_id, destination):
    source = get_character_dir(character_id)
    if not (source / "character.json").is_file():
        raise FileNotFoundError("角色配置不存在")
    manifest = authorize_character(source)
    if manifest:
        destination = Path(destination)
        if destination.resolve().is_relative_to(source.resolve()):
            raise ValueError("请将角色包保存到角色文件夹之外")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="desktop_pet_export_", dir=destination.parent) as temp:
            staged = Path(temp) / "character.zip"
            with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as archive:
                for name in (*manifest["files"], SIGNATURE_FILE):
                    archive.write(source / name, name)
            verify_package(source)
            staged.replace(destination)
        return destination
    voice_path = source / "voice" / "voice.json"
    with voice_path.open("r", encoding="utf-8-sig") as handle:
        voice = json.load(handle)
    if not isinstance(voice, dict):
        raise ValueError("角色语音配置格式不正确")
    assets = _package_voice_references(source, voice)
    models = _package_voice_models(source, voice, character_id)
    assets.update(models)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.resolve().is_relative_to(source.resolve()):
        raise ValueError("请将角色包保存到角色文件夹之外")
    # Replace only after a complete archive exists, preserving any earlier export.
    with tempfile.TemporaryDirectory(prefix="desktop_pet_export_", dir=destination.parent) as temp:
        staged = Path(temp) / "character.zip"
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in source.rglob("*"):
                relative = path.relative_to(source)
                name = relative.as_posix()
                if path.is_file() and path != voice_path and name not in assets and "__pycache__" not in relative.parts and not ({"models", "candidates", "faces"} & set(relative.parts)):
                    archive.write(path, name)
            for name, path in assets.items():
                archive.write(path, name)
            archive.writestr("voice/voice.json", json.dumps(voice, ensure_ascii=False, indent=2))
            archive.writestr("share_info.json", json.dumps({
                "format": "desktop_pet_character",
                "version": 2,
                "character_id": character_id,
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "models_included": bool(models),
                "model_files": [{"path": name, "size": path.stat().st_size}
                                for name, path in models.items()],
            }, ensure_ascii=False, indent=2))
        staged.replace(destination)
    return destination


def _validate_member(name):
    try:
        safe_relative(name.rstrip("/"))
        return True
    except ValueError:
        return False


def import_signed_directory(source):
    """Install a verified payload without rewriting its ID or signed bytes."""
    source = Path(source).resolve()
    manifest = verify_package(source)
    character_id = manifest["character_id"]
    target = get_character_dir(character_id)
    if target.exists():
        # Repeated launcher invocations are idempotent; replacement needs an
        # explicit update workflow instead of silently deleting user content.
        if verify_package(target) != manifest:
            raise ValueError("同 ID 角色包已存在且内容不同，请先在角色管理中移除旧包后再导入。")
        return character_id, load_json(target / "character.json", {}).get("display_name", character_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".signed_import_", dir=target.parent) as temp:
        staged = Path(temp) / "payload"
        shutil.copytree(source, staged)
        verify_package(staged)
        staged.rename(target)
    return character_id, load_json(target / "character.json", {}).get("display_name", character_id)


def import_character(archive_path):
    archive_path = Path(archive_path)
    with zipfile.ZipFile(archive_path, "r") as archive:
        names = archive.namelist()
        if "character.json" not in names or "voice/voice.json" not in names:
            raise ValueError("角色包缺少 character.json 或 voice/voice.json")
        if any(not _validate_member(item.orig_filename) for item in archive.infolist()):
            raise ValueError("角色包包含不安全路径")
        normalized = [name.rstrip("/").casefold() for name in names]
        if len(set(normalized)) != len(normalized):
            raise ValueError("角色包存在重复路径或大小写冲突。")
        if any((item.external_attr >> 16) & 0o170000 == 0o120000 for item in archive.infolist()):
            raise ValueError("角色包不能包含符号链接。")
        with tempfile.TemporaryDirectory(prefix="desktop_pet_import_") as temp:
            temp_dir = Path(temp)
            archive.extractall(temp_dir)
            raw_character = load_json(temp_dir / "character.json", {})
            if not isinstance(raw_character, dict) or not isinstance(raw_character.get("renderer", {}), dict):
                raise ValueError("角色配置格式无效。")
            if (temp_dir / SIGNATURE_FILE).exists() or raw_character.get("renderer", {}).get("current") == "live2d":
                authorize_character(temp_dir)
                return import_signed_directory(temp_dir)
            info = load_json(temp_dir / "share_info.json", {})
            if info.get("version") == 2 and info.get("models_included"):
                voice = load_json(temp_dir / "voice/voice.json", {})
                model = voice.get("model", {})
                declared = info.get("model_files", [])
                if len(declared) != 2:
                    raise ValueError("角色包中的语音权重清单不完整")
                for key in ("ckpt_file", "pth_file"):
                    relative = str(model.get(key) or "")
                    if not _validate_member(relative):
                        raise ValueError("角色包中的语音权重路径无效")
                    path = temp_dir / "voice" / relative
                    record = next((item for item in declared if item.get("path") == "voice/" + relative), None)
                    if not record or not path.is_file() or path.stat().st_size != record.get("size"):
                        raise ValueError("角色包中的语音权重缺失或不完整：{}".format(relative))
            character = load_json(temp_dir / "character.json", {})
            display_name = str(character.get("display_name", "导入角色")).strip() or "导入角色"
            character_id = generate_character_id(display_name)
            character["id"] = character_id
            target = get_character_dir(character_id)
            shutil.copytree(temp_dir, target, ignore=shutil.ignore_patterns("share_info.json"))
            save_json(target / "character.json", character)
    return character_id, display_name
