"""Sign a prepared Live2D package, never modifying its source artwork."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.package_signature import DOMAIN, SIGNATURE_FILE, OFFICIAL_PUBLIC_KEY, build_manifest, canonical


def sign_package(directory, key_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    directory, key_path = Path(directory).resolve(), Path(key_path).resolve()
    project = Path(__file__).resolve().parents[1]
    if key_path.is_relative_to(project) or key_path.is_relative_to(directory):
        raise ValueError("私钥必须保存在项目和角色包目录之外。")
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("需要 Ed25519 私钥。")
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if public != OFFICIAL_PUBLIC_KEY:
        raise ValueError("私钥不对应本体内置公钥，拒绝生成无法加载的包。")
    target = directory / SIGNATURE_FILE
    temporary = target.with_suffix(".new")
    if temporary.exists():
        raise ValueError("角色包中存在未完成的签名临时文件，请先移走该文件。")
    manifest = build_manifest(directory)
    envelope = {"manifest": manifest, "signature": key.sign(DOMAIN + canonical(manifest)).hex()}
    temporary.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--key", type=Path, required=True)
    args = parser.parse_args()
    print(sign_package(args.directory, args.key))


if __name__ == "__main__":
    main()
