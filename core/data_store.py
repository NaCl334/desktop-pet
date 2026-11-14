from datetime import datetime
import copy
import shutil
import uuid
from pathlib import Path
import re

from core.config import ROOT, load_json, save_json
from core.memory import MemoryStore
from core.history_memory import HistoryMemoryStore


class DataStore(object):
    """保存本地设置、长期记忆和角色聊天记录。"""

    def __init__(self):
        self.data_dir = ROOT / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.data_dir / "chat_history.json"
        self.conversations_path = self.data_dir / "conversations.json"
        self.memory_path = self.data_dir / "memory.json"
        self.memory_store = MemoryStore(self.memory_path)
        self.history_memory = HistoryMemoryStore(self.data_dir / "history_summaries")
        self.settings_path = self.data_dir / "settings.json"
        self.settings = load_json(
            self.settings_path,
            {
                "theme": "pink",
                "always_on_top": True,
                "mouse_passthrough": False,
                "voice_output": True,
                "voice_volume": 50,
                "voice_volume_scale": "boosted_v2",
            },
        )
        self.settings.pop("voice_input", None)
        if self.settings.get("setup_profile") == "asr":
            self.settings["setup_profile"] = "desktop"
        # 新音量刻度中 50% 等于旧版 100%。保留用户原有听感，避免升级后
        # 直接把所有旧设置放大为两倍。
        if self.settings.get("voice_volume_scale") != "boosted_v2":
            try:
                old_volume = max(
                    0,
                    min(
                        100,
                        int(round(float(self.settings.get("voice_volume", 100)))),
                    ),
                )
            except (TypeError, ValueError):
                old_volume = 100
            self.settings["voice_volume"] = int(round(old_volume / 2.0))
            self.settings["voice_volume_scale"] = "boosted_v2"
        try:
            self.settings["voice_volume"] = max(
                0,
                min(100, int(round(float(self.settings.get("voice_volume", 50))))),
            )
        except (TypeError, ValueError):
            self.settings["voice_volume"] = 50
        save_json(self.settings_path, self.settings)
        self.history = self._load_recent_history()
        self.conversations = load_json(self.conversations_path, [])
        if not isinstance(self.conversations, list):
            self.conversations = []
        self.memory = {"items": self.memory_store.list_memories()}
        self.current_conversation_id = ""
        self.current_conversation_name = ""
        self.current_character_id = ""
        self.current_character_name = ""
        active_ids = self.settings.get("active_conversation_ids")
        if not isinstance(active_ids, dict):
            active_ids = {}
        self.settings["active_conversation_ids"] = {
            str(key): str(value or "").strip()
            for key, value in active_ids.items()
            if str(key or "").strip() and str(value or "").strip()
        }
        self.restore_current_conversation()

    @staticmethod
    def _normalize_character_id(character_id):
        return None if character_id is None else str(character_id or "").strip()

    def _set_current_conversation(self, conversation, save=True, character_id=None):
        """只切换到已有会话，不创建会话。"""
        if not isinstance(conversation, dict):
            return None
        conversation_id = str(conversation.get("id", "") or "").strip()
        if not conversation_id:
            return None
        character_id = self._normalize_character_id(character_id)
        conversation_character_id = str(
            conversation.get("character_id", "") or ""
        ).strip()
        if conversation_character_id == "mixed" and character_id is not None:
            return None
        if (
            character_id is not None
            and conversation_character_id not in ("", character_id)
        ):
            return None
        self.current_conversation_id = conversation_id
        self.current_conversation_name = str(
            conversation.get("name", conversation_id) or conversation_id
        ).strip()
        self.current_character_id = str(
            conversation.get("character_id", "") or ""
        ).strip()
        self.current_character_name = str(
            conversation.get("character_name", "") or ""
        ).strip()
        if save:
            self.settings["current_conversation_id"] = conversation_id
            owner = self.current_character_id or self.current_character_name
            if owner:
                self.settings.setdefault("active_conversation_ids", {})[
                    owner
                ] = conversation_id
            save_json(self.settings_path, self.settings)
        return dict(conversation)

    def _conversation_summary_for_id(self, conversation_id, character_id=None):
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return None
        character_id = self._normalize_character_id(character_id)
        for conversation in self.list_conversations(character_id=character_id):
            if str(conversation.get("id", "")) == conversation_id:
                return conversation
        return None

    def restore_current_conversation(self, character_id=None, allow_recent_fallback=False):
        """恢复持久化的活动会话；默认不因启动而自动切换到其他会话。"""
        character_id = self._normalize_character_id(character_id)
        if character_id is None:
            saved_id = str(
                self.settings.get("current_conversation_id", "") or ""
            ).strip()
        else:
            saved_id = str(
                self.settings.get("active_conversation_ids", {}).get(
                    character_id, ""
                )
                or ""
            ).strip()
        conversation = self._conversation_summary_for_id(
            saved_id,
            character_id=character_id,
        )
        if conversation is None and allow_recent_fallback:
            conversations = self.list_conversations(character_id=character_id)
            conversation = conversations[0] if conversations else None
        if conversation is None:
            self.current_conversation_id = ""
            self.current_conversation_name = ""
            if character_id is not None:
                self.current_character_id = character_id
                self.current_character_name = ""
            return None
        return self._set_current_conversation(
            conversation,
            character_id=character_id,
        )

    def activate_conversation(self, conversation_id, character_id=None):
        """激活一个已有会话，返回摘要；找不到时不创建新会话。"""
        conversation = self._conversation_summary_for_id(
            conversation_id,
            character_id=character_id,
        )
        return self._set_current_conversation(
            conversation,
            character_id=character_id,
        )

    @staticmethod
    def _conversation_date_key(conversation):
        conversation_id = str((conversation or {}).get("id", "") or "").strip()
        match = re.match(r"^(20\d{2}-\d{2}-\d{2})-\d+$", conversation_id)
        if match:
            return match.group(1)
        created_at = str((conversation or {}).get("created_at", "") or "").strip()
        match = re.match(r"^(20\d{2}-\d{2}-\d{2})", created_at)
        return match.group(1) if match else ""

    @staticmethod
    def _conversation_sequence(conversation, date_key):
        conversation_id = str((conversation or {}).get("id", "") or "").strip()
        match = re.match(
            r"^{}-(\d+)$".format(re.escape(str(date_key))),
            conversation_id,
        )
        if match:
            return int(match.group(1))
        return None

    def conversations_on_date(self, date_value, character_id=None):
        """返回指定日期的会话，并标注其创建序号。"""
        date_key = str(date_value or "").strip()
        if not re.match(r"^20\d{2}-\d{2}-\d{2}$", date_key):
            return []
        matches = [
            dict(item)
            for item in self.list_conversations(character_id=character_id)
            if self._conversation_date_key(item) == date_key
        ]
        matches.sort(
            key=lambda item: (
                str(item.get("created_at", "") or ""),
                str(item.get("id", "") or ""),
            )
        )
        for index, item in enumerate(matches, 1):
            sequence = self._conversation_sequence(item, date_key)
            item["date_occurrence"] = sequence if sequence is not None else index
        return matches

    def resolve_conversation_on_date(
        self,
        date_value,
        occurrence=None,
        character_id=None,
    ):
        """按日期和可选的第几次会话解析，绝不在歧义时猜测。"""
        matches = self.conversations_on_date(
            date_value,
            character_id=character_id,
        )
        if occurrence is not None:
            try:
                wanted = int(occurrence)
            except (TypeError, ValueError):
                wanted = 0
            selected = next(
                (
                    item for item in matches
                    if int(item.get("date_occurrence", 0)) == wanted
                ),
                None,
            )
            return {
                "status": "ok" if selected is not None else "not_found",
                "conversation": selected,
                "matches": matches,
            }
        if len(matches) == 1:
            return {
                "status": "ok",
                "conversation": matches[0],
                "matches": matches,
            }
        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "conversation": None,
                "matches": matches,
            }
        return {
            "status": "not_found",
            "conversation": None,
            "matches": [],
        }

    def _load_recent_history(self):
        records = load_json(self.history_path, [])
        return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []

    def merge_character_conversations(self):
        """Merge known owners without guessing ownership of unidentified messages."""
        summaries = self.list_conversations()
        by_id = {str(item["id"]): item for item in summaries}
        groups = {}
        for item in summaries:
            owner = str(item.get("character_id") or "").strip()
            if owner and owner != "mixed":
                groups.setdefault(owner, []).append(item)
        for record in self.history:
            owner = str(record.get("character_id") or "").strip()
            if owner and owner != "mixed":
                groups.setdefault(owner, [])
        canonical = {}
        active = self.settings.get("active_conversation_ids", {})
        for owner, items in groups.items():
            reusable = [item for item in items if not str(item["id"]).startswith("legacy-")]
            chosen = next((item for item in reusable if item["id"] == active.get(owner)), None)
            chosen = chosen or (reusable[-1] if reusable else None)
            canonical[owner] = copy.deepcopy(chosen) if chosen else {
                "id": "character-" + uuid.uuid5(uuid.NAMESPACE_URL, owner).hex,
                "character_id": owner,
            }
            canonical[owner]["character_id"] = owner
            canonical[owner]["message_count"] = 0
        history = copy.deepcopy(self.history)
        used_legacy_ids = set()
        for record in history:
            source_id = str(record.get("conversation_id") or "legacy-" + str(record.get("created_at") or "unknown")[:10])
            summary = by_id.get(source_id, {}) if record.get("conversation_id") else {}
            owner = str(record.get("character_id") or summary.get("character_id") or "").strip()
            if owner not in canonical:
                used_legacy_ids.add(source_id)
                continue
            target = canonical[owner]
            name = str(target.get("character_name") or record.get("character_name") or owner)
            target["character_name"] = name
            target["name"] = name
            record.update(conversation_id=target["id"], conversation_name=name, character_id=owner)
            target["message_count"] += 1
            created = str(record.get("created_at") or "")
            if created:
                target["created_at"] = min(str(target.get("created_at") or created), created)
                target["updated_at"] = max(str(target.get("updated_at") or created), created)
        for owner, target in canonical.items():
            target["character_name"] = str(target.get("character_name") or owner)
            target["name"] = target["character_name"]
        unresolved = [copy.deepcopy(item) for item in self.conversations
                      if str(by_id.get(str(item.get("id")), item).get("character_id") or "").strip() not in canonical
                      and (by_id.get(str(item.get("id")), item).get("character_id") != "mixed"
                           or item.get("id") in used_legacy_ids)]
        conversations = sorted(unresolved + list(canonical.values()), key=lambda item: str(item.get("id") or ""))
        history.sort(key=lambda item: str(item.get("created_at") or ""))
        settings = copy.deepcopy(self.settings)
        settings["active_conversation_ids"] = {owner: item["id"] for owner, item in canonical.items()}
        current = by_id.get(str(settings.get("current_conversation_id") or ""), {})
        owner = str(current.get("character_id") or "")
        if owner in canonical:
            settings["current_conversation_id"] = canonical[owner]["id"]
        if history == self.history and conversations == self.conversations and settings == self.settings:
            return {"changed": False, "characters": len(canonical)}
        # Keep exact originals before committing the three related files.
        backup = self.data_dir / "backups" / ("single_character_history_" + uuid.uuid4().hex)
        backup.mkdir(parents=True)
        paths = (self.history_path, self.conversations_path, self.settings_path)
        existing = {path for path in paths if path.exists()}
        for path in existing:
            shutil.copy2(path, backup / path.name)
        try:
            for path, value in zip(paths, (history, conversations, settings)):
                save_json(path, value)
        except OSError:
            for path in paths:
                if path in existing:
                    shutil.copy2(backup / path.name, path)
                elif path.exists():
                    path.unlink()
            raise
        self.history, self.conversations, self.settings = history, conversations, settings
        self.restore_current_conversation()
        return {"changed": True, "characters": len(canonical), "backup": str(backup)}

    def ensure_character_conversation(self, character_id, character_name):
        """Resume the owner's continuous conversation, creating only on first use."""
        items = self.list_conversations(character_id=character_id)
        if len(items) > 1:
            self.merge_character_conversations()
            items = self.list_conversations(character_id=character_id)
        if items:
            return self._set_current_conversation(items[0], character_id=character_id)
        return self.start_new_conversation(character_id, character_name)

    def add_message(
        self,
        role,
        content,
        conversation_id=None,
        conversation_name=None,
        character_id=None,
        character_name=None,
        screen_image_path=None,
    ):
        now = datetime.now()
        conversation_id = conversation_id or self.current_conversation_id
        conversation_name = conversation_name or self.current_conversation_name
        character_id = character_id or self.current_character_id
        character_name = character_name or self.current_character_name
        character_id = str(character_id or "").strip()
        character_name = str(character_name or "").strip()
        if not str(conversation_id or "").strip():
            # 不写入无归属消息；由聊天入口负责创建或打开会话。
            return False
        conversation = next(
            (
                item for item in self.conversations
                if item.get("id") == conversation_id
            ),
            None,
        )
        if conversation is not None:
            owner_id, owner_name = self.get_conversation_character(conversation_id)
            if owner_id == "mixed" or (owner_id and owner_id != character_id):
                return False
            if not owner_id and character_id:
                conversation["character_id"] = character_id
            if not owner_name and character_name:
                conversation["character_name"] = character_name
        if conversation is None and conversation_id == self.current_conversation_id:
            conversation = {
                "id": conversation_id,
                "name": conversation_name,
                "created_at": now.isoformat(timespec="seconds"),
                "updated_at": now.isoformat(timespec="seconds"),
                "message_count": 0,
                "character_id": character_id,
                "character_name": character_name,
            }
            self.conversations.append(conversation)
        if conversation is None:
            # 不允许向一个不存在或不属于当前角色的会话写消息。
            return False
        record = {
            "id": "msg_" + uuid.uuid4().hex,
            "created_at": now.isoformat(timespec="seconds"),
            "role": role,
            "content": content,
            "conversation_id": conversation_id,
            "conversation_name": conversation_name,
            "character_id": character_id,
            "character_name": character_name,
        }
        if role == "user" and screen_image_path:
            record["screen_image_path"] = str(screen_image_path)
        self.history.append(record)
        save_json(self.history_path, self.history)
        conversation["message_count"] = int(conversation.get("message_count", 0)) + 1
        conversation["updated_at"] = record["created_at"]
        save_json(self.conversations_path, self.conversations)
        return True

    def start_new_conversation(self, character_id=None, character_name=None):
        """创建并激活一个空白新对话，会话编号为日期加当天序号。"""
        now = datetime.now()
        character_id = str(character_id or self.current_character_id or "").strip()
        character_name = str(
            character_name or self.current_character_name or ""
        ).strip()
        date_key = now.strftime("%Y-%m-%d")
        counters = self.settings.setdefault("conversation_daily_counts", {})
        existing_ids = {
            str(item.get("id"))
            for item in self.conversations
            if str(item.get("id", "")).startswith(date_key + "-")
        }
        try:
            count = int(counters.get(date_key, 0)) + 1
        except (TypeError, ValueError):
            count = 1
        while "{}-{:02d}".format(date_key, count) in existing_ids:
            count += 1
        counters[date_key] = count
        conversation_name = "{}-{:02d}".format(date_key, count)
        conversation = {
            "id": conversation_name,
            "name": conversation_name,
            "created_at": now.isoformat(timespec="seconds"),
            "updated_at": now.isoformat(timespec="seconds"),
            "message_count": 0,
            "character_id": character_id,
            "character_name": character_name,
        }
        self.conversations.append(conversation)
        save_json(self.conversations_path, self.conversations)
        self.current_conversation_id = conversation_name
        self.current_conversation_name = conversation_name
        self.current_character_id = character_id
        self.current_character_name = character_name
        self.settings["current_conversation_id"] = conversation_name
        if character_id:
            self.settings.setdefault("active_conversation_ids", {})[
                character_id
            ] = conversation_name
        save_json(self.settings_path, self.settings)
        return conversation

    def get_current_messages(self, character_id=None):
        if character_id is not None:
            owner_id, _owner_name = self.get_conversation_character(
                self.current_conversation_id
            )
            expected = str(character_id or "").strip()
            if owner_id != expected:
                return []
        return [
            record
            for record in self.history
            if record.get("conversation_id") == self.current_conversation_id
        ]

    def get_character_messages(self, character_id):
        """Return owned originals in chronological order, independently of dates."""
        def timestamp(record):
            try:
                return datetime.fromisoformat(str(record.get("created_at", ""))).timestamp()
            except (ValueError, TypeError, OverflowError, OSError):
                return float("-inf")
        return sorted(HistoryMemoryStore.owned(list(self.history), character_id),
                      key=timestamp)

    def list_conversations(self, character_id=None):
        """返回新旧消息记录合并后的会话摘要，按最近更新时间倒序。"""
        character_id = self._normalize_character_id(character_id)
        groups = {}
        for conversation in self.conversations:
            conversation_id = conversation.get("id")
            if conversation_id:
                group = dict(conversation)
                # 历史记录是消息数的事实来源；避免把持久化计数再次叠加。
                group["message_count"] = 0
                groups[conversation_id] = group
        for record in self.history:
            created_at = record.get("created_at", "")
            conversation_id = record.get("conversation_id")
            if not conversation_id:
                conversation_id = "legacy-{}".format(str(created_at)[:10] or "unknown")
            group = groups.setdefault(
                conversation_id,
                {
                    "id": conversation_id,
                    "name": "历史记录 {}".format(str(created_at)[:10] or "未知日期"),
                    "created_at": created_at,
                    "updated_at": created_at,
                    "message_count": 0,
                },
            )
            group["message_count"] = int(group.get("message_count", 0)) + 1
            if created_at > str(group.get("updated_at", "")):
                group["updated_at"] = created_at
        for group in groups.values():
            self._resolve_conversation_character(group)
        if character_id is not None:
            groups = {
                conversation_id: group
                for conversation_id, group in groups.items()
                if str(group.get("character_id", "") or "").strip()
                == character_id
            }
        return sorted(
            groups.values(),
            key=lambda item: str(item.get("updated_at", item.get("created_at", ""))),
            reverse=True,
        )

    def get_conversation_character(self, conversation_id):
        conversation = next(
            (
                item for item in self.conversations
                if item.get("id") == conversation_id
            ),
            None,
        )
        group = dict(conversation or {"id": conversation_id})
        self._resolve_conversation_character(group)
        return group.get("character_id", ""), group.get("character_name", "")

    def _resolve_conversation_character(self, conversation):
        conversation_id = conversation.get("id")
        character_ids = []
        character_names = []

        def collect(value, target):
            value = str(value or "").strip()
            if value and value not in target:
                target.append(value)

        collect(conversation.get("character_id"), character_ids)
        collect(conversation.get("character_name"), character_names)
        for record in self.history:
            if record.get("conversation_id") != conversation_id:
                continue
            collect(record.get("character_id"), character_ids)
            collect(record.get("character_name"), character_names)

        if len(character_ids) > 1:
            conversation["character_id"] = "mixed"
            conversation["character_name"] = "多角色"
            return
        if character_ids:
            conversation["character_id"] = character_ids[0]
        if character_names:
            conversation["character_name"] = character_names[0]
        elif character_ids:
            conversation["character_name"] = character_ids[0]

    def get_conversation_messages(self, conversation_id, character_id=None):
        if character_id is not None:
            owner_id, _owner_name = self.get_conversation_character(
                conversation_id
            )
            if owner_id != str(character_id or "").strip():
                return []
        if str(conversation_id).startswith("legacy-"):
            date_key = str(conversation_id)[7:]
            return [
                record
                for record in self.history
                if (
                    (
                        not record.get("conversation_id")
                        and str(record.get("created_at", "")).startswith(date_key)
                    )
                    or record.get("conversation_id") == conversation_id
                )
            ]
        return [
            record
            for record in self.history
            if record.get("conversation_id") == conversation_id
        ]

    def rename_conversation(self, conversation_id, name, character_id=None):
        """修改会话显示名称，不改变会话 ID 和消息内容。"""
        name = str(name or "").strip() or "未命名对话"
        if character_id is not None:
            owner_id, _owner_name = self.get_conversation_character(
                conversation_id
            )
            if owner_id != str(character_id or "").strip():
                return False
        for conversation in self.conversations:
            if conversation.get("id") == conversation_id:
                conversation["name"] = name
                save_json(self.conversations_path, self.conversations)
                return True
        return False

    def delete_conversation(self, conversation_id, character_id=None):
        """删除一段对话；legacy 会话也走同一套批量删除规则。"""
        return (
            self.delete_conversations(
                [conversation_id],
                character_id=character_id,
            )
            > 0
        )

    def delete_conversations(self, conversation_ids, character_id=None):
        """删除多段会话并返回实际删除的会话数。

        历史数据既可能只有会话摘要，也可能只有消息；旧版本消息还没有
        ``conversation_id``，需要按 ``legacy-YYYY-MM-DD`` 归入会话。
        """
        if isinstance(conversation_ids, (str, bytes)):
            conversation_ids = (conversation_ids,)
        requested_ids = {
            str(conversation_id or "").strip()
            for conversation_id in (conversation_ids or ())
            if str(conversation_id or "").strip()
        }
        if not requested_ids:
            return 0

        expected_character_id = (
            None
            if character_id is None
            else str(character_id or "").strip()
        )
        legacy_dates = {
            conversation_id[7:]
            for conversation_id in requested_ids
            if conversation_id.startswith("legacy-")
            and conversation_id[7:]
        }
        actual_ids = set()

        def character_matches(value):
            return (
                expected_character_id is None
                or str(value or "").strip() == expected_character_id
            )

        for conversation in self.conversations:
            conversation_id = str(
                conversation.get("id", "") or ""
            ).strip()
            if conversation_id not in requested_ids:
                continue
            owner_id, _owner_name = self.get_conversation_character(
                conversation_id
            )
            if character_matches(owner_id):
                actual_ids.add(conversation_id)

        for record in self.history:
            conversation_id = str(
                record.get("conversation_id", "") or ""
            ).strip()
            if conversation_id:
                if (
                    conversation_id in requested_ids
                    and character_matches(record.get("character_id"))
                ):
                    actual_ids.add(conversation_id)
                continue
            if not character_matches(record.get("character_id")):
                continue
            created_at = str(record.get("created_at", "") or "")
            for date_key in legacy_dates:
                if created_at.startswith(date_key):
                    actual_ids.add("legacy-{}".format(date_key))
                    break

        if not actual_ids:
            return 0

        deleted_legacy_dates = {
            conversation_id[7:]
            for conversation_id in actual_ids
            if conversation_id.startswith("legacy-")
        }

        def record_is_deleted(record):
            conversation_id = str(
                record.get("conversation_id", "") or ""
            ).strip()
            if conversation_id:
                return conversation_id in actual_ids
            created_at = str(record.get("created_at", "") or "")
            return any(
                created_at.startswith(date_key)
                for date_key in deleted_legacy_dates
            )

        before_conversations = len(self.conversations)
        before_history = len(self.history)
        self.conversations = [
            item
            for item in self.conversations
            if str(item.get("id", "") or "").strip() not in actual_ids
        ]
        self.history = [
            record for record in self.history
            if not record_is_deleted(record)
        ]
        changed = (
            len(self.conversations) != before_conversations
            or len(self.history) != before_history
        )
        current_deleted = self.current_conversation_id in actual_ids
        if current_deleted:
            self.current_conversation_id = ""
            self.current_conversation_name = ""
            self.current_character_id = ""
            self.current_character_name = ""
            self.settings["current_conversation_id"] = ""

        active_ids = self.settings.get("active_conversation_ids", {})
        active_ids_changed = False
        if isinstance(active_ids, dict):
            for owner_id, active_id in list(active_ids.items()):
                if str(active_id or "").strip() in actual_ids:
                    active_ids.pop(owner_id, None)
                    active_ids_changed = True

        if changed:
            save_json(self.history_path, self.history)
            save_json(self.conversations_path, self.conversations)
        if current_deleted or active_ids_changed:
            save_json(self.settings_path, self.settings)
        return len(actual_ids)

    def add_memory(self, content, tags=None, character_id=None):
        item = self.memory_store.add_memory(
            content,
            tags=tags,
            source="legacy_add_memory",
            category="general",
            evidence=str(content or ""),
            confidence=1.0,
            confirmation_status="confirmed",
            character_id=character_id,
        )
        self.memory = {"items": self.memory_store.list_memories()}
        return item

    def list_memories(self, character_id=None):
        self.memory = {
            "items": self.memory_store.list_memories(character_id=character_id)
        }
        return self.memory["items"]

    def get_memory(self, memory_id, character_id=None):
        return self.memory_store.get_memory(memory_id, character_id=character_id)

    def update_memory(self, memory_id, content=None, character_id=None, **changes):
        item = self.memory_store.update_memory(
            memory_id,
            content=content,
            character_id=character_id,
            **changes
        )
        self.memory = {
            "items": self.memory_store.list_memories(character_id=character_id)
        }
        return item

    def delete_memory(
        self,
        memory_id,
        remove_attachments=True,
        character_id=None,
    ):
        removed = self.memory_store.delete_memory(
            memory_id,
            remove_attachments=remove_attachments,
            character_id=character_id,
        )
        self.memory = {
            "items": self.memory_store.list_memories(character_id=character_id)
        }
        return removed

    def search_memories(self, query, limit=4, character_id=None):
        return self.memory_store.search(
            query,
            limit=limit,
            character_id=character_id,
        )

    def memory_context(
        self,
        query,
        limit=4,
        char_budget=1400,
        character_id=None,
    ):
        return self.memory_store.format_context(
            query,
            limit=limit,
            char_budget=char_budget,
            character_id=character_id,
        )

    def process_memory_turn(
        self,
        user_text,
        assistant_text="",
        image_path=None,
        vision_reply=False,
        character_id=None,
        source_refs=None,
    ):
        from core.history_memory import source_id
        if source_refs is None:
            originals = self.get_character_messages(character_id) if character_id else []
            source_refs = [source_id(record) for record in originals[-2:]]
        items = self.memory_store.process_turn(
            user_text,
            assistant_text=assistant_text,
            image_path=image_path,
            vision_reply=vision_reply,
            character_id=character_id,
            source_refs=source_refs,
        )
        if items:
            self.memory = {
                "items": self.memory_store.list_memories(
                    character_id=character_id
                )
            }
        return items

    def parse_memory_command(self, user_text):
        return self.memory_store.parse_memory_command(user_text)

    def forget_memories(self, query, character_id=None):
        count = self.memory_store.forget(query, character_id=character_id)
        self.memory = {
            "items": self.memory_store.list_memories(character_id=character_id)
        }
        return count

    def save_settings(self, settings):
        self.settings.update(settings)
        save_json(self.settings_path, self.settings)
