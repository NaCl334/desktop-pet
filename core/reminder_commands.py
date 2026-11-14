"""Parse explicit reminder requests locally; never infer actions from model replies."""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta


class ReminderParseError(ValueError):
    pass


@dataclass(frozen=True)
class ReminderRequest:
    title: str
    due_at: datetime
    duration_minutes: float = 0
    repeat_count: int = 0


@dataclass(frozen=True)
class ReminderDraft:
    """循环提醒已收集的部分信息，等待后续消息补齐总次数。"""

    title: str
    duration_minutes: float
    repeat_count: int | None = None


NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百壹贰叁肆伍陆柒捌玖拾佰兩]+)"
DURATION_UNIT = r"(?:小时|小時|分钟|分鐘|分|min|mins?|minute|minutes|秒钟|秒|天)"
DURATION_PART = rf"(?:{NUMBER}(?:个)?半|{NUMBER}(?:个)?|半)(?:{DURATION_UNIT})"
REMINDER_MARKER = r"(?:提醒|叫|通知|告诉|提示)(?:一下|下)?我"
DELETE_VERBS = r"(?:删除|删掉|删去|取消|移除|清除)"
CREATION_VERBS = r"(?:创建|建立|添加|新建|设置|安排|生成)"
BATCH_QUANTITY = r"(?:几个|几条|几项|几次|多个|多条|若干|一些|这些|那些|这几个)"


def _is_recent_batch_delete(text):
    """识别“刚才创建的几个任务删除”一类批量删除请求。

    语音识别有时会在“几个”和“任务”之间插入“重复型/代替型”等噪声。
    只要创建、数量、任务和删除动词的核心结构仍在，就交给本地任务存储
    按最近创建的一批待提醒任务处理，避免落入普通模型对话。
    """
    if not re.search(DELETE_VERBS, text):
        return False
    core = re.search(
        rf"{CREATION_VERBS}[^。！？!?删除取消移除清除]{{0,50}}"
        rf"{BATCH_QUANTITY}[^。！？!?删除取消移除清除]{{0,50}}"
        r"(?:任务|提醒)",
        text,
    )
    return bool(core)


def _number(text):
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    digits = dict(zip(
        "零〇一二兩两三四五六七八九壹贰叁參参肆伍陆陸柒捌玖",
        (0, 0, 1, 2, 2, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2, 3, 3, 4, 5, 6, 6, 7, 8, 9),
    ))
    total, digit = 0, 0
    for char in text:
        if char in "十拾百佰":
            total += (digit or 1) * (10 if char in "十拾" else 100)
            digit = 0
        else:
            digit = digits[char]
    return total + digit


def parse_repeat_count(content):
    """解析后续消息中的总提醒次数，例如“进行3次”“总共做三次”。"""
    text = str(content or "").strip()
    match = re.fullmatch(
        rf"(?:(?:那就|就|请|总共|一共|共)\s*)?"
        rf"(?:(?:进行|重复|执行|做|循环|提醒|再)\s*)?"
        rf"({NUMBER})\s*次"
        rf"(?:\s*(?:，|,)?\s*(?:现在(?:就)?开始|开始))?[。！!\s]*",
        text,
    )
    if (
        not match
        and not re.search(
            rf"每(?:隔)?\s*(?:{DURATION_PART})+",
            text,
            flags=re.IGNORECASE,
        )
        and re.search(r"间隔|提醒|刚才|上一条|上一句|前面|之前", text)
    ):
        match = re.search(
            rf"(?:进行|重复|执行|做|循环|提醒)\s*({NUMBER})\s*次"
            rf"(?:\s*(?:，|,)?\s*(?:现在(?:就)?开始|开始))?[。！!\s]*$",
            text,
        )
    if not match:
        return None
    value = _number(match.group(1))
    if value != int(value) or not 1 <= value <= 100:
        raise ReminderParseError("提醒总次数需要是1到100的整数。")
    return int(value)


def extract_reminder_followup_title(content):
    """从补充消息中提取事项，例如“提醒我做运动，进行2次”。"""
    text = str(content or "").strip()
    if not re.search(REMINDER_MARKER, text):
        return None
    if re.search(
        r"不要|不用|不必|别|取消|删除|删掉|删去|移除|清除|怎么|如何|为什么|"
        r"如果|假如|假设|翻译",
        text,
    ):
        return None
    text = re.sub(
        rf"[，,\s]*(?:总共|一共|共)?\s*(?:再\s*)?"
        rf"(?:重复|进行|执行|做|循环)?\s*{NUMBER}\s*次"
        rf"(?:\s*(?:，|,)?\s*(?:现在(?:就)?开始|开始))?[。！!\s]*$",
        "",
        text,
    ).strip()
    text = re.sub(
        r"^(?:请|麻烦你?|帮我|给我|替我|你){0,3}",
        "",
        text,
    ).strip()
    text = re.sub(
        rf"^(?:让你)?{REMINDER_MARKER}",
        "",
        text,
    ).strip(" ，,。.!！：:")
    if not text or text in ("一次", "一遍", "一下"):
        return None
    return text


def parse_reminder_delete_command(content):
    """解析明确的删除/取消提醒请求，返回一个或多个可匹配选择器。"""
    text = str(content or "").strip()
    if _is_recent_batch_delete(text):
        return "最近"
    if re.search(
        r"(?:另一个|另外一个|剩下(?:的)?那个|其余(?:的)?(?:任务|提醒))"
        r".*(?:任务|提醒)?\s*(?:也|也要|一起)?\s*"
        rf"{DELETE_VERBS}",
        text,
    ) or re.fullmatch(
        r"(?:另一个|另外一个|剩下(?:的)?那个)(?:任务|提醒)?"
        r"(?:也|也要|一起)?(?:删掉|删除|取消|移除|清除)?(?:吧|了)?",
        text,
    ):
        return "另一个"
    leading = re.match(
        rf"^(?:请|麻烦你?|帮我|给我|替我|你)?\s*{DELETE_VERBS}"
        r"(?:一个|个)?(?:任务)?(?:提醒)?(?:[:：\s，,]*)?(.*)$",
        text,
    )
    if leading:
        target = leading.group(1)
    else:
        embedded = re.match(
            rf"^(?:请|麻烦你?|帮我|给我|替我|你)?\s*"
            rf"(?:把|将)\s*(.+?)\s*(?:的)?(?:任务|提醒)?\s*{DELETE_VERBS}"
            r"(?:了)?(?:[:：\s，,。.!！?？].*)?$",
            text,
        )
        if not embedded:
            # 允许 ASR 或自然对话在删除命令前后带礼貌语和结果语气，
            # 例如“好的，收到！23:55那个、23:56喝水的，还有每10分钟那条，
            # 都取消，不再提醒啦”。这类文本仍然是明确的本地删除指令。
            if not re.search(DELETE_VERBS, text):
                return None
            selector_start = re.search(
                rf"(?:\d{{1,2}}[:：]\d{{2}}|每(?:隔)?\s*(?:{DURATION_PART})+|"
                r"(?:全部|所有|全都|都)(?:的)?(?:待提醒|当前)?(?:任务|提醒))",
                text,
                flags=re.IGNORECASE,
            )
            if not selector_start:
                return None
            target = text[selector_start.start():]
        else:
            target = embedded.group(1)
            # “将我刚才建立的几次任务全部删除”把“全部”放在删除动词之前，
            # 不能只按标题匹配，否则会把整句上下文当成任务标题。
            if re.search(
                r"(?:刚才|刚刚|最近|之前|前面).*(?:任务|提醒)"
                r".*(?:全部|所有|全都|都)\s*$",
                target,
            ):
                return "全部"

    # 删除动词可能位于选择器之后（“每10分钟那条，都取消”），
    # 先截去动词及其后的礼貌/补充语，再解析前面的选择器。
    target = re.sub(
        rf"[，,、；;\s]*(?:全部|所有|全都|都)?\s*{DELETE_VERBS}"
        r"(?:了|啦|吧)?[\s，,、；;。.!！?？]*(?:不再提醒|以后不用|现在不需要).*$",
        "",
        target,
        flags=re.IGNORECASE,
    )
    target = re.sub(
        rf"[，,、；;\s]*(?:全部|所有|全都|都)?\s*{DELETE_VERBS}"
        r"(?:了|啦|吧)?[\s，,、；;。.!！?？]*$",
        "",
        target,
        flags=re.IGNORECASE,
    )
    target = re.sub(
        r"(?:现在|之后|以后)?(?:就)?(?:不需要了|不用了|不需要|不要了|吧)$",
        "",
        target,
    ).strip(" \t\r\n，,。.!！：:?？")
    target = re.sub(r"(?:全部|所有|全都|都)\s*$", "", target).strip()
    if not target:
        return ""

    # 允许一句话里同时指定多个任务，例如“删除23:55那个、23:56喝水的，
    # 还有每10分钟那条”。保留每个选择器，交给 TaskStore 按时间、标题或
    # 间隔进行联合匹配。
    pieces = re.split(
        r"(?:[、，,；;]\s*|\s*(?:还有|以及|并且|和|跟|或者|或)\s*)",
        target,
    )
    selectors = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        piece = re.sub(r"^(?:上次|刚才|最近|之前|这个|那个)\s*", "", piece)
        piece = re.sub(
            r"(?:现在|之后|以后)?(?:就)?(?:不需要了|不用了|不需要|不要了|吧)$",
            "",
            piece,
        ).strip()
        piece = re.sub(
            r"(?:的)?(?:待处理|待提醒|当前)?(?:任务|提醒)\s*$",
            "",
            piece,
        ).strip()
        piece = re.sub(r"(?:那个|这条|那条|这个|那个)$", "", piece).strip()
        piece = piece.strip(" \t\r\n，,。.!！：:?？的")
        if not piece:
            continue
        # “每10分钟让你提醒我一次的任务”应按间隔定位，而不是把“一次”
        # 当成标题。
        interval = re.match(
            rf"^(?P<interval>每(?:隔)?\s*(?:{DURATION_PART}\s*)+)",
            piece,
            flags=re.IGNORECASE,
        )
        if interval:
            piece = re.sub(r"\s+", "", interval.group("interval"))
        else:
            piece = re.sub(
                rf"^每(?:隔)?\s*(?:{DURATION_PART}\s*)+(?:让你)?",
                "",
                piece,
                flags=re.IGNORECASE,
            ).strip()
            piece = re.sub(rf"^(?:让你)?{REMINDER_MARKER}", "", piece).strip()
            piece = piece.strip(" \t\r\n，,。.!！：:?？的")
        if piece and piece not in ("一个", "个"):
            selectors.append(piece)

    if not selectors:
        return ""
    if len(selectors) == 1 and selectors[0] in ("所有", "全部", "当前", "全都", "都"):
        return "全部" if selectors[0] in ("全都", "都") else selectors[0]
    return "|".join(dict.fromkeys(selectors))


def _parse_duration_seconds(value, unit):
    value = value.replace("个", "")
    amount = _number(value[:-1]) if value.endswith("半") else _number(value)
    if value.endswith("半"):
        amount += 0.5
    return amount * {
        "小时": 3600,
        "小時": 3600,
        "分钟": 60,
        "分鐘": 60,
        "分": 60,
        "min": 60,
        "mins": 60,
        "minute": 60,
        "minutes": 60,
        "秒钟": 1,
        "秒": 1,
        "天": 86400,
    }[unit.lower()]


def extract_reminder_draft(content):
    """提取循环提醒的内容和间隔，但允许暂时缺少总次数。"""
    text = str(content or "").strip()
    if not re.search(REMINDER_MARKER, text):
        return None
    if re.search(
        r"不要|不用|不必|别|取消|删除|删掉|删去|移除|清除|怎么|如何|为什么|"
        r"如果|假如|假设|翻译",
        text,
    ):
        return None
    if not re.search(r"每(?:隔)?", text):
        return None
    duration = re.search(
        rf"每(?:隔)?\s*(?P<duration>(?:{DURATION_PART}\s*)+)",
        text,
        flags=re.IGNORECASE,
    )
    if not duration:
        return None
    seconds = 0
    for part in re.finditer(
        rf"(?P<value>{NUMBER}(?:个)?半|{NUMBER}(?:个)?|半)(?P<unit>{DURATION_UNIT})",
        duration.group("duration"),
        flags=re.IGNORECASE,
    ):
        seconds += _parse_duration_seconds(part.group("value"), part.group("unit"))
    if not 1 <= seconds <= 366 * 86400:
        raise ReminderParseError("提醒间隔需要在1秒到366天之间。")
    remainder = text[:duration.start()] + text[duration.end():]
    remainder = re.sub(
        r"^(?:接下来|接着|之后|以后|那么|那就|请|麻烦你?|帮我|给我|替我|你){0,3}",
        "",
        remainder,
    ).strip()
    remainder = re.sub(r"^(?:设置|设定|创建|新建|添加|建)(?:一个|个)?(?:任务)?提醒[：:,，\s]*", "", remainder)
    remainder = re.sub(
        rf"^(?:让你)?{REMINDER_MARKER}",
        "",
        remainder,
    ).strip(" ，,。.!！：:")
    remainder = re.sub(r"(?:可以吗|好吗|行吗|谢谢|吧|哦|呀)[？?！!。\s]*$", "", remainder).strip(" ，,。.!！：:的")
    if not remainder:
        raise ReminderParseError("请补充要提醒的内容。")
    if remainder in ("一次", "一遍"):
        return ReminderDraft("", seconds / 60, parse_repeat_count(text))
    if remainder == "一下":
        # “提醒我一下”没有具体事项，但仍是明确的通用提醒请求。
        remainder = "提醒"
    return ReminderDraft(remainder, seconds / 60, parse_repeat_count(text))


def parse_reminder_command(content, now=None):
    """Return None for ordinary chat; reject incomplete commands without side effects."""
    now = now or datetime.now()
    text = str(content or "").strip()
    marker = re.search(REMINDER_MARKER, text)
    create = re.match(r"^(?:请|麻烦你?|帮我|给我|替我|你){0,3}(?:设置|设定|创建|新建|添加|建)(?:一个|个)?(?:任务)?提醒", text)
    if not marker and not create:
        return None
    # Quoted, negated, hypothetical and retrospective requests are not commands.
    if re.search(
        r"不要|不用|不必|别|取消|删除|删掉|删去|移除|清除|怎么|如何|为什么|"
        r"上次|之前|昨天|如果|假如|假设|翻译|[‘’“”「」]",
        text,
    ) or chr(34) in text:
        return None
    if len(re.findall(REMINDER_MARKER, text)) > 1:
        raise ReminderParseError("请一次设置一项提醒，并写明时间和内容。")
    repeated = bool(re.search(r"每|循环|重复", text))
    repeat_count = 0
    if repeated:
        count = re.search(
            rf"[，,\s]*(?:总共|一共|共)?\s*(?:再)?"
            rf"(?:重复|进行|执行|做|循环)?\s*({NUMBER})\s*次"
            rf"(?:\s*(?:，|,)?\s*(?:现在(?:就)?开始|开始))?[。！!\s]*$",
            text,
        )
        if not count or not re.search(rf"每(?:隔)?\s*(?:{DURATION_PART})+", text):
            raise ReminderParseError("循环提醒请写明间隔和总次数，例如：每30分钟提醒我喝水，进行3次。")
        repeat_count = _number(count.group(1))
        if repeat_count != int(repeat_count) or not 1 <= repeat_count <= 100:
            raise ReminderParseError("提醒总次数需要是1到100的整数。")
        repeat_count = int(repeat_count)
        text = text[:count.start()].strip()
    duration = re.search(
        rf"每(?:隔)?\s*(?P<duration>(?:{DURATION_PART}\s*)+)" if repeated else
        rf"(?P<duration>(?:{DURATION_PART}\s*)+)(?:以后|之後|之后|後|后)", text
    )
    duration_minutes = 0
    if duration:
        if not repeated and not re.search(r"后", duration.group()):
            raise ReminderParseError("请明确是多久以后提醒，例如：10分钟后提醒我喝水。")
        seconds = 0
        for part in re.finditer(
            rf"(?P<value>{NUMBER}(?:个)?半|{NUMBER}(?:个)?|半)(?P<unit>{DURATION_UNIT})",
            duration.group("duration"),
            flags=re.IGNORECASE,
        ):
            seconds += _parse_duration_seconds(part.group("value"), part.group("unit"))
        if not 1 <= seconds <= 366 * 86400:
            raise ReminderParseError("提醒间隔需要在1秒到366天之间。")
        due_at = now + timedelta(seconds=seconds)
        duration_minutes = seconds / 60
        remainder = text[:duration.start()] + text[duration.end():]
    else:
        clock = re.search(rf"(?P<period>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|今晚|明早|明晚|今早)?\s*(?P<hour>{NUMBER})(?:(?:点|时)(?P<minute>半|一刻|三刻|(?:{NUMBER})分?)?|[:：](?P<colon>\d{{1,2}}))", text)
        if not clock:
            raise ReminderParseError("请补充明确的时间和内容，例如：10分钟后提醒我喝水，或明天下午3点提醒我开会。")
        hour = _number(clock.group("hour"))
        minute_text = clock.group("colon") or clock.group("minute") or "0"
        minute = {"半": 30, "一刻": 15, "三刻": 45}.get(minute_text)
        if minute is None:
            minute = _number(minute_text.removesuffix("分"))
        period = clock.group("period") or ""
        if hour != int(hour) or minute != int(minute) or not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ReminderParseError("提醒时间无效，请使用0到23点、0到59分。")
        if period in ("中午", "下午", "傍晚", "晚上", "今晚", "明晚") and 1 <= hour < 12:
            hour += 12
        elif period in ("凌晨", "早上", "早晨", "上午", "明早", "今早") and hour == 12:
            hour = 0
        remainder = text[:clock.start()] + text[clock.end():]
        day = now.date()
        explicit_day = False
        date_match = re.search(r"(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})[日号]?", remainder)
        relative_day = re.search(r"今天|明天|大后天|后天", remainder)
        weekday = re.search(r"(下周|这周|本周|周|星期)([一二三四五六日天])", remainder)
        try:
            if date_match:
                year, month, date = date_match.groups()
                day = datetime(int(year or now.year), int(month), int(date)).date()
                remainder = remainder[:date_match.start()] + remainder[date_match.end():]
                explicit_day = True
            elif relative_day:
                day += timedelta(days={"今天": 0, "明天": 1, "后天": 2, "大后天": 3}[relative_day.group()])
                remainder = remainder[:relative_day.start()] + remainder[relative_day.end():]
                explicit_day = True
            elif weekday:
                target = "一二三四五六日".index(weekday.group(2).replace("天", "日"))
                offset = target - now.weekday()
                if weekday.group(1) == "下周":
                    offset += 7
                elif weekday.group(1) in ("周", "星期") and offset < 0:
                    offset += 7
                day += timedelta(days=offset)
                remainder = remainder[:weekday.start()] + remainder[weekday.end():]
                explicit_day = True
            elif period in ("明早", "明晚", "今晚", "今早"):
                day += timedelta(days=1 if period.startswith("明") else 0)
                explicit_day = True
            due_at = datetime.combine(day, datetime.min.time()).replace(hour=int(hour), minute=int(minute))
        except ValueError:
            raise ReminderParseError("日期无效，请检查年月日。") from None
        if due_at <= now:
            if explicit_day:
                raise ReminderParseError("这个提醒时间已经过去，请指定未来时间。")
            due_at += timedelta(days=1)
    remainder = re.sub(
        r"^(?:接下来|接着|那么|那就|请|麻烦你?|帮我|给我|替我|你){0,3}(?:在|于)?",
        "",
        remainder,
    ).strip()
    remainder = re.sub(r"^(?:设置|设定|创建|新建|添加|建)(?:一个|个)?(?:任务)?提醒[：:,，\s]*", "", remainder)
    remainder = re.sub(
        rf"^(?:让你)?{REMINDER_MARKER}",
        "",
        remainder,
    ).strip(" ，,。.!！：:")
    remainder = re.sub(r"^(?:在|于)\s*", "", remainder)
    remainder = re.sub(r"(?:可以吗|好吗|行吗|谢谢|吧|哦|呀)[？?！!。\s]*$", "", remainder).strip(" ，,。.!！：:的")
    generic_reminder = remainder in ("一次", "一遍", "一下")
    if (
        not remainder
        or (
            remainder in ("一次", "一遍", "一下")
            and repeated
        )
        or re.search(
            r"明天|后天|下周|下个月|每|[0-9零一二两三四五六七八九十]+(?:点|时|分钟|小时|秒|天后)|[:：]\d",
            remainder,
        )
    ):
        raise ReminderParseError("请一次写清一项提醒的时间和内容，例如：10分钟后提醒我喝水。")
    if generic_reminder:
        remainder = "提醒"
    if len(remainder) > 160:
        raise ReminderParseError("提醒内容过长，请精简到160字以内。")
    return ReminderRequest(remainder, due_at, duration_minutes, repeat_count)
