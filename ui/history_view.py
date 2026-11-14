"""Scrollable history bubbles; stored messages remain untouched."""
from datetime import datetime
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk

from core.config import ROOT


class HistoryView(tk.Canvas):
    def __init__(self, parent, colors, avatar_loader=None, content_formatter=None, **kwargs):
        super().__init__(parent, bg=colors["bg_soft"], highlightthickness=0,
                         bd=0, **kwargs)
        self.colors = colors
        self.avatar_loader = avatar_loader
        self.content_formatter = content_formatter
        self.records = []
        self.character = {}
        self.empty_text = "暂无历史对话记录。"
        self.bubbles = []
        self._avatars = []
        self._pending = None
        self._menu = None
        self._image_slots = []
        self._image_photos = {}
        self._image_cache = OrderedDict()
        self._image_jobs = {}
        self._image_pool = None
        self._image_after = None
        self.bind("<Configure>", self._schedule_layout)
        self.bind("<Map>", lambda _event: self._schedule_images(), add="+")
        self.bind("<Destroy>", self._destroy, add="+")

    def set_messages(self, records, character=None, empty_text="这段对话还没有消息。"):
        self.records = list(records)
        self.character = dict(character or {})
        self.empty_text = empty_text
        self._image_cache.clear()
        self._layout()
        self.yview_moveto(0)

    @staticmethod
    def timestamp(value):
        value = str(value or "").strip()
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value

    def _schedule_layout(self, _event=None):
        if self._pending is None:
            self._pending = self.after_idle(self._relayout)

    def _relayout(self):
        self._pending = None
        position = self.yview()[0]
        self._layout()
        self.yview_moveto(position)

    def _rounded(self, x1, y1, x2, y2, fill):
        radius = min(8, (x2 - x1) / 2, (y2 - y1) / 2)
        points = (x1+radius, y1, x2-radius, y1, x2, y1, x2, y1+radius,
                  x2, y2-radius, x2, y2, x2-radius, y2, x1+radius, y2,
                  x1, y2, x1, y2-radius, x1, y1+radius, x1, y1)
        return self.create_polygon(points, smooth=True, splinesteps=20, fill=fill, outline="")

    def _copy_menu(self, event, content):
        if self._menu is not None:
            self._menu.destroy()
        self._menu = tk.Menu(self, tearoff=False)
        def copy():
            self.clipboard_clear()
            self.clipboard_append(content)
        self._menu.add_command(label="复制消息", command=copy)
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def yview(self, *args):
        result = super().yview(*args)
        if args:
            self._schedule_images()
        return result

    def yview_moveto(self, fraction):
        super().yview_moveto(fraction)
        self._schedule_images()

    def yview_scroll(self, number, what):
        super().yview_scroll(number, what)
        self._schedule_images()

    def _schedule_images(self):
        if self._image_after is None:
            self._image_after = self.after_idle(self._refresh_images)

    @staticmethod
    def _read_thumbnail(path, size):
        try:
            with Image.open(path) as source:
                source.thumbnail(size, Image.Resampling.LANCZOS)
                return source.convert("RGBA"), ""
        except FileNotFoundError:
            return None, "图片已移动或删除"
        except (OSError, ValueError, Image.DecompressionBombError):
            return None, "图片无法读取"

    def _refresh_images(self):
        self._image_after = None
        # Workers only decode pixels; all Tk objects are created on the UI thread.
        for key, job in list(self._image_jobs.items()):
            if job.done():
                try:
                    self._image_cache[key] = job.result()
                except Exception:
                    self._image_cache[key] = (None, "图片无法读取")
                del self._image_jobs[key]
        top = self.canvasy(0) - 160
        bottom = top + self.winfo_height() + 320
        more = False
        for slot in self._image_slots:
            item = slot["item"]
            if slot["box"][3] < top or slot["box"][1] > bottom:
                if item in self._image_photos:
                    self.itemconfigure(item, image="")
                    self._image_photos.pop(item)
                    self.itemconfigure(slot["status"], state="normal")
                continue
            if item in self._image_photos:
                continue
            key = (slot["path"], slot["size"])
            if key in self._image_cache:
                source, error = self._image_cache[key]
                self._image_cache.move_to_end(key)
                if source is not None:
                    photo = ImageTk.PhotoImage(source, master=self)
                    self._image_photos[item] = photo
                    self.itemconfigure(item, image=photo)
                    self.itemconfigure(slot["status"], state="hidden")
                else:
                    self.itemconfigure(slot["status"], text=error)
            elif key not in self._image_jobs:
                if len(self._image_jobs) < 2:
                    if self._image_pool is None:
                        self._image_pool = ThreadPoolExecutor(max_workers=2,
                                                             thread_name_prefix="history-image")
                    self._image_jobs[key] = self._image_pool.submit(self._read_thumbnail, *key)
                more = True
        while len(self._image_cache) > 32:
            self._image_cache.popitem(last=False)
        if more or self._image_jobs:
            self._image_after = self.after(60, self._refresh_images)

    def _open_image(self, path):
        try:
            # Validate the attachment as an image before handing it to Windows.
            with Image.open(path) as source:
                source.verify()
            os.startfile(str(path))
        except FileNotFoundError:
            messagebox.showwarning("无法查看截图", "图片已移动或删除，无法打开原图。",
                                   parent=self.winfo_toplevel())
        except (OSError, ValueError, Image.DecompressionBombError):
            messagebox.showerror("无法查看截图", "图片无法读取，或系统未配置图片查看程序。",
                                 parent=self.winfo_toplevel())

    def _layout(self):
        for avatar in self._avatars:
            avatar.destroy()
        self._avatars.clear()
        self.delete("all")
        self.bubbles.clear()
        self._image_slots.clear()
        self._image_photos.clear()
        width = max(140, self.winfo_width())
        padding, avatar_size, gap = 12, 32, 8
        available = max(45, width - 2 * padding - avatar_size - gap)
        max_text = max(24, int(available * .82) - 24)
        y = 14
        if not self.records:
            self.create_text(padding, y, text=self.empty_text, anchor="nw",
                             width=width - padding * 2, fill=self.colors["muted"],
                             font=("Microsoft YaHei UI", 10))
        for record in self.records:
            user = record.get("role") == "user"
            content = str(record.get("content") or "")
            if record.get("role") == "assistant" and callable(self.content_formatter):
                content = self.content_formatter(content)
            attachment = record.get("screen_image_path")
            path = None
            if attachment:
                path = Path(str(attachment))
                if not path.is_absolute():
                    path = ROOT / path
            display_content = "" if path and content.strip() in ("截图", "【截图】") else content
            name = "我" if user else str(record.get("character_name")
                                         or self.character.get("character_name") or "未记录角色")
            text = self.create_text(0, 0, text=display_content or " ", width=max_text,
                                    anchor="nw", fill=self.colors["white"],
                                    font=("Microsoft YaHei UI", 10))
            bounds = self.bbox(text)
            text_width = bounds[2] - bounds[0]
            height = bounds[3] - bounds[1] + 20
            bubble_width = min(available, text_width + 24)
            image_size = None
            caption = None
            if path:
                image_width = max(1, min(280, max_text))
                image_size = (image_width, max(1, min(160, int(image_width * .625))))
                caption = self.create_text(0, 0, text="点击查看原图", anchor="nw",
                                           width=image_width, fill=self.colors["muted"],
                                           font=("Microsoft YaHei UI", 8))
                caption_bounds = self.bbox(caption)
                image_offset = height - 10 if display_content else 10
                height = image_offset + image_size[1] + 8 + caption_bounds[3] - caption_bounds[1] + 10
                bubble_width = min(available, max(bubble_width, image_width + 24))
            x = width - padding - bubble_width if user else padding + avatar_size + gap
            name_item = self.create_text(x + bubble_width if user else x, y, text=name,
                                        anchor="ne" if user else "nw", fill=self.colors["muted"],
                                        width=bubble_width, font=("Microsoft YaHei UI", 8))
            name_height = self.bbox(name_item)[3] - self.bbox(name_item)[1]
            top = y + name_height + 5
            bubble = self._rounded(x, top, x + bubble_width, top + height,
                                   "#FFFFFF" if user else self.colors["panel"])
            tail = None
            if not user:
                tail = self.create_polygon(x + 2, top + 9, x - 6, top + 15,
                                           x + 2, top + 21,
                                           fill=self.colors["panel"], outline="")
            self.coords(text, x + 12, top + 10)
            self.tag_raise(text)
            image_item = None
            if path:
                left, image_top = x + 12, top + image_offset
                frame = self.create_rectangle(left, image_top, left + image_size[0],
                                              image_top + image_size[1],
                                              fill=self.colors["bg_soft"], outline=self.colors["muted"])
                center = (left + image_size[0] / 2, image_top + image_size[1] / 2)
                status = self.create_text(*center, text="正在加载截图…", width=image_size[0],
                                          fill=self.colors["muted"], font=("Microsoft YaHei UI", 9))
                image_item = self.create_image(*center)
                self.coords(caption, left, image_top + image_size[1] + 8)
                self.tag_raise(caption)
                self._image_slots.append(dict(item=image_item, status=status, path=path,
                                              size=image_size,
                                              box=(left, image_top, left + image_size[0],
                                                   image_top + image_size[1])))
                for target in (frame, status, image_item, caption):
                    self.tag_bind(target, "<Button-1>", lambda _event, value=path: self._open_image(value))
                    self.tag_bind(target, "<Enter>", lambda _event: self.configure(cursor="hand2"))
                    self.tag_bind(target, "<Leave>", lambda _event: self.configure(cursor=""))
            if not user:
                avatar = tk.Label(self, text=name[:1] or "?", bg=self.colors["panel"],
                                  fg=self.colors["white"], font=("Microsoft YaHei UI", 12, "bold"))
                self._avatars.append(avatar)
                self.create_window(padding, top, window=avatar, anchor="nw",
                                   width=avatar_size, height=avatar_size)
                if callable(self.avatar_loader):
                    self.avatar_loader(record, self.character, avatar, (avatar_size, avatar_size))
            time_text = self.timestamp(record.get("created_at"))
            time_item = self.create_text(x + bubble_width if user else x, top + height + 5,
                                        text=time_text, width=bubble_width,
                                        anchor="ne" if user else "nw", fill=self.colors["muted"],
                                        font=("Microsoft YaHei UI", 8))
            for item in (text, bubble, tail):
                if item is None:
                    continue
                self.tag_bind(item, "<Button-3>", lambda event, value=content: self._copy_menu(event, value))
            self.bubbles.append(dict(role=record.get("role"), box=(x, top, x+bubble_width, top+height),
                                     text=text, bubble=bubble, tail=tail, time=time_item, name=name_item,
                                     image=image_item))
            y = self.bbox(time_item)[3] + 18
        self.configure(scrollregion=(0, 0, width, max(y, self.winfo_height())))
        self._schedule_images()

    def _destroy(self, event):
        if event.widget is not self:
            return
        if self._pending is not None:
            self.after_cancel(self._pending)
            self._pending = None
        if self._image_after is not None:
            self.after_cancel(self._image_after)
            self._image_after = None
        if self._image_pool is not None:
            self._image_pool.shutdown(wait=False, cancel_futures=True)
        self._image_photos.clear()
        self._image_cache.clear()
