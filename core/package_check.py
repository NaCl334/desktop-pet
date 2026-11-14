"""Headless verification entry point for installed bodies and release QA."""
import json
from pathlib import Path


def run(package, report_path):
    from core.config import ROOT
    from core.environment import live2d_present
    from core.package_signature import verify_package
    target = Path(report_path).resolve()
    if target.parent != (ROOT / "temp").resolve():
        raise ValueError("验签报告必须写入本体 temp 目录。")
    result = {"live2d_available": live2d_present(), "verified": False}
    try:
        manifest = verify_package(package)
        result.update(verified=True, character_id=manifest["character_id"], file_count=len(manifest["files"]))
    except Exception as error:
        result["error"] = str(error)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["verified"] else 1
