"""Shared rounded dropdown fields with one unobtrusive chevron."""
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk


def install_dropdown_styles(root, colors):
    cache_owner = root._root()
    if getattr(cache_owner, "_dropdown_style_images", None):
        return
    style = ttk.Style(root)
    images = []

    def field(outline):
        scale = 3
        image = Image.new("RGBA", (36 * scale, 36 * scale))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((1, 1, 36 * scale - 2, 36 * scale - 2),
                               radius=7 * scale, fill=colors["bg_soft"],
                               outline=outline, width=scale)
        photo = ImageTk.PhotoImage(image.resize((36, 36), Image.Resampling.LANCZOS), master=root)
        images.append(photo)
        return photo

    normal = field("#9dc9da")
    focused = field(colors["cyan"])
    arrow_image = Image.new("RGBA", (18 * 3, 16 * 3))
    ImageDraw.Draw(arrow_image).line([(5 * 3, 6 * 3), (9 * 3, 10 * 3), (13 * 3, 6 * 3)],
                                   fill=colors["muted"], width=3)
    arrow = ImageTk.PhotoImage(arrow_image.resize((18, 16), Image.Resampling.LANCZOS), master=root)
    images.append(arrow)
    for prefix, widget in (("PetSelect", "Combobox"), ("PetMenu", "Menubutton")):
        style.element_create(prefix + ".field", "image", normal,
                             ("focus", focused), ("active", focused), border=8, sticky="nsew")
        style.element_create(prefix + ".downarrow", "image", arrow, sticky="")
        style.layout("Brand.T" + widget, [(prefix + ".field", {"sticky": "nswe", "children": [
            (prefix + ".padding", {"sticky": "nswe", "children": [
                (prefix + ".downarrow", {"side": "right", "sticky": "ns"}),
                (widget + ".textarea" if widget == "Combobox" else widget + ".label",
                 {"side": "left", "sticky": "nswe"}),
            ]}),
        ]})])
        style.configure("Brand.T" + widget, foreground=colors["white"],
                        background=colors["bg_soft"], padding=(10, 2, 7, 2),
                        font=("Microsoft YaHei UI", 9), arrowsize=18, anchor="w")
        style.map("Brand.T" + widget,
                  foreground=[("disabled", colors["muted"]), ("readonly", colors["white"])],
                  selectbackground=[("readonly", colors["bg_soft"])],
                  selectforeground=[("readonly", colors["white"])])
    cache_owner._dropdown_style_images = images
