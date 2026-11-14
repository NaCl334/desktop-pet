"""Persistent voice-service feedback with bounded background health checks."""
import json
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import ttk
from core.setup_packages import VOICE_ENGINE_LABEL


class VoiceServicePanel(tk.Frame):
    def __init__(self, parent, *, colors, client, project_root, get_status,
                 set_status, on_start, monitor=True):
        super().__init__(parent, bg=colors["panel"])
        self.client = client
        self.project_root = Path(project_root).resolve()
        self.get_status = get_status
        self.set_status = set_status
        self.monitor = monitor
        self._closed = False
        self._after = None
        self._probe_running = False
        self._probe_result = None
        self._next_probe = 0
        self._progress_running = False
        self.grid_columnconfigure(0, weight=1)
        self.title_label = tk.Label(self, text="语音合成（本地 {}）".format(VOICE_ENGINE_LABEL), bg=colors["panel"],
                 fg=colors["white"], font=("Microsoft YaHei UI", 10, "bold"),
                 anchor="w")
        self.title_label.grid(row=0, column=0, sticky="ew", padx=14, pady=(6, 2))
        self.status_label = tk.Label(self, bg=colors["panel"], fg=colors["muted"],
                                     anchor="w", justify="left")
        self.status_label.grid(row=1, column=0, sticky="ew", padx=14)
        self.status_label.bind("<Configure>", lambda event: self.status_label.configure(
            wraplength=max(1, event.width)))
        ttk.Style(self).configure("Voice.Horizontal.TProgressbar", thickness=6,
            background=colors["cyan"], troughcolor=colors["bg"], borderwidth=0)
        progress_host = tk.Frame(self, bg=colors["bg"], height=12)
        progress_host.grid(row=2, column=0, sticky="ew", padx=14, pady=(2, 4))
        self.progress = ttk.Progressbar(progress_host, mode="determinate", style="Voice.Horizontal.TProgressbar")
        self.progress.place(x=0, y=0, relwidth=1, relheight=1)
        self.start_button = tk.Button(self, text="启动语音服务", command=on_start,
            bg=colors["cyan"], fg=colors["dark"], activebackground=colors["panel_soft"],
            disabledforeground=colors["dark"], relief="flat", bd=0, padx=12, pady=7, cursor="hand2")
        self.start_button.grid(row=0, column=1, rowspan=3, padx=14, pady=10)
        self.bind("<Destroy>", self._destroyed, add="+")
        self._after = self.after(100, self._tick)

    def destroy(self):
        self.progress.stop()
        super().destroy()

    def _destroyed(self, event):
        if event.widget is self:
            self._closed = True
            if self._after is not None:
                self.after_cancel(self._after)
                self._after = None

    def _probe(self, baseline):
        result = {"state": "stopped", "text": "语音服务未运行，点击右侧按钮启动。"}
        try:
            from core.gpt_sovits import GPTSoVITSClient
            from core.voice_service_repair import local_environment_status
            environment = local_environment_status(self.client) if isinstance(self.client, GPTSoVITSClient) else None
            if environment:
                result = environment
                return
            identity = json.loads(self.client._request("/desktop_pet/service", timeout=1.5))
            if not identity.get("project_root") or not identity.get("instance_id") or Path(identity["project_root"]).resolve() != self.project_root:
                result = {"state": "failed", "text": "语音端口被另一套服务占用，可以切换备用端口。",
                          "action": "repair", "action_label": "修复并启动"}
            else:
                result = {"state": "ready", "text": "语音服务运行中"}
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            process = getattr(self.client, "process", None)
            if baseline.get("state") == "ready" and process is not None and process.poll() is None:
                result = {"state": "ready", "text": "语音服务进程运行中，正在等待状态响应…"}
            elif baseline.get("state") == "failed":
                result = baseline
        finally:
            self._probe_result = (baseline, result)
            self._probe_running = False

    def _tick(self):
        self._after = None
        if self._closed:
            return
        status = self.get_status()
        if self._probe_result is not None:
            baseline, result = self._probe_result
            self._probe_result = None
            if status is baseline and status.get("state") not in ("starting", "checking"):
                self.set_status(result)
                status = result
        state = status.get("state", "unknown")
        busy = state in ("starting", "checking")
        text = status.get("text", "正在检查语音服务状态…")
        if busy and status.get("started_at") is not None:
            text += " · 已用 {} 秒".format(max(0, int(time.monotonic()-status["started_at"])))
        self.status_label.configure(text=text)
        operation = status.get("operation")
        self.start_button.configure(text=("安装中…" if operation == "install" else "修复中…" if operation == "repair" else "启动中…") if busy else
            "正在运行" if state == "ready" else status.get("action_label", "重试启动") if state in ("failed", "missing") else "启动语音服务",
            state="disabled" if busy or state == "ready" else "normal")
        percent = status.get("percent")
        if busy and isinstance(percent, (int, float)):
            self.progress.stop()
            self.progress.configure(mode="determinate", value=max(0, min(100, percent)))
            self._progress_running = False
        elif busy and not self._progress_running:
            self.progress.configure(mode="indeterminate")
            self.progress.start(18)
            self._progress_running = True
        elif not busy:
            if self._progress_running:
                self.progress.stop()
            self.progress.configure(mode="determinate", value=100 if state == "ready" else 0)
            self._progress_running = False
        if self.monitor and not busy and not self._probe_running and time.monotonic() >= self._next_probe and self.winfo_viewable():
            self._next_probe = time.monotonic() + 5
            self._probe_running = True
            threading.Thread(target=self._probe, args=(status,), daemon=True,
                             name="desktop-pet-voice-status").start()
        self._after = self.after(500, self._tick)


class VoiceStartupNotice:
    """Share startup state without creating a conversation or stealing focus."""

    def __init__(self, app, colors, get_status=None, on_start=None, title=None):
        self.app = app
        self.get_status = get_status or (lambda: app._tts_service_status)
        self.closed = False
        self.timer = None
        self.ready_at = None
        self.window = tk.Toplevel(app.root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(bg=colors["cyan"])
        app._configure_tool_window_style(self.window)
        self.panel = VoiceServicePanel(self.window, colors=colors,
            client=app.gpt_sovits_client, project_root=Path.cwd(),
            get_status=self.get_status,
            set_status=lambda value: setattr(app, "_tts_service_status", value),
            on_start=on_start or app._start_tts_service_manually, monitor=False)
        if title:
            self.panel.title_label.configure(text=title)
        self.panel.pack(fill="both", expand=True, padx=1, pady=1)
        self.binding = app.root.bind("<Destroy>", self._root_destroyed, add="+")
        self.tick()

    def _root_destroyed(self, event):
        if event.widget is self.app.root:
            self.destroy()

    def tick(self):
        self.timer = None
        if self.closed:
            return
        app = self.app
        if getattr(app, "_quitting", False):
            self.destroy()
            return
        ready = self.get_status().get("state") == "ready"
        if ready:
            if self.ready_at is None:
                self.ready_at = time.monotonic()
            elif time.monotonic() - self.ready_at >= 2.5:
                self.destroy()
                return
        else:
            self.ready_at = None
        blocked = (not app.root.winfo_viewable()
            or not getattr(app, "_portrait_available", True)
            or getattr(app, "screenshot_in_progress", False)
            or getattr(app, "_external_screenshot_window_states", None) is not None
            or getattr(app, "portrait_editing", False)
            or bool(getattr(app, "drag_start", None)))
        if blocked:
            self.window.withdraw()
        else:
            from ui.pet_toolbar import fit_position
            canvas = app.portrait_canvas
            x, y = canvas.winfo_rootx(), canvas.winfo_rooty()
            width, height = canvas.winfo_width(), canvas.winfo_height()
            bounds = app._context_screen_bounds({"portrait_screen_x": x,
                "portrait_screen_y": y, "portrait_width": width, "portrait_height": height})
            notice_width = min(440, max(240, bounds[2] - bounds[0] - 16))
            self.window.update_idletasks()
            notice_height = self.window.winfo_reqheight()
            nx, ny = fit_position(x + (width-notice_width)//2,
                y - notice_height - 12, notice_width, notice_height, bounds)
            app._position_context_bar(self.window, {"switcher_width": notice_width,
                "switcher_height": notice_height, "panel_x": nx, "panel_y": ny})
            self.window.attributes("-topmost", bool(app.root.attributes("-topmost")))
            self.window.deiconify()
        self.timer = app.root.after(200, self.tick)

    def destroy(self):
        if self.closed:
            return
        self.closed = True
        if self.timer is not None:
            self.app.root.after_cancel(self.timer)
            self.timer = None
        self.app.root.unbind("<Destroy>", self.binding)
        if self.window.winfo_exists():
            self.window.destroy()
