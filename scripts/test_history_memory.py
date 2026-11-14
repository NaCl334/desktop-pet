"""Cross-day continuity, budget, original provenance and correction isolation."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.context_budget import PromptBudget, ContextBudgetError, recent_turns
from core.conversation import ConversationClient
from core.history_memory import HistoryMemoryStore, record_day, source_id
from core.memory import MemoryStore


class HistoryMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = HistoryMemoryStore(Path(self.temp.name) / "days")
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 11, 9, 12, tzinfo=timezone(timedelta(hours=8)))

    def row(self, text, role="user", age=0, owner="sakura", index=0):
        return {"id": owner + str(index) + role, "character_id": owner, "conversation_id": "one",
                "created_at": (self.now-timedelta(days=age)).isoformat(), "role": role, "content": text}

    def client(self, **config):
        return ConversationClient(config, history_memory=self.store,
            runtime_context=SimpleNamespace(format_for_model=lambda: "2026-11-09"))

    def test_100_rounds_with_pending_input_are_exact_originals(self):
        rows = [self.row("original {} {}".format(n, role), role, index=n)
                for n in range(120) for role in ("user", "assistant")]
        rows.append(self.row("pending", index=120))
        before = copy.deepcopy(rows)
        selected = recent_turns(rows)
        self.assertEqual(len(selected), 201)
        self.assertEqual(selected[0]["content"], "original 20 user")
        result = self.client().build_messages(rows, {})
        self.assertEqual([r["content"] for r in result[1:]], [r["content"] for r in selected])
        self.assertEqual(rows, before)

    def test_midnight_does_not_clear_minutes_old_messages(self):
        rows = [self.row("23:58 未完成战斗", index=1), self.row("还想看结果", "assistant", index=1),
                self.row("00:02 已经过关", index=2)]
        rows[0]["created_at"] = "2026-11-08T23:58:00+08:00"
        rows[1]["created_at"] = "2026-11-08T23:59:00+08:00"
        rows[2]["created_at"] = "2026-11-09T00:02:00+08:00"
        self.store.update("sakura", rows)
        result = self.client().build_messages(rows, {})
        self.assertEqual([r["content"] for r in result[1:]], [r["content"] for r in rows])
        self.assertNotIn("用户原话摘录", result[0]["content"])

    def test_daily_summaries_are_incremental_and_do_not_replace_originals(self):
        rows = [self.row("游戏进展：过了第一关", index=1)]
        before = copy.deepcopy(rows)
        self.store.update("sakura", rows)
        path = self.store._path("sakura")
        first = path.read_bytes()
        self.store.update("sakura", rows)
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual(rows, before)
        rows.append(self.row("我想知道下一关", "assistant", index=1))
        self.store.update("sakura", rows)
        day = record_day(rows[0])
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(document["days"][day]["processed"]), 2)
        self.assertEqual(len(document["days"][day]["items"]), 2)

    def test_stale_snapshot_does_not_overwrite_newer_daily_notes(self):
        rows = [self.row("第一条", index=1), self.row("第二条", index=2)]
        self.store.update("sakura", rows)
        before = self.store._path("sakura").read_bytes()
        self.store.update("sakura", rows[:1])
        self.assertEqual(self.store._path("sakura").read_bytes(), before)

    def test_edited_original_rebuilds_notes_without_losing_revision_guard(self):
        row = self.row("旧说法咖啡", index=1)
        self.store.update("sakura", [row])
        row["content"] = "新说法红茶"
        self.assertNotIn("旧说法咖啡", "\n".join(self.store.context_blocks("sakura", [row], [], "你好", now=self.now)))
        self.store.update("sakura", [row])
        notes = self.store._load("sakura")["days"][record_day(row)]["items"]
        self.assertIn("红茶", notes[0]["text"])
        self.assertNotIn("咖啡", json.dumps(notes, ensure_ascii=False))

    def test_extract_notes_upgrade_when_model_becomes_available(self):
        row = self.row("游戏进展", index=1)
        self.store.update("sakura", [row])
        self.store.update("sakura", [row], summarize=lambda rows: [
            {"text": "经过语义归纳的进展", "kind": "progress", "source_ids": [source_id(row)]}])
        daily = self.store._load("sakura")["days"][record_day(row)]
        self.assertEqual(daily["summary_mode"], "model")
        self.assertEqual(len(daily["items"]), 1)
        self.assertEqual(daily["items"][0]["text"], "经过语义归纳的进展")

    def test_default_seven_days_and_older_history_on_demand(self):
        rows = [self.row("远古独特火龙战斗", age=30, index=1),
                self.row("昨天完成桌宠项目", age=1, index=2)]
        self.store.update("sakura", rows)
        default = "\n".join(self.store.context_blocks("sakura", rows, [], "你好", now=self.now))
        self.assertIn("桌宠项目", default)
        self.assertNotIn("独特火龙", default)
        recalled = "\n".join(self.store.context_blocks("sakura", rows, [], "火龙战斗", now=self.now))
        self.assertIn("远古独特火龙战斗", recalled)
        self.assertIn(source_id(rows[0]), recalled)

    def test_single_term_queries_can_retrieve_older_originals(self):
        for text, query in (("coffee was our earlier topic", "coffee"), ("上月谈过咖啡", "咖啡")):
            with self.subTest(query=query):
                row = self.row(text, age=30, index=1)
                blocks = "\n".join(self.store.context_blocks("sakura", [row], [], query, now=self.now))
                self.assertIn(text, blocks)

    def test_summary_and_raw_provenance_do_not_duplicate_recent_window(self):
        rows = [self.row("独特游戏进展", age=1, index=1)]
        self.store.update("sakura", rows)
        self.assertEqual(self.store.context_blocks("sakura", rows, rows, "游戏进展", now=self.now), [])
        blocks = "\n".join(self.store.context_blocks("sakura", rows, [], "游戏进展", now=self.now))
        self.assertEqual(blocks.count("独特游戏进展"), 1)

    def test_character_isolation_in_summary_raw_lookup_and_payload(self):
        a = self.row("樱专属秘密游戏", index=1)
        b = self.row("华淡独特项目", owner="huadan", index=1)
        self.store.update("sakura", [a, b])
        blocks = "\n".join(self.store.context_blocks("sakura", [a, b], [], "华淡独特项目", now=self.now))
        self.assertNotIn("华淡", blocks)
        self.assertEqual(self.store.resolve_sources("sakura", [a, b], [source_id(b)]), [])
        result = self.client().build_messages([b, a], {})
        self.assertNotIn("华淡独特项目", json.dumps(result, ensure_ascii=False))

    def test_source_lookup_returns_full_unmodified_original(self):
        row = self.row("原文"*3000, index=1)
        self.assertEqual(self.store.resolve_sources("sakura", [row], [source_id(row)])[0]["content"], row["content"])

    def test_all_seven_days_fit_default_compact_summary_budget(self):
        rows = [self.row("项目进展" + str(age) + "很长的细节"*40, age=age, index=age) for age in range(9)]
        self.store.update("sakura", rows, max_batches=20)
        result = "\n".join(self.store.context_blocks("sakura", rows, [], "你好", now=self.now))
        for age in range(7):
            self.assertIn((self.now-timedelta(days=age)).date().isoformat(), result)
        self.assertNotIn((self.now-timedelta(days=8)).date().isoformat(), result)

    def test_legacy_reference_is_stable_without_editing_record(self):
        row = self.row("旧消息", index=1)
        del row["id"]
        before = dict(row)
        self.assertEqual(source_id(row), source_id(dict(row)))
        self.store.update("sakura", [row])
        self.assertEqual(row, before)

    def test_invalid_model_source_references_are_not_saved(self):
        row = self.row("真实经历", index=1)
        self.store.update("sakura", [row], summarize=lambda rows: [
            {"text": "无依据事实", "source_ids": ["invented"]}])
        saved = self.store._load("sakura")
        self.assertEqual(saved["days"][record_day(row)]["items"], [])

    def test_user_correction_retires_old_summary_and_does_not_affect_other_owner(self):
        old = self.row("我喜欢咖啡", age=2, index=1)
        correction = self.row("更正：咖啡改为红茶", index=2)
        self.store.update("sakura", [old, correction])
        doc = self.store._load("sakura")
        self.assertEqual(doc["days"][record_day(old)]["items"][0]["status"], "superseded")
        result = "\n".join(self.store.context_blocks("sakura", [old, correction], [], "你好", now=self.now))
        self.assertNotIn("用户原话摘录：我喜欢咖啡", result)

    def test_latest_correction_is_respected_before_background_summary_finishes(self):
        old = self.row("我喜欢咖啡", age=1, index=1)
        self.store.update("sakura", [old])
        correction = self.row("更正：咖啡改为红茶", index=2)
        blocks = "\n".join(self.store.context_blocks("sakura", [old, correction], [correction], "你好", now=self.now))
        self.assertNotIn("用户原话摘录：我喜欢咖啡", blocks)

    def test_later_reaffirmation_is_not_retired_by_an_earlier_correction(self):
        rows = [self.row("我喜欢咖啡", age=2, index=1),
                self.row("更正：咖啡改为红茶", age=1, index=2),
                self.row("我又开始喜欢咖啡了", index=3)]
        self.store.update("sakura", rows)
        doc = self.store._load("sakura")
        self.assertEqual(doc["days"][record_day(rows[-1])]["items"][0]["status"], "active")

    def test_joke_retraction_and_manual_revision_keep_audit_history(self):
        rows = [self.row("我喜欢咖啡", age=1, index=1), self.row("刚才我喜欢咖啡是开玩笑", index=2)]
        self.store.update("sakura", rows)
        doc = self.store._load("sakura")
        oldday = record_day(rows[0])
        note = doc["days"][oldday]["items"][0]
        self.assertEqual(note["status"], "retracted")
        revised = self.store.revise("sakura", oldday, note["id"], "用户并没有确定偏好", "uncertain")
        self.assertEqual(revised["certainty"], "uncertain")
        self.assertTrue(revised["revisions"])
        self.assertIsNone(self.store.revise("huadan", oldday, note["id"], "不能跨角色修改"))
        blocks = "\n".join(self.store.context_blocks("sakura", rows, [], "你好", now=self.now))
        self.assertIn("用户并没有确定偏好", blocks)

    def test_manual_revision_is_not_reversed_by_already_reviewed_correction(self):
        rows = [self.row("我喜欢咖啡", age=1, index=1), self.row("更正：咖啡改为红茶", index=2)]
        self.store.update("sakura", rows)
        day = record_day(rows[0])
        note = self.store._load("sakura")["days"][day]["items"][0]
        self.store.revise("sakura", day, note["id"], "咖啡的说法已作废，目前喜欢红茶")
        blocks = "\n".join(self.store.context_blocks("sakura", rows, [], "你好", now=self.now))
        self.assertIn("目前喜欢红茶", blocks)

    def test_deleted_originals_are_never_resurrected_from_cached_summaries(self):
        rows = [self.row("应该被清除的旧话题", index=1)]
        self.store.update("sakura", rows)
        self.assertEqual(self.store.context_blocks("sakura", [], [], "旧话题", now=self.now), [])

    def test_background_generation_does_not_block_calling_thread(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def update(*args, **kwargs):
            entered.set()
            release.wait(3)
            finished.set()
            return False
        with patch.object(self.store, "update", side_effect=update):
            start = time.monotonic()
            self.store.schedule("sakura", [self.row("项目", index=1)], {})
            self.assertLess(time.monotonic()-start, .5)
            self.assertTrue(entered.wait(1))
            release.set()
            self.assertTrue(finished.wait(1))

    def test_shared_budget_accounts_for_personality_images_memory_and_reply(self):
        budget = PromptBudget({"context_window_tokens": 12000, "reply_reserve_tokens": 2000,
            "context_safety_tokens": 1000, "image_reserve_tokens": 4000})
        messages = [{"role": "user", "content": "旧内容"*300},
                    {"role": "assistant", "content": "旧回复"*300},
                    {"role": "user", "content": [{"type": "text", "text": "当前截图"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
        result = budget.fit("角色个性"*100, messages, ["长期记忆"*60, "每日摘要"*60])
        self.assertLessEqual(budget.cost(result)+budget.reply+budget.safety, budget.window)
        self.assertEqual(result[-1], messages[-1])

    def test_overflow_keeps_contiguous_suffix_and_never_changes_originals(self):
        rows = [self.row("message {} ".format(i)+"长"*100, index=i) for i in range(20)]
        before = copy.deepcopy(rows)
        client = self.client(context_window_tokens=11000, reply_reserve_tokens=1024)
        result = client.build_messages(rows, {})
        kept = [r["content"] for r in result[1:]]
        self.assertEqual(kept, [r["content"] for r in rows[-len(kept):]])
        self.assertEqual(rows, before)
        self.assertLess(client.context_report["input_estimate"], client.context_report["window"])

    def test_oversized_current_message_raises_instead_of_silent_truncation(self):
        with self.assertRaises(ContextBudgetError):
            PromptBudget({"context_window_tokens": 1024}).fit("设定", [{"role": "user", "content": "长"*3000}])

    def test_long_term_correction_and_jokes_are_not_permanent_facts(self):
        memory = MemoryStore(Path(self.temp.name)/"memory.json", Path(self.temp.name)/"images")
        memory.process_turn("我喜欢咖啡", character_id="sakura")
        memory.process_turn("我喜欢咖啡", character_id="huadan")
        memory.process_turn("更正：咖啡改为红茶", character_id="sakura")
        self.assertNotIn("我喜欢咖啡", memory.format_context("咖啡", character_id="sakura"))
        self.assertIn("红茶", memory.format_context("红茶", character_id="sakura"))
        self.assertIn("我喜欢咖啡", memory.format_context("咖啡", character_id="huadan"))
        self.assertEqual(memory.process_turn("我可能喜欢宇宙旅行", character_id="sakura"), [])
        memory.process_turn("刚才我喜欢红茶是开玩笑", character_id="sakura")
        self.assertNotIn("我喜欢红茶", memory.format_context("红茶", character_id="sakura"))

    def test_stable_commitments_are_preserved_in_long_term_memory(self):
        memory = MemoryStore(Path(self.temp.name)/"memory.json", Path(self.temp.name)/"images")
        memory.process_turn("我们约好以后每次开游戏都先备份存档", character_id="sakura", source_refs=["message1"])
        result = memory.format_context("备份存档", character_id="sakura")
        self.assertIn("备份存档", result)
        self.assertNotIn("备份存档", memory.format_context("备份存档", character_id="huadan"))

    def test_scheduled_memory_captures_sources_before_next_turn_arrives(self):
        from ui.app import DesktopPetApp
        app = DesktopPetApp.__new__(DesktopPetApp)
        app.character_id = "sakura"
        app.app_config = {}
        app._runtime_errors = []
        rows = [self.row("我喜欢红茶", index=1), self.row("记住了", "assistant", index=1)]
        store = SimpleNamespace(get_character_messages=lambda owner: list(rows),
            process_memory_turn=Mock(), history_memory=Mock())
        app.store = store
        targets = []
        def capture(**kwargs):
            targets.append(kwargs["target"])
            return SimpleNamespace(start=lambda: None)
        with patch("ui.app.threading.Thread", side_effect=capture):
            app._schedule_memory_processing("我喜欢红茶", "记住了")
        rows.append(self.row("下一轮的问题", index=2))
        targets[0]()
        self.assertEqual(store.process_memory_turn.call_args.kwargs["source_refs"], [source_id(r) for r in rows[:2]])
        self.assertEqual(len(store.history_memory.schedule.call_args.args[1]), 2)


if __name__ == "__main__":
    unittest.main()
