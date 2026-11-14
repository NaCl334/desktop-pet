"""会话自然语言导航指令解析。"""

import re
from datetime import date, datetime, timedelta


_RESUME_VERB_RE = re.compile(r"(继续|延续|恢复|打开|接着)")
_SESSION_WORD_RE = re.compile(r"(会话|对话)")
_OCCURRENCE_RE = re.compile(
    r"(?:第\s*(\d+)\s*(?:次|个)?|(?:会话|对话)\s*(?:第\s*)?(\d+)\s*(?:次)?|#\s*(\d+))"
)
_FULL_CHINESE_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)?"
)
_FULL_NUMERIC_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)"
)
_MONTH_DAY_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)(?!\d)"
)


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.now().astimezone().date()


def _valid_date(year, month, day):
    try:
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def parse_conversation_date(text, now=None):
    """从中文或数字日期中解析本地日期；无法确认时返回 None。"""
    text = str(text or "").strip()
    reference = _as_date(now)
    relative = {
        "今天": reference,
        "今日": reference,
        "昨天": reference - timedelta(days=1),
        "昨日": reference - timedelta(days=1),
        "前天": reference - timedelta(days=2),
    }
    for label, value in relative.items():
        if label in text:
            return value

    match = _FULL_CHINESE_DATE_RE.search(text)
    if match:
        return _valid_date(*match.groups())
    match = _FULL_NUMERIC_DATE_RE.search(text)
    if match:
        return _valid_date(*match.groups())
    match = _MONTH_DAY_RE.search(text)
    if match:
        month, day = match.groups()
        return _valid_date(reference.year, month, day)
    return None


def parse_resume_conversation_command(text, now=None):
    """解析“继续某日期会话”的用户指令，不匹配普通聊天。"""
    text = " ".join(str(text or "").split()).strip()
    if not text or not _RESUME_VERB_RE.search(text):
        return None
    if not _SESSION_WORD_RE.search(text) and not _OCCURRENCE_RE.search(text):
        return None
    conversation_date = parse_conversation_date(text, now=now)
    if conversation_date is None:
        return None

    occurrence_match = _OCCURRENCE_RE.search(text)
    occurrence = None
    if occurrence_match:
        for group in occurrence_match.groups():
            if group:
                occurrence = int(group)
                break
        if occurrence < 1:
            return None
    return {
        "kind": "resume_conversation",
        "date": conversation_date.isoformat(),
        "occurrence": occurrence,
    }
