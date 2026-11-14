import json
import copy
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from core.config import ROOT


TASKS_PATH = ROOT / "data" / "tasks.json"


class TaskStore(object):
    """保存桌宠任务提醒，并在到时只触发一次。"""

    _store_lock = threading.RLock()
    _recent_batch_window = timedelta(minutes=30)

    def __init__(self, path=TASKS_PATH):
        self.path = Path(path)
        self.tasks = self._load()
        self._revision = 0

    def _load(self):
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, list) else []
        except (OSError, ValueError):
            return []

    def _save(self):
        with self._store_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(self.tasks, handle, ensure_ascii=False, indent=2)
            temporary.replace(self.path)
            self._revision += 1

    @property
    def revision(self):
        with self._store_lock:
            return self._revision

    @staticmethod
    def _task_due_at(task):
        try:
            return datetime.fromisoformat(str(task.get("due_at", "")))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _task_created_at(task):
        try:
            return datetime.fromisoformat(str(task.get("created_at", "")))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _recent_pending(cls, pending):
        """返回最新创建时间窗口内的待提醒任务，作为“刚才那几个任务”。"""
        created_times = [
            created_at
            for task in pending
            if (created_at := cls._task_created_at(task)) is not None
        ]
        if not created_times:
            return []
        latest = max(created_times)
        cutoff = latest - cls._recent_batch_window
        return [
            task
            for task in pending
            if (created_at := cls._task_created_at(task)) is not None
            and created_at >= cutoff
        ]

    @classmethod
    def _matches_selector(cls, task, selector):
        selector = re.sub(r"\s+", "", str(selector or "").strip()).replace("：", ":")
        if not selector:
            return False
        title = re.sub(r"\s+", "", str(task.get("title", "")).strip())
        due_at = cls._task_due_at(task)
        time_match = re.fullmatch(r"(\d{1,2}):(\d{2})(.*)", selector)
        if time_match and due_at is not None:
            hour, minute = int(time_match.group(1)), int(time_match.group(2))
            if due_at.hour == hour and due_at.minute == minute:
                suffix = time_match.group(3).strip()
                return not suffix or suffix in title
            return False
        interval_match = re.fullmatch(
            r"每(?:隔)?(\d+(?:\.\d+)?)(小时|小時|分钟|分鐘|分|min|mins?|"
            r"minute|minutes|秒钟|秒|天)",
            selector,
            flags=re.IGNORECASE,
        )
        if interval_match:
            try:
                expected = float(interval_match.group(1))
                unit = interval_match.group(2).lower()
                multiplier = {
                    "小时": 60,
                    "小時": 60,
                    "分钟": 1,
                    "分鐘": 1,
                    "分": 1,
                    "min": 1,
                    "mins": 1,
                    "minute": 1,
                    "minutes": 1,
                    "秒钟": 1 / 60,
                    "秒": 1 / 60,
                    "天": 1440,
                }[unit]
                actual = float(task.get("duration_minutes", 0) or 0)
                return abs(actual - expected * multiplier) < 1e-6
            except (KeyError, TypeError, ValueError):
                return False
        return title in (selector, selector + "任务", selector + "提醒")

    def add(self, title, due_at):
        with self._store_lock:
            title = str(title or "").strip()
            if not title:
                raise ValueError("任务内容不能为空")
            if not isinstance(due_at, datetime):
                raise TypeError("任务时间必须是 datetime")
            task = {
                "id": "{}-{}".format(
                    due_at.strftime("%Y%m%d%H%M%S"), len(self.tasks) + 1
                ),
                "title": title,
                "due_at": due_at.isoformat(timespec="seconds"),
                "mode": "scheduled",
                "repeat_count": 0,
                "total_cycles": 1,
                "run_count": 0,
                "task_history": [],
                "status": "pending",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            return self._append_task(task)

    def add_timer(self, title, duration_minutes, repeat_count=0):
        """创建倒计时任务；repeat_count 表示首次提醒后的重复次数。"""
        with self._store_lock:
            title = str(title or "").strip()
            try:
                duration_minutes = float(duration_minutes)
                repeat_count = int(repeat_count or 0)
            except (TypeError, ValueError):
                raise ValueError("计时时间和循环次数必须是数字")
            if not title:
                raise ValueError("任务内容不能为空")
            if duration_minutes <= 0:
                raise ValueError("计时时间必须大于 0")
            if repeat_count < 0:
                raise ValueError("循环次数不能小于 0")
            now = datetime.now()
            task = {
                "id": "{}-{}".format(
                    now.strftime("%Y%m%d%H%M%S"), len(self.tasks) + 1
                ),
                "title": title,
                "due_at": (now + timedelta(minutes=duration_minutes)).isoformat(
                    timespec="seconds"
                ),
                "mode": "timer",
                "duration_minutes": duration_minutes,
                "repeat_count": repeat_count,
                "total_cycles": repeat_count + 1,
                "run_count": 0,
                "task_history": [],
                "status": "pending",
                "created_at": now.isoformat(timespec="seconds"),
            }
            return self._append_task(task)

    def _append_task(self, task):
        with self._store_lock:
            previous = self.tasks[:]
            self.tasks.append(task)
            self.tasks.sort(key=lambda item: str(item.get("due_at", "")))
            try:
                self._save()
            except OSError:
                # A failed save must not leave a live, unacknowledged reminder.
                self.tasks = previous
                raise
            return task

    def pending(self):
        with self._store_lock:
            return [
                copy.deepcopy(task)
                for task in self.tasks
                if task.get("status") == "pending"
            ]

    def find_pending_by_title(self, title=""):
        """查找待处理提醒；空标题表示查找全部待处理提醒。"""
        with self._store_lock:
            normalized = str(title or "").strip()
            pending = [
                task for task in self.tasks
                if task.get("status") == "pending"
            ]
            if not normalized or normalized in ("所有", "全部", "当前"):
                return copy.deepcopy(pending)
            if normalized in ("最近", "刚才", "刚刚"):
                return copy.deepcopy(self._recent_pending(pending))
            selectors = [
                item.strip() for item in normalized.split("|") if item.strip()
            ]
            matches = [
                task for task in pending
                if any(self._matches_selector(task, item) for item in selectors)
            ]
            return copy.deepcopy(matches)

    def find_pending_by_ids(self, task_ids):
        """按任务 ID 查找仍待提醒的任务。"""
        ids = {
            str(task_id).strip()
            for task_id in (task_ids or ())
            if str(task_id).strip()
        }
        if not ids:
            return []
        with self._store_lock:
            return copy.deepcopy([
                task for task in self.tasks
                if task.get("status") == "pending"
                and str(task.get("id", "")) in ids
            ])

    def remove_pending_by_ids(self, task_ids):
        """原子删除指定 ID 的待提醒任务，返回删除数量。"""
        ids = {
            str(task_id).strip()
            for task_id in (task_ids or ())
            if str(task_id).strip()
        }
        if not ids:
            return 0
        with self._store_lock:
            matched_ids = {
                str(task.get("id", ""))
                for task in self.tasks
                if task.get("status") == "pending"
                and str(task.get("id", "")) in ids
            }
            if not matched_ids:
                return 0
            previous = copy.deepcopy(self.tasks)
            before = len(self.tasks)
            self.tasks = [
                task for task in self.tasks
                if str(task.get("id", "")) not in matched_ids
            ]
            try:
                self._save()
            except OSError:
                self.tasks = previous
                raise
            return before - len(self.tasks)

    def remove_pending_by_title(self, title=""):
        """删除标题匹配的待处理提醒，返回删除数量。"""
        with self._store_lock:
            matches = self.find_pending_by_title(title)
            if not matches:
                return 0
            ids = {task.get("id") for task in matches}
            previous = self.tasks[:]
            before = len(self.tasks)
            self.tasks = [
                task for task in self.tasks
                if task.get("id") not in ids
            ]
            try:
                self._save()
            except OSError:
                self.tasks = previous
                raise
            return before - len(self.tasks)

    def completed(self):
        with self._store_lock:
            return [
                copy.deepcopy(task)
                for task in self.tasks
                if task.get("status") in ("completed", "notified")
            ]

    def get(self, task_id):
        with self._store_lock:
            for task in self.tasks:
                if task.get("id") == task_id:
                    return copy.deepcopy(task)
        return None

    def remind_again(self, task_id, duration_minutes=30):
        """用已完成任务的内容创建一个新的倒计时任务。"""
        with self._store_lock:
            for task in self.tasks:
                if task.get("id") != task_id:
                    continue
                return self.add_timer(
                    task.get("title", ""),
                    duration_minutes,
                    repeat_count=0,
                )
        return None

    def due(self, now=None):
        with self._store_lock:
            now = now or datetime.now()
            previous = copy.deepcopy(self.tasks)
            due_tasks = []
            changed = False
            for task in self.tasks:
                if task.get("status") != "pending":
                    continue
                try:
                    due_at = datetime.fromisoformat(task["due_at"])
                except (KeyError, TypeError, ValueError):
                    task["status"] = "invalid"
                    changed = True
                    continue
                if due_at <= now:
                    task["notified_at"] = now.isoformat(timespec="seconds")
                    task["run_count"] = int(task.get("run_count", 0) or 0) + 1
                    try:
                        repeat_count = int(
                            task.get("repeat_count", task.get("repeat_minutes", 0)) or 0
                        )
                    except (TypeError, ValueError):
                        repeat_count = 0
                    if not task.get("total_cycles"):
                        task["total_cycles"] = int(
                            task.get("run_count", 0) or 0
                        ) + repeat_count + 1
                    if repeat_count > 0:
                        task["repeat_count"] = repeat_count - 1
                        task["status"] = "pending"
                        try:
                            duration_minutes = float(
                                task.get("duration_minutes", 0) or 0
                            )
                        except (TypeError, ValueError):
                            duration_minutes = 0
                        if duration_minutes <= 0:
                            task["status"] = "invalid"
                            due_tasks.append(task)
                            changed = True
                            continue
                        task["due_at"] = (
                            now + timedelta(minutes=duration_minutes)
                        ).isoformat(timespec="seconds")
                    else:
                        task["status"] = "completed"
                        task["completed_at"] = now.isoformat(timespec="seconds")
                    task["reminder_index"] = int(task.get("run_count", 0) or 0)
                    task["remaining_cycles"] = int(
                        task.get("repeat_count", 0) or 0
                    )
                    due_tasks.append(task)
                    changed = True
            if changed:
                try:
                    self._save()
                except OSError:
                    self.tasks = previous
                    raise
            return copy.deepcopy(due_tasks)

    def complete(self, task_id):
        with self._store_lock:
            for task in self.tasks:
                if task.get("id") != task_id:
                    continue
                previous = copy.deepcopy(self.tasks)
                task["status"] = "completed"
                task["completed_at"] = datetime.now().isoformat(timespec="seconds")
                try:
                    self._save()
                except OSError:
                    self.tasks = previous
                    raise
                return True
        return False

    def append_history(self, task_id, user_text, assistant_text, phase="", metadata=None):
        """保存单个任务自己的模型上下文，不混入普通聊天记录。"""
        with self._store_lock:
            for task in self.tasks:
                if task.get("id") != task_id:
                    continue
                previous = copy.deepcopy(self.tasks)
                history = task.setdefault("task_history", [])
                if not isinstance(history, list):
                    history = []
                    task["task_history"] = history
                history.append(
                    {
                        "role": "user",
                        "content": str(user_text or ""),
                        "phase": str(phase or ""),
                    }
                )
                history.append(
                    {
                        "role": "assistant",
                        "content": str(assistant_text or ""),
                        "phase": str(phase or ""),
                    }
                )
                task["task_history"] = history[-12:]
                if isinstance(metadata, dict):
                    task.update(metadata)
                try:
                    self._save()
                except OSError:
                    self.tasks = previous
                    raise
                return True
        return False

    def update_metadata(self, task_id, metadata):
        """更新任务的预生成回复、音频路径等运行元数据。"""
        if not isinstance(metadata, dict):
            return False
        with self._store_lock:
            for task in self.tasks:
                if task.get("id") != task_id:
                    continue
                previous = copy.deepcopy(self.tasks)
                task.update(metadata)
                try:
                    self._save()
                except OSError:
                    self.tasks = previous
                    raise
                return True
        return False

    def remove(self, task_id):
        with self._store_lock:
            before = len(self.tasks)
            previous = self.tasks[:]
            self.tasks = [task for task in self.tasks if task.get("id") != task_id]
            if len(self.tasks) != before:
                try:
                    self._save()
                except OSError:
                    self.tasks = previous
                    raise
                return True
        return False
