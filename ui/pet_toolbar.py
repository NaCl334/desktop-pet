"""A small, non-activating toolbar anchored to the pet's viewport."""
import ctypes
import os
import time
import tkinter as tk
from tkinter import ttk

from core.config import list_characters


TOOLS = (("输入", "输入"), ("角色", "角色"),
         ("历史对话", "历史对话"), ("设置", "调节"), ("退出桌宠", "退出桌宠"))
BG = "#FFFFFF"
TEXT = "#345566"
MUTED = "#77909E"
ACCENT = "#1488B4"
LINE = "#D6E6EF"
HOVER = "#E5F4FC"


def fit_position(x, y, width, height, bounds):
    left, top, right, bottom = bounds
    return (max(left, min(x, right - width)),
            max(top, min(y, bottom - height)))


def rounded_panel(canvas, width, height, fill=BG, outline=LINE):
    radius = 7
    points = (1 + radius, 1, width - radius, 1, width - 1, 1,
              width - 1, 1 + radius, width - 1, height - radius,
              width - 1, height - 1, width - radius, height - 1,
              1 + radius, height - 1, 1, height - 1, 1, height - radius,
              1, 1 + radius, 1, 1)
    return canvas.create_polygon(points, smooth=True, splinesteps=12,
                                 fill=fill, outline=outline, width=1)


class PetToolbar:
    def __init__(self, app):
        self.app = app
        self.root = app.root
        self.opened = False
        self.closed = False
        self.panel = None
        self.tooltip = None
        self.timer = None
        self.animation = None
        self.animation_started = 0.0
        self.expansion = 0.0
        self.alpha = 0.0
        self.last_near = time.monotonic()
        self.hide_until = 0.0
        self.mouse_down = False
        self.signature = None
        self.combos = {}
        self.dropdown = None
        self.dropdown_combo = None
        self.dropdown_list = None
        self.dropdown_scroll = None
        self.buttons = {}
        self.tools_height = len(TOOLS) * 48 + 6
        self.expanded_height = 56 + self.tools_height + 4
        self.window = self._window()
        self.window.attributes("-alpha", 0)
        self.gear = self._button(self.window, "设置", self.toggle, "展开菜单")
        self.gear.place(x=5, y=4, width=44, height=44)
        self.tools = tk.Canvas(self.window, bg=self.window.cget("bg"), bd=0,
                               highlightthickness=0)
        rounded_panel(self.tools, 48, self.tools_height)
        for name, icon in TOOLS:
            button = self._button(self.tools, icon, lambda key=name: self.activate(key), name)
            button.pack(padx=3, pady=3)
            self.buttons[name] = button
        self.window.bind("<Escape>", self._escape)
        self.root_bind = self.root.bind("<Destroy>", self._on_destroy, add="+")
        self.click_bind = self.root.bind_all("<ButtonPress-1>", self._on_pointer_press, add="+")
        self.app.portrait_canvas._portrait_input_blocked = self.contains_point
        self.timer = self.root.after(120, self._tick)

    def _window(self):
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.overrideredirect(True)
        # A transient created before its owner maps can remain an unmapped 1x1
        # native window on Windows, even while Tk reports its state as normal.
        if self.root.winfo_viewable():
            window.transient(self.root)
        window.configure(bg=getattr(self.app, "transparent_color", "#010203"))
        try:
            window.attributes("-transparentcolor", window.cget("bg"))
        except tk.TclError:
            pass
        self.app._configure_tool_window_style(window)
        return window

    def _button(self, parent, icon, command, hint):
        canvas = tk.Canvas(parent, width=42, height=42, bg=BG,
                           highlightthickness=0, bd=0, cursor="hand2", takefocus=True)
        canvas._selected = False

        def paint(hover=False):
            selected = hover or canvas._selected
            self.app._draw_tray_menu_icon(canvas, icon, ACCENT if selected else TEXT)
            canvas.scale("all", 0, 0, 1.15, 1.15)
            canvas.move("all", 11, 11)
            canvas.configure(bg=HOVER if selected else BG)
            if parent is self.window:
                canvas.configure(bg=self.window.cget("bg"))
                background = rounded_panel(canvas, 42, 42, HOVER if selected else BG)
                canvas.tag_lower(background)

        def enter(_event=None):
            paint(True)
            self._show_hint(canvas, hint)

        def leave(_event=None):
            paint()
            self._hide_hint()

        canvas._paint = paint
        canvas.bind("<Enter>", enter)
        canvas.bind("<Leave>", leave)
        canvas.bind("<FocusIn>", enter)
        canvas.bind("<FocusOut>", leave)
        canvas.bind("<Button-1>", lambda _event: command())
        canvas.bind("<Return>", lambda _event: command())
        canvas.bind("<space>", lambda _event: command())
        paint()
        return canvas

    def _show_hint(self, widget, text):
        self._hide_hint()
        self.tooltip = self._window()
        tk.Label(self.tooltip, text=text, bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 9), padx=9, pady=5,
                 highlightbackground=LINE, highlightthickness=1).pack()
        self.tooltip.update_idletasks()
        width, height = self.tooltip.winfo_reqwidth(), self.tooltip.winfo_reqheight()
        x = widget.winfo_rootx() - width - 9
        if x < self.bounds[0]:
            x = widget.winfo_rootx() + widget.winfo_width() + 9
        x, y = fit_position(x, widget.winfo_rooty() + 6, width, height, self.bounds)
        self._position(self.tooltip, width, height, x, y)
        self.tooltip.attributes("-topmost", self.window.attributes("-topmost"))
        self.tooltip.deiconify()

    def _hide_hint(self):
        if self.tooltip is not None:
            self.tooltip.destroy()
            self.tooltip = None

    def _blocked(self):
        return (self.closed or time.monotonic() < self.hide_until
                or not self.root.winfo_viewable()
                or getattr(self.app, "_quitting", False)
                or not getattr(self.app, "_portrait_available", True)
                or getattr(self.app, "portrait_editing", False)
                or bool(getattr(self.app, "drag_start", None))
                or bool(getattr(self.app, "_pet_layout_depth", 0))
                or getattr(self.app, "screenshot_in_progress", False)
                or getattr(self.app, "_external_screenshot_window_states", None) is not None
                or getattr(self.app.store, "settings", {}).get("mouse_passthrough", False))

    def _anchor(self):
        canvas = self.app.portrait_canvas
        x, y = canvas.winfo_rootx(), canvas.winfo_rooty()
        width, height = canvas.winfo_width(), canvas.winfo_height()
        self.bounds = self.app._context_screen_bounds({
            "portrait_screen_x": x, "portrait_screen_y": y,
            "portrait_width": width, "portrait_height": height})
        left, right = x, x + width
        renderer = getattr(self.app, "renderer", None)
        box = getattr(renderer, "image_bbox", None)
        if isinstance(box, (tuple, list)) and len(box) == 4:
            offset = getattr(renderer, "_stage_pixel_offset", (0, 0))
            left = min(left, x + box[0] + offset[0])
            right = max(right, x + box[2] + offset[0])
        gear_x = right + 10
        if gear_x + 54 > self.bounds[2]:
            gear_x = left - 64
        self.x, self.y = fit_position(gear_x, y + 8, 54, self.expanded_height, self.bounds)
        self.pet_rect = (min(left, self.x) - 24, y - 24,
                         max(right, self.x + 54) + 24, y + height + 24)

    def _layout(self):
        height = 52 + round((self.expanded_height - 52) * self.expansion)
        self._position(self.window, 54, height, self.x, self.y)
        if self.expansion:
            self.tools.place(x=3, y=56, width=48, height=self.tools_height)
        else:
            self.tools.place_forget()
        if self.panel is not None:
            width, height = 244, self.panel.winfo_reqheight()
            canvas = self.app.portrait_canvas
            x = max(self.x + 64, canvas.winfo_rootx() + canvas.winfo_width() + 12)
            if x + width > self.bounds[2]:
                x = canvas.winfo_rootx() - width - 12
            x, y = fit_position(x, self.y + 56, width, height, self.bounds)
            self._position(self.panel, width, height, x, y)
            self._layout_dropdown()

    def _layout_dropdown(self):
        if self.dropdown is None:
            return
        combo = self.dropdown_combo
        x = combo.winfo_rootx() - self.panel.winfo_rootx()
        bottom = combo.winfo_rooty() - self.panel.winfo_rooty() + combo.winfo_height() + 2
        panel_height = self.panel.winfo_height()
        below = max(1, panel_height - bottom - 4)
        requested = self.dropdown.winfo_reqheight()
        height = min(requested, below)
        if requested > below or self.dropdown_list.size() > 6:
            self.dropdown_scroll.pack(side="right", fill="y", before=self.dropdown_list)
        else:
            self.dropdown_scroll.pack_forget()
        self.dropdown.place(x=x, y=bottom, width=combo.winfo_width(), height=height)
        self.dropdown.lift()

    def _position(self, window, width, height, x, y):
        geometry = (width, height, x, y)
        # Tk can recreate the native window during the first deiconify/topmost
        # transition. A cached request then no longer describes the real window.
        actual_changed = window.winfo_viewable() and (
            window.winfo_width() != width or window.winfo_height() != height
            or window.winfo_rootx() != x or window.winfo_rooty() != y)
        if getattr(window, "_toolbar_geometry", None) != geometry or actual_changed:
            self.app._position_context_bar(window, {
                "switcher_width": width, "switcher_height": height,
                "panel_x": x, "panel_y": y})
            window._toolbar_geometry = geometry

    def show(self):
        if self._blocked():
            return "break"
        self._anchor()
        self._hide_hint()
        self.last_near = time.monotonic()
        self.opened = True
        self.app._context_menu_open = True
        self.gear._selected = True
        self.gear._paint()
        self.alpha = 1.0
        self.window.attributes("-alpha", 1.0)
        self.window.attributes("-topmost", bool(self.root.attributes("-topmost")))
        self.window.deiconify()
        self.window.lift()
        self.mouse_down = self._mouse_pressed()
        self._animate(True)
        return "break"

    def toggle(self):
        self._hide_hint()
        if self.opened:
            self.close()
        else:
            self.show()

    def _escape(self, _event=None):
        self.close()
        return "break"

    def raise_windows(self):
        if self._dropdown_open():
            return
        for window in (self.window, self.panel, self.tooltip):
            if window is not None and window.winfo_viewable():
                window.lift()

    def _animate(self, opening):
        if self.animation is not None:
            self.root.after_cancel(self.animation)
            self.animation = None
        self.animation_started = time.monotonic()
        start, target = self.expansion, float(opening)

        def frame():
            self.animation = None
            if self.closed:
                return
            progress = min(1, (time.monotonic() - self.animation_started) / .18)
            self.expansion = start + (target - start) * (1 - (1 - progress) ** 3)
            self._layout()
            if progress < 1:
                self.animation = self.root.after(16, frame)

        frame()

    def close(self, immediate=False):
        if self.closed:
            return
        was_open = self.opened
        self.opened = False
        if was_open:
            self.app._context_menu_open = False
        self._hide_hint()
        self._close_panel()
        self.gear._selected = False
        self.gear._paint()
        for button in self.buttons.values():
            button._selected = False
            button._paint()
        if immediate:
            if self.animation is not None:
                self.root.after_cancel(self.animation)
                self.animation = None
            self.expansion = 0
            self.tools.place_forget()
            self.window.withdraw()
        elif self.expansion:
            self._animate(False)

    def hide_for_capture(self):
        self.hide_until = time.monotonic() + 1
        self.close(immediate=True)

    def activate(self, name):
        self._hide_hint()
        if name == "角色":
            if self.panel is None:
                self.show_role()
            else:
                self._close_panel()
                self.buttons[name]._selected = False
                self.buttons[name]._paint()
            return
        self.close()
        if name == "输入":
            self.app._show_inline_input()
        elif name == "历史对话":
            self.app._show_control_center("conversation")
        elif name == "设置":
            self.app.show_config()
        elif name == "退出桌宠":
            self.app.quit_app()

    def _close_panel(self):
        self._close_dropdown()
        if self.panel is not None:
            self.panel.destroy()
            self.panel = None
        self.combos = {}
        self.signature = None

    def _combo(self, parent, label, choices, current, callback):
        tk.Label(parent, text=label, bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", pady=(9, 5))
        ids = [item[0] for item in choices]
        names = [item[1] for item in choices]
        names = [name if names.count(name) == 1 else "{} ({})".format(name, i + 1)
                 for i, name in enumerate(names)]
        combo = ttk.Combobox(parent, values=names, state="readonly" if ids else "disabled",
                             style="Brand.TCombobox", width=18, font=("Microsoft YaHei UI", 9))
        combo.pack(fill="x")
        if ids:
            combo.current(ids.index(current) if current in ids else 0)
        self.combos[label] = combo
        combo.bind("<Button-1>", lambda _event: "break")
        for sequence in ("<ButtonRelease-1>", "<Down>", "<space>", "<Return>"):
            combo.bind(sequence, lambda _event, field=combo: self._open_dropdown(field))

        def selected(_event):
            index = combo.current()
            if index >= 0:
                callback(ids[index])
                if self.panel is not None:
                    self.show_role()

        combo.bind("<<ComboboxSelected>>", selected)
        return combo

    def _close_dropdown(self):
        if self.dropdown is not None:
            self.dropdown.destroy()
        self.dropdown = self.dropdown_combo = self.dropdown_list = None
        self.dropdown_scroll = None

    def _open_dropdown(self, combo):
        same = self.dropdown_combo is combo
        self._close_dropdown()
        if same or combo.instate(["disabled"]):
            self.panel.update_idletasks()
            self._layout()
            return "break"
        # Keep options in the same native window as the field. Independent
        # combobox popdowns can lose activation to the layered pet windows.
        self.dropdown_combo = combo
        frame = self.dropdown = tk.Frame(self.panel, bg=BG,
                                          highlightbackground=ACCENT, highlightthickness=2)
        values = combo.cget("values")
        listing = self.dropdown_list = tk.Listbox(
            frame, height=min(6, len(values)), exportselection=False,
            activestyle="none", bd=0, highlightthickness=0,
            bg=BG, fg=TEXT, selectbackground=HOVER, selectforeground=ACCENT,
            font=("Microsoft YaHei UI", 10))
        scroll = self.dropdown_scroll = self.app._brand_scrollbar(
            frame, orient="vertical", command=listing.yview, style="Brand.Vertical.TScrollbar")
        listing.configure(yscrollcommand=scroll.set)
        if len(values) > 6:
            scroll.pack(side="right", fill="y")
        listing.pack(side="left", fill="both", expand=True)
        for value in values:
            listing.insert("end", value)
        if combo.current() >= 0:
            listing.selection_set(combo.current())
            listing.activate(combo.current())
            listing.see(combo.current())

        def choose(event=None):
            selected = listing.curselection()
            if event is not None and event.type == tk.EventType.ButtonRelease:
                index = listing.nearest(event.y)
                bounds = listing.bbox(index)
                if not bounds or not (bounds[1] <= event.y < bounds[1] + bounds[3]):
                    return "break"
                selected = (index,)
            if selected:
                index = selected[0]
                self._close_dropdown()
                combo.current(index)
                combo.event_generate("<<ComboboxSelected>>", when="tail")
            return "break"

        def cancel(_event=None):
            self._close_dropdown()
            combo.focus_set()
            self.panel.update_idletasks()
            self._layout()
            return "break"

        listing.bind("<ButtonRelease-1>", choose)
        listing.bind("<Return>", choose)
        listing.bind("<space>", choose)
        listing.bind("<Escape>", cancel)
        self.panel.update_idletasks()
        self._layout()
        listing.focus_set()
        return "break"

    def show_role(self):
        if not self.opened:
            self.show()
        if not self.opened:
            return
        self._close_panel()
        self.panel = self._window()
        self.panel.configure(bg=BG, highlightbackground=LINE, highlightthickness=1)
        self.panel.bind("<Escape>", self._escape)
        body = tk.Frame(self.panel, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        tk.Label(body, text="角色", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 10, "bold"), anchor="w").pack(fill="x")
        try:
            characters = list_characters()
        except (OSError, ValueError):
            characters = []
        choices = [(item["id"], item.get("display_name") or item["id"]) for item in characters]
        self._combo(body, "当前角色", choices, self.app.character_id, self.app.switch_character)
        config = self.app.character_config
        live = self.app._is_live2d_character_config(config)
        outfits = (self.app._live2d_runtime_outfit_groups(config) if live
                   else self.app._portrait_outfits(config))
        self._combo(body, "服装", [(item["id"], item["name"]) for item in outfits],
                    self.app._active_runtime_portrait_outfit_id(config),
                    self.app._switch_runtime_portrait_outfit)
        if live:
            renderer = getattr(self.app, "renderer", None)
            if callable(getattr(renderer, "behavior_preferences", None)):
                values = self.app._live2d_control_values(renderer)
                for title, key in (("鼠标跟随", "follow"), ("暂停动画", "paused"),
                                   ("自动表现", "auto_behavior")):
                    self._switch(body, title, values[key],
                                 lambda value, key=key: self.app._set_live2d_control(key, value))
        else:
            outfit = self.app._active_portrait_outfit(config)
            actions = [self.app._portrait_action(outfit, "")] + list(outfit.get("actions", []))
            self._combo(body, "动作", [(item["id"], item["name"]) for item in actions],
                        self.app._active_portrait_action_id(config),
                        self.app._switch_runtime_portrait_action)
            self._switch(body, "自动切换动作", self.app._portrait_auto_actions_enabled(config),
                         self.app._set_portrait_auto_actions)
        ttk.Separator(body).pack(fill="x", pady=(12, 8))
        tk.Button(body, text="角色编辑", bg=BG, fg=ACCENT, relief="flat", bd=0,
                  cursor="hand2", anchor="w", font=("Microsoft YaHei UI", 9),
                  command=self._edit_role).pack(fill="x")
        self.signature = self._signature()
        self.buttons["角色"]._selected = True
        self.buttons["角色"]._paint()
        self.panel.update_idletasks()
        self._layout()
        self.panel.attributes("-topmost", bool(self.root.attributes("-topmost")))
        self.panel.deiconify()
        self.panel.lift()

    def _switch(self, parent, title, enabled, callback):
        variable = tk.BooleanVar(master=parent, value=enabled)

        def toggle():
            result = callback(bool(variable.get()))
            if result is False:
                variable.set(not variable.get())
            self.signature = self._signature()

        self.app._brand_checkbutton(parent, text=title, variable=variable, command=toggle,
                                    bg=BG, fg=TEXT).pack(fill="x", pady=(10, 0))

    def _edit_role(self):
        self.close()
        self.app._show_character_editor(self.app.character_id)

    def _signature(self):
        return (self.app.character_id, id(self.app.character_config),
                id(getattr(self.app, "renderer", None)))

    def _dropdown_open(self):
        if self.dropdown is not None:
            return True
        for combo in self.combos.values():
            path = str(combo) + ".popdown"
            if self.root.tk.call("winfo", "exists", path) and self.root.tk.call("winfo", "ismapped", path):
                return True
        return False

    def _on_pointer_press(self, event):
        if self.closed or self.dropdown is None:
            return
        widget = str(event.widget)
        if widget == str(self.dropdown_combo) or widget.startswith(str(self.dropdown) + "."):
            return
        if widget == str(self.dropdown):
            return
        self._close_dropdown()

    def _pointer_press_at(self, x, y):
        if self.dropdown is not None and not (
                self._inside(self.dropdown, x, y) or self._inside(self.dropdown_combo, x, y)):
            self._close_dropdown()
        if self.opened and not any(self._inside(window, x, y)
                                   for window in (self.window, self.panel)):
            self.close()

    @staticmethod
    def _inside(window, x, y):
        if window is None or not window.winfo_viewable():
            return False
        return (window.winfo_rootx() <= x < window.winfo_rootx() + window.winfo_width()
                and window.winfo_rooty() <= y < window.winfo_rooty() + window.winfo_height())

    @staticmethod
    def _mouse_pressed():
        if os.name != "nt":
            return False
        return any(ctypes.windll.user32.GetAsyncKeyState(key) & 0x8000 for key in (1, 2))

    def contains_point(self, x, y):
        if self.closed:
            return False
        return (any(self._inside(window, x, y) for window in
                    (self.window, self.panel, self.tooltip)))

    def _tick(self):
        if self.timer is not None:
            self.root.after_cancel(self.timer)
        self.timer = None
        if self.closed:
            return
        try:
            if self._blocked():
                self.close(immediate=True)
            else:
                self._anchor()
                self._layout()
                topmost = bool(self.root.attributes("-topmost"))
                for window in (self.window, self.panel, self.tooltip):
                    if window is not None and bool(window.attributes("-topmost")) != topmost:
                        window.attributes("-topmost", topmost)
                x, y = self.root.winfo_pointerxy()
                down = self._mouse_pressed()
                if self.opened and os.name == "nt" and ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000:
                    self.close()
                if down and not self.mouse_down:
                    self._pointer_press_at(x, y)
                self.mouse_down = down
                left, top, right, bottom = self.pet_rect
                near = left <= x < right and top <= y < bottom
                if near or self.opened or self._inside(self.window, x, y):
                    self.last_near = time.monotonic()
                target = 1.0 if self.opened or time.monotonic() - self.last_near < .8 else 0.0
                next_alpha = self.alpha + max(-.3, min(.3, target - self.alpha))
                if self.alpha != next_alpha:
                    self.alpha = next_alpha
                    self.window.attributes("-alpha", self.alpha)
                if self.alpha:
                    if self.window.state() != "normal" or not self.window.winfo_viewable():
                        self.window.withdraw()
                        self.window.deiconify()
                        # Apply again after mapping; Windows can discard the
                        # initial geometry requested while the window was hidden.
                        self.window.__dict__.pop("_toolbar_geometry", None)
                        self._layout()
                else:
                    if self.window.state() != "withdrawn":
                        self.window.withdraw()
                    self._hide_hint()
                if self.panel is not None and self.signature != self._signature() and not self._dropdown_open():
                    self.show_role()
        except tk.TclError:
            if not self.root.winfo_exists():
                return
            raise
        self.timer = self.root.after(100, self._tick)

    def _on_destroy(self, event):
        if event.widget is self.root:
            # Tk destroys child widgets before delivering the root's event.
            self.closed = True
            for job in (self.timer, self.animation):
                if job is not None:
                    self.root.after_cancel(job)
            self.timer = self.animation = None
            self._unbind_pointer_press()

    def _unbind_pointer_press(self):
        if self.click_bind is not None:
            self.root._unbind(("bind", "all", "<ButtonPress-1>"), self.click_bind)
            self.click_bind = None

    def destroy(self):
        if self.closed:
            return
        self.close(immediate=True)
        self.closed = True
        for job in (self.timer, self.animation):
            if job is not None:
                self.root.after_cancel(job)
        self.timer = self.animation = None
        self.app.portrait_canvas.__dict__.pop("_portrait_input_blocked", None)
        self.root.unbind("<Destroy>", self.root_bind)
        self._unbind_pointer_press()
        self.window.destroy()
