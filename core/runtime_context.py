"""Runtime date, region and holiday context for model requests.

The service deliberately keeps this information ephemeral.  Only the holiday
calendar is cached locally; the generated context is rebuilt for every request
from the current system clock.
"""

import html
import locale
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

from core.config import ROOT, load_json, save_json


DEFAULT_REGION_CODE = "CN"
HOLIDAY_CACHE_PATH = ROOT / "data" / "holiday_calendar_cache.json"
HOLIDAY_CACHE_VERSION = 1
HOLIDAY_CACHE_TTL_SECONDS = 24 * 60 * 60
RUNTIME_CONTEXT_MARKER = "本轮运行时信息（仅本轮有效，不写入长期记忆）"

REGION_DEFINITIONS = {
    "CN": {
        "name": "中国大陆",
        "calendar_name": "中国大陆法定节假日与调休安排",
    },
    "US": {
        "name": "美国",
        "calendar_name": "美国联邦节假日",
    },
    "JP": {
        "name": "日本",
        "calendar_name": "日本国民祝日",
    },
    "GB": {
        "name": "英国",
        "calendar_name": "英国公共假日",
    },
    "DE": {
        "name": "德国",
        "calendar_name": "德国法定节假日",
    },
    "FR": {
        "name": "法国",
        "calendar_name": "法国法定节假日",
    },
}

# The 2026 baseline is bundled from the State Council holiday arrangement.
# It lets a fresh offline install provide a verified current-year result while
# the background updater checks the official source after startup.
CHINA_OFFICIAL_SOURCES = {
    2026: (
        "国务院办公厅关于2026年部分节假日安排的通知",
        "https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm",
    ),
}

CHINA_2026_RANGES = (
    ("元旦", "2026-01-01", "2026-01-03"),
    ("春节", "2026-02-15", "2026-02-23"),
    ("清明节", "2026-04-04", "2026-04-06"),
    ("劳动节", "2026-05-01", "2026-05-05"),
    ("端午节", "2026-06-19", "2026-06-21"),
    ("中秋节", "2026-09-25", "2026-09-27"),
    ("国庆节", "2026-10-01", "2026-10-07"),
)

CHINA_2026_LEGAL_DATES = {
    "2026-01-01": "元旦",
    "2026-02-16": "春节",
    "2026-02-17": "春节",
    "2026-02-18": "春节",
    "2026-02-19": "春节",
    "2026-04-05": "清明节",
    "2026-05-01": "劳动节",
    "2026-05-02": "劳动节",
    "2026-06-19": "端午节",
    "2026-09-25": "中秋节",
    "2026-10-01": "国庆节",
    "2026-10-02": "国庆节",
    "2026-10-03": "国庆节",
}

CHINA_2026_WORK_DATES = {
    "2026-01-04": "元旦调休",
    "2026-02-14": "春节调休",
    "2026-02-28": "春节调休",
    "2026-05-09": "劳动节调休",
    "2026-09-20": "国庆节调休",
    "2026-10-10": "国庆节调休",
}


def _iso_now(value=None):
    value = value or datetime.now().astimezone()
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat(timespec="seconds")


def _format_utc_offset(value):
    offset = value.utcoffset() if value.tzinfo else None
    if offset is None:
        return "未知"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return "UTC{}{:02d}:{:02d}".format(
        sign,
        total_minutes // 60,
        total_minutes % 60,
    )


def _strip_html(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_region_code(value):
    normalized = str(value or "").strip().replace("-", "_").lower()
    aliases = {
        "cn": "CN",
        "china": "CN",
        "中国": "CN",
        "中国大陆": "CN",
        "zh_cn": "CN",
        "zh_sg": "CN",
        "0804": "CN",
        "00000804": "CN",
        "us": "US",
        "en_us": "US",
        "0409": "US",
        "00000409": "US",
        "jp": "JP",
        "ja_jp": "JP",
        "0411": "JP",
        "00000411": "JP",
        "gb": "GB",
        "en_gb": "GB",
        "0809": "GB",
        "00000809": "GB",
        "de": "DE",
        "de_de": "DE",
        "0407": "DE",
        "00000407": "DE",
        "fr": "FR",
        "fr_fr": "FR",
        "040c": "FR",
        "0000040c": "FR",
    }
    if normalized in aliases:
        return aliases[normalized]
    for prefix, region_code in (
        ("zh_cn", "CN"),
        ("en_us", "US"),
        ("ja_jp", "JP"),
        ("en_gb", "GB"),
        ("de_de", "DE"),
        ("fr_fr", "FR"),
    ):
        if normalized.startswith(prefix):
            return region_code
    return ""


def read_windows_region():
    """Read the Windows locale first, then use the Python locale fallback."""
    raw_values = []
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Control Panel\International",
        ) as key:
            for name in ("LocaleName", "Locale"):
                try:
                    raw_values.append(str(winreg.QueryValueEx(key, name)[0]))
                except OSError:
                    continue
    except (ImportError, OSError):
        pass

    try:
        current_locale = locale.getlocale()[0]
        if current_locale:
            raw_values.append(str(current_locale))
    except (ValueError, TypeError):
        pass

    for raw in raw_values:
        code = _normalize_region_code(raw)
        if code:
            return {
                "code": code,
                "name": REGION_DEFINITIONS[code]["name"],
                "source": "Windows 区域设置" if raw in raw_values[:2] else "系统 locale",
                "raw": raw,
                "verified": True,
            }
    return {
        "code": DEFAULT_REGION_CODE,
        "name": REGION_DEFINITIONS[DEFAULT_REGION_CODE]["name"],
        "source": "项目默认地区（无法可靠读取系统区域）",
        "raw": "",
        "verified": False,
    }


def _calendar_dates_from_ranges(ranges, legal_dates, work_dates):
    values = {}
    for name, start_text, end_text in ranges:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
        current = start
        while current <= end:
            key = current.isoformat()
            if key in legal_dates:
                status = "legal_holiday"
                holiday_name = legal_dates[key]
            else:
                status = "adjusted_rest_day"
                holiday_name = name
            values[key] = {
                "status": status,
                "name": holiday_name,
                "source_kind": "official_arrangement",
            }
            current += timedelta(days=1)
    for key, name in work_dates.items():
        values[key] = {
            "status": "adjusted_workday",
            "name": name,
            "source_kind": "official_arrangement",
        }
    return values


def bundled_calendars():
    source_name, source_url = CHINA_OFFICIAL_SOURCES[2026]
    return {
        "CN": {
            "2026": {
                "region_code": "CN",
                "year": 2026,
                "verified": True,
                "classification_verified": True,
                "source": {
                    "name": source_name,
                    "url": source_url,
                    "publisher": "中国政府网",
                    "kind": "bundled_official_baseline",
                    "retrieved_at": "",
                    "packaged_at": "2026-10-07",
                },
                "dates": _calendar_dates_from_ranges(
                    CHINA_2026_RANGES,
                    CHINA_2026_LEGAL_DATES,
                    CHINA_2026_WORK_DATES,
                ),
                "last_checked_at": "",
            }
        }
    }


def parse_official_china_notice(text, year, source_url, retrieved_at):
    """Parse the date/range language used by an official Chinese notice.

    The parser is intentionally conservative.  If the notice does not expose
    enough structure to distinguish legal days from adjusted rest days, it
    returns an unverified calendar instead of guessing.
    """
    cleaned = _strip_html(text)
    labels = ("元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节")
    clauses = []
    for index, label in enumerate(labels):
        start = cleaned.find(label)
        if start < 0:
            continue
        end = min(
            [
                position
                for next_label in labels[index + 1 :]
                for position in [cleaned.find(next_label, start + len(label))]
                if position >= 0
            ]
            or [len(cleaned)]
        )
        clauses.append((label, cleaned[start:end]))

    dates = {}
    parsed_ranges = []
    parsed_work_dates = {}
    recognized = 0
    for label, clause in clauses:
        date_tokens = [
            (int(month), int(day))
            for month, day in re.findall(r"(\d{1,2})月(\d{1,2})日", clause)
        ]
        if not date_tokens:
            continue
        recognized += 1
        range_match = re.search(
            r"(\d{1,2})月(\d{1,2})日(?:[^。；;]*?)(?:至|到)(?:(\d{1,2})月)?(\d{1,2})日",
            clause,
        )
        if range_match:
            start_month, start_day, end_month, end_day = range_match.groups()
            start_date = date(year, int(start_month), int(start_day))
            end_date = date(
                year + 1 if end_month and int(end_month) < int(start_month) else year,
                int(end_month or start_month),
                int(end_day),
            )
            current = start_date
            while current <= end_date:
                key = current.isoformat()
                dates[key] = {
                    "status": "adjusted_rest_day",
                    "name": label,
                    "source_kind": "official_arrangement",
                }
                current += timedelta(days=1)

            parsed_ranges.append(
                (
                    label,
                    start_date.isoformat(),
                    end_date.isoformat(),
                )
            )

        # Only consume the contiguous date list immediately before "上班".
        # Reading every date before that word would incorrectly reclassify the
        # holiday range itself as an adjusted workday.
        work_date_groups = re.findall(
            r"((?:\d{1,2}月\d{1,2}日(?:（[^）]*）)?"
            r"(?:、|，|,)?\s*)+)上班",
            clause,
        )
        for work_date_group in work_date_groups:
            for month, day in re.findall(
                r"(\d{1,2})月(\d{1,2})日",
                work_date_group,
            ):
                key = date(year, int(month), int(day)).isoformat()
                parsed_work_dates[key] = "{}调休".format(label)

    for key, name in parsed_work_dates.items():
        dates[key] = {
            "status": "adjusted_workday",
            "name": name,
            "source_kind": "official_arrangement",
        }

    expected_ranges = {
        (name, start, end)
        for name, start, end in CHINA_2026_RANGES
    }
    actual_ranges = set(parsed_ranges)
    expected_work_dates = {
        key: name for key, name in CHINA_2026_WORK_DATES.items()
    }
    arrangement_verified = (
        int(year) == 2026
        and actual_ranges == expected_ranges
        and set(parsed_work_dates) == set(expected_work_dates)
        and recognized == len(labels)
    )
    if arrangement_verified:
        # The annual notice supplies the ranges and make-up workdays.  For the
        # 2026 official arrangement, the statutory holiday dates are bundled
        # from the same government arrangement and overlaid only after the
        # complete schedule has matched exactly.
        for key, holiday_name in CHINA_2026_LEGAL_DATES.items():
            dates[key] = {
                "status": "legal_holiday",
                "name": holiday_name,
                "source_kind": "official_arrangement",
            }
        for key, holiday_name in expected_work_dates.items():
            dates[key]["name"] = holiday_name

    # A partial or structurally different parse is not safe enough to be
    # presented as verified holiday information.  It remains a diagnostic
    # candidate only and must not replace a verified cached calendar.
    verified = arrangement_verified and len(dates) >= 20
    return {
        "region_code": "CN",
        "year": int(year),
        "verified": verified,
        "classification_verified": bool(arrangement_verified),
        "source": {
            "name": "国务院办公厅年度节假日安排（自动解析）",
            "url": source_url,
            "publisher": "中国政府网",
            "kind": "official_html",
            "retrieved_at": retrieved_at,
            "packaged_at": "",
        },
        "dates": dates,
        "last_checked_at": retrieved_at,
    }


class RuntimeContextService(object):
    """Build ephemeral runtime context and refresh holiday data in background."""

    def __init__(
        self,
        app_config=None,
        cache_path=None,
        now_factory=None,
        region_reader=None,
        fetcher=None,
    ):
        self.app_config = app_config if isinstance(app_config, dict) else {}
        self.cache_path = Path(cache_path or HOLIDAY_CACHE_PATH)
        self._now_factory = now_factory or (lambda: datetime.now().astimezone())
        self._region_reader = region_reader or read_windows_region
        self._fetcher = fetcher or self._fetch_url
        self._lock = threading.RLock()
        self._refresh_thread = None
        self._refresh_started = False
        self._last_refresh_error = ""
        self._region_error = ""
        self._last_refresh_started_at = ""
        self._last_refresh_finished_at = ""
        self._cache = self._load_cache()

    def _load_cache(self):
        value = load_json(self.cache_path, {})
        if not isinstance(value, dict):
            value = {}
        calendars = value.get("calendars")
        if not isinstance(calendars, dict):
            calendars = {}
        bundled = bundled_calendars()
        for region_code, region_calendars in bundled.items():
            target = calendars.setdefault(region_code, {})
            if not isinstance(target, dict):
                calendars[region_code] = target = {}
            for year, calendar in region_calendars.items():
                existing = target.get(year)
                existing_source = (
                    existing.get("source", {})
                    if isinstance(existing, dict)
                    else {}
                )
                if not isinstance(existing, dict) or (
                    existing_source.get("kind") == "bundled_official_baseline"
                    and existing_source.get("url")
                    != calendar["source"].get("url")
                ):
                    target[year] = calendar
        cache = {
            "version": HOLIDAY_CACHE_VERSION,
            "calendars": calendars,
        }
        try:
            save_json(self.cache_path, cache)
        except OSError:
            # A read-only installation can still use the bundled baseline.
            pass
        return cache

    def start_background_refresh(self, years=None):
        with self._lock:
            if self._refresh_started and self._refresh_thread is not None:
                return self._refresh_thread
            self._refresh_started = True
            self._last_refresh_started_at = _iso_now(self._now_factory())
            self._refresh_thread = threading.Thread(
                target=self.refresh,
                args=(years,),
                name="holiday-calendar-refresh",
                daemon=True,
            )
            self._refresh_thread.start()
            return self._refresh_thread

    def refresh(self, years=None):
        current = self._now_factory()
        years = list(years or (current.year, current.year + 1))
        try:
            for region_code in self._region_codes_to_refresh():
                for year in years:
                    self._refresh_one(region_code, int(year))
        except Exception as error:
            with self._lock:
                self._last_refresh_error = str(error)
        finally:
            with self._lock:
                self._last_refresh_finished_at = _iso_now(self._now_factory())

    def _region_codes_to_refresh(self):
        override = self._region_override()
        override_code = _normalize_region_code(override)
        if override_code in REGION_DEFINITIONS:
            return [override_code]
        try:
            detected = self._region_reader()
        except Exception as error:
            self._region_error = "系统区域读取失败：{}".format(error)
            detected = {}
        code = _normalize_region_code(override) or str(
            detected.get("code", DEFAULT_REGION_CODE)
        ).upper()
        return [code if code in REGION_DEFINITIONS else DEFAULT_REGION_CODE]

    def _refresh_one(self, region_code, year):
        if region_code != "CN":
            return
        source_url = CHINA_OFFICIAL_SOURCES.get(year, ("", ""))[1]
        if not source_url:
            return
        retrieved_at = _iso_now(self._now_factory())
        try:
            raw = self._fetcher(source_url)
            calendar = parse_official_china_notice(
                raw,
                year,
                source_url,
                retrieved_at,
            )
            if not (
                calendar.get("verified")
                and calendar.get("classification_verified")
            ):
                raise ValueError("官方年度安排解析结果不完整，已保留原缓存")
        except Exception as error:
            with self._lock:
                self._last_refresh_error = "{} {}：{}".format(
                    region_code,
                    year,
                    error,
                )
            return
        with self._lock:
            calendars = self._cache.setdefault("calendars", {}).setdefault(
                region_code,
                {},
            )
            calendars[str(year)] = calendar
            try:
                save_json(self.cache_path, self._cache)
            except OSError as error:
                self._last_refresh_error = "节假日日历缓存写入失败：{}".format(error)

    @staticmethod
    def _fetch_url(url):
        request = urllib_request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "ChatPet/1.0 (holiday calendar updater)",
            },
            method="GET",
        )
        try:
            with urllib_request.urlopen(request, timeout=12) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as error:
            raise RuntimeError("HTTP {}".format(error.code)) from error

    def _region(self):
        override = self._region_override()
        override_code = _normalize_region_code(override)
        if override_code in REGION_DEFINITIONS:
            return {
                "code": override_code,
                "name": REGION_DEFINITIONS[override_code]["name"],
                "source": "配置覆盖",
                "raw": str(override),
                "verified": True,
            }
        try:
            detected = self._region_reader()
        except Exception as error:
            self._region_error = "系统区域读取失败：{}".format(error)
            detected = {}
        if isinstance(detected, dict) and detected.get("code") in REGION_DEFINITIONS:
            return dict(detected)
        return {
            "code": DEFAULT_REGION_CODE,
            "name": REGION_DEFINITIONS[DEFAULT_REGION_CODE]["name"],
            "source": "项目默认地区（区域读取失败）",
            "raw": "",
            "verified": False,
            "error": self._region_error,
        }

    def _region_override(self):
        runtime_config = self.app_config.get("runtime_context")
        if not isinstance(runtime_config, dict):
            return ""
        return str(runtime_config.get("region_override", "") or "").strip()

    def _calendar_for(self, region_code, year):
        with self._lock:
            region_calendars = self._cache.get("calendars", {}).get(region_code, {})
            calendar = region_calendars.get(str(year))
            return dict(calendar) if isinstance(calendar, dict) else None

    def _holiday_status(self, current, region):
        calendar = self._calendar_for(region["code"], current.year)
        key = current.date().isoformat()
        calendar_verified = bool(
            calendar
            and calendar.get("verified")
            and calendar.get("classification_verified", False)
        )
        if calendar_verified:
            item = calendar.get("dates", {}).get(key)
            if item:
                return {
                    **item,
                    "verified": bool(
                        calendar.get("classification_verified", False)
                    ),
                    "calendar_verified": True,
                    "calendar_year": current.year,
                    "source": calendar.get("source", {}),
                    "last_checked_at": calendar.get("last_checked_at", ""),
                }
            return {
                "status": (
                    "ordinary_weekend"
                    if current.weekday() >= 5
                    else "ordinary_workday"
                ),
                "name": "",
                "verified": True,
                "calendar_verified": True,
                "calendar_year": current.year,
                "source": calendar.get("source", {}),
                "last_checked_at": calendar.get("last_checked_at", ""),
            }

        # Weekday itself is deterministic, but without the annual calendar we
        # must not claim that a weekday is or is not an official holiday.
        return {
            "status": (
                "ordinary_weekend_unverified"
                if current.weekday() >= 5
                else "unknown"
            ),
            "name": "",
            "verified": False,
            "calendar_verified": False,
            "calendar_year": current.year,
            "source": (calendar or {}).get("source", {}),
            "last_checked_at": (calendar or {}).get("last_checked_at", ""),
        }

    def get_context(self, now=None):
        current = now or self._now_factory()
        if current.tzinfo is None:
            current = current.astimezone()
        region = self._region()
        holiday = self._holiday_status(current, region)
        tzinfo = current.tzinfo
        timezone_name = (
            getattr(tzinfo, "key", None)
            or current.tzname()
            or "本机时区"
        )
        with self._lock:
            last_error = self._last_refresh_error
            refresh_started = self._last_refresh_started_at
            refresh_finished = self._last_refresh_finished_at
            region_error = self._region_error
        return {
            "local_datetime": current.isoformat(timespec="seconds"),
            "local_date": current.date().isoformat(),
            "local_time": current.strftime("%H:%M:%S"),
            "weekday": current.strftime("%A"),
            "weekday_index": current.weekday(),
            "weekday_zh": (
                "星期一",
                "星期二",
                "星期三",
                "星期四",
                "星期五",
                "星期六",
                "星期日",
            )[current.weekday()],
            "timezone_name": str(timezone_name),
            "utc_offset": _format_utc_offset(current),
            "clock_source": "Windows 本机系统时钟",
            "clock_observed_at": _iso_now(current),
            "region": region,
            "region_error": region_error,
            "holiday": holiday,
            "refresh": {
                "started_at": refresh_started,
                "finished_at": refresh_finished,
                "last_error": last_error,
            },
        }

    def format_for_model(self, now=None):
        context = self.get_context(now=now)
        region = context["region"]
        holiday = context["holiday"]
        source = holiday.get("source", {}) or {}
        if holiday.get("status") == "legal_holiday":
            holiday_text = "法定节假日"
        elif holiday.get("status") == "adjusted_rest_day":
            holiday_text = "调休休息日"
        elif holiday.get("status") == "adjusted_workday":
            holiday_text = "调休工作日"
        elif holiday.get("status") == "ordinary_weekend":
            holiday_text = "普通周末"
        elif holiday.get("status") == "ordinary_weekend_unverified":
            holiday_text = "普通周末（年度节假日未核实）"
        elif holiday.get("status") == "ordinary_workday":
            holiday_text = "普通工作日"
        else:
            holiday_text = "未核实（不得根据星期自行推断节假日）"
        if holiday.get("name"):
            holiday_text += "，名称：{}".format(holiday["name"])
        trust = "已核实" if holiday.get("verified") else "未核实"
        return (
            "{}：\n"
            "- 当前本地日期：{}\n"
            "- 当前本地时间：{}\n"
            "- 星期：{}\n"
            "- 时区名称：{}\n"
            "- UTC 偏移：{}\n"
            "- 时钟来源：{}；观察时间：{}\n"
            "- 自动识别地区：{}（{}；原始值：{}；识别可信：{}）\n"
            "- 地区诊断：{}\n"
            "- 当日节假日状态：{}\n"
            "- 日历来源：{}；发布方：{}；地址：{}\n"
            "- 适用地区：{}\n"
            "- 数据年份：{}\n"
            "- 日历缓存最后检查：{}\n"
            "- 节假日可信状态：{}\n"
            "- 后台更新状态：{}；最近错误：{}\n"
            "使用规则：优先使用以上本轮运行时信息；系统时钟错误时不要自行纠正。"
            "节假日状态为未核实时必须明确说明未知，不得根据训练知识、聊天历史或星期几猜测。"
        ).format(
            RUNTIME_CONTEXT_MARKER,
            context["local_date"],
            context["local_time"],
            context["weekday_zh"],
            context["timezone_name"],
            context["utc_offset"],
            context["clock_source"],
            context["clock_observed_at"],
            region["name"],
            region["source"],
            region.get("raw", "") or "无",
            "是" if region.get("verified") else "否",
            context.get("region_error", "") or "无",
            holiday_text,
            source.get("name", "无可用来源"),
            source.get("publisher", "未知"),
            source.get("url", "无"),
            region["name"],
            holiday.get("calendar_year", current_year := context["local_date"][:4]),
            holiday.get("last_checked_at", "") or "无",
            trust,
            "后台检查已完成"
            if context["refresh"].get("finished_at")
            else "后台检查进行中或尚未完成",
            context["refresh"].get("last_error", "") or "无",
        )


_DEFAULT_RUNTIME_CONTEXT_SERVICE = None
_DEFAULT_RUNTIME_CONTEXT_LOCK = threading.Lock()


def configure_runtime_context(app_config=None, service=None):
    global _DEFAULT_RUNTIME_CONTEXT_SERVICE
    with _DEFAULT_RUNTIME_CONTEXT_LOCK:
        _DEFAULT_RUNTIME_CONTEXT_SERVICE = (
            service
            if service is not None
            else RuntimeContextService(app_config)
        )
        return _DEFAULT_RUNTIME_CONTEXT_SERVICE


def get_runtime_context_service():
    global _DEFAULT_RUNTIME_CONTEXT_SERVICE
    with _DEFAULT_RUNTIME_CONTEXT_LOCK:
        if _DEFAULT_RUNTIME_CONTEXT_SERVICE is None:
            _DEFAULT_RUNTIME_CONTEXT_SERVICE = RuntimeContextService()
        return _DEFAULT_RUNTIME_CONTEXT_SERVICE


def append_runtime_context(messages, service=None):
    """Return a copied message list with current runtime context in system text."""
    service = service or get_runtime_context_service()
    copied = [dict(message) for message in (messages or [])]
    runtime_text = service.format_for_model()
    end_marker = (
        "节假日状态为未核实时必须明确说明未知，"
        "不得根据训练知识、聊天历史或星期几猜测。"
    )
    for message in copied:
        if message.get("role") == "system":
            content = message.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict)
                )
            content = str(content)
            marker_start = content.find(RUNTIME_CONTEXT_MARKER)
            if marker_start >= 0:
                end_start = content.find(end_marker, marker_start)
                if end_start >= 0:
                    end_end = end_start + len(end_marker)
                    content = (
                        content[:marker_start]
                        + runtime_text
                        + content[end_end:]
                    )
                    message["content"] = content
                    return copied
                return copied
            message["content"] = "{}\n{}".format(content, runtime_text)
            return copied
    copied.insert(0, {"role": "system", "content": runtime_text})
    return copied
