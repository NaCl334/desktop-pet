import ctypes
import threading
import time
from ctypes import wintypes


MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_ALT = 0x0001
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_LWIN = 0x5B
VK_RWIN = 0x5C
MODIFIER_VIRTUAL_KEYS = {
    VK_SHIFT: "Shift",
    VK_LSHIFT: "Shift",
    VK_RSHIFT: "Shift",
    VK_CONTROL: "Ctrl",
    VK_LCONTROL: "Ctrl",
    VK_RCONTROL: "Ctrl",
    VK_MENU: "Alt",
    VK_LMENU: "Alt",
    VK_RMENU: "Alt",
    VK_LWIN: "Win",
    VK_RWIN: "Win",
}


def parse_hotkey(value):
    """解析 Ctrl+Shift+S 形式的快捷键，返回 Win32 修饰键和虚拟键码。"""
    parts = [part.strip() for part in str(value or "").split("+") if part.strip()]
    if not parts:
        raise ValueError("快捷键不能为空")
    modifiers = 0
    key = parts[-1].upper()
    modifier_map = {
        "CTRL": MOD_CONTROL,
        "CONTROL": MOD_CONTROL,
        "SHIFT": MOD_SHIFT,
        "ALT": MOD_ALT,
        "WIN": MOD_WIN,
        "WINDOWS": MOD_WIN,
    }
    for part in parts[:-1]:
        modifier = modifier_map.get(part.upper())
        if modifier is None:
            raise ValueError("不支持的快捷键修饰键：{}".format(part))
        modifiers |= modifier
    if len(key) == 1 and key.isalpha():
        virtual_key = ord(key)
    elif len(key) == 1 and key.isdigit():
        virtual_key = ord(key)
    elif key == "ESC":
        virtual_key = 0x1B
    elif key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
        virtual_key = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError("不支持的快捷键：{}".format(value))
    return modifiers, virtual_key


class WindowsHotkeyManager(object):
    """注册全局键盘热键。"""

    def __init__(self, bindings, on_error=None):
        self.bindings = bindings
        self.on_error = on_error
        self._thread = None
        self._thread_id = 0
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._release_lock = threading.Lock()
        self._watched_keys = set()
        self._registered = {}
        self._received_hotkeys = []
        self._last_error = ""

    def start(self):
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._ready_event.clear()
            self._registered = {}
            self._received_hotkeys = []
            self._last_error = ""
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        self._ready_event.wait(timeout=2)

    def stop(self):
        with self._lifecycle_lock:
            thread = self._thread
            thread_id = self._thread_id
            self._stop_event.set()
            if thread_id:
                try:
                    ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
                except (AttributeError, OSError):
                    pass
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2)
            if self._thread is thread:
                self._thread = None
            self._thread_id = 0

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = kernel32.GetCurrentThreadId()
        registered = []
        for hotkey_id, binding in enumerate(self.bindings, start=1):
            modifiers, virtual_key, callback, label = binding[:4]
            release_callback = binding[4] if len(binding) > 4 else None
            if user32.RegisterHotKey(None, hotkey_id, modifiers, virtual_key):
                registered.append((hotkey_id, callback, release_callback, virtual_key))
                self._registered[hotkey_id] = label
            elif self.on_error:
                self._last_error = "快捷键注册失败：{}".format(label)
                self.on_error(self._last_error)
        self._ready_event.set()

        message = wintypes.MSG()
        try:
            while not self._stop_event.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message != WM_HOTKEY:
                    continue
                self._received_hotkeys.append(int(message.wParam))
                for hotkey_id, callback, release_callback, virtual_key in registered:
                    if hotkey_id == message.wParam:
                        try:
                            callback()
                        except Exception as error:
                            if self.on_error:
                                self.on_error("快捷键回调失败：{}".format(error))
                        if release_callback is not None:
                            self._watch_key_release(virtual_key, release_callback)
                        break
        finally:
            for hotkey_id, _callback, _release_callback, _virtual_key in registered:
                user32.UnregisterHotKey(None, hotkey_id)

    def _watch_key_release(self, virtual_key, callback):
        with self._release_lock:
            if virtual_key in self._watched_keys:
                return
            self._watched_keys.add(virtual_key)

        def worker():
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.GetAsyncKeyState.argtypes = [wintypes.INT]
                user32.GetAsyncKeyState.restype = wintypes.SHORT
                while not self._stop_event.is_set():
                    if not (user32.GetAsyncKeyState(virtual_key) & 0x8000):
                        callback()
                        break
                    time.sleep(0.01)
            finally:
                with self._release_lock:
                    self._watched_keys.discard(virtual_key)

        threading.Thread(target=worker, daemon=True).start()

class WindowsKeyboardCapture(object):
    """轮询 Windows 物理键状态，绕过输入法和 Tk 键盘事件。"""

    def __init__(self, callback, on_error=None):
        self.callback = callback
        self.on_error = on_error
        self._thread = None
        self._thread_id = 0
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._started = False
        self._reported = False

    def start(self):
        if self._thread is not None:
            return self._started
        self._stop_event.clear()
        self._ready_event.clear()
        self._started = False
        self._reported = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready_event.wait(timeout=2)
        if not self._started:
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1)
            self._thread = None
            self._thread_id = 0
        return self._started

    def stop(self):
        self._stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
        self._thread_id = 0
        self._started = False

    def _run(self):
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetAsyncKeyState.argtypes = [wintypes.INT]
            user32.GetAsyncKeyState.restype = wintypes.SHORT
            self._thread_id = threading.get_ident()
            modifier_keys = tuple(MODIFIER_VIRTUAL_KEYS)
            candidate_keys = tuple(
                list(range(0x30, 0x3A))
                + list(range(0x41, 0x5B))
                + list(range(0x70, 0x7C))
                + [0x08, 0x09, 0x0D, 0x1B, 0x20]
            )

            def is_down(vk):
                return bool(user32.GetAsyncKeyState(vk) & 0x8000)

            previous = {vk: is_down(vk) for vk in candidate_keys}
            self._started = True
            self._ready_event.set()
            while not self._stop_event.is_set():
                modifiers = {
                    MODIFIER_VIRTUAL_KEYS[vk]
                    for vk in modifier_keys
                    if is_down(vk)
                }
                for vk in candidate_keys:
                    current = is_down(vk)
                    if current and not previous[vk]:
                        if self._reported or self._stop_event.is_set():
                            break
                        self._reported = True
                        self.callback(self._format_key(vk, 0, modifiers))
                        self._stop_event.set()
                        break
                    previous[vk] = current
                time.sleep(0.004)
        except Exception as error:
            self._started = False
            self._ready_event.set()
            if self.on_error:
                self.on_error("无法读取 Windows 物理按键：{}".format(error))
        finally:
            self._started = False

    @staticmethod
    def _format_key(vk, scan_code, modifiers):
        names = {
            0x0D: "Enter",
            0x1B: "Esc",
            0x20: "Space",
            0x08: "Backspace",
            0x09: "Tab",
        }
        key = names.get(vk)
        if key is None and 0x70 <= vk <= 0x7B:
            key = "F{}".format(vk - 0x6F)
        if key is None and 0x30 <= vk <= 0x39:
            key = chr(vk)
        if key is None and 0x41 <= vk <= 0x5A:
            key = chr(vk)
        if key is None:
            buffer = ctypes.create_unicode_buffer(64)
            lparam = (int(scan_code) << 16) | (1 << 24)
            length = ctypes.windll.user32.GetKeyNameTextW(lparam, buffer, 64)
            key = buffer.value[:length] if length else "VK{}".format(vk)
        modifier_order = ("Ctrl", "Alt", "Shift", "Win")
        prefix = [name for name in modifier_order if name in modifiers]
        return "+".join(prefix + [key])
