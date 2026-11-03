"""长输入恢复分支：保留原文、完整计数、明确失败；无数据库和模型网络。"""

import asyncio
from copy import deepcopy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models.context_state import ContextMessage, ContextSummary, ContextTooLargeError, HistoryGap, InputCompressionError
from app.services.context_manager import COMPRESSED_INPUT_PREFIX, ContextManager
from scripts.check_context_manager import FakeSummaryGenerator, FakeTokenCounter, short_history


class ContextLongInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.counter = FakeTokenCounter()
        self.summarizer = FakeSummaryGenerator()
        self.compressor = SimpleNamespace(compress=AsyncMock(return_value="问题：调整学习率。限制：不能改变模型。"))
        self.manager = ContextManager(
            self.counter, self.summarizer, input_compressor=self.compressor,
            system_prompt="S", max_input_tokens=1000, compact_at_tokens=900,
            max_summary_tokens=200,
        )

    async def test_long_question_recovers_and_original_keeps_same_object_and_content(self):
        history = short_history()
        history[-1] = ContextMessage(3, "human", "长材料" * 1000)
        before = deepcopy(history)
        current = history[-1]
        result = await self.manager.prepare(history)
        self.assertTrue(result.input_compressed)
        self.assertFalse(result.was_compacted)
        self.assertEqual(history, before)
        self.assertIs(history[-1], current)
        self.compressor.compress.assert_awaited_once()
        self.assertEqual(self.compressor.compress.call_args.args[0], current.content)
        self.assertEqual(result.model_messages[1:3], [(m.role, m.content) for m in history[:2]])
        self.assertEqual(result.model_messages[-1], ("human", COMPRESSED_INPUT_PREFIX + self.compressor.compress.return_value))
        self.assertEqual(result.input_tokens, self.counter.count_messages(result.model_messages))
        self.assertLessEqual(result.input_tokens, self.manager.max_input_tokens)
        self.assertEqual(self.summarizer.calls, [])

    async def test_normal_input_does_not_call_compressor(self):
        result = await self.manager.prepare(short_history())
        self.assertFalse(result.input_compressed)
        self.compressor.compress.assert_not_awaited()

    async def test_existing_summary_and_gap_not_lost(self):
        summary = ContextSummary("历史条件", 10, 2, (HistoryGap(2, 3),))
        history = [ContextMessage(13, "human", "长材料" * 1000)]
        result = await self.manager.prepare(history, summary)
        self.assertIs(result.summary, summary)
        self.assertEqual(result.missing_ranges, (HistoryGap(2, 3), HistoryGap(11, 12)))
        self.assertTrue(any("历史缺失提示" in text for _, text in result.model_messages))

    async def test_compressed_input_between_soft_and_hard_budget_can_continue(self):
        self.compressor.compress.return_value = "x" * 870
        result = await self.manager.prepare([ContextMessage(1, "human", "x" * 2000)])
        self.assertGreaterEqual(result.input_tokens, self.manager.compact_at_tokens)
        self.assertLessEqual(result.input_tokens, self.manager.max_input_tokens)
        self.assertTrue(result.input_compressed)

    async def test_final_full_context_is_recounted_not_just_current_question(self):
        history = [ContextMessage(1, "human", "a" * 600), ContextMessage(2, "ai", "回复"),
                   ContextMessage(3, "human", "x" * 2000)]
        before = deepcopy(history)
        self.compressor.compress.return_value = "x" * 500  # 单独能放下，连同历史却放不下。
        with self.assertRaisesRegex(ContextTooLargeError, "完整上下文仍超过预算.*拆分任务"):
            await self.manager.prepare(history)
        self.assertEqual(history, before)
        self.compressor.compress.assert_awaited_once()

    async def test_history_alone_too_large_stops_without_model_calls_or_dropping_history(self):
        history = [ContextMessage(1, "human", "a" * 1200), ContextMessage(2, "ai", "回复"),
                   ContextMessage(3, "human", "x" * 2000)]
        before = deepcopy(history)
        with self.assertRaisesRegex(ContextTooLargeError, "历史资料已占满.*拆分任务"):
            await self.manager.prepare(history)
        self.compressor.compress.assert_not_awaited()
        self.assertEqual(history, before)

    async def test_bad_results_never_replace_original(self):
        for reply in (None, [], "", "   ", "x" * 2000):
            with self.subTest(reply_type=type(reply).__name__):
                history = [ContextMessage(1, "human", "x" * 2000)]
                before = deepcopy(history)
                self.compressor.compress.return_value = reply
                with self.assertRaises((InputCompressionError, ContextTooLargeError)):
                    await self.manager.prepare(history)
                self.assertEqual(history, before)

    async def test_failure_and_cancellation_preserve_input_and_propagate(self):
        for error in (InputCompressionError("精简失败"), asyncio.CancelledError()):
            history = [ContextMessage(1, "human", "x" * 2000)]
            before = deepcopy(history)
            self.compressor.compress.side_effect = error
            with self.assertRaises(type(error)):
                await self.manager.prepare(history)
            self.assertEqual(history, before)


if __name__ == "__main__":
    unittest.main()
