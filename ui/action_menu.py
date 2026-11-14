"""Small grouped action menus with visible headings and predictable hover states."""
import tkinter as tk


class ActionMenu:
    def __init__(self, button, commands, colors):
        self.button = button
        self.colors = colors
        self.entries = []
        self.popup = None
        self.active = None
        self._focus_after = None
        for item in commands:
            self.entries.append({"label": item[0] if item else "", "command": item[1] if item else None,
                                 "heading": bool(item and item[1] is None),
                                 "state": "disabled" if not item or item[1] is None else "normal"})
        button.bind("<Button-1>", self._toggle)
        for sequence in ("<space>", "<Return>", "<Down>"):
            button.bind(sequence, self._toggle)
        button.bind("<Destroy>", lambda _event: self.unpost(), add="+")

    def index(self, item):
        if item == "end":
            return len(self.entries) - 1
        if isinstance(item, int):
            return item
        return next(i for i, entry in enumerate(self.entries) if entry["label"] == item)

    def entrycget(self, item, option):
        return self.entries[self.index(item)][option]

    def entryconfigure(self, item, **options):
        index = self.index(item)
        self.entries[index].update(options)
        self._paint(index)

    def invoke(self, item):
        entry = self.entries[self.index(item)]
        if entry["state"] != "disabled" and entry["command"] is not None:
            self.unpost()
            return entry["command"]()

    def _toggle(self, _event=None):
        if self.popup is None:
            self.post()
        else:
            self.unpost()
        return "break"

    def _paint(self, index, hover=False):
        entry = self.entries[index]
        label = entry.get("widget")
        if label is None or not label.winfo_exists() or not entry["label"]:
            return
        heading = entry["heading"]
        label.configure(
            bg=("#acd9ef" if hover else "#c2e5f5") if heading else
               self.colors["cyan"] if hover and entry["state"] != "disabled" else self.colors["bg_soft"],
            fg=self.colors["white"] if heading or entry["state"] != "disabled" else self.colors["muted"],
        )

    def _hover(self, index):
        if self.active is not None:
            self._paint(self.active)
        self.active = index
        if index is not None:
            self._paint(index, True)

    def _move(self, direction):
        choices = [i for i, item in enumerate(self.entries) if item["state"] != "disabled"]
        if choices:
            current = choices.index(self.active) if self.active in choices else (-1 if direction > 0 else 0)
            self._hover(choices[(current + direction) % len(choices)])
        return "break"

    def _choose(self):
        if self.active is not None:
            self.invoke(self.active)
        return "break"

    def _outside(self, event):
        if self.popup is None:
            return
        x, y = self.popup.winfo_rootx(), self.popup.winfo_rooty()
        if not (x <= event.x_root < x + self.popup.winfo_width()
                and y <= event.y_root < y + self.popup.winfo_height()):
            self.unpost()
            return "break"

    def post(self):
        if self.popup is not None:
            return
        self.previous_focus = self.button.focus_get()
        self.previous_grab = self.button.grab_current()
        popup = self.popup = tk.Toplevel(self.button, bg=self.colors["bg_soft"], bd=0,
                                         highlightthickness=1, highlightbackground=self.colors["line"])
        popup.withdraw()
        popup.overrideredirect(True)
        popup.transient(self.button.winfo_toplevel())
        popup.attributes("-topmost", self.button.winfo_toplevel().attributes("-topmost"))
        self.active = None
        for index, item in enumerate(self.entries):
            if not item["label"]:
                tk.Frame(popup, bg=self.colors["line"], height=1).pack(fill="x", pady=4)
                continue
            label = tk.Label(popup, text=item["label"], anchor="w", padx=14,
                             pady=6 if item["heading"] else 5,
                             font=("Microsoft YaHei UI", 10, "bold" if item["heading"] else "normal"),
                             cursor="arrow" if item["state"] == "disabled" else "hand2")
            item["widget"] = label
            label.pack(fill="x")
            self._paint(index)
            label.bind("<Enter>", lambda _event, i=index: self._hover(i))
            label.bind("<Leave>", lambda _event: self._hover(None))
            label.bind("<ButtonRelease-1>", lambda _event, i=index: self.invoke(i))
        popup.bind("<Escape>", lambda _event: self.unpost())
        popup.bind("<Up>", lambda _event: self._move(-1))
        popup.bind("<Down>", lambda _event: self._move(1))
        popup.bind("<Return>", lambda _event: self._choose())
        popup.bind("<space>", lambda _event: self._choose())
        popup.bind("<ButtonPress-1>", self._outside, add="+")
        popup.bind("<FocusOut>", self._schedule_focus_check)
        popup.update_idletasks()
        width = max(self.button.winfo_width(), popup.winfo_reqwidth())
        height = popup.winfo_reqheight()
        x = min(self.button.winfo_rootx(), popup.winfo_screenwidth() - width)
        y = self.button.winfo_rooty() + self.button.winfo_height()
        if y + height > popup.winfo_screenheight():
            y = max(0, self.button.winfo_rooty() - height)
        popup.geometry(f"{width}x{height}+{max(0, x)}+{y}")
        popup.deiconify()
        popup.lift()
        popup.grab_set()
        popup.focus_set()

    def _schedule_focus_check(self, _event=None):
        if self.popup is not None and self._focus_after is None:
            self._focus_after = self.button.after_idle(self._check_focus)

    def _check_focus(self):
        self._focus_after = None
        popup = self.popup
        if popup is None:
            return
        focus = self.button.focus_get()
        if focus is None or focus.winfo_toplevel() != popup:
            self.unpost()

    def unpost(self):
        if self._focus_after is not None:
            self.button.after_cancel(self._focus_after)
            self._focus_after = None
        popup, self.popup = self.popup, None
        if popup is None:
            return
        try:
            popup.grab_release()
            popup.destroy()
            if self.previous_grab is not None and self.previous_grab.winfo_exists():
                self.previous_grab.grab_set()
            if self.previous_focus is not None and self.previous_focus.winfo_exists():
                self.previous_focus.focus_set()
        except tk.TclError:
            pass
        self.active = None
