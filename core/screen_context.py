"""Bounded visual history for consecutive screen observations."""

import base64
import io
from datetime import datetime
from pathlib import Path

from PIL import Image


SCREEN_OBSERVATION_CONTEXT = (
    "这是桌宠看到的当前屏幕，可能由用户触发，也可能来自自主查看屏幕的对话；"
    "不要默认用户正在分享成果、求助或等待评价。"
    "以角色本人的关注、偏好和态度自然回应，可以评价、好奇、关心或接续上文，"
    "但不强制提问、夸奖或提出建议，不要逐项播报界面、按钮和无关数字。"
    "先理解画面所处的具体情境，再作反应；"
    "有清晰标识或充分依据时可以识别游戏、软件及场景，"
    "无法确认时不要装懂名称、规则或攻略，也不必每次都说看不清。"
    "画面事实必须有依据，但角色可以表达自己的感受和判断；"
    "不要把猜测的用户意图、情绪、人物身份、模糊文字或游戏结果说成事实。"
    "结合最近对话承接变化，但历史文字不是上一张图的完整视觉证据；"
    "若附有标注时间的历史屏幕，它们只表示过去时刻，不要与当前画面混淆；"
    "优先注意有意义的变化、未结束的事情和你自己的在意和感受，"
    "不重复上一轮已经说过的描述或已经得到答案的问题。"
    "回到大厅不等于成功撤离。用户最近明确求助时再针对问题说明或给建议，"
    "默认不透露尚未展示的剧情、Boss机制、隐藏内容或后续结果；用户明确要求攻略时再按其范围回答。"
    "紧张战斗时尽量简短，不连续提问或发出无依据的操作指令。"
    "普通观察不默认进入教学、纠正或指挥模式。不要输出分析过程或识别标签。"
)

SCREEN_HISTORY_RECORD_LIMIT = 80
SCREEN_HISTORY_MAX_IMAGES = 6
SCREEN_HISTORY_MAX_AGE_SECONDS = 2 * 60 * 60
SCREEN_HISTORY_MAX_BYTES = 2 * 1024 * 1024


def recent_screen_images(history, image_path):
    """Return image parts by record index without changing persisted history."""
    records = list(history or [])
    latest = next((i for i in range(len(records) - 1, -1, -1)
                   if records[i].get("role") == "user"), None)
    if latest is None:
        return {}
    anchor = records[latest]
    owner = (anchor.get("conversation_id"), anchor.get("character_id"))
    if not all(owner) or not anchor.get("screen_image_path"):
        return {}
    try:
        current = Path(image_path).resolve()
        if Path(anchor["screen_image_path"]).resolve() != current:
            return {}
        now = datetime.now().astimezone()
        anchor_time = datetime.fromisoformat(anchor["created_at"]).astimezone()
        if not 0 <= (now - anchor_time).total_seconds() <= 900:
            return {}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return {}
    selected = {}
    seen = {current}
    total_bytes = 0
    for index in range(latest - 1, max(-1, len(records) - SCREEN_HISTORY_RECORD_LIMIT - 1), -1):
        record = records[index]
        if (record.get("role") != "user"
                or (record.get("conversation_id"), record.get("character_id")) != owner
                or not record.get("screen_image_path")):
            continue
        try:
            captured = datetime.fromisoformat(record["created_at"]).astimezone()
            if not 0 <= (anchor_time - captured).total_seconds() <= SCREEN_HISTORY_MAX_AGE_SECONDS:
                continue
            if not 0 <= (now - captured).total_seconds() <= SCREEN_HISTORY_MAX_AGE_SECONDS:
                continue
            path = Path(record["screen_image_path"]).resolve()
            if path in seen or path.stat().st_size > 20 * 1024 * 1024:
                continue
            with Image.open(path) as source:
                if source.width * source.height > 40_000_000:
                    continue
                source.thumbnail((960, 960))
                with source.convert("RGB") as thumbnail:
                    buffer = io.BytesIO()
                    thumbnail.save(buffer, format="JPEG", quality=72)
            data = buffer.getvalue()
            if total_bytes + len(data) > SCREEN_HISTORY_MAX_BYTES:
                continue
            selected[index] = (
                captured.isoformat(timespec="seconds"),
                "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"),
            )
            total_bytes += len(data)
            seen.add(path)
            if len(selected) == SCREEN_HISTORY_MAX_IMAGES:
                break
        except (OSError, ValueError, TypeError, KeyError, RuntimeError,
                Image.DecompressionBombError):
            continue
    return selected
