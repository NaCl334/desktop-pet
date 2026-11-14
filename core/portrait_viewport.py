"""Normalized crop settings for the local portrait display."""

DEFAULT_VIEWPORT = {
    "center_x": 0.5,
    "center_y": 0.19,
    "zoom": 1.7,
}
MIN_ZOOM = 1.0
MAX_ZOOM = 8.0
MIN_VIEWPORT_SIZE = 0.01


def normalize_viewport(value, fallback=None):
    fallback = fallback or DEFAULT_VIEWPORT
    value = value if isinstance(value, dict) else {}
    result = {}
    for key, minimum, maximum in (
        ("center_x", 0.0, 1.0),
        ("center_y", 0.0, 1.0),
        ("zoom", MIN_ZOOM, MAX_ZOOM),
    ):
        try:
            number = float(value.get(key, fallback[key]))
        except (TypeError, ValueError):
            number = float(fallback[key])
        result[key] = min(maximum, max(minimum, number))
    for key in ("width", "height"):
        if key in value:
            try:
                number = float(value[key])
            except (TypeError, ValueError):
                number = float(fallback.get(key, MIN_VIEWPORT_SIZE))
            result[key] = min(1.0, max(MIN_VIEWPORT_SIZE, number))
    return result


def resolve_viewport(settings, state):
    settings = settings if isinstance(settings, dict) else {}
    default = normalize_viewport(settings.get("default"))
    overrides = settings.get("states", {})
    if isinstance(overrides, dict) and state in overrides:
        return normalize_viewport(overrides[state], fallback=default)
    return default


def viewport_crop_box(image_size, viewport=None, aspect_ratio=1.0):
    width, height = max(1, int(image_size[0])), max(1, int(image_size[1]))
    viewport = normalize_viewport(viewport)
    if "width" in viewport and "height" in viewport:
        crop_width = width * viewport["width"]
        crop_height = height * viewport["height"]
    else:
        try:
            aspect_ratio = max(0.05, float(aspect_ratio))
        except (TypeError, ValueError):
            aspect_ratio = 1.0
        crop_width = min(width, height * aspect_ratio) / viewport["zoom"]
        crop_height = crop_width / aspect_ratio
    crop_width = min(float(width), max(1.0, crop_width))
    crop_height = min(float(height), max(1.0, crop_height))

    center_x = viewport["center_x"] * width
    center_y = viewport["center_y"] * height
    left = min(max(0.0, center_x - crop_width / 2), width - crop_width)
    top = min(max(0.0, center_y - crop_height / 2), height - crop_height)
    right = left + crop_width
    bottom = top + crop_height

    box = (
        max(0, int(round(left))),
        max(0, int(round(top))),
        min(width, max(1, int(round(right)))),
        min(height, max(1, int(round(bottom)))),
    )
    if box[2] <= box[0]:
        box = (box[0], box[1], min(width, box[0] + 1), box[3])
    if box[3] <= box[1]:
        box = (box[0], box[1], box[2], min(height, box[1] + 1))
    return box


def crop_viewport(image, viewport=None, aspect_ratio=1.0):
    return image.crop(viewport_crop_box(image.size, viewport, aspect_ratio))


def viewport_from_crop_box(box, image_size, aspect_ratio=1.0):
    width, height = max(1, int(image_size[0])), max(1, int(image_size[1]))
    left, top, right, bottom = (float(value) for value in box)
    crop_width = max(1.0, right - left)
    crop_height = max(1.0, bottom - top)
    center_x = min(width, max(0.0, (left + right) / 2.0))
    center_y = min(height, max(0.0, (top + bottom) / 2.0))
    return normalize_viewport(
        {
            "center_x": center_x / width,
            "center_y": center_y / height,
            "width": min(1.0, crop_width / width),
            "height": min(1.0, crop_height / height),
        }
    )
