"""Prepare editor thumbnails without touching Tk or changing portrait assets."""

from core.portrait_viewport import crop_viewport


def prepare_portrait_previews(path, full_size, padding, viewport):
    from PIL import Image, ImageOps

    with Image.open(path) as opened:
        image = opened.convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    if bbox:
        image = image.crop(bbox)
    inner_size = tuple(max(1, size - padding * 2) for size in full_size)
    contained = ImageOps.contain(image, inner_size, method=Image.Resampling.LANCZOS)
    full = Image.new("RGBA", full_size, (255, 255, 255, 255))
    full.paste(
        contained,
        ((full.width - contained.width) // 2, (full.height - contained.height) // 2),
        contained,
    )
    local = ImageOps.contain(
        crop_viewport(image, viewport), (164, 164), method=Image.Resampling.LANCZOS
    )
    return full, local
