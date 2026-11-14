import queue
import os
from pathlib import Path
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk

from core.branding import ICON_PATH, LOGO_PATH
from core.config import ROOT
from core.installer import SetupError, install_profile
from core.install_location import install_in_directory
from core.setup_packages import VOICE_ENGINE_LABEL


class SetupWizard:
    def __init__(self, root, report, on_finished=None, on_dismissed=None,
                 on_completed=None, needs_character=False, on_relocated=None):
        self.root = root
        self.report = report
        self.on_finished = on_finished
        self.on_dismissed = on_dismissed
        self.on_completed = on_completed
        self.needs_character = needs_character
        self._initial_needs_character = needs_character
        self.on_relocated = on_relocated
        self.install_directory = ROOT.resolve()
        self._completion_handled = False
        self.dialog = None
        self.profile_var = tk.StringVar(master=root, value="desktop")
        self.status_var = tk.StringVar(master=root, value="")
        self.detail_var = tk.StringVar(master=root)
        self.cancel_event = threading.Event()
        self.worker = None
        self._events = queue.Queue()
        self._dismissed_notified = False
        self._page = 0
        self._poll_after = None
        self._close_after = None

    def show(self):
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            return self.dialog
        dialog = self.dialog = tk.Toplevel(self.root)
        dialog.title("我的桌宠安装向导")
        try:
            dialog.iconbitmap(str(ICON_PATH))
        except tk.TclError:
            try:
                with Image.open(LOGO_PATH) as source:
                    self._window_icon = ImageTk.PhotoImage(source.convert("RGBA"), master=dialog)
                dialog.iconphoto(False, self._window_icon)
            except (OSError, ValueError, tk.TclError):
                pass
        scale = max(1.0, float(self.root.tk.call("tk", "scaling")) / (96 / 72))
        width = min(round(740 * scale), max(680, dialog.winfo_screenwidth() - 80))
        height = min(round(660 * scale), max(540, dialog.winfo_screenheight() - 80))
        dialog.geometry("{}x{}".format(width, height))
        dialog.minsize(min(width, round(680 * scale)), min(height, round(540 * scale)))
        dialog.configure(bg="white")
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", self.cancel)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(1, weight=1)

        heading = tk.Frame(dialog, bg="white")
        heading.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 18))
        heading.grid_columnconfigure(0, weight=1)
        self.heading = tk.Label(heading, text="欢迎使用我的桌宠", anchor="w",
            bg="white", fg="#202936", font=("Microsoft YaHei UI", 17, "bold"))
        self.heading.grid(row=0, column=0, sticky="w")
        self.subtitle = tk.Label(heading, text="完成首次运行配置，即可开启桌面陪伴。",
            bg="white", fg="#687484", font=("Microsoft YaHei UI", 9), anchor="w")
        self.subtitle.grid(row=1, column=0, sticky="w", pady=(6, 0))
        try:
            with Image.open(LOGO_PATH) as source:
                image = source.convert("RGBA")
                image.thumbnail((64, 64), Image.Resampling.LANCZOS)
            self._logo = ImageTk.PhotoImage(image, master=dialog)
            tk.Label(heading, image=self._logo, bg="white").grid(row=0, column=1, rowspan=2, padx=(20, 0))
        except (OSError, ValueError):
            self._logo = None
        tk.Frame(dialog, bg="#e5e9ee", height=1).grid(row=0, column=0, sticky="sew")

        self.body = tk.Frame(dialog, bg="white")
        self.body.grid(row=1, column=0, sticky="nsew", padx=28, pady=20)
        self.body.grid_columnconfigure(0, weight=1)
        self.welcome = tk.Frame(self.body, bg="white")
        self.welcome.grid(row=0, column=0, sticky="nsew")
        tk.Label(self.welcome, text="让她陪在你的桌面上", bg="white", fg="#202936",
            font=("Microsoft YaHei UI", 12, "bold"), anchor="w").pack(anchor="w", pady=(8, 16))
        tk.Label(self.welcome,
            text="选择运行方式，确认本地语音环境。\n\n现有角色、立绘和聊天记录会继续保留。",
            bg="white", fg="#4c596b", font=("Microsoft YaHei UI", 10),
            justify="left", anchor="nw").pack(fill="x")

        self.options = tk.Frame(self.body, bg="white")
        self.options.grid(row=0, column=0, sticky="nsew")
        self.options.grid_columnconfigure(0, weight=1)
        tk.Label(self.options, text="安装位置", bg="white", fg="#4c596b",
                 font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", pady=(0, 4))
        self.location_var = tk.StringVar(master=self.root, value=str(ROOT.resolve()))
        location_row = tk.Frame(self.options, bg="white")
        location_row.pack(fill="x", pady=(0, 10))
        self.location_entry = ttk.Entry(location_row, textvariable=self.location_var)
        self.location_entry.pack(side="left", fill="x", expand=True)
        self.location_button = ttk.Button(location_row, text="浏览…", command=self.choose_location)
        self.location_button.pack(side="right", padx=(8, 0))
        self.profile_choices = {}
        for profile, label in (("desktop", "仅使用桌宠"), ("full", "桌宠与本地语音（{}）".format(VOICE_ENGINE_LABEL))):
            # Keep one shared choice selected, even when its checkmark is clicked again.
            choice = tk.Checkbutton(self.options, text=label, variable=self.profile_var,
                onvalue=profile, offvalue=profile,
                command=self.refresh_details, bg="white", fg="#263446", selectcolor="white",
                activebackground="white", font=("Microsoft YaHei UI", 10),
                anchor="w")
            choice.pack(fill="x", pady=3)
            self.profile_choices[profile] = choice
        tk.Label(self.options, textvariable=self.detail_var, bg="white", fg="#687484",
            font=("Microsoft YaHei UI", 9), anchor="nw", justify="left", wraplength=610).pack(
                fill="x", padx=24, pady=(6, 10))
        self.details_button = tk.Button(self.options, text="环境详情  ⌄", command=self.toggle_details,
            bg="white", fg="#2175bf", activebackground="white", relief="flat", bd=0,
            cursor="hand2", anchor="w", font=("Microsoft YaHei UI", 9))
        self.details_button.pack(anchor="w", pady=(8, 4))
        self.details = tk.Frame(self.options, bg="white")
        names = {"live2d": "Live2D（可选）", "python": "Python", "torch": "语音计算环境", "compute": "运算设备",
                 "ffmpeg": "音频处理", "gpt_sovits_runtime": VOICE_ENGINE_LABEL + " 引擎", "gpt_sovits_model": "v2Pro 基础语音模型"}
        self.component_status_labels = {}
        for row, (key, item) in enumerate(self.report.get("components", {}).items()):
            if key not in names:
                continue
            tk.Label(self.details, text=names[key], bg="white", fg="#4c596b",
                     anchor="w", font=("Microsoft YaHei UI", 9)).grid(row=row, column=0, sticky="w", padx=(4, 28), pady=2)
            status = tk.Label(self.details, text="已就绪" if item.get("available") else "未就绪",
                     bg="white", fg="#27805a" if item.get("available") else "#b75c2a",
                     anchor="w", font=("Microsoft YaHei UI", 9))
            status.grid(row=row, column=1, sticky="w")
            self.component_status_labels[key] = status

        self.completion = tk.Frame(self.body, bg="white")
        self.completion.grid(row=0, column=0, sticky="nsew")
        self.completion_title = tk.Label(self.completion, text="桌宠已准备就绪", bg="white",
            fg="#202936", font=("Microsoft YaHei UI", 12, "bold"), anchor="w")
        self.completion_title.pack(anchor="w", pady=(8, 16))
        self.completion_message = tk.Label(self.completion, bg="white", fg="#4c596b",
            font=("Microsoft YaHei UI", 10), justify="left", anchor="nw", wraplength=610)
        self.completion_message.pack(fill="x")

        footer = tk.Frame(dialog, bg="#f6f7f9", highlightbackground="#e5e9ee", highlightthickness=1)
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        tk.Label(footer, textvariable=self.status_var, bg="#f6f7f9", fg="#687484",
                 anchor="w", justify="left", wraplength=640, font=("Microsoft YaHei UI", 9)).grid(
                     row=0, column=0, columnspan=3, sticky="ew", padx=24, pady=(10, 4))
        style = ttk.Style(dialog)
        for element in ("trough", "pbar"):
            name = "Setup.Progressbar." + element
            if name not in style.element_names():
                style.element_create(name, "from", "clam", "Horizontal.Progressbar." + element)
        style.layout("Setup.Horizontal.TProgressbar", [
            ("Setup.Progressbar.trough", {"sticky": "nswe", "children": [
                ("Setup.Progressbar.pbar", {"side": "left", "sticky": "ns"}),
            ]}),
        ])
        style.configure("Setup.Horizontal.TProgressbar", background="#2eae4e",
                        troughcolor="#e5e9ee", bordercolor="#e5e9ee",
                        lightcolor="#2eae4e", darkcolor="#2eae4e", thickness=12)
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=100,
                                       style="Setup.Horizontal.TProgressbar")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(24, 12))
        self.progress_label = tk.Label(footer, text="0%", bg="#f6f7f9", fg="#687484",
                                      font=("Microsoft YaHei UI", 9), width=10, anchor="e")
        self.progress_label.grid(row=1, column=2, sticky="e", padx=(0, 24))
        self.progress.grid_remove()
        self.progress_label.grid_remove()
        buttons = tk.Frame(footer, bg="#f6f7f9")
        buttons.grid(row=2, column=0, columnspan=3, sticky="e", padx=24, pady=(8, 16))

        def button(text, command, primary=False):
            return tk.Button(buttons, text=text, command=command, width=10, padx=5, pady=5,
                relief="solid", bd=1, bg="#2175bf" if primary else "white",
                fg="white" if primary else "#344254", activebackground="#e5eef7",
                font=("Microsoft YaHei UI", 9), cursor="hand2")

        self.back_button = button("上一步", self.previous_page)
        self.back_button.pack(side="left", padx=(0, 8))
        self.install_button = button("下一步", self.next_page, True)
        self.install_button.pack(side="left", padx=(0, 8))
        self.close_button = button("取消", self.cancel)
        self.close_button.pack(side="left")
        self.refresh_details()
        self._show_page(0)
        return dialog

    def _show_page(self, page):
        self._page = page
        self.welcome.grid() if page == 0 else self.welcome.grid_remove()
        self.options.grid() if page == 1 else self.options.grid_remove()
        self.completion.grid() if page == 2 else self.completion.grid_remove()
        self.back_button.configure(state="disabled" if page == 0 else "normal")
        if page != 1:
            self.back_button.pack_forget()
        else:
            self.back_button.pack(side="left", padx=(0, 8), before=self.install_button)
        self.install_button.configure(text="下一步" if page == 0 else "开始配置")
        self.heading.configure(text=("欢迎使用我的桌宠", "选择运行方式", "配置完成")[page])

    def _show_completion(self, title, message, on_close=None):
        self._show_page(2)
        self.completion_title.configure(text=title)
        self.completion_message.configure(text=message)
        self.status_var.set("")
        self.progress.grid_remove()
        self.progress_label.grid_remove()
        self.close_button.pack_forget()
        close = on_close or self._complete_setup
        self.install_button.configure(text="完成", state="normal", command=close)
        self.dialog.protocol("WM_DELETE_WINDOW", close)

    def _complete_setup(self):
        if self._completion_handled:
            return
        if self.install_directory != ROOT.resolve():
            try:
                subprocess.Popen([str(self.install_directory / "我的桌宠.exe"),
                                  "--open-characters", "--wait-for-process", str(os.getpid())],
                                 cwd=self.install_directory)
            except OSError as error:
                messagebox.showerror("启动失败", str(error), parent=self.dialog)
                return
            self._completion_handled = True
            self._close_after_success()
            if self.on_relocated:
                self.on_relocated()
            return
        self._completion_handled = True
        self._close_after_success()
        if self.on_completed:
            self.on_completed()

    def next_page(self):
        if self._page == 0:
            self._show_page(1)
        else:
            self.start_install()

    def previous_page(self):
        self._show_page(0)

    def toggle_details(self):
        if self.details.winfo_manager():
            self.details.pack_forget()
            self.details_button.configure(text="环境详情  ⌄")
        else:
            self.details.pack(fill="x")
            self.details_button.configure(text="环境详情  ⌃")

    def refresh_details(self):
        profile = self.profile_var.get()
        self.detail_var.set("无需安装本地语音组件；需要语音时可另行配置 {}。".format(VOICE_ENGINE_LABEL) if profile == "desktop" else
            "缺少环境时自动下载官方 GPT-SoVITS v2Pro 整合包（约 8.2–8.9 GB），需要至少 32 GiB 可用空间。"
            "安装到上方目录，支持断点续传；已有完整环境直接复用。角色音色另行导入。")
        for key, label in getattr(self, "component_status_labels", {}).items():
            item = self.report.get("components", {}).get(key, {})
            unused = profile == "desktop" and key != "python"
            text = "无需配置" if unused else (
                item.get("summary", "CPU") if key == "compute" else
                "已就绪" if item.get("available") else "待配置")
            label.configure(text=text, fg="#687484" if unused else
                            "#27805a" if item.get("available") else "#b75c2a")

    def start_install(self):
        if self.worker is not None and self.worker.is_alive():
            return
        profile = self.profile_var.get()
        selected = self.location_var.get().strip()
        if not selected or not Path(selected).is_absolute():
            self.status_var.set("请选择有效的绝对安装路径。")
            return
        self.install_directory = Path(selected).expanduser().resolve()
        self.needs_character = self._initial_needs_character or self.install_directory != ROOT.resolve()
        self.cancel_event.clear()
        self.location_entry.configure(state="disabled")
        self.location_button.configure(state="disabled")
        for choice in self.profile_choices.values():
            choice.configure(state="disabled")
        self.install_button.configure(state="disabled", text="配置中…")
        self.back_button.configure(state="disabled")
        self.close_button.configure(text="取消配置")
        self.status_var.set("正在准备…")
        self.progress.grid()
        self.progress_label.grid()
        self._set_progress(0)

        def worker():
            try:
                notify = lambda text: self._events.put(("progress", text))
                if self.install_directory == ROOT.resolve():
                    report = install_profile(profile, progress=notify, cancel_event=self.cancel_event)
                else:
                    report = install_in_directory(self.install_directory, profile, notify, self.cancel_event)
                if isinstance(report, dict) and "components" in report:
                    self._events.put(("report", report))
                self._events.put(("done", profile, True, ""))
            except Exception as error:
                self._events.put(("done", profile, False, str(error)))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()
        self._poll_after = self.root.after(30, self._poll_events)

    def _poll_events(self):
        if self._poll_after is not None:
            self.root.after_cancel(self._poll_after)
            self._poll_after = None
        if self.dialog is None or not self.dialog.winfo_exists():
            return
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                self._poll_after = self.root.after(30, self._poll_events)
                return
            if event[0] == "progress":
                value = event[1]
                self.status_var.set(value.get("text", "") if isinstance(value, dict) else value)
                if isinstance(value, dict) and value.get("percent") is not None:
                    self._set_progress(value["percent"])
            elif event[0] == "report":
                self.report = event[1]
                self.refresh_details()
            else:
                self.finish_install(*event[1:])
                return

    def finish_install(self, profile, success, error):
        if self.dialog is None or not self.dialog.winfo_exists():
            return
        if self.cancel_event.is_set():
            self._dismiss()
            return
        if success:
            self._set_progress(100)
            message = ("已完成首次运行配置。\n\n当前还没有角色。\n点击“完成”进入角色设置，添加你的第一个角色。"
                       if self.needs_character else
                       "已完成首次运行配置。\n\n点击“完成”关闭向导，开始使用桌宠。")
            self._show_completion("配置已完成" if self.needs_character else "桌宠已准备就绪", message)
            if self.on_finished:
                self.on_finished(profile, True)
        else:
            self.location_entry.configure(state="normal")
            self.location_button.configure(state="normal")
            for choice in self.profile_choices.values():
                choice.configure(state="normal")
            self.status_var.set("配置未完成：{}".format(error))
            self.install_button.configure(state="normal", text="重试")
            self.back_button.configure(state="normal")
            self.close_button.configure(text="关闭")
            if self.on_finished:
                self.on_finished(profile, False)

    def choose_location(self):
        selected = filedialog.askdirectory(parent=self.dialog, title="选择安装位置",
                                          initialdir=self.location_var.get(), mustexist=False)
        if selected:
            self.location_var.set(selected)

    def _set_progress(self, value, label=None):
        value = max(0, min(100, value))
        self.progress.configure(value=value)
        self.progress_label.configure(text=label if label is not None else "{}%".format(round(value)))

    def _close_after_success(self):
        for attribute in ("_poll_after", "_close_after"):
            after_id = getattr(self, attribute)
            if after_id is not None:
                self.root.after_cancel(after_id)
                setattr(self, attribute, None)
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.destroy()
        self.dialog = None

    def _dismiss(self):
        if not self._dismissed_notified and self.on_dismissed:
            self._dismissed_notified = True
            self.on_dismissed()
        self._close_after_success()

    def cancel(self):
        self.cancel_event.set()
        if self.worker is not None and self.worker.is_alive():
            self.status_var.set("正在取消…")
            return
        self._dismiss()
