"""发布版自卸载：仅删除带发布标记的当前桌宠目录。"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


def _is_relative_to(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def is_release_install(root):
    """确认目录是由发布脚本生成的解压版，而不是开发项目。"""
    root = Path(root).resolve()
    marker = root / "发布清单.txt"
    config_path = root / "config" / "app_config.json"
    if not marker.is_file() or not config_path.is_file():
        return False
    try:
        with config_path.open("r", encoding="utf-8-sig") as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(config, dict) and bool(config.get("release_template", False))


def _temporary_script_path(root):
    temp_dir = Path(tempfile.gettempdir()).resolve()
    if _is_relative_to(temp_dir, root):
        fallback = Path(os.environ.get("LOCALAPPDATA", temp_dir)) / "Temp"
        fallback.mkdir(parents=True, exist_ok=True)
        temp_dir = fallback.resolve()
    return temp_dir / "my_desktop_pet_uninstall_{}.ps1".format(uuid.uuid4().hex)


def schedule_release_uninstall(root, process_id):
    """在主进程退出后删除当前发布目录，并返回临时卸载脚本路径。"""
    root = Path(root).resolve()
    if len(root.parts) < 3 or root == Path(root.anchor):
        raise RuntimeError("卸载目录无效。")
    if not is_release_install(root):
        raise RuntimeError("为保护开发文件，仅可卸载解压后的正式发布版。")

    script_path = _temporary_script_path(root)
    script = """param(
    [int]$DesktopPetPid,
    [string]$Target
)

$ErrorActionPreference = "SilentlyContinue"
try {
    Wait-Process -Id $DesktopPetPid -ErrorAction SilentlyContinue
} catch {
}
Start-Sleep -Seconds 1
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if (-not (Test-Path -LiteralPath $Target)) {
        break
    }
    try {
        Remove-Item -LiteralPath $Target -Recurse -Force -ErrorAction Stop
        break
    } catch {
        Start-Sleep -Seconds 1
    }
}
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
"""
    script_path.write_text(script, encoding="utf-8-sig")
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    command = [
        str(powershell if powershell.is_file() else "powershell.exe"),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(script_path),
        "-DesktopPetPid",
        str(int(process_id)),
        "-Target",
        str(root),
    ]
    try:
        subprocess.Popen(
            command,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        script_path.unlink(missing_ok=True)
        raise
    from core.body_location import unregister_body
    unregister_body(root)
    return script_path
