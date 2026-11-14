"""Mouse input styles shared by the pet root and layered portraits."""
import ctypes
from ctypes import wintypes
import os


def passthrough_style(style, enabled):
    # Keep alpha, tool-window and activation flags owned by other components.
    return (style | 0x00080000 | 0x20) if enabled else (style & ~0x20)


def set_window_passthrough(window, enabled):
    if os.name != "nt":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ancestor = user32.GetAncestor
    ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
    ancestor.restype = wintypes.HWND
    hwnd = ancestor(window.winfo_id(), 2) or window.winfo_id()
    get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_long.restype = ctypes.c_ssize_t
    set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_long.restype = ctypes.c_ssize_t
    style = get_long(hwnd, -20)
    expected = passthrough_style(style, enabled)
    if style != expected:
        ctypes.set_last_error(0)
        set_long(hwnd, -20, expected)
        error = ctypes.get_last_error()
        if error:
            raise ctypes.WinError(error)
        if get_long(hwnd, -20) != expected:
            raise OSError("Windows did not retain mouse passthrough style")
    return True
