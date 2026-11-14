import ctypes
import os
import sys
from pathlib import Path


if __name__ == "__main__" and "--verify-character-package" in sys.argv:
    from core.package_check import run
    index = sys.argv.index("--verify-character-package")
    raise SystemExit(run(sys.argv[index + 1], sys.argv[index + 2]))


if __name__ == "__main__" and "--setup-worker" in sys.argv:
    from core.install_location import run_setup_worker
    index = sys.argv.index("--setup-worker")
    raise SystemExit(run_setup_worker(sys.argv[index + 1], sys.argv[index + 2]))


if __name__ == "__main__" and "--live2d-settings-worker" in sys.argv:
    from core.live2d_background import run_worker
    run_worker()
    raise SystemExit(0)


_DPI_AWARENESS_RESULT = None


def _configure_windows_dpi_awareness():
    """Enable physical-pixel window geometry before Tk or Live2D starts."""
    global _DPI_AWARENESS_RESULT
    if _DPI_AWARENESS_RESULT is not None:
        return _DPI_AWARENESS_RESULT
    if os.name != "nt":
        _DPI_AWARENESS_RESULT = "not-windows"
        return _DPI_AWARENESS_RESULT

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if set_context(ctypes.c_void_p(-4)):
            _DPI_AWARENESS_RESULT = "per-monitor-v2"
            return _DPI_AWARENESS_RESULT
    except (AttributeError, OSError):
        pass

    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        awareness = ctypes.c_int()
        get_awareness = shcore.GetProcessDpiAwareness
        get_awareness.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        get_awareness.restype = ctypes.c_long
        if (
            get_awareness(None, ctypes.byref(awareness)) == 0
            and awareness.value >= 2
        ):
            _DPI_AWARENESS_RESULT = "already-per-monitor"
            return _DPI_AWARENESS_RESULT

        set_awareness = shcore.SetProcessDpiAwareness
        set_awareness.argtypes = [ctypes.c_int]
        set_awareness.restype = ctypes.c_long
        # PROCESS_PER_MONITOR_DPI_AWARE
        if set_awareness(2) == 0:
            _DPI_AWARENESS_RESULT = "per-monitor"
            return _DPI_AWARENESS_RESULT
    except (AttributeError, OSError):
        pass

    try:
        if user32.SetProcessDPIAware():
            _DPI_AWARENESS_RESULT = "system"
            return _DPI_AWARENESS_RESULT
    except (AttributeError, OSError):
        pass

    _DPI_AWARENESS_RESULT = "unchanged"
    return _DPI_AWARENESS_RESULT


def _configure_tk_runtime():
    """为 Conda/便携 Python 指定 Tcl/Tk 脚本目录，避免中文路径自动探测失败。"""
    project_root = Path(__file__).resolve().parent
    roots = [
        Path(sys.prefix),
        Path(getattr(sys, "base_prefix", sys.prefix)),
        project_root / "runtime",
    ]
    seen = set()
    layouts = (
        ("Library", "lib"),
        ("lib",),
        ("tcl",),
    )
    for root in roots:
        root = root.resolve()
        if root in seen:
            continue
        seen.add(root)
        for layout in layouts:
            library_root = root.joinpath(*layout)
            tcl_path = library_root / "tcl8.6"
            tk_path = library_root / "tk8.6"
            if (tcl_path / "init.tcl").is_file():
                os.environ["TCL_LIBRARY"] = str(tcl_path)
            if (tk_path / "tk.tcl").is_file():
                os.environ["TK_LIBRARY"] = str(tk_path)
            if os.environ.get("TCL_LIBRARY") and os.environ.get("TK_LIBRARY"):
                return


_configure_tk_runtime()
_configure_windows_dpi_awareness()

from core.branding import APP_NAME
from ui.app import DesktopPetApp


_SINGLE_INSTANCE_MUTEX = None


def _acquire_single_instance():
    """Prevent multiple desktop-pet processes from creating duplicate windows."""
    global _SINGLE_INSTANCE_MUTEX
    if hasattr(ctypes, "windll"):
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        _SINGLE_INSTANCE_MUTEX = kernel32.CreateMutexW(
            None,
            False,
            "Local\\MyDesktopPet.SingleInstance",
        )
        if not _SINGLE_INSTANCE_MUTEX:
            return False
        return ctypes.get_last_error() != 183
    return True


def run_desktop_pet():
    if "--wait-for-process" in sys.argv and os.name == "nt":
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        pid = int(sys.argv[sys.argv.index("--wait-for-process") + 1])
        process = kernel32.OpenProcess(0x100000, False, pid)
        if process:
            try:
                if kernel32.WaitForSingleObject(process, 60000) == 0x102:
                    raise RuntimeError("旧窗口尚未退出，请退出后启动安装目录中的桌宠。")
            finally:
                kernel32.CloseHandle(process)
    open_characters = "--open-characters" in sys.argv
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--character", default="")
    parser.add_argument("--character-package", default="")
    options, _ = parser.parse_known_args()
    from core.package_signature import valid_character_id
    if options.character and not valid_character_id(options.character):
        raise ValueError("启动器指定的角色 ID 无效。")
    if options.character_package and not options.character:
        raise ValueError("角色包启动参数缺少角色 ID。")
    if _acquire_single_instance():
        startup_errors = []
        target = options.character
        if options.character_package:
            try:
                from core.backup import import_signed_directory
                from core.package_signature import verify_package
                manifest = verify_package(options.character_package)
                if manifest["character_id"] != target:
                    raise ValueError("启动器角色 ID 与角色包不一致。")
                import_signed_directory(options.character_package)
            except (OSError, ValueError) as error:
                startup_errors.append(str(error))
                target = ""
        if getattr(sys, "frozen", False):
            from core.body_location import register_body
            register_body(sys.executable)
        app = DesktopPetApp(startup_character=target, startup_errors=startup_errors)
        if open_characters:
            app.root.after(0, lambda: app.show_control_page("characters"))
        app.run()
    elif options.character or open_characters:
        import time
        import uuid
        from core.tray import request_character_settings
        from core.tray import request_switch_character
        token = uuid.uuid4().hex
        for attempt in range(3):
            acknowledged = (request_switch_character(APP_NAME, options.character,
                            options.character_package, token) if options.character
                            else request_character_settings(APP_NAME))
            if acknowledged:
                return
            if attempt < 2:
                time.sleep(0.4)
        raise RuntimeError("已有一个桌宠在运行，但切换或打开角色设置失败，请退出旧窗口后重试。")


if __name__ == "__main__":
    try:
        run_desktop_pet()
    except Exception as error:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, str(error), "桌宠启动失败", 0x10)
        else:
            raise
