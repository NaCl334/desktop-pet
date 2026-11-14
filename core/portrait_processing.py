from pathlib import Path
import shutil

from PIL import Image


IMAGE_EXTENSIONS = (".png", ".gif", ".ppm", ".jpg", ".jpeg")
EDGE_DARK_PIXEL_LIMIT = 70
EDGE_LUMA_DELTA = 35


def _repair_edge_colors(image):
    """修复透明边缘中的黑色底色污染，同时保留原 alpha。"""
    source = image.convert("RGBA")
    result = source.copy()
    pixels = source.load()
    output = result.load()
    width, height = source.size
    repaired = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if pixels[x, y][3] >= 245
    }
    # 透明边缘有时由多层半透明像素组成；逐层向外传播实色，避免
    # 只修复紧邻实色的第一层后，外侧仍保留黑色 RGB。
    for _ in range(6):
        pending = []
        for y in range(height):
            for x in range(width):
                red, green, blue, alpha = pixels[x, y]
                if alpha == 0 or alpha >= 245 or (x, y) in repaired:
                    continue
                neighbors = []
                for offset_y in (-1, 0, 1):
                    for offset_x in (-1, 0, 1):
                        if not offset_x and not offset_y:
                            continue
                        nx, ny = x + offset_x, y + offset_y
                        if (nx, ny) in repaired:
                            neighbors.append(output[nx, ny][:3])
                if neighbors:
                    pending.append(
                        (
                            x,
                            y,
                            (
                                sum(value[0] for value in neighbors) // len(neighbors),
                                sum(value[1] for value in neighbors) // len(neighbors),
                                sum(value[2] for value in neighbors) // len(neighbors),
                                alpha,
                            ),
                        )
                    )
        if not pending:
            break
        for x, y, value in pending:
            output[x, y] = value
            repaired.add((x, y))
    return result


def _remove_tiny_edge_fragments(image, max_pixels=8, max_alpha=32):
    """删除透明图中孤立的极小低 alpha 碎片，保留主体描边和细节。"""
    source = image.convert("RGBA")
    result = source.copy()
    pixels = source.load()
    output = result.load()
    width, height = source.size
    candidates = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if 0 < pixels[x, y][3] <= max_alpha
    }
    while candidates:
        start = candidates.pop()
        component = [start]
        pending = [start]
        while pending:
            x, y = pending.pop()
            for offset_y in (-1, 0, 1):
                for offset_x in (-1, 0, 1):
                    point = (x + offset_x, y + offset_y)
                    if point in candidates:
                        candidates.remove(point)
                        pending.append(point)
                        component.append(point)
        if len(component) <= max_pixels:
            for x, y in component:
                output[x, y] = (0, 0, 0, 0)
    return result


def _repair_opaque_edge_colors(image):
    """修复硬化 alpha 后外轮廓上的黑色/灰色污染，不影响内部描边。"""
    source = image.convert("RGBA")
    result = source.copy()
    pixels = source.load()
    output = result.load()
    width, height = source.size
    boundary = set()
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            if pixels[x, y][3] < 245:
                continue
            if any(
                pixels[nx, ny][3] < 16
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
            ):
                boundary.add((x, y))

    for x, y in boundary:
        red, green, blue, alpha = pixels[x, y]
        if max(red, green, blue) >= EDGE_DARK_PIXEL_LIMIT:
            continue
        samples = []
        for radius in (1, 2, 3):
            for offset_y in range(-radius, radius + 1):
                for offset_x in range(-radius, radius + 1):
                    if max(abs(offset_x), abs(offset_y)) != radius:
                        continue
                    nx, ny = x + offset_x, y + offset_y
                    if (
                        0 <= nx < width
                        and 0 <= ny < height
                        and (nx, ny) not in boundary
                        and pixels[nx, ny][3] >= 245
                    ):
                        samples.append(pixels[nx, ny][:3])
            if samples:
                break
        if not samples:
            continue
        sample = tuple(sum(color[index] for color in samples) // len(samples) for index in range(3))
        current_luma = 0.299 * red + 0.587 * green + 0.114 * blue
        sample_luma = 0.299 * sample[0] + 0.587 * sample[1] + 0.114 * sample[2]
        if sample_luma - current_luma >= EDGE_LUMA_DELTA:
            output[x, y] = sample + (alpha,)
    return result


def enhance_portrait(source_path, destination_path, mode="full", target_scale=2):
    """生成不改写像素的运行副本，仅裁掉完全透明的外围区域。"""
    source = Image.open(source_path).convert("RGBA")
    alpha = source.getchannel("A")
    bbox = alpha.getbbox()
    if bbox:
        source = source.crop(bbox)

    destination_path = Path(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source.save(destination_path, optimize=True)
    return destination_path


def enhance_portrait_folder(source_folder, destination_folder, mode="full"):
    """复制并增强单张图片或文件夹内的立绘，源文件保持不变。"""
    source_path = Path(source_folder)
    destination_folder = Path(destination_folder)
    if not source_path.exists():
        raise FileNotFoundError(str(source_path))
    destination_folder.mkdir(parents=True, exist_ok=True)

    if source_path.is_file():
        if source_path.suffix.lower() not in IMAGE_EXTENSIONS:
            return []
        sources = [source_path]
        use_idle_name = True
    elif source_path.is_dir():
        sources = [
            source
            for source in sorted(source_path.rglob("*"))
            if source.is_file() and source.suffix.lower() in IMAGE_EXTENSIONS
        ]
        use_idle_name = False
    else:
        return []

    copied = []
    for source in sources:
        # 运行时立绘统一转换为 PNG。JPG/JPEG 没有 alpha 通道，直接按原扩展名
        # 保存 RGBA 图像会失败；统一格式也让后续渲染和透明处理保持一致。
        destination = destination_folder / (
            "idle.png" if use_idle_name else "{}.png".format(source.stem)
        )
        if source.resolve() == destination.resolve():
            copied.append(destination)
            continue
        enhance_portrait(source, destination, mode=mode)
        copied.append(destination)
    if copied and not (destination_folder / "idle.png").exists():
        shutil.copy2(str(copied[0]), str(destination_folder / "idle.png"))
    return copied

