"""Locate the body without storing mutable paths inside signed payloads."""
import hashlib
import os
from pathlib import Path

REGISTRY_KEY = r"Software\我的桌宠"


def executable_path(value):
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_dir():
        path = path / "我的桌宠.exe"
    if not path.is_absolute() or path.suffix.lower() != ".exe" or not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"MZ":
                return None
    except OSError:
        return None
    return path.resolve()


def registered_body():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            return executable_path(winreg.QueryValueEx(key, "InstallPath")[0])
    except (ImportError, OSError, TypeError, ValueError):
        return None


def register_body(executable):
    path = executable_path(executable)
    if path is None:
        return False
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            winreg.SetValueEx(key, "InstallPath", 0, winreg.REG_SZ, str(path))
        return True
    except (ImportError, OSError):
        return False


def unregister_body(root):
    path = registered_body()
    if path is None or path.parent != Path(root).resolve():
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "InstallPath")
    except (ImportError, OSError):
        pass


def cache_path(package):
    identity = hashlib.sha256(os.fsencode(Path(package).resolve())).hexdigest()[:24]
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "我的桌宠" / "launchers" / identity / "body_path.txt"


def locate_body(package):
    try:
        path = executable_path(cache_path(package).read_text(encoding="utf-8").strip())
        if path:
            return path
    except (OSError, ValueError):
        pass
    return registered_body()


def remember_body(package, executable):
    path = executable_path(executable)
    if path is None:
        raise ValueError("请选择已安装的桌宠本体 EXE。")
    cache = cache_path(package)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(str(path), encoding="utf-8")
    register_body(path)
    return path
