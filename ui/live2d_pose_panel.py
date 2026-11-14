"""Separate model-update and manual-pose dialogs using an isolated renderer."""

import copy
import math
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from ui.dropdown_style import install_dropdown_styles


class PoseReviewPanel:
    def __init__(self, parent, renderer, candidates, *, colors, button,
                 title="模型更新内容", confirm_text="确认更新", baseline=None, scale=None,
                 manual=False, resources=None, configure_popup=None, scrollbar=None,
                 validate_manual=None, renderer_lock=None, close_renderer=False, shared_preview=False):
        self.renderer = renderer
        self.result = None
        self.rows = []
        self.jobs = []
        self.closed = False
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pose-preview")
        self._results = queue.SimpleQueue()
        self._futures = set()
        self._renderer_lock = renderer_lock or threading.RLock()
        self._close_renderer = close_renderer
        self._shared_preview = shared_preview
        self._preview_generation = 0
        self._loading = False
        self.pending_preview = None
        self.preview_parameters = None
        self.baseline = dict((baseline or {}).get("parameters", {}))
        self.colors = colors
        self.button = button
        self.manual = manual
        self.configure_popup = configure_popup
        self.scrollbar = scrollbar or ttk.Scrollbar
        self.validate_manual = validate_manual
        self.resources = resources or {"motions": [], "expressions": []}
        self.dialog = tk.Toplevel(parent, bg=colors["bg_soft"])
        self.dialog.withdraw()
        self.dialog.title(title)
        self.dialog.transient(parent.winfo_toplevel())
        if manual:
            self.dialog.geometry("560x700")
            self.dialog.minsize(500, 650)
        else:
            self.dialog.geometry("500x620")
            self.dialog.minsize(420, 580)
        self.dialog.protocol("WM_DELETE_WINDOW", self.cancel)
        self.dialog.bind("<Escape>", lambda _: self.cancel())
        self.dialog.grid_columnconfigure(0, weight=1)
        self.dialog.grid_rowconfigure(1, weight=1)
        self.status = tk.StringVar(self.dialog, value="正在加载预览")
        self._label(self.dialog, title, font=("Microsoft YaHei UI", 14, "bold")).grid(
            row=0, column=0, sticky="w", padx=16, pady=(12, 8))
        if manual:
            self._build_manual(scale)
        else:
            self._build_update(confirm_text)
        footer = tk.Frame(self.dialog, bg=colors["bg_soft"])
        footer.grid(row=2, column=0, sticky="ew", padx=16, pady=12)
        footer.grid_columnconfigure(0, weight=1)
        self.status_label = self._label(footer, "", textvariable=self.status, wraplength=440)
        self.status_label.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.retry_button = button(footer, "重试预览", self.retry)
        self.retry_button.grid(row=1, column=0, sticky="w")
        self.retry_button.grid_remove()
        self.cancel_button = button(footer, "取消", self.cancel)
        self.cancel_button.grid(row=1, column=1, padx=6)
        self.confirm_button = button(footer, confirm_text, self.accept, accent=True)
        self.confirm_button.grid(row=1, column=2)
        self.confirm_button.configure(state="disabled")
        self.specs = {}
        self.parameter_labels = {}
        self.ready = False
        self.candidates = copy.deepcopy(candidates) if not manual else []
        if manual:
            self.add_button = self.confirm_button
            self.value.trace_add("write", self._schedule_preview)
        self.dialog.bind("<Destroy>", self._destroyed, add="+")
        self.dialog.bind("<Configure>", self._resize_help, add="+")
        self._later(20, self._load)
        self._later(20, self._poll_results)

    def _build_update(self, confirm_text):
        colors = self.colors
        self.tabs = ttk.Notebook(self.dialog, style="Brand.TNotebook")
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=16)
        candidate_tab = tk.Frame(self.tabs, bg=colors["bg_soft"])
        self.tabs.add(candidate_tab, text="姿势")
        candidate_tab.grid_columnconfigure(0, weight=1)
        candidate_tab.grid_rowconfigure(1, weight=1)
        self.candidate_help = self._label(
            candidate_tab, "从模型资源中识别出的新增姿势。勾选要加入角色的姿势，再点击“{}”。".format(confirm_text),
            justify="left", wraplength=480, font=("Microsoft YaHei UI", 9))
        self.candidate_help.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 8))
        self.canvas = tk.Canvas(candidate_tab, bg=colors["bg_soft"], highlightthickness=0)
        scrollbar = self.scrollbar(candidate_tab, orient="vertical", command=self.canvas.yview,
                                  style="Brand.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.cards = tk.Frame(self.canvas, bg=colors["bg_soft"])
        self.window_id = self.canvas.create_window(0, 0, window=self.cards, anchor="nw")
        self.cards.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._layout)
        self.canvas.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units"))
        self.empty_label = self._label(self.cards, "未发现新增姿势。",
                                       wraplength=280, justify="left")
        self.empty_label.grid(row=0, column=0, padx=12, pady=16)
        self.resource_lists = {}
        for kind, label in (("motions", "动作"), ("expressions", "表情")):
            items = self.resources.get(kind, [])
            tab = tk.Frame(self.tabs, bg=colors["bg_soft"])
            self.tabs.add(tab, text="{} · {}".format(label, len(items)))
            self._label(tab, "新增{}随模型一并导入。{}".format(
                label, "自动播放仍遵循动作安全规则。" if kind == "motions" else ""),
                justify="left", wraplength=340, font=("Microsoft YaHei UI", 9)).pack(
                    fill="x", padx=10, pady=10)
            listing = tk.Listbox(tab, bg=colors["bg_soft"], fg=colors["white"],
                                 selectbackground=colors["bg_soft"], selectforeground=colors["white"],
                                 font=("Microsoft YaHei UI", 10), relief="flat", highlightthickness=0)
            scrollbar = self.scrollbar(tab, orient="vertical", command=listing.yview,
                                      style="Brand.Vertical.TScrollbar")
            scrollbar.pack(side="right", fill="y")
            listing.configure(yscrollcommand=scrollbar.set)
            listing.pack(fill="both", expand=True, padx=10, pady=(0, 10))
            for item in items:
                listing.insert("end", item["name"])
            if not items:
                listing.insert("end", "未发现新增{}".format(label))
            self.resource_lists[kind] = listing

    def _build_manual(self, scale):
        colors = self.colors
        style = ttk.Style(self.dialog)
        install_dropdown_styles(self.dialog, colors)
        # Native Windows arrows ignore brand colors; replace only these elements.
        for suffix, arrows in (("TSpinbox", ("Spinbox.uparrow", "Spinbox.downarrow")),):
            replacements = {}
            for arrow in arrows:
                element = "Pose." + arrow
                if element not in style.element_names():
                    style.element_create(element, "from", "clam", arrow)
                replacements[arrow] = element

            def replace_arrows(layout):
                return [(replacements.get(name, name), {
                    key: replace_arrows(value) if key == "children" else value
                    for key, value in options.items()
                }) for name, options in layout]

            local_style = "Pose.Brand." + suffix
            style.layout(local_style, replace_arrows(style.layout("Brand." + suffix)))
            style.configure(local_style, background=colors["panel"],
                            arrowcolor=colors["white"], bordercolor=colors["panel"])
            style.map(local_style, background=[("active", colors["cyan"])])
        manual = tk.Frame(self.dialog, bg=colors["bg_soft"])
        manual.grid(row=1, column=0, sticky="nsew", padx=16)
        manual.grid_columnconfigure(0, weight=1)
        self.manual_help = self._label(
            manual, "选择控制新姿势的参数（如 ParamPose），调整数值并命名。\n"
            "仅登记模型已有姿势，不会生成新动作。",
            justify="left", wraplength=480, font=("Microsoft YaHei UI", 9))
        self.manual_help.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        photo_frame = tk.Frame(manual, width=330, height=235, bg=colors["bg_soft"])
        photo_frame.grid(row=1, column=0, pady=4)
        photo_frame.grid_propagate(False)
        photo_frame.grid_rowconfigure(0, weight=1)
        photo_frame.grid_columnconfigure(0, weight=1)
        self.manual_photo = self._label(photo_frame, "预览待加载")
        self.manual_photo.grid(sticky="nsew")
        fields = tk.Frame(manual, bg=colors["bg_soft"])
        fields.grid(row=2, column=0, sticky="ew", padx=10)
        fields.grid_columnconfigure(1, weight=1)
        self._label(fields, "新姿势参数").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.parameter = tk.StringVar(self.dialog)
        self.selector = ttk.Combobox(fields, state="readonly", textvariable=self.parameter,
                                     width=22, style="Brand.TCombobox")
        self.selector.grid(row=0, column=1, sticky="ew", pady=4)
        self.selector.bind("<<ComboboxSelected>>", self._select_parameter)
        if self.configure_popup is not None:
            self.configure_popup(self.selector)
        self._label(fields, "数值").grid(row=1, column=0, sticky="w")
        self.value = tk.DoubleVar(self.dialog, value=0)
        self.spin = ttk.Spinbox(fields, textvariable=self.value, width=12, format="%.3f",
                                style="Pose.Brand.TSpinbox")
        self.spin.grid(row=1, column=1, sticky="ew", pady=4)
        self.brand_scale = scale is not None
        self.scale = (scale(fields, variable=self.value, command=self._slide,
                            bg=colors["bg_soft"], resolution=0)
                      if self.brand_scale else
                      ttk.Scale(fields, variable=self.value, command=self._slide))
        self.scale.grid(row=2, column=1, sticky="ew", pady=4)
        self._label(fields, "名称").grid(row=3, column=0, sticky="w")
        self.name = tk.StringVar(self.dialog, value="新姿势")
        self.name_entry = ttk.Entry(fields, textvariable=self.name, style="Brand.TEntry")
        self.name_entry.grid(row=3, column=1, sticky="ew", pady=4)

    def _label(self, parent, text, **kwargs):
        return tk.Label(parent, text=text, bg=self.colors["bg_soft"],
                        fg=self.colors["white"], anchor="w", **kwargs)

    def _later(self, delay, callback):
        def run():
            self.jobs.remove(job)
            if not self.closed:
                callback()
        job = self.dialog.after(delay, run)
        self.jobs.append(job)
        return job

    def _resize_help(self, event):
        if event.widget is self.dialog:
            for label in (getattr(self, "candidate_help", None),
                          getattr(self, "manual_help", None), self.status_label):
                if label is None:
                    continue
                label.configure(wraplength=max(160, event.width - 64))

    def _submit(self, work, complete, failed=None):
        def run():
            with self._renderer_lock:
                return work()

        future = self._pool.submit(run)
        self._futures.add(future)
        future.add_done_callback(lambda result: self._results.put((result, complete, failed)))
        return future

    def _poll_results(self):
        while not self.closed:
            try:
                future, complete, failed = self._results.get_nowait()
            except queue.Empty:
                break
            self._futures.discard(future)
            if future.cancelled():
                continue
            try:
                result = future.result()
            except Exception as error:
                if failed is not None:
                    failed(error)
                else:
                    self.status.set("模型预览失败：{}".format(error))
                    self.retry_button.grid()
            else:
                complete(result)
        if not self.closed:
            self._later(20, self._poll_results)

    def _load(self):
        if self._loading or self.closed:
            return
        self._loading = True
        if self.manual:
            self.selector.configure(state="disabled")
            self.spin.configure(state="disabled")
            self.add_button.configure(state="disabled")

        def work():
            specs = {item["id"]: item for item in self.renderer.parameter_specs()}
            labels = {(item["name"] if item["name"] == key else
                       "{} ({})".format(item["name"], key)): key for key, item in specs.items()}
            preferred = next((label for label, key in labels.items() if key.casefold() == "parampose"),
                             next(iter(labels), ""))
            parameters = dict(self.baseline)
            if self.manual:
                if not preferred:
                    raise ValueError("模型没有可调整的参数")
                key = labels[preferred]
                parameters[key] = self.baseline.get(key, specs[key]["default"])
            image = self._render_image({"parameters": parameters}, (318, 223), specs) if self.manual else None
            return specs, labels, preferred, parameters, image

        def complete(result):
            self.specs, labels, preferred, parameters, image = result
            if self.manual:
                self.parameter_labels = labels
                self.selector.configure(state="readonly")
                self.spin.configure(state="normal")
                self.selector.configure(values=list(labels))
                self.parameter.set(preferred)
                self._select_parameter()
                self._set_image(self.manual_photo, image)
                self.preview_parameters = parameters
                self.add_button.configure(state="normal")
            for candidate in self.candidates:
                self._add_row(candidate)
            self.candidates = []
            self._loading = False
            self.ready = True
            if not self.manual:
                self.confirm_button.configure(state="normal")
            self._update_status()
        def failed(error):
            self._loading = False
            self.status.set("模型预览失败：{}".format(error))
            self.retry_button.grid()

        self._submit(work, complete, failed)

    def _image(self, label, appearance, size):
        self._set_image(label, self._render_image(appearance, size))

    def _render_image(self, appearance, size, specs=None):
        specs = self.specs if specs is None else specs
        parameters = appearance.get("parameters", {})
        for key, value in parameters.items():
            spec = specs.get(key)
            if (not spec or type(value) not in (int, float)
                    or not math.isfinite(value) or not spec["minimum"] <= value <= spec["maximum"]
                    or (spec["step"] == 1 and abs(value - round(value)) > 1e-6)):
                raise ValueError("参数不存在、超出范围或不符合步长：{}".format(key))
        image = (self.renderer.shared_thumbnail(appearance=appearance) if self._shared_preview else
                 self.renderer.preview_thumbnail(appearance=appearance))
        if image is None:
            raise ValueError("模型预览不可用")
        if image.getchannel("A").getbbox() is None:
            raise ValueError("模型画面为空")
        # Live2D 的完整画布通常比角色实际边界大很多。裁掉透明留白后，
        # 预览区能显示清晰的角色立绘，同时保留少量边距避免贴边。
        alpha_bbox = image.getchannel("A").getbbox()
        if alpha_bbox is not None:
            left, top, right, bottom = alpha_bbox
            pad_x = max(4, round((right - left) * 0.08))
            pad_y = max(4, round((bottom - top) * 0.08))
            image = image.crop((
                max(0, left - pad_x), max(0, top - pad_y),
                min(image.width, right + pad_x), min(image.height, bottom + pad_y),
            ))
        image.thumbnail(size, Image.Resampling.LANCZOS)
        return image

    @staticmethod
    def _set_image(label, image):
        photo = ImageTk.PhotoImage(image, master=label)
        label.configure(image=photo, text="")
        label.image = photo

    def _add_row(self, appearance):
        self.empty_label.grid_remove()
        frame = tk.Frame(self.cards, bg=self.colors["bg_soft"], width=152, height=220)
        frame.grid_propagate(False)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        label = self._label(frame, "等待预览")
        label.configure(anchor="center")
        label.grid(row=0, column=0, sticky="nsew")
        chosen = tk.BooleanVar(self.dialog, value=False)
        check = tk.Checkbutton(frame, text=appearance["name"], variable=chosen,
                               command=self._update_status, wraplength=140, anchor="w",
                               bg=self.colors["bg_soft"], fg=self.colors["white"],
                               selectcolor=self.colors["bg_soft"], state="disabled")
        check.grid(row=1, column=0, sticky="ew")
        row = {"frame": frame, "label": label, "check": check, "chosen": chosen,
               "appearance": copy.deepcopy(appearance), "ready": False}
        self.rows.append(row)
        self._layout()
        self._later(30, lambda: self._render_row(row))

    def _render_row(self, row):
        def complete(image):
            self._set_image(row["label"], image)
            row["ready"] = True
            row["chosen"].set(True)
            row["check"].configure(state="normal")
            self._update_status()

        def failed(error):
            row["chosen"].set(False)
            row["label"].configure(text="预览失败", image="")
            row["error"] = str(error)
            self._update_status()

        self._submit(lambda: self._render_image(row["appearance"], (140, 174)), complete, failed)

    def _layout(self, event=None):
        width = max(1, self.canvas.winfo_width())
        self.canvas.itemconfigure(self.window_id, width=width)
        columns = max(1, width // 164)
        for index, row in enumerate(self.rows):
            row["frame"].grid(row=index // columns, column=index % columns, padx=5, pady=5)
        self.status_label.configure(wraplength=max(200, self.dialog.winfo_width() - 40))

    def _select_parameter(self, _event=None):
        key = getattr(self, "parameter_labels", {}).get(self.parameter.get())
        if key is None:
            return
        spec = self.specs[key]
        self.spin.configure(from_=spec["minimum"], to=spec["maximum"], increment=spec["step"])
        self.scale.configure(from_=spec["minimum"], to=spec["maximum"])
        if self.brand_scale:
            self.scale.configure(resolution=spec["step"])
        self.value.set(self.baseline.get(key, spec["default"]))

    def _slide(self, value):
        key = self.parameter_labels.get(self.parameter.get())
        if key and self.specs[key]["step"] == 1:
            self.value.set(round(float(value)))

    def _parameters(self):
        key = self.parameter_labels.get(self.parameter.get())
        if not key:
            raise ValueError("没有可预览的参数")
        return {**self.baseline, key: self.value.get()}

    def _schedule_preview(self, *_):
        if self._loading or self.closed:
            return
        self._preview_generation += 1
        self.add_button.configure(state="disabled")
        if self.pending_preview is not None:
            self.dialog.after_cancel(self.pending_preview)
        self.pending_preview = self._later(120, self._preview_manual)

    def _preview_manual(self):
        self.pending_preview = None
        generation = self._preview_generation
        try:
            parameters = self._parameters()
        except (ValueError, tk.TclError):
            return
        pending = getattr(self, "_preview_future", None)
        if pending is not None:
            pending.cancel()

        def complete(image):
            if generation != self._preview_generation:
                return
            self._set_image(self.manual_photo, image)
            self.preview_parameters = parameters
            self.add_button.configure(state="normal")
            self._update_status()
        def failed(error):
            if generation != self._preview_generation:
                return
            self.preview_parameters = None
            self.manual_photo.configure(text="预览不可用", image="")
            self.status.set("预览失败：{}".format(error))
            self.retry_button.grid()

        self._preview_future = self._submit(
            lambda: self._render_image({"parameters": parameters}, (318, 223)), complete, failed)

    def add_manual(self):
        try:
            parameters = self._parameters()
        except (ValueError, tk.TclError):
            return
        if parameters != self.preview_parameters:
            return
        name = self.name.get().strip()
        if not name:
            self.status.set("请填写姿势名称")
            return
        if any(row["appearance"]["parameters"] == parameters for row in self.rows):
            self.status.set("该姿势已在候选列表中")
            return
        proposal = {"name": name, "parameters": copy.deepcopy(parameters)}
        if self.validate_manual is not None:
            try:
                self.validate_manual(proposal)
            except (ValueError, OSError, RuntimeError) as error:
                self.status.set(str(error))
                return
        self.result = [proposal]
        self.dialog.destroy()

    def _update_status(self):
        if self.manual:
            if self.preview_parameters is not None:
                self.retry_button.grid_remove()
            self.status.set("姿势预览已就绪" if self.preview_parameters is not None else "正在加载预览")
            return
        selected = sum(row["chosen"].get() and row["ready"] for row in self.rows)
        failed = sum("error" in row and not row["ready"] for row in self.rows)
        self.retry_button.grid() if failed else self.retry_button.grid_remove()
        self.status.set("已选 {} 个姿势；新增 {} 个动作、{} 个表情{}".format(
            selected, len(self.resources.get("motions", [])), len(self.resources.get("expressions", [])),
            "；{} 个预览失败".format(failed) if failed else ""))

    def retry(self):
        if not self.ready:
            self._load()
            return
        for row in self.rows:
            if not row["ready"]:
                self._later(30, lambda row=row: self._render_row(row))
        if self.manual:
            self._schedule_preview()

    def accept(self):
        if self.ready:
            if self.manual:
                self.add_manual()
                return
            self.result = [copy.deepcopy(row["appearance"]) for row in self.rows
                           if row["ready"] and row["chosen"].get()]
            self.dialog.destroy()

    def cancel(self):
        self.result = None
        self.dialog.destroy()

    def _destroyed(self, event):
        if event.widget is not self.dialog or self.closed:
            return
        self.closed = True
        for future in self._futures:
            future.cancel()
        if self._close_renderer:
            def close():
                with self._renderer_lock:
                    self.renderer.close()
            self._pool.submit(close)
        self._pool.shutdown(wait=False)
        for job in self.jobs:
            try:
                self.dialog.after_cancel(job)
            except tk.TclError:
                pass

    def show(self):
        parent = self.dialog.master
        previous_focus = parent.focus_get()
        previous_grab = parent.grab_current()
        previous_grab_status = previous_grab.grab_status() if previous_grab is not None else None
        try:
            parent.update_idletasks()
            if self.closed:
                return self.result
            width, height = 500, 620
            x = max(0, parent.winfo_rootx() + (parent.winfo_width() - width) // 2)
            y = max(0, parent.winfo_rooty() + (parent.winfo_height() - height) // 2)
            self.dialog.geometry("{}x{}+{}+{}".format(width, height, x, y))
            self.dialog.deiconify()
            self.dialog.lift()
            try:
                self.dialog.wait_visibility()
            except tk.TclError:
                if self.closed:
                    return self.result
                raise
            if self.closed:
                return self.result
            self.dialog.grab_set()
            self.dialog.focus_set()
            self.dialog.wait_window()
        finally:
            if previous_grab is not None:
                try:
                    if previous_grab.winfo_exists():
                        if previous_grab_status == "global":
                            previous_grab.grab_set_global()
                        else:
                            previous_grab.grab_set()
                except tk.TclError:
                    pass
            if previous_focus is not None:
                try:
                    if previous_focus.winfo_exists():
                        previous_focus.focus_set()
                except tk.TclError:
                    pass
        return self.result
