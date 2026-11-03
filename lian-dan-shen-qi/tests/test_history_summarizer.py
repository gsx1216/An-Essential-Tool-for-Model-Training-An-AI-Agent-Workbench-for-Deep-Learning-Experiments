"""历史摘要模型适配器：只使用假网关，验证预算、分片、增量和失败。"""

import asyncio
from collections import defaultdict
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from app.models.context_state import ContextCompactionError, ContextMessage, ContextSummary, ContextTooLargeError, HistoryGap
from app.services.context_manager import ContextManager
from app.services.history_summarizer import ModelHistorySummarizer
from scripts.check_context_manager import FakeTokenCounter, long_history


def payload(call):
    return json.loads(call.args[0][1][1].split("\n", 1)[1])


class HistorySummarizerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.counter = FakeTokenCounter()
        self.gateway = SimpleNamespace(invoke=AsyncMock(return_value={"content": "历史目标：调参。限制：保持模型。"}))

    def summarizer(self, **options):
        return ModelHistorySummarizer(self.gateway, self.counter, **options)

    async def test_one_call_includes_old_summary_roles_real_seq_and_missing_ranges(self):
        previous = ContextSummary("已有摘要", 10, 2, (HistoryGap(2, 3),))
        messages = [ContextMessage(13, "human", "新要求：不能更换模型"), ContextMessage(14, "ai", "建议调参")]
        gaps = (HistoryGap(2, 3), HistoryGap(11, 12))
        result = await self.summarizer().summarize(previous_summary=previous, messages=messages,
                                                  max_tokens=200, missing_ranges=gaps)
        self.assertEqual(result, self.gateway.invoke.return_value["content"])
        self.gateway.invoke.assert_awaited_once()
        data = payload(self.gateway.invoke.call_args)
        self.assertEqual(data["已有历史摘要"], previous.content)
        self.assertEqual([m["seq"] for m in data["新增历史片段"]], [13, 14])
        self.assertEqual([m["role"] for m in data["新增历史片段"]], ["human", "ai"])
        self.assertEqual(data["原文缺失范围"], [{"start_seq": 2, "end_seq": 3}, {"start_seq": 11, "end_seq": 12}])
        self.assertEqual(self.gateway.invoke.call_args.kwargs["max_tokens"], 185)

    async def test_multiple_batches_and_oversize_single_message_cover_all_characters_in_order(self):
        messages = [ContextMessage(1, "human", "长问题\n" * 400),
                    ContextMessage(2, "ai", "长回复\n" * 200)]
        original = deepcopy(messages)
        summarizer = self.summarizer(max_request_tokens=900)
        result = await summarizer.summarize(previous_summary=None, messages=messages, max_tokens=100)
        self.assertGreater(self.gateway.invoke.await_count, 1)
        fragments = defaultdict(list)
        for call in self.gateway.invoke.await_args_list:
            self.assertLessEqual(self.counter.count_messages(call.args[0]), 900)
            for part in payload(call)["新增历史片段"]:
                fragments[part["seq"]].append(part)
        for message in messages:
            parts = fragments[message.seq]
            self.assertEqual("".join(p["content"] for p in parts), message.content)
            cursor = 0
            for part in parts:
                self.assertEqual(part["start_char"], cursor)
                cursor = part["end_char"]
            self.assertEqual(cursor, len(message.content))
        for call in self.gateway.invoke.await_args_list[1:]:
            self.assertEqual(payload(call)["已有历史摘要"], result)
        self.assertEqual(messages, original)

    async def test_call_limit_ends_without_returning_partial_summary_as_complete(self):
        with self.assertRaisesRegex(ContextTooLargeError, "调用超过.*拆分任务"):
            await self.summarizer(max_request_tokens=900, max_calls=1).summarize(
                previous_summary=None, messages=[ContextMessage(1, "human", "x" * 3000)], max_tokens=100)
        self.assertEqual(self.gateway.invoke.await_count, 1)

    async def test_unusable_request_budget_never_calls_model(self):
        with self.assertRaisesRegex(ContextTooLargeError, "无法放入摘要请求预算"):
            await self.summarizer(max_request_tokens=20).summarize(
                previous_summary=None, messages=[ContextMessage(1, "human", "问题")], max_tokens=100)
        self.gateway.invoke.assert_not_awaited()

    async def test_bad_model_output_and_oversize_summary_are_rejected(self):
        for reply in (None, {}, {"content": ""}, {"content": []}, {"content": "x" * 500}):
            with self.subTest(reply_type=type(reply).__name__):
                self.gateway.invoke.return_value = reply
                with self.assertRaises(ContextCompactionError):
                    await self.summarizer().summarize(previous_summary=None,
                        messages=[ContextMessage(1, "human", "问题")], max_tokens=100)

    async def test_provider_failure_is_fixed_error_and_cancellation_is_propagated(self):
        for error in (RuntimeError("PRIVATE_PROVIDER_BODY"), asyncio.CancelledError()):
            self.gateway.invoke.side_effect = error
            expected = asyncio.CancelledError if isinstance(error, asyncio.CancelledError) else ContextCompactionError
            with self.assertRaises(expected) as caught:
                await self.summarizer().summarize(previous_summary=None,
                    messages=[ContextMessage(1, "human", "问题")], max_tokens=100)
            self.assertNotIn("PRIVATE_PROVIDER_BODY", str(caught.exception))

    async def test_total_timeout_cancels_pending_call(self):
        summarizer = self.summarizer()
        summarizer.timeout_seconds = 0.01
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()
        self.gateway.invoke.side_effect = blocked
        with self.assertRaisesRegex(ContextCompactionError, "历史摘要超时"):
            await summarizer.summarize(previous_summary=None,
                messages=[ContextMessage(1, "human", "问题")], max_tokens=100)

    async def test_invalid_inputs_including_unmarked_gaps_do_not_call_model(self):
        cases = [dict(messages=[]), dict(messages=[ContextMessage(True, "human", "问题")]),
                 dict(messages=[ContextMessage(1, "ai", "错误角色顺序")]),
                 dict(messages=[ContextMessage(3, "human", "缺口未标记")]),
                 dict(messages=[ContextMessage(1, "human", "问题")], max_tokens=True),
                 dict(messages=[ContextMessage(1, "human", "问题")], max_tokens=4097),
                 dict(messages=[ContextMessage(1, "human", "问题")], previous_summary="错误"),
                 dict(messages=[ContextMessage(3, "human", "问题")], missing_ranges=(HistoryGap(1, True),))]
        for case in cases:
            settings = dict(previous_summary=None, max_tokens=100)
            settings.update(case)
            with self.subTest(case=list(case)), self.assertRaises(ValueError):
                await self.summarizer().summarize(**settings)
        self.gateway.invoke.assert_not_awaited()

    async def test_old_gap_outside_old_coverage_is_invalid(self):
        previous = ContextSummary("旧摘要", 10, 2, (HistoryGap(5, 12),))
        with self.assertRaises(ValueError):
            await self.summarizer().summarize(previous_summary=previous,
                messages=[ContextMessage(11, "human", "问题")], max_tokens=100,
                missing_ranges=previous.missing_ranges)
        self.gateway.invoke.assert_not_awaited()

    async def test_invalid_config_and_counter(self):
        for options in (dict(max_calls=True), dict(max_calls=0), dict(max_request_tokens=-1), dict(timeout_seconds=0)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.summarizer(**options)
        summarizer = self.summarizer()
        summarizer.counter = Mock(count_messages=Mock(return_value=True))
        with self.assertRaises(ValueError):
            await summarizer.summarize(previous_summary=None,
                messages=[ContextMessage(1, "human", "问题")], max_tokens=100)
        self.gateway.invoke.assert_not_awaited()

    async def test_budget_too_small_for_summary_overhead_never_calls_model(self):
        with self.assertRaisesRegex(ContextCompactionError, "格式开销"):
            await self.summarizer().summarize(previous_summary=None,
                messages=[ContextMessage(1, "human", "问题")], max_tokens=1)
        self.gateway.invoke.assert_not_awaited()

    async def test_manager_uses_concrete_adapter_and_keeps_current_question_out_of_summary(self):
        history = long_history()
        manager = ContextManager(self.counter, self.summarizer(max_request_tokens=900),
            system_prompt="S", max_input_tokens=1000, compact_at_tokens=700,
            max_summary_tokens=200, keep_recent_turns=2)
        result = await manager.prepare(history)
        self.assertTrue(result.was_compacted)
        self.assertEqual(result.summary.summarized_until_seq, 6)
        self.assertEqual(result.model_messages[-1], ("human", history[-1].content))
        for call in self.gateway.invoke.await_args_list:
            self.assertTrue(all(part["seq"] <= 6 for part in payload(call)["新增历史片段"]))
            self.assertNotIn(history[-1].content, call.args[0][1][1])


if __name__ == "__main__":
    unittest.main()
