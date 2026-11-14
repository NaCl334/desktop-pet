"""Process-isolated native rendering for settings previews.

live2d-py can hold the GIL during model loading. A thread alone cannot keep
Tk responsive, so settings workers exchange plain data with this process.
"""
import atexit
import os
import pickle
import struct
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.rendering import Live2DRenderer


def _read(stream, count):
    chunks = bytearray()
    while len(chunks) < count:
        chunk = stream.read(count - len(chunks))
        if not chunk:
            raise EOFError("Live2D preview process disconnected")
        chunks.extend(chunk)
    return bytes(chunks)


def _receive(stream):
    size, = struct.unpack("!I", _read(stream, 4))
    return pickle.loads(_read(stream, size))


def _send(stream, value):
    payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(struct.pack("!I", len(payload)))
    stream.write(payload)
    stream.flush()


class BackgroundLive2DRenderer(Live2DRenderer):
    """Keep metadata local; native calls are made only by settings workers."""

    def __init__(self, *args, **kwargs):
        self._process = None
        self._rpc_lock = threading.RLock()
        self._commands = None
        self._closed = False
        super().__init__(*args, **kwargs)
        self._background_spec = {
            "character_dir": str(self.character_dir),
            "model_path": str(self.model_path),
            "state_definitions": self.state_definitions,
            "appearance": self._appearance,
            "viewport_settings": self.viewport_settings,
        }

    def _request(self, method, *args, **kwargs):
        with self._rpc_lock:
            if self._closed:
                raise RuntimeError("Live2D preview renderer is closed")
            if self._process is None:
                root = Path(__file__).resolve().parents[1]
                command = ([sys.executable, "--live2d-settings-worker"]
                           if getattr(sys, "frozen", False)
                           else [sys.executable, "-m", "core.live2d_background"])
                self._process = subprocess.Popen(
                    command, cwd=str(root), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                atexit.register(self._terminate)
                _send(self._process.stdin, self._background_spec)
            _send(self._process.stdin, (
                method, args, kwargs,
                self.behavior_preferences(), self._follow_enabled,
                self._paused, self._idle_action_interval,
            ))
            success, value = _receive(self._process.stdout)
            if not success:
                raise RuntimeError(value)
            return value

    def prepare(self, state, mode="full"):
        if self._model is not None:
            return True
        try:
            metadata, specs = self._request("prepare", state, mode)
            self.__dict__.update(metadata)
            self._background_parameter_specs = specs
            self._model = True  # Ready marker only; native model stays remote.
            return True
        except Exception as error:
            self.last_error = str(error)
            self._failed = True
            return False

    def parameter_specs(self):
        if not self.prepare("idle"):
            raise RuntimeError(self.last_error)
        return self._background_parameter_specs

    def shared_thumbnail(self, **kwargs):
        if not self.prepare("idle"):
            raise RuntimeError(self.last_error)
        return self._request("shared_thumbnail", **kwargs)

    def preview_thumbnail(self, **kwargs):
        if not self.prepare("idle"):
            raise RuntimeError(self.last_error)
        return self._request("preview_thumbnail", **kwargs)

    def _queue_preview(self, method, *args):
        # Card clicks must never wait for the pipe or a rendering operation.
        if self._closed or self._process is None:
            return False
        if self._commands is None:
            self._commands = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="live2d-preview-command")
        self._commands.submit(self._request, method, *args)
        return True

    def begin_preview(self):
        return self._queue_preview("begin_preview")

    def end_preview(self):
        return self._queue_preview("end_preview")

    def preview_expression(self, state):
        return self._queue_preview("preview_expression", state)

    def preview_pose(self, appearance):
        return self._queue_preview("preview_pose", appearance)

    def preview_motion(self, group, index=0):
        return self._queue_preview("preview_motion", group, index)

    def _terminate(self):
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()
            self._process = None

    def close(self):
        with self._rpc_lock:
            self._closed = True
            self._model = None
            self._terminate()
            atexit.unregister(self._terminate)
        if self._commands is not None:
            self._commands.shutdown(wait=False, cancel_futures=True)


def _worker_pipe(stream, handle_id, mode):
    if stream is not None:
        return stream.buffer
    # Windowed Python/PyInstaller leaves sys.stdin/stdout as None even when
    # Popen supplies valid Windows pipe handles.
    import ctypes
    import msvcrt
    from ctypes import wintypes

    get_handle = ctypes.WinDLL("kernel32", use_last_error=True).GetStdHandle
    get_handle.argtypes = [wintypes.DWORD]
    get_handle.restype = wintypes.HANDLE
    handle = get_handle(handle_id & 0xffffffff)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError("Missing Live2D worker pipe")
    flags = os.O_BINARY | (os.O_RDONLY if mode == "rb" else os.O_WRONLY)
    return os.fdopen(msvcrt.open_osfhandle(handle, flags), mode, buffering=0)


def run_worker():
    # Native libraries print directly to fd 1. Keep the protocol on a duplicate
    # and redirect both native and Python diagnostics away from the pipe.
    source = _worker_pipe(sys.stdin, -10, "rb")
    target = _worker_pipe(sys.stdout, -11, "wb")
    output = os.fdopen(os.dup(target.fileno()), "wb", buffering=0)
    diagnostics = open(os.devnull, "w", encoding="utf-8")
    os.dup2(diagnostics.fileno(), target.fileno())
    sys.stdout = sys.stderr = diagnostics
    renderer = None
    try:
        spec = _receive(source)
        renderer = Live2DRenderer(**spec)
        previous_preferences = None
        while True:
            method, args, kwargs, behavior, follow, paused, interval = _receive(
                source)
            try:
                preferences = behavior, follow, paused, interval
                if preferences != previous_preferences:
                    renderer.set_behavior_preferences(behavior)
                    renderer.set_mouse_follow(follow)
                    renderer.set_paused(paused)
                    renderer.set_idle_action_interval(interval)
                    previous_preferences = preferences
                value = getattr(renderer, method)(*args, **kwargs)
                if method == "preview_thumbnail":
                    # Isolated thumbnails temporarily disable animation and
                    # follow. Restore the caller's preferences on the next RPC.
                    previous_preferences = None
                if method == "prepare":
                    if not value:
                        raise RuntimeError(renderer.last_error)
                    visible = bool(renderer._glfw.get_window_attrib(
                        renderer._window, renderer._glfw.VISIBLE))
                    if visible:
                        raise RuntimeError("Settings rendering window must remain hidden")
                    metadata = {}
                    excluded = {
                        "_model", "_glfw", "_gl", "_live2d", "_window",
                        "_runtime_initialized", "_gl_initialized", "_frame_size",
                    }
                    for key, item in renderer.__dict__.items():
                        if key in excluded:
                            continue
                        try:
                            pickle.dumps(item)
                        except (TypeError, ValueError, pickle.PicklingError):
                            continue
                        metadata[key] = item
                    metadata["_background_window_visible"] = visible
                    value = metadata, renderer.parameter_specs()
                _send(output, (True, value))
            except Exception as error:
                _send(output, (False, "{}: {}".format(type(error).__name__, error)))
    except EOFError:
        pass
    finally:
        if renderer is not None:
            renderer.close()
        output.close()


if __name__ == "__main__":
    run_worker()
