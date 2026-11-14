"""Authenticated, immutable Live2D payloads. Private keys are never read here."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

OFFICIAL_PUBLIC_KEY = bytes.fromhex("d6a9e2d6a8ea2b33943e862e22d3a7e3ad62f32626633b7e182c4a8966990d52")
SIGNATURE_FILE = "signature.ed25519"
DOMAIN = b"DesktopPet.CharacterPackage.v1\0"
MAX_SIGNATURE_BYTES = 8 * 1024 * 1024


class PackageSignatureError(ValueError):
    pass


def valid_character_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 120
            and value not in (".", "..")
            and value.upper() not in {"CON", "PRN", "AUX", "NUL", *{"COM"+str(i) for i in range(1,10)}, *{"LPT"+str(i) for i in range(1,10)}}
            and all(c.isalnum() or c in "_-" for c in value))


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\0" in value:
        raise PackageSignatureError("角色包包含不安全路径。")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") or p.endswith((".", " ")) for p in value.split("/")):
        raise PackageSignatureError("角色包包含不安全路径。")
    if any(p.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *{"COM"+str(i) for i in range(1,10)}, *{"LPT"+str(i) for i in range(1,10)}} for p in path.parts):
        raise PackageSignatureError("角色包包含保留文件名。")
    return path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PackageSignatureError("角色包 JSON 存在重复字段。")
            result[key] = value
        return result
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=pairs)
    except (OSError, UnicodeError, ValueError) as error:
        raise PackageSignatureError("角色包配置无法读取。") from error


def _linked(path):
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def payload_files(directory):
    directory = Path(directory).absolute()
    if not directory.is_dir() or any(_linked(p) for p in (directory, *directory.parents)):
        raise PackageSignatureError("角色包目录无效或包含链接。")
    result = {};seen = set()
    for current, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            if _linked(path):
                raise PackageSignatureError("签名角色包不能包含链接。")
        for name in files:
            path = Path(current) / name
            relative = path.relative_to(directory).as_posix()
            safe_relative(relative)
            if relative.casefold() in seen:
                raise PackageSignatureError("角色包文件名存在大小写冲突。")
            seen.add(relative.casefold())
            if relative != SIGNATURE_FILE:
                result[relative] = {"sha256": file_hash(path), "size": path.stat().st_size}
    return result


def _check_model_references(directory, files):
    """Prevent a signed model from referencing content outside its signed set."""
    for name in files:
        if not name.lower().endswith(".model3.json"):
            continue
        model = directory / name
        data = _json(model)
        if not isinstance(data, dict):
            raise PackageSignatureError("Live2D 模型配置格式无效。")
        refs = data.get("FileReferences", {})
        if not isinstance(refs, dict):
            raise PackageSignatureError("Live2D 模型引用格式无效。")
        if (not isinstance(refs.get("Moc"), str) or not refs["Moc"]
                or not isinstance(refs.get("Textures", []), list)
                or not isinstance(refs.get("Expressions", []), list)
                or not isinstance(refs.get("Motions", {}), dict)):
            raise PackageSignatureError("Live2D 模型资源引用格式无效。")
        def check(value):
            if isinstance(value, str) and value:
                # Cubism permits parent-relative references, but signed resources
                # must resolve to an actual member of this same payload.
                if "\\" in value or ":" in value or "\0" in value or value.startswith("/"):
                    raise PackageSignatureError("Live2D 模型引用了包外资源。")
                target = (model.parent / value).resolve()
                try:
                    relative = target.relative_to(directory.resolve()).as_posix()
                except ValueError:
                    raise PackageSignatureError("Live2D 模型引用了包外资源。") from None
                if relative not in files:
                    raise PackageSignatureError("Live2D 模型资源缺失或未签名：" + relative)
        for key in ("Moc", "Physics", "Pose", "UserData", "DisplayInfo"):
            check(refs.get(key))
        for value in refs.get("Textures", []):
            check(value)
        for item in refs.get("Expressions", []):
            if not isinstance(item, dict):
                raise PackageSignatureError("Live2D 表情引用格式无效。")
            check(item.get("File"))
        for group in refs.get("Motions", {}).values():
            if not isinstance(group, list):
                raise PackageSignatureError("Live2D 动作组格式无效。")
            for item in group:
                if not isinstance(item, dict):
                    raise PackageSignatureError("Live2D 动作引用格式无效。")
                check(item.get("File"));check(item.get("Sound"))


def build_manifest(directory):
    directory = Path(directory)
    character = _json(directory / "character.json")
    if not isinstance(character, dict) or not valid_character_id(character.get("id")):
        raise PackageSignatureError("角色 ID 无效。")
    if not isinstance(character.get("renderer", {}), dict):
        raise PackageSignatureError("角色渲染配置格式无效。")
    if character.get("renderer", {}).get("current") != "live2d":
        raise PackageSignatureError("签名脚本仅用于 Live2D 角色包。")
    files = payload_files(directory)
    if "voice/voice.json" not in files or not any(n.lower().endswith(".model3.json") for n in files):
        raise PackageSignatureError("角色包缺少语音配置或模型配置文件。")
    _check_model_references(directory, files)
    return {"format": "desktop-pet-signed-character", "version": 1,
            "character_id": character["id"], "files": files}


def verify_package(directory):
    """Read and hash afresh on every call; no success cache is used."""
    directory = Path(directory)
    signature_path = directory / SIGNATURE_FILE
    if not signature_path.is_file() or signature_path.stat().st_size > MAX_SIGNATURE_BYTES:
        raise PackageSignatureError("此角色包未经官方签名，无法加载 Live2D 角色。")
    envelope = _json(signature_path)
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        manifest = envelope["manifest"]
        signature = bytes.fromhex(envelope["signature"])
        Ed25519PublicKey.from_public_bytes(OFFICIAL_PUBLIC_KEY).verify(signature, DOMAIN + canonical(manifest))
    except ImportError as error:
        raise PackageSignatureError("缺少角色包验签组件 cryptography，请安装依赖或更新桌宠。") from error
    except (KeyError, TypeError, ValueError, InvalidSignature) as error:
        raise PackageSignatureError("角色包签名无效，无法加载 Live2D 角色。") from error
    actual = build_manifest(directory)
    if manifest != actual:
        raise PackageSignatureError("角色包内容已改变，签名校验失败。请重新下载官方角色包。")
    return manifest


def authorize_character(directory, require_live2d=False, model_path=None):
    directory = Path(directory)
    character = _json(directory / "character.json")
    if not isinstance(character, dict) or not isinstance(character.get("renderer", {}), dict):
        raise PackageSignatureError("角色配置格式无效。")
    # A signature is checked even if someone changes renderer.current to static.
    signed = (directory / SIGNATURE_FILE).exists()
    if require_live2d or signed or character.get("renderer", {}).get("current") == "live2d":
        manifest = verify_package(directory)
        if model_path is not None:
            try:
                relative = Path(model_path).resolve().relative_to(directory.resolve()).as_posix()
            except ValueError:
                raise PackageSignatureError("不能加载签名角色包以外的 Live2D 模型。") from None
            if relative not in manifest["files"] or not relative.lower().endswith(".model3.json"):
                raise PackageSignatureError("Live2D 模型配置文件未包含在角色包签名中。")
        return manifest
    if not isinstance(character, dict) or not valid_character_id(character.get("id")):
        raise PackageSignatureError("角色配置或 ID 无效。")
    return None


def choose_startup_character(requested, saved):
    from core.config import get_character_dir, list_characters
    from core.environment import live2d_present
    errors = []
    available = list_characters()
    # Only static characters are chosen automatically. An explicit or saved
    # signed Live2D choice may be restored when components are available.
    candidates = [requested, saved] + [r["id"] for r in available
        if isinstance(r.get("renderer", {}), dict) and r.get("renderer", {}).get("current", "static") == "static"]
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if not valid_character_id(candidate):
                raise PackageSignatureError("指定的角色 ID 无效。")
            if candidate in seen:
                continue
            seen.add(candidate)
            directory = get_character_dir(candidate)
            manifest = authorize_character(directory)
            if manifest and not live2d_present():
                raise PackageSignatureError("未安装 Live2D 组件，请安装组件或使用立绘角色。")
            return candidate, errors
        except (OSError, ValueError) as error:
            errors.append(str(error))
    return "", errors
