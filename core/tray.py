import ctypes
import json
import threading
import time
from ctypes import wintypes
from pathlib import Path


WM_QUIT = 0x0012
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_NCCREATE = 0x0081
WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 20
WM_TRAY_NOTIFY = WM_APP + 21
WM_TRAY_OPEN_CHARACTERS = WM_APP + 22
WM_TRAY_SWITCH_CHARACTER = WM_APP + 23
WM_COPYDATA = 0x004A


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]


def decode_character_request(payload):
    from core.package_signature import valid_character_id
    if not 0 < len(payload) <= 32768:
        raise ValueError("切换请求过长。")
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict) or not valid_character_id(data.get("character_id")):
        raise ValueError("切换角色 ID 无效。")
    package = data.get("package", "")
    if not isinstance(package, str) or "\0" in package or (package and not Path(package).is_absolute()):
        raise ValueError("角色包目录无效。")
    token = data.get("request_id", "")
    if not isinstance(token, str) or not 0 < len(token) <= 64:
        raise ValueError("切换请求标识无效。")
    return {"character_id": data["character_id"], "package": package, "request_id": token}
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIM_MODIFY = 0x00000001
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
MF_STRING = 0x00000000
MF_POPUP = 0x00000010
TPM_LEFTALIGN = 0x0000
TPM_BOTTOMALIGN = 0x0020
IDI_APPLICATION = 32512
LRESULT = ctypes.c_longlong


def request_character_settings(title, _payload=None):
    """Ask the existing local tray window to open characters; require its acknowledgement."""
    if not hasattr(ctypes, "windll"):
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                         wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                         ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM
    acknowledged = False

    @callback_type
    def visit(hwnd, _parameter):
        nonlocal acknowledged
        name = ctypes.create_unicode_buffer(256)
        caption = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, len(name))
        if not name.value.startswith("DesktopPetTrayWindow_"):
            return True
        user32.GetWindowTextW(hwnd, caption, len(caption))
        if caption.value != title and not caption.value.startswith(title + " - "):
            return True
        result = ctypes.c_size_t()
        message, parameter = WM_TRAY_OPEN_CHARACTERS, 0
        if _payload is not None:
            buffer = ctypes.create_string_buffer(_payload)
            copy = COPYDATASTRUCT(WM_TRAY_SWITCH_CHARACTER, len(_payload), ctypes.cast(buffer, ctypes.c_void_p))
            message, parameter = WM_COPYDATA, ctypes.addressof(copy)
        delivered = user32.SendMessageTimeoutW(hwnd, message, 0, parameter,
                                              0x0002, 1500, ctypes.byref(result))
        acknowledged = bool(delivered and result.value == 1)
        return not acknowledged

    user32.EnumWindows(visit, 0)
    return acknowledged


def request_switch_character(title, character_id, package="", request_id=None):
    import uuid
    payload = json.dumps({"character_id": character_id, "package": str(package),
                          "request_id": request_id or uuid.uuid4().hex}, ensure_ascii=False).encode("utf-8")
    decode_character_request(payload)
    return request_character_settings(title, _payload=payload)


class WindowsTrayIcon(object):
    """Windows 原生系统托盘图标，不依赖第三方托盘库。"""

    def __init__(
        self,
        title,
        on_show,
        on_tasks,
        on_topmost,
        on_quit,
        on_menu=None,
        menu_items=None,
        icon_path=None,
        on_characters=None,
        on_switch_character=None,
    ):
        self.title = title
        self.on_show = on_show
        self.on_tasks = on_tasks
        self.on_topmost = on_topmost
        self.on_quit = on_quit
        self.on_menu = on_menu
        self.on_characters = on_characters
        self.on_switch_character = on_switch_character
        self._menu_items = list(menu_items or ())
        self.icon_path = str(icon_path) if icon_path else ""
        self._thread = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._notification = None
        self._lock = threading.Lock()
        self._hwnd = None
        self._start_error = ""
        self._class_name = "DesktopPetTrayWindow_{}".format(id(self))

    def set_menu_items(self, menu_items):
        with self._lock:
            self._menu_items = list(menu_items or ())

    def start(self):
        if self._thread is not None:
            return bool(self._hwnd)
        self._stop.clear()
        self._ready.clear()
        self._hwnd = None
        self._start_error = ""
        self._class_name = "DesktopPetTrayWindow_{}_{}".format(
            id(self), int(time.time() * 1000)
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2)
        return bool(self._hwnd)

    def stop(self):
        self._stop.set()
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id, WM_QUIT, 0, 0
            )
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
        self._thread_id = 0
        self._hwnd = None

    def notify(self, title, text):
        with self._lock:
            self._notification = (str(title), str(text))
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id, WM_TRAY_NOTIFY, 0, 0
            )

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = LRESULT
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.UnregisterClassW.restype = wintypes.BOOL
        user32.RegisterClassW.argtypes = [ctypes.c_void_p]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user32.LoadIconW.restype = wintypes.HICON
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HICON
        user32.CreatePopupMenu.argtypes = []
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_size_t,
            wintypes.LPCWSTR,
        ]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            ctypes.c_void_p,
        ]
        user32.TrackPopupMenu.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyMenu.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        wnd_proc_type = ctypes.WINFUNCTYPE(
            LRESULT,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", wnd_proc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeout", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON),
            ]

        self._thread_id = kernel32.GetCurrentThreadId()
        callback = wnd_proc_type(self._window_proc)
        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW(
            0, callback, 0, 0, instance, None, None, None, None, self._class_name
        )
        registered = user32.RegisterClassW(ctypes.byref(window_class))
        if not registered:
            self._start_error = "RegisterClassW:{}".format(ctypes.get_last_error())
        hwnd = user32.CreateWindowExW(
            0,
            self._class_name,
            self.title,
            0x80000000,
            0,
            0,
            0,
            0,
            None,
            None,
            instance,
            None,
        )
        self._hwnd = hwnd
        if not hwnd:
            self._start_error = "{} CreateWindowExW:{}".format(
                self._start_error,
                ctypes.get_last_error(),
            ).strip()
            user32.UnregisterClassW(self._class_name, instance)
            self._ready.set()
            return
        icon = None
        if self.icon_path and Path(self.icon_path).is_file():
            icon = user32.LoadImageW(
                None,
                self.icon_path,
                1,
                0,
                0,
                0x00000010 | 0x00000040,
            )
        if not icon:
            icon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        self._notify_data = NOTIFYICONDATAW()
        self._notify_data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        self._notify_data.hWnd = hwnd
        self._notify_data.uID = 1
        self._notify_data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self._notify_data.uCallbackMessage = WM_TRAY_CALLBACK
        self._notify_data.hIcon = icon
        self._notify_data.szTip = self.title[:127]
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._notify_data))
        self._ready.set()
        message = wintypes.MSG()
        try:
            while not self._stop.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
        finally:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._notify_data))
            if hwnd:
                user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(self._class_name, instance)

    def _window_proc(self, hwnd, message, wparam, lparam):
        user32 = ctypes.windll.user32
        if message == WM_COPYDATA:
            if not lparam or self.on_switch_character is None:
                return 0
            try:
                data = ctypes.cast(lparam, ctypes.POINTER(COPYDATASTRUCT)).contents
                if data.dwData != WM_TRAY_SWITCH_CHARACTER or not data.lpData or not 0 < data.cbData <= 32768:
                    return 0
                request = decode_character_request(ctypes.string_at(data.lpData, data.cbData))
                self.on_switch_character(request)
                return 1
            except (ValueError, UnicodeError, TypeError):
                return 0
        if message in (WM_NCCREATE, 0x000C, 0x000D, 0x000E):
            # Preserve the native caption so another instance can identify this window.
            default_proc = ctypes.WinDLL("user32", use_last_error=True).DefWindowProcW
            default_proc.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
            default_proc.restype = LRESULT
            return default_proc(hwnd, message, wparam, lparam)
        if message == WM_TRAY_CALLBACK:
            if lparam == WM_LBUTTONUP:
                self.on_show()
            elif lparam == WM_RBUTTONUP:
                if self.on_menu is not None:
                    self.on_menu()
                elif self._menu_items:
                    self._show_menu(hwnd)
                else:
                    self._show_menu(hwnd)
            return 0
        if message == WM_TRAY_NOTIFY:
            self._show_notification()
            return 0
        if message == WM_TRAY_OPEN_CHARACTERS:
            if self.on_characters is not None:
                self.on_characters()
                return 1
            return 0
        if message == WM_COMMAND:
            command_id = int(wparam) & 0xFFFF
            with self._lock:
                callbacks = {
                    int(item_id): callback
                    for item_id, _label, callback, _children
                    in self._iter_menu_items(self._menu_items)
                }
            callback = callbacks.get(command_id)
            if callback:
                callback()
            return 0
        if message == WM_CLOSE:
            self._stop.set()
            return 0
        # 这是仅用于托盘消息的隐藏窗口，未处理消息无需继续交给默认窗口过程。
        return 0

    def _show_menu(self, hwnd):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreatePopupMenu.argtypes = []
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_size_t,
            wintypes.LPCWSTR,
        ]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            ctypes.c_void_p,
        ]
        user32.TrackPopupMenu.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyMenu.restype = wintypes.BOOL
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        with self._lock:
            menu_items = list(self._menu_items)

        def append_items(parent_menu, items):
            for item in items:
                command_id, label, _callback, children = (
                    self._menu_item_parts(item)
                )
                if children:
                    submenu = user32.CreatePopupMenu()
                    if not submenu:
                        continue
                    append_items(submenu, children)
                    user32.AppendMenuW(
                        parent_menu,
                        MF_POPUP | MF_STRING,
                        submenu,
                        label,
                    )
                else:
                    user32.AppendMenuW(
                        parent_menu,
                        MF_STRING,
                        command_id,
                        label,
                    )

        append_items(menu, menu_items)
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu(
            menu,
            TPM_LEFTALIGN | TPM_BOTTOMALIGN,
            point.x,
            point.y,
            0,
            hwnd,
            None,
        )
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)

    @staticmethod
    def _menu_item_parts(item):
        if len(item) >= 4:
            item_id, label, callback, children = item[:4]
        else:
            item_id, label, callback = item[:3]
            children = ()
        return item_id, label, callback, tuple(children or ())

    @staticmethod
    def _iter_menu_items(items):
        for item in items or ():
            item_id, label, callback, children = (
                WindowsTrayIcon._menu_item_parts(item)
            )
            yield item_id, label, callback, children
            for child in WindowsTrayIcon._iter_menu_items(children):
                yield child

    def _show_notification(self):
        with self._lock:
            notification = self._notification
            self._notification = None
        if not notification or not self._hwnd:
            return
        title, text = notification
        self._notify_data.uFlags = NIF_INFO
        self._notify_data.szInfoTitle = title[:63]
        self._notify_data.szInfo = text[:255]
        self._notify_data.dwInfoFlags = 0
        ctypes.windll.shell32.Shell_NotifyIconW(
            NIM_MODIFY, ctypes.byref(self._notify_data)
        )
