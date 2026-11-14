import ctypes
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from PIL import ImageGrab

from core.config import ROOT


DEFAULT_SCREENSHOT_DIRECTORY = "data/screenshots"


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def resolve_screenshot_directory(directory=None):
    value = str(directory or DEFAULT_SCREENSHOT_DIRECTORY).strip()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def capture_desktop_screenshot(directory=None, retries=3):
    """保存当前桌面截图，失败时最多重试指定次数。"""
    screenshot_dir = resolve_screenshot_directory(directory)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(max(1, retries)):
        for capture in (
            _capture_with_mss,
            _capture_with_win32,
            lambda: ImageGrab.grab(all_screens=True),
        ):
            try:
                image = capture()
                if image is None:
                    continue
                filename = "screenshot_{}.png".format(
                    datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                )
                destination = screenshot_dir / filename
                image.save(destination, format="PNG")
                return destination
            except Exception as error:
                last_error = error
        if attempt + 1 < retries:
            time.sleep(0.25)
    raise RuntimeError("截图失败：{}".format(last_error))


def _capture_with_mss():
    """若用户安装了 mss，优先使用它改善多显示器捕获；未安装则返回 None。"""
    try:
        import mss
    except ImportError:
        return None
    with mss.mss() as capture:
        monitor = capture.monitors[0]
        raw = capture.grab(monitor)
        from PIL import Image

        return Image.frombytes("RGB", raw.size, raw.rgb)


def _capture_with_win32():
    """使用 Win32 BitBlt 捕获虚拟桌面，作为无 mss 环境的回退方案。"""
    if not hasattr(ctypes, "windll"):
        return None
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    gdi32.CreateDCW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
    ]
    gdi32.CreateDCW.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.BitBlt.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(BITMAPINFOHEADER),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = wintypes.BOOL
    x = user32.GetSystemMetrics(76)
    y = user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78)
    height = user32.GetSystemMetrics(79)
    if width <= 0 or height <= 0:
        raise RuntimeError("无法读取桌面尺寸")

    screen_dc = gdi32.CreateDCW("DISPLAY", None, None, None)
    if not screen_dc:
        raise RuntimeError("无法创建屏幕设备上下文")
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    if not memory_dc or not bitmap:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        gdi32.DeleteDC(screen_dc)
        raise RuntimeError("无法创建桌面截图缓冲区")

    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not gdi32.BitBlt(
            memory_dc,
            0,
            0,
            width,
            height,
            screen_dc,
            x,
            y,
            0x00CC0020,
        ):
            raise RuntimeError("BitBlt 截图失败")
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            buffer,
            ctypes.byref(header),
            0,
        ):
            raise RuntimeError("GetDIBits 截图失败")
        from PIL import Image

        return Image.frombytes(
            "RGBA",
            (width, height),
            buffer,
            "raw",
            "BGRA",
        ).convert("RGB")
    finally:
        gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        gdi32.DeleteDC(screen_dc)
