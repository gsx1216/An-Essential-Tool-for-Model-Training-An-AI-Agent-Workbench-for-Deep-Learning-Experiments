"""完整历史摘要回归；依赖均为假对象，原文和已有摘要保持不可变。"""

import asyncio
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace

from app.logging_config import DEFAULT_LOG_DIR, configure_logging
from app.models.context_state import ContextCompactionError, ContextMessage, ContextSummary, ContextTooLargeError, HistoryGap
from app.services.context_manager import ContextManager
from scripts import check_context_manager as exercise


class ContextManagerExerciseRegression(exercise.ContextManagerChecks):
    """把原先的全部练习检查纳入每次 unittest 常规回归。"""


class ContextCompactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.counter = exercise.FakeTokenCounter()
        self.summarizer = exercise.FakeSummaryGenerator()

    def manager(self, **options):
        settings = dict(system_prompt="S", max_input_tokens=1000, compact_at_tokens=700,
                        max_summary_tokens=200, keep_recent_turns=2)
        settings.update(options)
        return ContextManager(self.counter, self.summarizer, **settings)

    async def test_only_covered_gaps_enter_new_summary_while_recent_gaps_stay_visible(self):
        summary = ContextSummary("已有目标与限制", 10, 2, (HistoryGap(2, 3),))
        history = [ContextMessage(11, "human", "A" * 120), ContextMessage(12, "ai", "B" * 120),
                   ContextMessage(15, "human", "C" * 120), ContextMessage(16, "ai", "D" * 120),
                   ContextMessage(19, "human", "E" * 120), ContextMessage(20, "ai", "F" * 120),
                   ContextMessage(21, "human", "当前问题")]
        original = deepcopy(history)
        result = await self.manager(keep_recent_turns=1).prepare(history, summary)
        self.assertTrue(result.was_compacted)
        self.assertFalse(result.input_compressed)
        self.assertEqual(result.summary.summarized_until_seq, 16)
        self.assertEqual(result.summary.version, 3)
        self.assertEqual(result.summary.missing_ranges, (HistoryGap(2, 3), HistoryGap(13, 14)))
        self.assertEqual(self.summarizer.calls[0]["missing_ranges"], result.summary.missing_ranges)
        self.assertEqual(result.missing_ranges, (*result.summary.missing_ranges, HistoryGap(17, 18)))
        self.assertTrue(any("17～18" in text for _, text in result.model_messages))
        self.assertEqual(history, original)
        self.assertEqual(summary, ContextSummary("已有目标与限制", 10, 2, (HistoryGap(2, 3),)))

    async def test_cut_before_gap_can_cover_unanswered_question_without_fabricating_ai(self):
        history = [ContextMessage(1, "human", "A" * 140), ContextMessage(2, "ai", "B" * 140),
                   ContextMessage(3, "human", "C" * 140), ContextMessage(5, "ai", "D" * 140),
                   ContextMessage(6, "human", "E" * 140), ContextMessage(7, "ai", "F" * 140),
                   ContextMessage(8, "human", "当前问题")]
        result = await self.manager().prepare(history)
        self.assertEqual(result.summary.summarized_until_seq, 3)
        self.assertEqual([m.seq for m in self.summarizer.calls[0]["messages"]], [1, 2, 3])
        self.assertEqual(result.summary.missing_ranges, ())
        self.assertEqual(result.missing_ranges, (HistoryGap(4, 4),))
        self.assertIn(("ai", "D" * 140), result.model_messages)

    async def test_new_summary_can_be_reused_next_request_without_resummarizing_covered_raw_text(self):
        manager = self.manager()
        history = exercise.long_history()
        first = await manager.prepare(history)
        remaining = [m for m in history if m.seq > first.summary.summarized_until_seq]
        remaining.extend([ContextMessage(12, "ai", "本轮回复"), ContextMessage(13, "human", "继续追问")])
        result = await manager.prepare(remaining, first.summary)
        self.assertFalse(result.was_compacted)
        self.assertIs(result.summary, first.summary)
        self.assertEqual(len(self.summarizer.calls), 1)
        self.assertEqual(result.model_messages[-1], ("human", "继续追问"))

    async def test_final_request_over_hard_budget_is_rejected_after_full_recount(self):
        history = exercise.long_history()
        self.summarizer.reply = "Z" * 180
        probe = self.manager()
        candidate = probe._assemble_messages(history[6:], ContextSummary(self.summarizer.reply, 6, 1))
        candidate_tokens = self.counter.count_messages(candidate)
        manager = self.manager(max_input_tokens=candidate_tokens - 1, compact_at_tokens=candidate_tokens - 20)
        original = deepcopy(history)
        with self.assertRaisesRegex(ContextTooLargeError, "完整上下文仍超过预算"):
            await manager.prepare(history)
        self.assertEqual(history, original)
        self.assertEqual(len(self.summarizer.calls), 1)

    async def test_summary_that_does_not_reduce_context_is_not_accepted(self):
        history = [ContextMessage(1, "human", "a"), ContextMessage(2, "ai", "b"),
                   ContextMessage(3, "human", "c"), ContextMessage(4, "ai", "d"),
                   ContextMessage(5, "human", "x" * 100)]
        with self.assertRaisesRegex(ContextCompactionError, "未减少输入占用"):
            await self.manager(compact_at_tokens=100, max_summary_tokens=90, keep_recent_turns=1).prepare(history)
        self.assertEqual(len(self.summarizer.calls), 1)

    async def test_summary_exactly_at_own_budget_is_accepted_without_stripping(self):
        self.summarizer.reply = " \n摘要\n "
        budget = self.counter.count_messages([("human", self.summarizer.reply)])
        result = await self.manager(max_summary_tokens=budget).prepare(exercise.long_history())
        self.assertEqual(result.summary.content, self.summarizer.reply)
        self.assertTrue(result.was_compacted)

    async def test_bad_result_types_are_rejected(self):
        for reply in (True, 10, [], {}):
            with self.subTest(reply_type=type(reply).__name__):
                self.summarizer.reply = reply
                with self.assertRaises(ContextCompactionError):
                    await self.manager().prepare(exercise.long_history())

    async def test_cancellation_does_not_replace_old_summary_or_original_history(self):
        self.summarizer.summarize = AsyncMock(side_effect=asyncio.CancelledError())
        history = exercise.long_history(11)
        original = deepcopy(history)
        summary = ContextSummary("已有摘要", 10, 3)
        with self.assertRaises(asyncio.CancelledError):
            await self.manager().prepare(history, summary)
        self.assertEqual(history, original)
        self.assertEqual(summary.version, 3)

    async def test_reused_manager_does_not_store_per_session_history_or_version(self):
        self.summarizer.summarize = AsyncMock(side_effect=["会话A摘要", "会话B摘要"])
        manager = self.manager()
        a = await manager.prepare(exercise.long_history())
        b = await manager.prepare(exercise.long_history())
        self.assertEqual(a.summary.version, 1)
        self.assertEqual(b.summary.version, 1)
        self.assertEqual(a.summary.content, "会话A摘要")
        self.assertEqual(b.summary.content, "会话B摘要")

    async def test_long_current_question_and_old_history_can_both_be_compacted(self):
        history = exercise.long_history()
        history[-1] = ContextMessage(11, "human", "长问题及必要材料" * 1000)
        original = deepcopy(history)
        compressor = SimpleNamespace(compress=AsyncMock(return_value="当前问题：如何调整学习率？"))
        manager = self.manager(input_compressor=compressor)
        result = await manager.prepare(history)
        self.assertTrue(result.input_compressed)
        self.assertTrue(result.was_compacted)
        self.assertLessEqual(result.input_tokens, manager.max_input_tokens)
        self.assertEqual(result.summary.summarized_until_seq, 6)
        self.assertIn("如何调整学习率", result.model_messages[-1][1])
        self.assertEqual(history, original)
        compressor.compress.assert_awaited_once()
        self.assertEqual(len(self.summarizer.calls), 1)

    async def test_combined_compaction_failure_never_replaces_original_question(self):
        history = exercise.long_history()
        history[-1] = ContextMessage(11, "human", "长材料" * 1000)
        original = deepcopy(history)
        compressor = SimpleNamespace(compress=AsyncMock(return_value="精简问题"))
        self.summarizer.error = ContextCompactionError("历史摘要失败")
        with self.assertRaises(ContextCompactionError):
            await self.manager(input_compressor=compressor).prepare(history)
        self.assertEqual(history, original)

    async def test_invalid_summary_logs_fixed_reason_without_summary_or_history_text(self):
        with TemporaryDirectory() as directory:
            configure_logging(Path(directory), console=False, force=True, log_format="text")
            try:
                self.summarizer.reply = "PRIVATE_SUMMARY" * 100
                with self.assertRaises(ContextCompactionError):
                    await self.manager().prepare(exercise.long_history())
                logs = "\n".join(path.read_text(encoding="utf-8") for path in Path(directory).rglob("*.txt"))
                self.assertIn("历史摘要无效或未减少输入占用", logs)
                self.assertNotIn("PRIVATE_SUMMARY", logs)
                self.assertNotIn("当前这个参数怎么调整", logs)
            finally:
                configure_logging(DEFAULT_LOG_DIR, console=False, force=True)


if __name__ == "__main__":
    unittest.main()
