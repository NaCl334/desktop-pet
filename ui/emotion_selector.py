import tkinter as tk
from tkinter import font as tkfont

from core.emotion import EMOTION_GROUPS, PRESET_EMOTIONS


class EmotionSelector(tk.Frame):
    """Three category tabs and a cyclic, three-slot emotion selector."""

    def __init__(self, master, variable, available, colors, **kwargs):
        super().__init__(master, bg=colors["panel"], **kwargs)
        self.variable = variable
        self.colors = colors
        available = set(available)
        self.groups = {
            group: tuple(key for key in keys if key in available)
            for group, keys in EMOTION_GROUPS.items()
        }
        self.group = "正向"
        self.positions = dict.fromkeys(self.groups, 0)
        self.grid_columnconfigure(0, weight=1)
        tabs = tk.Frame(self, bg=colors["panel"])
        tabs.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.tabs = {}
        for column, group in enumerate(self.groups):
            tabs.grid_columnconfigure(column, weight=1, uniform="category")
            button = tk.Button(
                tabs, text=group, command=lambda group=group: self.select_group(group),
                font=("Microsoft YaHei UI", 10), relief="flat", borderwidth=0,
                cursor="hand2", pady=7, takefocus=True,
            )
            button.grid(row=0, column=column, sticky="ew", padx=2)
            self.tabs[group] = button

        self.carousel = tk.Frame(self, bg=colors["panel"], height=62)
        self.carousel.grid(row=1, column=0, sticky="ew")
        self.carousel.grid_propagate(False)
        self.carousel.grid_rowconfigure(0, weight=1)
        slot_font = tkfont.Font(root=self, family="Microsoft YaHei UI", size=11, weight="bold")
        slot_width = max(slot_font.measure(item["label"]) for item in PRESET_EMOTIONS.values()) + 16
        for column in (1, 2, 3):
            self.carousel.grid_columnconfigure(column, weight=1, uniform="emotion")
        self.previous = self._arrow("<", -1, 0)
        self.next = self._arrow(">", 1, 4)
        self.carousel.configure(width=slot_width * 3 + self.previous.winfo_reqwidth()
                                + self.next.winfo_reqwidth())
        self.slots = []
        for column, offset in enumerate((-1, 0, 1), start=1):
            label = tk.Label(
                self.carousel, bg=colors["panel"],
                fg=colors["white"] if offset == 0 else colors["muted"],
                font=("Microsoft YaHei UI", 11, "bold" if offset == 0 else "normal"),
                anchor="center", height=2,
            )
            label.grid(row=0, column=column, sticky="nsew", padx=2)
            label.bind("<Configure>", lambda event, label=label:
                       label.configure(wraplength=max(1, event.width - 4)))
            if offset:
                label.configure(cursor="hand2")
                label.bind("<Button-1>", lambda event, delta=offset: self.step(delta))
            self.slots.append(label)
        self.carousel.configure(height=max(
            62, *(widget.winfo_reqheight() for widget in
                  (*self.slots, self.previous, self.next))))
        for widget in (self.carousel, *self.slots, self.previous, self.next):
            widget.bind("<MouseWheel>", self._wheel)
            widget.bind("<Button-4>", lambda event: self._wheel(event, -1))
            widget.bind("<Button-5>", lambda event: self._wheel(event, 1))
        self.bind("<Left>", lambda event: self.step(-1))
        self.bind("<Right>", lambda event: self.step(1))
        self._refresh()

    def _arrow(self, text, delta, column):
        button = tk.Button(
            self.carousel, text=text, width=1, padx=4, relief="flat", borderwidth=0,
            bg=self.colors["panel"], fg=self.colors["cyan"],
            activebackground=self.colors["panel"], activeforeground=self.colors["cyan"],
            font=("Microsoft YaHei UI", 14), cursor="hand2",
            command=lambda: self.step(delta),
        )
        button.grid(row=0, column=column, sticky="ns")
        return button

    def select_group(self, group):
        self.group = group
        self._refresh()

    def step(self, delta):
        keys = self.groups[self.group]
        if keys:
            self.positions[self.group] = (self.positions[self.group] + delta) % len(keys)
        self._refresh()
        return "break"

    def _wheel(self, event, delta=None):
        if delta is None:
            amount = getattr(event, "delta", 0)
            if not amount:
                return "break"
            delta = -1 if amount > 0 else 1
        return self.step(delta)

    def _refresh(self):
        for group, button in self.tabs.items():
            selected = group == self.group
            button.configure(
                bg=self.colors["cyan"] if selected else self.colors["panel"],
                fg="#FFFFFF" if selected else self.colors["muted"],
                activebackground=self.colors["cyan"],
                activeforeground="#FFFFFF",
            )
        keys = self.groups[self.group]
        position = self.positions[self.group]
        self.variable.set(PRESET_EMOTIONS[keys[position]]["label"] if keys else "")
        for label, offset in zip(self.slots, (-1, 0, 1)):
            text = (PRESET_EMOTIONS[keys[(position + offset) % len(keys)]]["label"]
                    if keys else ("本组已全部添加" if offset == 0 else ""))
            label.configure(text=text)
        for button in (self.previous, self.next):
            button.configure(state="normal" if len(keys) > 1 else "disabled")
