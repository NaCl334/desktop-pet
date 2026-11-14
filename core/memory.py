"""轻量本地长期记忆。

该模块只负责个人/项目历史，不负责当前日期、时间、时区或节假日等实时事实。
存储继续使用 data/memory.json，避免引入数据库或外部服务。
"""

import hashlib
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path

from core.config import ROOT, load_json, save_json
from core.history_memory import explicit_correction, retracted_claim


_EXPLICIT_REMEMBER_RE = re.compile(
    r"^\s*(?:请|帮我|麻烦你|以后请)?(?:记住|记下|记得|保存)\s*[:：，,]?\s*(.*)$",
    re.IGNORECASE,
)
_EXPLICIT_FORGET_RE = re.compile(
    r"^\s*(?:请|帮我|麻烦你|把)?(?:忘记|忘掉|删除记忆|删掉记忆|不必记得)\s*[:：，,]?\s*(.*)$",
    re.IGNORECASE,
)
_SENSITIVE_RE = re.compile(
    r"(密码|口令|验证码|api[\s_-]*key|access[\s_-]*token|refresh[\s_-]*token|"
    r"私钥|密钥|secret|身份证|银行卡|信用卡|钱包助记词|seed phrase)",
    re.IGNORECASE,
)
_REALTIME_RE = re.compile(
    r"(现在|当前|今天|今日|明天|昨天|此刻|刚刚|最近天气|天气|新闻|节假日|"
    r"星期几|几点|时间|日期|时区|调休|放假吗)",
    re.IGNORECASE,
)
_TEMPORARY_RE = re.compile(
    r"(等会儿|等一会|一会儿|稍后|晚点|暂时|这次|今天心情|现在有点|刚才|"
    r"好累|好饿|困了|马上|临时)",
    re.IGNORECASE,
)
_TASK_RE = re.compile(r"(提醒我|提醒一下|定时|闹钟|待办|任务提醒)")
_PREFERENCE_RE = re.compile(
    r"(我喜欢|我不喜欢|我偏好|我习惯|我的偏好|默认使用|默认用|请用|称呼我|"
    r"叫我|我常用|我更喜欢|我希望你)"
)
_PROJECT_RE = re.compile(
    r"(项目|工程|代码库|仓库|桌宠|正在开发|正在制作|长期目标|项目背景|"
    r"架构|计划长期|以后继续|当前目标|开发目标)"
)
_PROFILE_RE = re.compile(r"(我是|我叫|我的名字|我的职业|我住在|我来自|我的身份)")
_COMMITMENT_RE = re.compile(r"我们约好|我们约定|长期约定|以后每次|以后都|长期承诺")
_KEYWORD_RE = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clean_text(value, limit=500):
    text = " ".join(str(value or "").split()).strip()
    return text[:limit].rstrip()


def _keywords(value):
    text = str(value or "").lower()
    return [item for item in _KEYWORD_RE.findall(text) if len(item) >= 2]


def _safe_float(value, default=0.8):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class MemoryStore(object):
    """线程安全的 JSON 长期记忆存储和轻量检索器。"""

    MAX_CONTENT = 500
    DEFAULT_CONTEXT_ITEMS = 4
    DEFAULT_CONTEXT_CHARS = 1400

    def __init__(self, memory_path=None, image_dir=None):
        self.memory_path = Path(memory_path or (ROOT / "data" / "memory.json"))
        self.image_dir = Path(image_dir or (ROOT / "data" / "memory_images"))
        self._lock = threading.RLock()
        self.image_dir.mkdir(parents=True, exist_ok=True)

    def _load_items_locked(self):
        raw = load_json(self.memory_path, {"items": []})
        if isinstance(raw, dict):
            raw_items = raw.get("items", [])
        elif isinstance(raw, list):
            raw_items = raw
        else:
            raw_items = []
        if not isinstance(raw_items, list):
            raw_items = []
        return [self._normalize_item(item, index) for index, item in enumerate(raw_items)]

    def _normalize_item(self, item, index=0):
        item = dict(item) if isinstance(item, dict) else {"content": str(item)}
        content = _clean_text(item.get("content"), self.MAX_CONTENT)
        created_at = str(item.get("created_at") or _now())
        updated_at = str(item.get("updated_at") or created_at)
        memory_id = str(item.get("id") or "").strip()
        if not memory_id:
            digest = hashlib.sha1(
                "{}|{}|{}".format(created_at, content, index).encode("utf-8")
            ).hexdigest()[:16]
            memory_id = "mem_{}".format(digest)
        tags = item.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)] if tags else []
        attachments = item.get("attachments", [])
        if not isinstance(attachments, list):
            attachments = []
        normalized_attachments = []
        for attachment in attachments:
            if isinstance(attachment, dict):
                path = str(attachment.get("path", "")).strip()
                if path:
                    normalized_attachments.append(
                        {
                            "id": str(attachment.get("id") or uuid.uuid4().hex[:12]),
                            "path": path.replace("\\", "/"),
                            "original_name": str(
                                attachment.get("original_name") or Path(path).name
                            ),
                        }
                    )
            elif str(attachment).strip():
                path = str(attachment).strip().replace("\\", "/")
                normalized_attachments.append(
                    {
                        "id": uuid.uuid4().hex[:12],
                        "path": path,
                        "original_name": Path(path).name,
                    }
                )
        category = str(item.get("category") or "").strip() or "general"
        source = str(item.get("source") or "").strip() or "legacy_memory"
        status = str(item.get("confirmation_status") or "").strip() or "confirmed"
        return {
            **item,
            "id": memory_id,
            "character_id": str(item.get("character_id") or "").strip(),
            "category": category,
            "content": content,
            "source": source,
            "evidence": _clean_text(item.get("evidence"), 500),
            "created_at": created_at,
            "updated_at": updated_at,
            "confidence": _safe_float(item.get("confidence"), 0.75),
            "confirmation_status": status,
            "attachments": normalized_attachments,
            "tags": [str(tag) for tag in tags if str(tag).strip()],
            "conflict_group": str(item.get("conflict_group") or "").strip(),
            "epistemic_status": str(item.get("epistemic_status") or "reported"),
            "source_refs": list(item.get("source_refs") or []),
            "revisions": list(item.get("revisions") or []),
        }

    def _save_items_locked(self, items):
        save_json(self.memory_path, {"items": items})

    @staticmethod
    def _matches_character(item, character_id=None):
        """None 保留旧版全量接口；空字符串只匹配未归属的旧记忆。"""
        if character_id is None:
            return True
        return str((item or {}).get("character_id") or "").strip() == str(
            character_id
        ).strip()

    def list_memories(self, character_id=None):
        with self._lock:
            return [
                dict(item)
                for item in self._load_items_locked()
                if self._matches_character(item, character_id)
            ]

    def get_memory(self, memory_id, character_id=None):
        memory_id = str(memory_id or "").strip()
        with self._lock:
            return next(
                (
                    item
                    for item in self._load_items_locked()
                    if item["id"] == memory_id
                    and self._matches_character(item, character_id)
                ),
                None,
            )

    def add_memory(
        self,
        content,
        tags=None,
        category="general",
        source="manual",
        evidence="",
        confidence=1.0,
        confirmation_status="confirmed",
        attachments=None,
        character_id=None,
        source_refs=None,
        epistemic_status="reported",
    ):
        content = _clean_text(content, self.MAX_CONTENT)
        if not content:
            return None
        character_id = (
            None if character_id is None else str(character_id or "").strip()
        )
        with self._lock:
            items = self._load_items_locked()
            duplicate = next(
                (
                    item
                    for item in items
                    if item.get("content", "").casefold() == content.casefold()
                    and item.get("category") == category
                    and self._matches_character(item, character_id)
                ),
                None,
            )
            if duplicate is not None:
                # process_turn 可能已经为这次图片消息准备了附件；
                # 重复记忆不应留下无法关联到任何记忆的孤立文件。
                for attachment in attachments or []:
                    self._remove_attachment_locked(
                        attachment.get("path", "")
                        if isinstance(attachment, dict)
                        else attachment
                    )
                return dict(duplicate)
            item = {
                "id": "mem_{}".format(uuid.uuid4().hex[:16]),
                "character_id": character_id or "",
                "category": str(category or "general"),
                "content": content,
                "source": str(source or "manual"),
                "evidence": _clean_text(evidence, 500),
                "created_at": _now(),
                "updated_at": _now(),
                "confidence": _safe_float(confidence, 0.8),
                "confirmation_status": str(
                    confirmation_status or "confirmed"
                ),
                "attachments": list(attachments or []),
                "tags": [str(tag) for tag in (tags or []) if str(tag).strip()],
                "conflict_group": "",
                "source_refs": list(source_refs or []),
                "epistemic_status": epistemic_status,
                "revisions": [],
            }
            self._mark_conflicts_locked(items, item)
            items.append(item)
            self._save_items_locked(items)
            return dict(item)

    def _conflict_key(self, item):
        category = str(item.get("category") or "general")
        content = str(item.get("content") or "")
        if category != "preference":
            return ""
        markers = (
            "我喜欢",
            "我不喜欢",
            "我偏好",
            "我习惯",
            "默认使用",
            "默认用",
            "请用",
            "称呼我",
            "叫我",
            "我常用",
            "我更喜欢",
            "我希望你",
        )
        for marker in markers:
            if marker in content:
                prefix = content.split(marker, 1)[0].strip()
                return "{}:{}".format(category, prefix[:40])
        return "{}:{}".format(category, content[:24])

    def _mark_conflicts_locked(self, items, new_item):
        new_key = self._conflict_key(new_item)
        if not new_key:
            return
        related = [
            item
            for item in items
            if item.get("id") != new_item.get("id")
            and item.get("confirmation_status") not in ("superseded", "retracted")
            and self._matches_character(
                item, str(new_item.get("character_id") or "")
            )
            and self._conflict_key(item) == new_key
            and item.get("content", "").casefold()
            != new_item.get("content", "").casefold()
        ]
        if not related:
            return
        group = next(
            (str(item.get("conflict_group") or "") for item in related if item.get("conflict_group")),
            "",
        ) or "conflict_{}".format(uuid.uuid4().hex[:12])
        new_item["conflict_group"] = group
        new_item["confirmation_status"] = "pending_conflict"
        for item in related:
            item["conflict_group"] = group
            item["confirmation_status"] = "pending_conflict"
            item["updated_at"] = _now()

    def update_memory(self, memory_id, content=None, character_id=None, **changes):
        memory_id = str(memory_id or "").strip()
        with self._lock:
            items = self._load_items_locked()
            target = next(
                (
                    item
                    for item in items
                    if item["id"] == memory_id
                    and self._matches_character(item, character_id)
                ),
                None,
            )
            if target is None:
                return None
            if content is not None:
                value = _clean_text(content, self.MAX_CONTENT)
                if not value:
                    return None
                target.setdefault("revisions", []).append({"content": target["content"],
                    "confirmation_status": target.get("confirmation_status"), "updated_at": _now()})
                target["content"] = value
            for key in (
                "category",
                "source",
                "evidence",
                "confirmation_status",
                "conflict_group",
                "epistemic_status",
            ):
                if key in changes and changes[key] is not None:
                    target[key] = str(changes[key])
            if "confidence" in changes:
                target["confidence"] = _safe_float(changes["confidence"])
            if "tags" in changes and isinstance(changes["tags"], list):
                target["tags"] = [str(tag) for tag in changes["tags"]]
            target["updated_at"] = _now()
            if content is not None and changes.get("confirmation_status") is None:
                target["confirmation_status"] = "confirmed"
            self._save_items_locked(items)
            return dict(target)

    def delete_memory(self, memory_id, remove_attachments=True, character_id=None):
        memory_id = str(memory_id or "").strip()
        with self._lock:
            items = self._load_items_locked()
            target = next(
                (
                    item
                    for item in items
                    if item["id"] == memory_id
                    and self._matches_character(item, character_id)
                ),
                None,
            )
            if target is None:
                return False
            if remove_attachments:
                for attachment in target.get("attachments", []):
                    self._remove_attachment_locked(attachment.get("path", ""))
            items = [item for item in items if item["id"] != memory_id]
            self._save_items_locked(items)
            return True

    def _remove_attachment_locked(self, relative_path):
        if not relative_path:
            return False
        candidate = (ROOT / str(relative_path)).resolve()
        image_root = self.image_dir.resolve()
        try:
            candidate.relative_to(image_root)
        except ValueError:
            return False
        if candidate.is_file():
            candidate.unlink()
            return True
        return False

    def _copy_attachment_locked(self, source, memory_id):
        source_path = Path(source)
        if not source_path.is_file():
            return None
        suffix = source_path.suffix.lower() or ".img"
        destination = self.image_dir / "{}{}".format(memory_id, suffix)
        if source_path.resolve() != destination.resolve():
            shutil.copy2(str(source_path), str(destination))
        return {
            "id": uuid.uuid4().hex[:12],
            "path": destination.relative_to(ROOT).as_posix(),
            "original_name": source_path.name,
        }

    def _should_retain_image(self, user_text, explicit, candidate_content):
        text = "{} {}".format(user_text or "", candidate_content or "")
        durable = bool(_PROJECT_RE.search(text) or "长期" in text or "以后" in text)
        return bool(explicit or durable)

    def _category_for(self, content):
        if _COMMITMENT_RE.search(content):
            return "commitment"
        if _PREFERENCE_RE.search(content):
            return "preference"
        if _PROJECT_RE.search(content):
            return "project"
        if _PROFILE_RE.search(content):
            return "profile"
        return "general"

    def parse_memory_command(self, user_text):
        text = _clean_text(user_text, self.MAX_CONTENT)
        match = _EXPLICIT_FORGET_RE.match(text)
        if match:
            return {"kind": "forget", "query": _clean_text(match.group(1), 300)}
        match = _EXPLICIT_REMEMBER_RE.match(text)
        if match:
            return {"kind": "remember", "content": _clean_text(match.group(1), 500)}
        return None

    def _candidate_from_text(self, user_text, vision_summary=""):
        text = _clean_text(user_text, self.MAX_CONTENT)
        command = self.parse_memory_command(text)
        explicit = bool(command and command.get("kind") == "remember")
        candidate = command.get("content", "") if explicit else text
        if explicit and not candidate and vision_summary:
            candidate = "图片摘要：{}".format(_clean_text(vision_summary, 360))
        if _SENSITIVE_RE.search(candidate):
            return None
        if re.search(r"开玩笑|假如|假设|也许|可能|我猜|说不定", candidate):
            return None
        if _REALTIME_RE.search(candidate) or _TEMPORARY_RE.search(candidate):
            return None
        if _TASK_RE.search(candidate) and not explicit:
            return None
        if not candidate or len(candidate) < 4:
            return None
        stable = explicit or bool(
            _PREFERENCE_RE.search(candidate)
            or _PROJECT_RE.search(candidate)
            or _PROFILE_RE.search(candidate)
            or _COMMITMENT_RE.search(candidate)
        )
        if not stable:
            return None
        category = self._category_for(candidate)
        return {
            "content": candidate,
            "category": category,
            "source": "explicit_user" if explicit else "user_message",
            "evidence": text,
            "confidence": 0.98 if explicit else 0.84,
            "confirmation_status": "confirmed" if explicit else "inferred",
            "explicit": explicit,
        }

    def process_turn(
        self,
        user_text,
        assistant_text="",
        image_path=None,
        vision_reply=False,
        character_id=None,
        source_refs=None,
    ):
        """回复完成后调用，返回本次新增的记忆；不会保存普通闲聊。"""
        user_text = _clean_text(user_text, self.MAX_CONTENT)
        correction = explicit_correction(user_text)
        retracted = retracted_claim(user_text)
        if correction or retracted:
            old, new = correction if correction else (retracted, "")
            replacement = new
            with self._lock:
                items = self._load_items_locked()
                for item in items:
                    if self._matches_character(item, character_id) and old in item["content"]:
                        replacement = item["content"].replace(old, new) if new else ""
                        item.setdefault("revisions", []).append({"content": item["content"],
                            "confirmation_status": item["confirmation_status"], "updated_at": _now()})
                        item.update(confirmation_status="retracted" if retracted else "superseded", corrected_to=new,
                                    correction_evidence=user_text, updated_at=_now())
                self._save_items_locked(items)
            candidate = self._candidate_from_text(replacement) if replacement else None
        else:
            candidate = None
        vision_summary = _clean_text(assistant_text, 360) if vision_reply else ""
        if not correction and not retracted:
            candidate = self._candidate_from_text(user_text, vision_summary)
        if candidate is None:
            return []
        attachments = []
        with self._lock:
            if image_path and self._should_retain_image(
                user_text,
                candidate["explicit"],
                candidate["content"],
            ):
                memory_id = "mem_{}".format(uuid.uuid4().hex[:16])
                attachment = self._copy_attachment_locked(image_path, memory_id)
                if attachment:
                    attachments.append(attachment)
        item = self.add_memory(
            candidate["content"],
            category=candidate["category"],
            source=candidate["source"],
            evidence=candidate["evidence"],
            confidence=candidate["confidence"],
            confirmation_status=candidate["confirmation_status"],
            attachments=attachments,
            character_id=character_id,
            source_refs=source_refs,
            epistemic_status="corrected" if correction else "reported",
        )
        return [item] if item else []

    def forget(self, query, character_id=None):
        query = _clean_text(query, 300)
        with self._lock:
            items = self._load_items_locked()
            if not query:
                return 0
            query_words = _keywords(query)
            matched = []
            for item in items:
                if not self._matches_character(item, character_id):
                    continue
                content = str(item.get("content", ""))
                if query.casefold() in content.casefold() or (
                    query_words
                    and all(word.casefold() in content.casefold() for word in query_words)
                ):
                    matched.append(item)
            for item in matched:
                for attachment in item.get("attachments", []):
                    self._remove_attachment_locked(attachment.get("path", ""))
            if matched:
                removed_ids = {item["id"] for item in matched}
                self._save_items_locked(
                    [item for item in items if item["id"] not in removed_ids]
                )
            return len(matched)

    def search(self, query, limit=DEFAULT_CONTEXT_ITEMS, character_id=None):
        query = _clean_text(query, 300)
        words = _keywords(query)
        if not words:
            return []
        with self._lock:
            items = [
                item
                for item in self._load_items_locked()
                if self._matches_character(item, character_id)
            ]
        scored = []
        for item in items:
            haystack = "{} {} {}".format(
                item.get("content", ""),
                item.get("category", ""),
                " ".join(item.get("tags", [])),
            ).lower()
            score = sum(1 for word in words if word.lower() in haystack)
            if query.casefold() in str(item.get("content", "")).casefold():
                score += 3
            if score:
                scored.append((score, str(item.get("updated_at", "")), item))
        scored.sort(key=lambda value: (value[0], value[1]), reverse=True)
        return [dict(item) for _score, _updated, item in scored[: max(0, int(limit))]]

    def format_context(
        self,
        query,
        limit=None,
        char_budget=None,
        character_id=None,
    ):
        limit = self.DEFAULT_CONTEXT_ITEMS if limit is None else max(0, int(limit))
        char_budget = (
            self.DEFAULT_CONTEXT_CHARS
            if char_budget is None
            else max(200, int(char_budget))
        )
        items = self.search(
            query,
            limit=limit,
            character_id=character_id,
        )
        if not items:
            return ""
        lines = [
            "长期记忆（仅用于个人/项目历史；不是当前日期、时间或节假日事实）："
        ]
        used = len(lines[0])
        for item in items:
            status = item.get("confirmation_status", "confirmed")
            if status in ("superseded", "retracted"):
                continue
            prefix = "待确认冲突" if status == "pending_conflict" else status
            line = "- [{}|{}|{}] {}".format(
                item.get("category", "general"),
                prefix,
                item.get("epistemic_status", "reported"),
                item.get("content", ""),
            )
            if used + len(line) + 1 > char_budget:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines) if len(lines) > 1 else ""

    def export_view(self, character_id=None):
        return self.list_memories(character_id=character_id)
