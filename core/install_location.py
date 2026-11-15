"""Deploy a fresh application and configure it with its own executable."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import tempfile

from core.config import ROOT
from core.installer import SetupError, _check_cancel, _notify, preflight_space


def validate_install_dir(path):
    """返回安装位置错误；只在最近的现有祖先中试写，绝不创建安装目录。"""
    try:
        raw = str(path).strip()
        if not raw or not Path(raw).expanduser().is_absolute():
            return "请选择有效的绝对安装路径。"
        if len(raw) > 180:
            return "路径过长，请选择不超过 180 个字符的安装位置。"
        target = Path(raw).expanduser().resolve()
        if len(str(target)) > 180:
            return "路径过长，请选择不超过 180 个字符的安装位置。"
        if target == Path(target.anchor):
            return "不能装到磁盘根目录。"
        for name, fallback, message in (
            ("SystemRoot", r"C:\Windows", "不能装到系统目录。"),
            ("ProgramFiles", r"C:\Program Files", "不能装到 Program Files，请选择用户目录或非系统盘。"),
            ("ProgramFiles(x86)", r"C:\Program Files (x86)", "不能装到 Program Files，请选择用户目录或非系统盘。"),
        ):
            protected = Path(os.environ.get(name, fallback)).resolve()
            if target == protected or protected in target.parents:
                return message
        if os.name == "nt":
            import ctypes
            get_type = ctypes.windll.kernel32.GetDriveTypeW
            get_type.argtypes = [ctypes.c_wchar_p]
            get_type.restype = ctypes.c_uint
            if get_type(target.anchor) in (2, 4) or str(target).startswith("\\\\"):
                return "不支持安装到移动磁盘或网络位置。"
            if get_type(target.anchor) != 3:
                return "请选择可用的本地固定磁盘。"
        ancestor = target
        while not ancestor.exists():
            if ancestor == ancestor.parent:
                return "安装位置不存在或不可访问。"
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            return "安装位置必须是文件夹。"
        with tempfile.TemporaryFile(prefix=".desktop_pet_write_", dir=ancestor):
            pass
        return None
    except (OSError, ValueError, RuntimeError):
        return "没有写入权限或路径无效，请换一个位置。"


def install_in_directory(destination, profile, progress=None, cancel_event=None,
                         cache_dir=None, keep_cache=False):
    error = validate_install_dir(destination)
    if error:
        raise SetupError(error)
    target = Path(destination).expanduser().resolve()
    source = ROOT.resolve()
    if target == source or target in source.parents:
        raise SetupError("请选择当前程序目录或另一个空文件夹。")
    if source in target.parents and target.relative_to(source).parts[0] in (
            "assets", "runtime", "config", "characters", "engines", "models", "data", "temp"):
        raise SetupError("不能安装到当前程序的资源或数据子目录。")
    if not getattr(sys, "frozen", False):
        raise SetupError("选择其他安装位置请使用正式 EXE；源码运行不复制开发环境。")
    marker = target / ".desktop_pet_setup.json"
    if target.exists() and any(target.iterdir()):
        try:
            owned = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            owned = {}
        if owned.get("source") != str(source):
            raise SetupError("所选目录不为空，请选择空文件夹，避免覆盖现有文件。")
    _check_cancel(cancel_event)
    if profile == "full":
        preflight_space(target, cache_dir)
    target.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"source": str(source)}), encoding="utf-8")
    executable = target / "我的桌宠.exe"
    if not executable.is_file():
        _notify(progress, "正在复制桌宠程序到所选目录…", 1)
        def copy_file(src, dst):
            _check_cancel(cancel_event)
            return shutil.copy2(src, dst)
        # Publish the EXE last so interrupted copies are never treated as complete.
        for name in ("assets", "runtime"):
            folder = source / name
            if folder.is_dir():
                shutil.copytree(folder, target / name, dirs_exist_ok=True, copy_function=copy_file,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("characters", "models/gpt_sovits", "data", "config", "temp"):
            (target / name).mkdir(parents=True, exist_ok=True)
        template = source / "config/app_config.example.json"
        if not template.is_file():
            template = source / "config/app_config.json"
        config = json.loads(template.read_text(encoding="utf-8-sig"))
        for key in ("conversation_model", "vision_model"):
            config[key] = {"provider": "", "base_url": "", "model": "", "api_key": "", "api_key_env": ""}
        config["autostart"] = {"enabled": False}
        config["release_template"] = True
        (target / "config/app_config.json").write_text(json.dumps(config), encoding="utf-8")
        (target / "发布清单.txt").write_text("我的桌宠安装目录。卸载会删除本目录中的程序、角色及本地数据。\n", encoding="utf-8")
        (target / "config/gpt_sovits.json").write_text(json.dumps({
            "engine": "local_api_v2", "host": "127.0.0.1", "port": 9880,
            "python": "engines/gpt_sovits/runtime/python.exe",
            "api_script": "engines/gpt_sovits/start_api.py",
            "tts_config": "engines/gpt_sovits/tts_infer_project.yaml",
            "characters": {}, "enabled": False,
        }), encoding="utf-8")
        # Reuse an explicitly supplied offline pack without moving its original.
        offline = source / "setup_components"
        if offline.is_dir():
            shutil.copytree(offline, target / "setup_components", dirs_exist_ok=True,
                            copy_function=copy_file)
        partial = target / "我的桌宠.exe.copying"
        shutil.copy2(sys.executable, partial)
        partial.replace(executable)
    _check_cancel(cancel_event)
    settings_file = target / "data/settings.json"
    settings = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.exists() else {}
    if cache_dir is not None:
        settings.update(setup_cache_dir=str(cache_dir), keep_setup_cache=bool(keep_cache))
        settings_file.write_text(json.dumps(settings), encoding="utf-8")
    status_path = target / "temp" / ("setup_" + uuid.uuid4().hex + ".jsonl")
    process = subprocess.Popen(
        [str(executable), "--setup-worker", profile, str(status_path)], cwd=target,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    result = None
    offset = 0
    exited = False
    try:
        while True:
            _check_cancel(cancel_event)
            if status_path.exists():
                with status_path.open("r", encoding="utf-8") as handle:
                    handle.seek(offset)
                    for line in handle:
                        if not line.endswith("\n"):
                            break
                        event = json.loads(line)
                        offset += len(line.encode("utf-8"))
                        if event["kind"] == "progress" and progress:
                            progress(event["value"])
                        elif event["kind"] == "error":
                            result = {"success": False, "error": event.get("error", "配置失败")}
                        elif event["kind"] == "result":
                            result = event
            if result is not None or exited:
                break
            exited = process.poll() is not None
            time.sleep(.1)
        process.wait(timeout=10)
        if result is None or not result.get("success") or process.returncode:
            raise SetupError((result or {}).get("error", "新目录配置进程未正常完成，请重试。"))
        marker.unlink(missing_ok=True)
        return result.get("report")
    finally:
        if process.poll() is None:
            # Cooperative cancellation also cleans extraction and model probes.
            status_path.with_suffix(".cancel").touch()
            try:
                process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        status_path.unlink(missing_ok=True)
        status_path.with_suffix(".cancel").unlink(missing_ok=True)


def run_setup_worker(profile, status_file):
    import threading
    from core.installer import install_profile
    path = Path(status_file).resolve()
    if path.parent != (ROOT / "temp").resolve():
        raise SetupError("配置进度文件必须位于安装目录的 temp 中。")
    cancel = threading.Event()
    finished = threading.Event()

    def watch_cancel():
        while not finished.wait(.1):
            if path.with_suffix(".cancel").exists():
                cancel.set()
                return

    threading.Thread(target=watch_cancel, daemon=True).start()
    success = False
    with path.open("w", encoding="utf-8", newline="\n") as output:
        def emit(kind, **value):
            output.write(json.dumps({"kind": kind, **value}) + "\n")
            output.flush()
        try:
            report = install_profile(profile, lambda event: emit("progress", value=event), cancel)
            settings_file = ROOT / "data/settings.json"
            settings = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.exists() else {}
            settings.update(setup_wizard_completed=True, setup_wizard_dismissed=False,
                            setup_profile=profile, character_id="", voice_output=profile == "full")
            settings_file.write_text(json.dumps(settings), encoding="utf-8")
            if getattr(sys, "frozen", False):
                from core.body_location import register_body
                register_body(sys.executable)
            emit("result", success=True, report=report)
            success = True
        except Exception as error:
            emit("error", error=str(error), error_type=type(error).__name__)
            emit("result", success=False, error=str(error))
        finally:
            finished.set()
    return 0 if success else 1
