"""Focused reminder parsing, persistence and shared text/ASR entry tests."""
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.reminder_commands import (
    extract_reminder_draft,
    extract_reminder_followup_title,
    parse_reminder_command,
    parse_reminder_delete_command,
    parse_repeat_count,
    ReminderParseError,
)
from core.tasks import TaskStore
from ui.app import DesktopPetApp

NOW = datetime(2026, 9, 25, 16, 0, 0)


class ReminderReplyText:
    """为提醒回复测试提供最小的 inline_reply_text 行为。"""

    def __init__(self, on_insert):
        self.content = ""
        self.state = "disabled"
        self._on_insert = on_insert

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]

    def delete(self, _start, _end):
        self.content = ""

    def insert(self, _index, value):
        self.content = str(value)
        self._on_insert(self.content)

    def yview_moveto(self, _fraction):
        return None


class ReminderTests(unittest.TestCase):
    def test_relative_times_and_chinese_asr_numbers(self):
        for text, seconds, title in (
            ("十分钟后提醒我喝水", 600, "喝水"),
            ("帮我在半小时后提醒我开会", 1800, "开会"),
            ("提醒我一个半小时后取快递", 5400, "取快递"),
            ("请两小时三十分钟后叫我休息", 9000, "休息"),
            ("设置一个任务提醒：20秒后喝水", 20, "喝水"),
            ("提醒我0.5分钟后伸懒腰", 30, "伸懒腰"),
            ("提醒我在10分钟后开会", 600, "开会"),
        ):
            with self.subTest(text=text):
                result = parse_reminder_command(text, NOW)
                self.assertEqual(result.title, title)
                self.assertEqual(result.due_at, NOW + timedelta(seconds=seconds))

    def test_calendar_and_clock(self):
        for text, expected in (
            ("明天下午三点三十分提醒我开会", datetime(2026,9,26,15,30)),
            ("请提醒我明天上午九点半开会", datetime(2026,9,26,9,30)),
            ("大后天晚上八点提醒我开会", datetime(2026,9,28,20)),
            ("明早八点提醒我开会", datetime(2026,9,26,8)),
            ("下周一上午九点提醒我开会", datetime(2026,9,28,9)),
            ("2026年10月1日15:30提醒我开会", datetime(2026,10,1,15,30)),
            ("今天18:05提醒我开会", datetime(2026,9,25,18,5)),
            ("九点提醒我开会", datetime(2026,9,26,9)),
        ):
            with self.subTest(text=text):
                result = parse_reminder_command(text, NOW)
                self.assertEqual(result.title, "开会")
                self.assertEqual(result.due_at, expected)

    def test_incomplete_or_invalid_commands_do_not_guess(self):
        for text in ("提醒我喝水", "一会儿提醒我喝水", "10分钟后提醒我",
                     "今天上午八点提醒我开会", "明天25点提醒我开会",
                     "2月30日18点提醒我开会", "零分钟后提醒我喝水",
                     "每小时提醒我喝水", "明天下午三点或四点提醒我开会",
                     "10分钟后提醒我喝水，20分钟后提醒我睡觉"):
            with self.subTest(text=text), self.assertRaises(ReminderParseError):
                parse_reminder_command(text, NOW)

    def test_chat_negative_and_quotes_are_not_actions(self):
        for text in ("你好", "你记得昨天的聊天吗", "不要十分钟后提醒我喝水",
                     "怎么设置提醒", "如果十分钟后提醒我喝水就好了",
                     "翻译‘十分钟后提醒我喝水’", "上次你提醒我开会了"):
            with self.subTest(text=text):
                self.assertIsNone(parse_reminder_command(text, NOW))

    def test_finite_repeat_uses_existing_semantics(self):
        result = parse_reminder_command("每30分钟提醒我喝水，重复2次", NOW)
        self.assertEqual((result.title, result.duration_minutes, result.repeat_count), ("喝水",30,2))

    def test_minute_abbreviation_and_follow_up_draft(self):
        draft = extract_reminder_draft("接下来每13min提醒我喝水")
        self.assertIsNotNone(draft)
        self.assertEqual((draft.title, draft.duration_minutes), ("喝水", 13))
        self.assertEqual(parse_repeat_count("进行3次"), 3)

    def test_repeat_count_accepts_reminder_wording(self):
        for text in ("提醒7次", "提醒 7 次", "那就提醒7次"):
            with self.subTest(text=text):
                self.assertEqual(parse_repeat_count(text), 7)

    def test_follow_up_repeat_and_missing_fields_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            draft = extract_reminder_draft("每13min提醒我喝水")
            task = store.add_timer(draft.title, draft.duration_minutes, 3)
            self.assertEqual(task["duration_minutes"], 13)
            self.assertEqual(task["total_cycles"], 4)
            self.assertIsNone(extract_reminder_draft("重复3次"))
            self.assertIsNone(parse_reminder_delete_command("你好"))

    def test_natural_language_repeat_and_delete_variants(self):
        result = parse_reminder_command(
            "每13分钟提醒我喝水，进行3次",
            NOW,
        )
        self.assertEqual((result.title, result.repeat_count), ("喝水", 3))
        self.assertEqual(parse_repeat_count("总共做3次，现在就开始"), 3)
        self.assertEqual(
            parse_repeat_count("我刚才已经说了间隔时间，要进行2次"),
            2,
        )
        self.assertEqual(
            extract_reminder_draft("每3分钟告诉我现在几点").title,
            "现在几点",
        )
        self.assertEqual(
            extract_reminder_draft("接下来每3分钟提醒我一下").title,
            "提醒",
        )
        self.assertEqual(
            extract_reminder_draft("每3分钟提醒我一次").title,
            "",
        )
        self.assertEqual(
            extract_reminder_followup_title("提醒我做运动，进行2次"),
            "做运动",
        )
        self.assertEqual(
            parse_reminder_delete_command("删除上次提醒我休息的任务"),
            "休息",
        )
        self.assertEqual(
            parse_reminder_delete_command("将我刚才建立的几次任务全部删除"),
            "最近",
        )
        for text in (
            "将刚才创建的几个任务删除",
            "将刚才创建的几个重复型的任务删除",
            "将刚才创建的几个代替型的任务删除",
            "香港在创建的几个代替型的任务删除",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_reminder_delete_command(text), "最近")
        self.assertEqual(
            parse_reminder_delete_command("删除所有待提醒任务"),
            "所有",
        )
        self.assertEqual(
            parse_reminder_delete_command(
                "将每3min让你提醒我一次的任务删掉，现在不需要了"
            ),
            "每3min",
        )
        self.assertIsNone(
            extract_reminder_draft(
                "将每3min让你提醒我一次的任务删掉，现在不需要了"
            )
        )

    def test_conversational_multi_selector_delete_command(self):
        self.assertEqual(
            parse_reminder_delete_command(
                "好的，收到！23:55那个、23:56喝水的，还有每10分钟那条，"
                "都取消，不再提醒啦。"
            ),
            "23:55|23:56喝水|每10分钟",
        )
        self.assertEqual(
            parse_reminder_delete_command("删除喝水或休息"),
            "喝水|休息",
        )
        self.assertEqual(
            parse_reminder_delete_command("把喝水或者休息删掉"),
            "喝水|休息",
        )

    def test_delete_named_tasks_across_task_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            store.add("单次任务", NOW + timedelta(hours=1))
            store.add_timer("倒计时任务", 13, 0)
            store.add_timer("循环任务", 13, 2)

            selector = parse_reminder_delete_command(
                "删除单次任务、倒计时任务或循环任务"
            )
            self.assertEqual(store.remove_pending_by_title(selector), 3)
            self.assertEqual(store.pending(), [])

    def test_voice_delete_all_pending_reminders(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DesktopPetApp.__new__(DesktopPetApp)
            app.task_store = TaskStore(Path(directory) / "tasks.json")
            for title in ("喝水", "休息", "运动"):
                app.task_store.add_timer(title, 13, 2)
            app._conversation_generation = app._tts_request_id = 0
            app._reminder_draft = None
            app._mark_companionship_activity = Mock()
            app._stop_tts_playback = Mock()
            app._safe_root_after = Mock()
            app._character_display_name = lambda: "华淡"
            app.character_id = "huadan"
            app.character_config = {"display_name": "华淡", "personality": ""}
            app.output_language = "zh"
            app._handle_task_event = Mock()
            app.store = Mock()
            app.store.get_conversation_character.return_value = ("huadan", "华淡")
            app._finish_message = Mock()
            app._show_runtime_notice = Mock()
            app._refresh_control_tasks_view = Mock()

            app._handle_message(
                "将我刚才建立的几次任务全部删除",
                Mock(),
                input_context="",
            )

            self.assertEqual(TaskStore(app.task_store.path).pending(), [])
            self.assertIn("3个", app._finish_message.call_args.args[1])

    def test_delete_one_then_delete_the_other_task(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DesktopPetApp.__new__(DesktopPetApp)
            app.task_store = TaskStore(Path(directory) / "tasks.json")
            app._conversation_generation = app._tts_request_id = 0
            app._reminder_draft = None
            app._recent_delete_context = None
            app._mark_companionship_activity = Mock()
            app._stop_tts_playback = Mock()
            app._safe_root_after = Mock()
            app._character_display_name = lambda: "华淡"
            app.character_id = "huadan"
            app.character_config = {"display_name": "华淡", "personality": ""}
            app.output_language = "zh"
            app._handle_task_event = Mock()
            app.store = Mock()
            app.store.get_conversation_character.return_value = ("huadan", "华淡")
            app._finish_message = Mock()
            app._show_runtime_notice = Mock()
            app._refresh_control_tasks_view = Mock()
            append = Mock()

            app._handle_message("10分钟后提醒我做报告", append)
            app._handle_message("11分钟后提醒我休息", append)
            app._handle_message("删除做报告", append)
            app._handle_message("另一个任务也删掉吧", append)

            self.assertEqual(TaskStore(app.task_store.path).pending(), [])
            self.assertIn("另一个休息任务也取消了", app._finish_message.call_args.args[1])

    def test_recent_batch_delete_preserves_older_pending_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            old_task = store.add_timer("旧任务", 13, 0)
            old_created_at = (
                datetime.now() - timedelta(minutes=31)
            ).isoformat(timespec="seconds")
            store.tasks[0]["created_at"] = old_created_at
            store._save()
            store.add_timer("喝水", 13, 0)
            store.add_timer("休息", 13, 0)

            self.assertEqual(
                [task["title"] for task in store.find_pending_by_title("最近")],
                ["喝水", "休息"],
            )
            self.assertEqual(store.remove_pending_by_title("最近"), 2)
            remaining = store.pending()
            self.assertEqual([task["id"] for task in remaining], [old_task["id"]])

    def test_delete_pending_reminders_and_preserve_audio_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            task = store.add_timer("喝水", 13, 3)
            audio_path = str(Path(directory) / "task_audio" / "reminder.wav")
            self.assertTrue(store.update_metadata(task["id"], {
                "prepared_due_reply": "该喝水啦。",
                "prepared_due_audio_path": audio_path,
                "prepared_due_language": "zh",
            }))
            loaded = TaskStore(store.path)
            self.assertEqual(loaded.pending()[0]["prepared_due_audio_path"], audio_path)
            self.assertEqual(loaded.remove_pending_by_title("喝水"), 1)
            self.assertEqual(loaded.pending(), [])

    def test_delete_repeating_task_after_one_trigger_before_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            task = store.add_timer("循环喝水", 1, 2)
            due_at = datetime.fromisoformat(task["due_at"])

            events = store.due(due_at + timedelta(seconds=1))

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["status"], "pending")
            self.assertEqual(events[0]["repeat_count"], 1)
            self.assertEqual(store.remove_pending_by_title("循环喝水"), 1)
            self.assertEqual(store.pending(), [])

    def test_delete_pending_reminders_by_time_title_and_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            store.add("休息", datetime(2026, 9, 25, 23, 55))
            store.add("喝水", datetime(2026, 9, 25, 23, 56))
            store.add_timer("提醒", 10, 2)
            removed = store.remove_pending_by_title(
                "23:55|23:56喝水|每10分钟"
            )
            self.assertEqual(removed, 3)
            self.assertEqual(store.pending(), [])
            self.assertEqual(TaskStore(store.path).pending(), [])

    def test_delete_save_failure_rolls_back_memory_and_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            task = store.add_timer("喝水", 13)
            before = store.path.read_bytes()
            with patch.object(store, "_save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.remove_pending_by_title("喝水")
            self.assertEqual(store.pending()[0]["id"], task["id"])
            self.assertEqual(store.path.read_bytes(), before)

    def test_persist_reload_due_and_no_duplicate_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            store = TaskStore(path)
            task = store.add("喝水", NOW + timedelta(seconds=2))
            fresh = TaskStore(path)
            self.assertEqual(fresh.pending()[0]["id"], task["id"])
            self.assertEqual(fresh.due(NOW), [])
            self.assertEqual(len(fresh.due(NOW + timedelta(seconds=3))), 1)
            self.assertEqual(fresh.due(NOW + timedelta(seconds=4)), [])
            self.assertEqual(TaskStore(path).tasks[0]["status"], "completed")

    def test_save_failure_rolls_back_both_creation_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            original = store.add("原任务", NOW)
            before = store.path.read_bytes()
            with patch.object(store, "_save", side_effect=PermissionError("read only")):
                for create in (lambda: store.add("新任务", NOW), lambda: store.add_timer("新任务", 1)):
                    with self.assertRaises(OSError):
                        create()
                    self.assertEqual(store.tasks, [original])
                    self.assertEqual(store.path.read_bytes(), before)

    def test_shared_handler_for_text_and_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DesktopPetApp.__new__(DesktopPetApp)
            app.task_store = TaskStore(Path(directory) / "tasks.json")
            app._conversation_generation = app._tts_request_id = 0
            app._mark_companionship_activity = Mock()
            app._stop_tts_playback = Mock()
            app._safe_root_after = Mock()
            app._character_display_name = lambda: "华淡"
            app.character_id = "huadan"
            app.character_config = {"display_name": "华淡", "personality": ""}
            app.output_language = "zh"
            app._handle_task_event = Mock()
            app.store = Mock()
            app.store.get_conversation_character.return_value = ("huadan", "华淡")
            app._finish_message = Mock()
            app._show_runtime_notice = Mock()
            app._refresh_control_tasks_view = Mock()
            for context in ("", ""):
                app._handle_message("十分钟后提醒我喝水", Mock(), input_context=context)
            self.assertEqual(len(TaskStore(app.task_store.path).pending()), 2)
            self.assertEqual(app._handle_task_event.call_count, 2)
            with patch.object(app.task_store, "_save", side_effect=OSError("disk full")):
                app._handle_message("一分钟后提醒我休息", Mock())
            self.assertIn("保存失败", app._finish_message.call_args.args[1])
            self.assertEqual(len(app.task_store.pending()), 2)
            app._show_runtime_notice.assert_called_once()

    def test_natural_repeat_count_is_total_cycles_in_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DesktopPetApp.__new__(DesktopPetApp)
            app.task_store = TaskStore(Path(directory) / "tasks.json")
            app._conversation_generation = app._tts_request_id = 0
            app._reminder_draft = extract_reminder_draft("每3分钟提醒我喝水")
            app._mark_companionship_activity = Mock()
            app._stop_tts_playback = Mock()
            app._safe_root_after = Mock()
            app._character_display_name = lambda: "华淡"
            app.character_id = "huadan"
            app.character_config = {"display_name": "华淡", "personality": ""}
            app.output_language = "zh"
            app._handle_task_event = Mock()
            app.store = Mock()
            app.store.get_conversation_character.return_value = ("huadan", "华淡")
            app._finish_message = Mock()
            app._show_runtime_notice = Mock()
            app._refresh_control_tasks_view = Mock()
            app._handle_message("总共做3次，现在就开始", Mock())
            task = TaskStore(app.task_store.path).pending()[0]
            self.assertEqual(task["repeat_count"], 2)
            self.assertEqual(task["total_cycles"], 3)

    def test_complete_repeat_command_still_creates_in_one_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DesktopPetApp.__new__(DesktopPetApp)
            app.task_store = TaskStore(Path(directory) / "tasks.json")
            app._conversation_generation = app._tts_request_id = 0
            app._reminder_draft = None
            app._mark_companionship_activity = Mock()
            app._stop_tts_playback = Mock()
            app._safe_root_after = Mock()
            app._character_display_name = lambda: "华淡"
            app.character_id = "huadan"
            app.character_config = {"display_name": "华淡", "personality": ""}
            app.output_language = "zh"
            app._handle_task_event = Mock()
            app.store = Mock()
            app.store.get_conversation_character.return_value = ("huadan", "华淡")
            app._finish_message = Mock()
            app._show_runtime_notice = Mock()
            app._refresh_control_tasks_view = Mock()

            app._handle_message("每13分钟提醒我喝水，进行3次", Mock())

            tasks = TaskStore(app.task_store.path).pending()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["title"], "喝水")
            self.assertEqual(tasks[0]["total_cycles"], 3)
            self.assertEqual(app._handle_task_event.call_count, 1)

    def test_two_reminders_create_from_screenshot_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DesktopPetApp.__new__(DesktopPetApp)
            app.task_store = TaskStore(Path(directory) / "tasks.json")
            app._conversation_generation = app._tts_request_id = 0
            app._reminder_draft = None
            app._mark_companionship_activity = Mock()
            app._stop_tts_playback = Mock()
            app._safe_root_after = Mock()
            app._character_display_name = lambda: "华淡"
            app.character_id = "huadan"
            app.character_config = {"display_name": "华淡", "personality": ""}
            app.output_language = "zh"
            app._handle_task_event = Mock()
            app.store = Mock()
            app.store.get_conversation_character.return_value = ("huadan", "华淡")
            app._finish_message = Mock()
            app._show_runtime_notice = Mock()
            app._refresh_control_tasks_view = Mock()
            append = Mock()

            app._handle_message("10分钟后提醒我吃饭", append)
            app._handle_message("之后每7分钟提醒我喝水", append)
            app._handle_message("提醒7次", append)

            tasks = TaskStore(app.task_store.path).pending()
            self.assertEqual(len(tasks), 2)
            self.assertEqual(
                {task["title"] for task in tasks},
                {"吃饭", "喝水"},
            )
            water = next(task for task in tasks if task["title"] == "喝水")
            self.assertEqual(water["duration_minutes"], 7)
            self.assertEqual(water["total_cycles"], 7)
            self.assertEqual(water["repeat_count"], 6)
            self.assertEqual(app._handle_task_event.call_count, 2)

    def test_two_turn_generic_reminder_creates_task(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DesktopPetApp.__new__(DesktopPetApp)
            app.task_store = TaskStore(Path(directory) / "tasks.json")
            app._conversation_generation = app._tts_request_id = 0
            app._reminder_draft = None
            app._mark_companionship_activity = Mock()
            app._stop_tts_playback = Mock()
            app._safe_root_after = Mock()
            app._character_display_name = lambda: "华淡"
            app.character_id = "huadan"
            app.character_config = {"display_name": "华淡", "personality": ""}
            app.output_language = "zh"
            app._handle_task_event = Mock()
            app.store = Mock()
            app.store.get_conversation_character.return_value = ("huadan", "华淡")
            app._finish_message = Mock()
            app._show_runtime_notice = Mock()
            app._refresh_control_tasks_view = Mock()
            append = Mock()

            app._handle_message("接下来每3分钟提醒我一下", append)
            self.assertIsNotNone(app._reminder_draft)
            app._handle_message(
                "我刚才已经说了间隔时间，要进行2次",
                append,
            )

            tasks = TaskStore(app.task_store.path).pending()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["title"], "提醒")
            self.assertEqual(tasks[0]["duration_minutes"], 3)
            self.assertEqual(tasks[0]["total_cycles"], 2)
            self.assertEqual(app._handle_task_event.call_count, 1)

    def test_two_turn_interval_then_content_creates_task(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DesktopPetApp.__new__(DesktopPetApp)
            app.task_store = TaskStore(Path(directory) / "tasks.json")
            app._conversation_generation = app._tts_request_id = 0
            app._reminder_draft = None
            app._mark_companionship_activity = Mock()
            app._stop_tts_playback = Mock()
            app._safe_root_after = Mock()
            app._character_display_name = lambda: "华淡"
            app.character_id = "huadan"
            app.character_config = {"display_name": "华淡", "personality": ""}
            app.output_language = "zh"
            app._handle_task_event = Mock()
            app.store = Mock()
            app.store.get_conversation_character.return_value = ("huadan", "华淡")
            app._finish_message = Mock()
            app._show_runtime_notice = Mock()
            app._refresh_control_tasks_view = Mock()
            append = Mock()

            app._handle_message("接下来每8分钟提醒我一次", append)
            self.assertEqual(app._reminder_draft.title, "")
            app._handle_message("提醒我做运动，进行2次", append)

            tasks = TaskStore(app.task_store.path).pending()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["title"], "做运动")
            self.assertEqual(tasks[0]["duration_minutes"], 8)
            self.assertEqual(tasks[0]["total_cycles"], 2)
            self.assertEqual(app._handle_task_event.call_count, 1)

    def test_reminder_reply_commits_text_before_voice_starts(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app._mark_companionship_activity = Mock()
        app._conversation_generation = 1
        app._conversation_in_progress = False
        app._tts_request_id = 10
        app.output_language = "zh"
        app.app_config = {"conversation_model": {}}
        app.store = Mock()
        app.store.current_conversation_id = "conversation-1"
        app.store.settings = {"voice_output": True}
        app.store.add_message = Mock()
        delayed_callbacks = []

        def safe_after(delay, callback):
            if delay <= 0:
                callback()
            else:
                delayed_callbacks.append(callback)

        app._safe_root_after = safe_after
        app._set_tts_error = Mock()
        app.set_state = Mock()
        app._schedule_idle_restore = Mock()
        app._runtime_errors = []
        app._record_voice_timing = Mock()
        pipeline_callbacks = {}
        pipeline_calls = []

        def fake_start(reply, on_started=None, on_failure=None, **_kwargs):
            pipeline_callbacks["first_play"] = on_started
            pipeline_callbacks["failure"] = on_failure
            pipeline_callbacks["before_play"] = _kwargs.get("on_before_play")
            pipeline_calls.append(("reply", reply))
            return 10

        app._start_tts_for_reply = fake_start
        displayed = []

        def _append_inline_chat(role, content):
            displayed.append((role, content))

        app.inline_reply_text = ReminderReplyText(
            lambda content: displayed.append(("assistant", content))
        )

        app._finish_message(
            "删除喝水",
            "好，我已经把1个待提醒任务取消了。",
            _append_inline_chat,
            conversation_generation=1,
            reminder_response=True,
        )

        self.assertEqual(displayed, [("assistant", "正在思考……")])
        self.assertEqual(
            pipeline_calls,
            [("reply", "好，我已经把1个待提醒任务取消了。")],
        )
        pipeline_callbacks["before_play"]()
        self.assertEqual(
            displayed,
            [
                ("assistant", "正在思考……"),
                ("assistant", "好，我已经把1个待提醒任务取消了。"),
            ],
        )
        pipeline_callbacks["first_play"]()
        self.assertEqual(
            displayed[-1],
            ("assistant", "好，我已经把1个待提醒任务取消了。"),
        )
        app._record_voice_timing.assert_called_once()
        self.assertEqual(app._record_voice_timing.call_args.args[1], "text_displayed")

    def test_reminder_reply_does_not_commit_text_from_first_play_only(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app._mark_companionship_activity = Mock()
        app._conversation_generation = 1
        app._conversation_in_progress = False
        app._tts_request_id = 10
        app.output_language = "zh"
        app.app_config = {"conversation_model": {}}
        app.store = Mock()
        app.store.current_conversation_id = "conversation-1"
        app.store.settings = {"voice_output": True}
        app.store.add_message = Mock()
        app._set_tts_error = Mock()
        app.set_state = Mock()
        app._schedule_idle_restore = Mock()
        app._runtime_errors = []
        app._record_voice_timing = Mock()
        delayed_callbacks = []

        def safe_after(delay, callback):
            if delay <= 0:
                callback()
            else:
                delayed_callbacks.append(callback)

        app._safe_root_after = safe_after
        displayed = []

        def fake_start(_reply, on_started=None, **_kwargs):
            app._fake_on_started = on_started
            app._fake_on_before_play = _kwargs.get("on_before_play")
            return 10

        app._start_tts_for_reply = fake_start

        def _append_inline_chat(role, content):
            displayed.append((role, content))

        app.inline_reply_text = ReminderReplyText(
            lambda content: displayed.append(("assistant", content))
        )

        app._finish_message(
            "删除喝水",
            "好，我已经把1个待提醒任务取消了。",
            _append_inline_chat,
            conversation_generation=1,
            reminder_response=True,
        )
        self.assertEqual(displayed, [("assistant", "正在思考……")])
        app._fake_on_started()
        self.assertEqual(displayed, [("assistant", "正在思考……")])
        app._fake_on_before_play()
        self.assertEqual(
            displayed[-1],
            ("assistant", "好，我已经把1个待提醒任务取消了。"),
        )

    def test_task_event_reply_replaces_thinking_without_explicit_generation(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app._mark_companionship_activity = Mock()
        app._conversation_generation = 7
        app._conversation_in_progress = False
        app._tts_request_id = 10
        app.output_language = "zh"
        app.app_config = {"conversation_model": {}}
        app.store = Mock()
        app.store.current_conversation_id = "conversation-1"
        app.store.settings = {"voice_output": True}
        app.store.add_message = Mock()
        delayed_callbacks = []

        def safe_after(delay, callback):
            if delay <= 0:
                callback()
            else:
                delayed_callbacks.append(callback)

        app._safe_root_after = safe_after
        app._set_tts_error = Mock()
        app.set_state = Mock()
        app._schedule_idle_restore = Mock()
        app._runtime_errors = []
        app._record_voice_timing = Mock()
        pipeline_callbacks = {}

        def fake_start(_reply, on_started=None, **_kwargs):
            pipeline_callbacks["first_play"] = on_started
            pipeline_callbacks["before_play"] = _kwargs.get("on_before_play")
            return 10

        app._start_tts_for_reply = fake_start
        displayed = []

        def _append_inline_chat(role, content):
            displayed.append((role, content))

        app.inline_reply_text = ReminderReplyText(
            lambda content: displayed.append(("assistant", content))
        )

        app._show_reminder_response(
            "好啦，已经记下了。",
            _append_inline_chat,
        )
        self.assertEqual(displayed, [("assistant", "正在思考……")])

        pipeline_callbacks["first_play"]()
        self.assertEqual(
            displayed,
            [("assistant", "正在思考……")],
        )
        pipeline_callbacks["before_play"]()

        self.assertEqual(
            displayed[-1],
            ("assistant", "好啦，已经记下了。"),
        )

    def test_reminder_reply_is_not_dropped_when_a_later_tts_request_replaces_id(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app._mark_companionship_activity = Mock()
        app._conversation_generation = 1
        app._conversation_in_progress = False
        app._tts_request_id = 10
        app.output_language = "zh"
        app.app_config = {"conversation_model": {}}
        app.store = Mock()
        app.store.current_conversation_id = "conversation-1"
        app.store.settings = {"voice_output": True}
        app.store.add_message = Mock()
        app._set_tts_error = Mock()
        app.set_state = Mock()
        app._schedule_idle_restore = Mock()
        app._runtime_errors = []
        app._record_voice_timing = Mock()
        pending_callbacks = []
        app._safe_root_after = lambda _delay, callback: pending_callbacks.append(callback)
        first_play = threading.Event()
        displayed = []

        def fake_pipeline(_language, **_kwargs):
            return {
                "feed": lambda _value: None,
                "finish": lambda: None,
                "first_play": first_play,
                "request_id": 10,
            }

        app._create_stream_tts_pipeline = fake_pipeline
        app._finish_message(
            "删除喝水",
            "好，我已经把1个待提醒任务取消了。",
            lambda role, content: displayed.append((role, content)),
            conversation_generation=1,
            reminder_response=True,
        )

        app._tts_request_id = 11
        first_play.set()
        pending_callbacks[-2]()

        self.assertEqual(
            displayed[-1],
            ("assistant", "好，我已经把1个待提醒任务取消了。"),
        )

    def test_task_event_reply_is_written_to_chat_history(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.root = Mock()
        app.root.winfo_exists.return_value = True
        app._append_inline_chat = Mock()
        app._show_reminder_response = Mock()
        app._finish_task_event_queue_item = Mock()
        app.task_store = Mock()
        app.store = Mock()
        app.store.settings = {"voice_output": False}
        app.character_id = "huadan"
        app._character_display_name = lambda: "华淡"
        event = {
            "task_id": "task-1",
            "phase": "created",
            "reminder_index": 0,
            "remaining_cycles": 2,
        }
        app._finish_task_event(event, "用户刚刚说：每3分钟提醒我喝水", "记下啦。")
        app.store.add_message.assert_called_once_with(
            "assistant",
            "记下啦。",
            character_id="huadan",
            character_name="华淡",
        )

    def test_context_is_compact_and_tray_keeps_recovery_actions(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.store = SimpleNamespace(settings={})
        app.display_mode = "full"
        app.root = Mock()
        app.root.attributes.return_value = False
        app.character_config = {}
        app._portrait_outfits = Mock(return_value=[{}, {}])

        actions = app._get_context_menu_actions()
        labels = [
            app._context_menu_item_parts(item)[0]
            for item in actions
        ]
        self.assertEqual(
            labels,
            [
                "输入",
                "角色",
                "历史对话",
                "设置",
            ],
        )
        role_item = actions[1]
        self.assertEqual(role_item[1], app._show_pet_role_panel)
        self.assertTrue(all(not app._context_menu_item_parts(item)[2] for item in actions))

        tray_items = app._get_tray_menu_items()
        self.assertEqual(
            [item[1] for item in tray_items],
            ["输入", "角色", "历史对话", "任务提醒", "设置", "开启鼠标穿透", "退出桌宠"],
        )
        self.assertTrue(all(not item[3] for item in tray_items))
        all_ids = [
            item_id
            for item_id, _label, _callback, children in tray_items
        ] + [
            child_id
            for item in tray_items
            for child_id, _label, _callback, _children in item[3]
        ]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_start_tray_routes_menu_open_to_tk_thread(self):
        app = DesktopPetApp.__new__(DesktopPetApp)
        app._character_display_name = Mock(return_value="华淡")
        app._enqueue_tray_event = Mock()
        menu_items = (
            (1, "全身模式", Mock(), ()),
            (2, "置顶", Mock(), ()),
            (3, "退出桌宠", Mock(), ()),
        )
        app._get_tray_menu_items = Mock(return_value=menu_items)
        app._safe_root_after = Mock()

        with patch("ui.app.WindowsTrayIcon") as tray_class:
            tray = tray_class.return_value
            tray.start = Mock()
            app._start_tray()

        tray_class.assert_called_once()
        call_kwargs = tray_class.call_args.kwargs
        self.assertEqual(call_kwargs["menu_items"], menu_items)
        call_kwargs["on_menu"]()
        app._enqueue_tray_event.assert_called_once_with("menu")
        tray.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
