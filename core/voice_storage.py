import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from core.config import DEFAULT_VOICE_DIRECTORY, ROOT


def resolve_voice_directory(directory=None):
    value = str(directory or DEFAULT_VOICE_DIRECTORY).strip()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def ensure_voice_directory(directory=None):
    """创建并检查语音目录，失败时保留原始 OSError 供界面提示。"""
    path = resolve_voice_directory(directory)
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".voice_write_probe_{}".format(uuid.uuid4().hex)
    try:
        with probe.open("xb") as handle:
            handle.write(b"")
    finally:
        probe.unlink(missing_ok=True)
    return path


def sanitize_voice_filename_component(value):
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = text.rstrip(" .")
    return text or "角色"


def archive_voice_file(source, directory=None, character_name="", created_at=None):
    """将完整生成的音频安全归档，保留历史文件并避免同秒覆盖。"""
    source_path = Path(source)
    if not source_path.is_file():
        raise FileNotFoundError("待归档的语音文件不存在：{}".format(source_path))

    destination_dir = ensure_voice_directory(directory)
    timestamp = created_at or datetime.now()
    safe_name = sanitize_voice_filename_component(character_name)
    extension = source_path.suffix.lower() or ".wav"
    stem = "{}-{}".format(
        safe_name,
        timestamp.strftime("%Y%m%d_%H%M%S"),
    )
    index = 1
    while True:
        suffix = "" if index == 1 else "_{}".format(index)
        destination = destination_dir / "{}{}{}".format(
            stem,
            suffix,
            extension,
        )
        try:
            with source_path.open("rb") as source_handle:
                with destination.open("xb") as destination_handle:
                    shutil.copyfileobj(source_handle, destination_handle)
            return destination
        except FileExistsError:
            index += 1
