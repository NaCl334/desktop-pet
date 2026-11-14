"""Create a self-contained signed copy; source characters are never modified."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.backup import _package_voice_models, _package_voice_references
from core.package_signature import valid_character_id, verify_package, payload_files
from scripts.sign_character_package import sign_package


def prepare(source, output, launcher, key, character_id=None, archive=False):
    source, output, launcher = (Path(p).resolve() for p in (source, output, launcher))
    if output.exists() or output.is_relative_to(source):
        raise ValueError("请选择不存在且位于原角色之外的输出目录。")
    if not launcher.is_file():
        raise ValueError("请先构建角色包启动器。")
    payload_files(source)  # Reject links and unsafe names before copying.
    character = json.loads((source / "character.json").read_text(encoding="utf-8-sig"))
    original_id = character["id"]
    character["id"] = character_id or original_id
    if not valid_character_id(character["id"]):
        raise ValueError("角色 ID 无效。")
    voice = json.loads((source / "voice/voice.json").read_text(encoding="utf-8-sig"))
    assets = _package_voice_references(source, voice)
    assets.update(_package_voice_models(source, voice, original_id))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare_signed_", dir=output.parent) as temp:
        staged = Path(temp) / "payload"
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "signature.ed25519"))
        for name, path in assets.items():
            target = staged / name;target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        (staged / "character.json").write_text(json.dumps(character, ensure_ascii=False, indent=2), encoding="utf-8")
        (staged / "voice/voice.json").write_text(json.dumps(voice, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.copy2(launcher, staged / "启动角色.exe")
        sign_package(staged, key)
        verify_package(staged)
        staged.rename(output)
    if archive:
        destination = Path(str(output) + ".zip")
        with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED, compresslevel=4) as handle:
            for file in output.rglob("*"):
                if file.is_file():handle.write(file, file.relative_to(output).as_posix())
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--character-id")
    parser.add_argument("--zip", action="store_true")
    args = parser.parse_args()
    print(prepare(args.source, args.output, args.launcher, args.key, args.character_id, args.zip))


if __name__ == "__main__":
    main()
