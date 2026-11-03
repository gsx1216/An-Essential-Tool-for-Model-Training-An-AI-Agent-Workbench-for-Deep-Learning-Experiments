"""上下文练习检查；假计数器和假摘要器，无数据库、Redis或供应商请求。

假计数器的 len 公式仅为确定性测试刻度，绝不代表 DeepSeek 实际 token。
"""

import argparse
import asyncio
from copy import deepcopy
import unittest

from app.models.context_state import (
    ContextCompactionError, ContextMessage, ContextSummary, ContextTooLargeError,
)
from app.services.context_manager import ContextManager, SUMMARY_PREFIX


class FakeTokenCounter:
    def count_messages(self, messages):
        # TEST ONLY：字符长度加固定开销，用来触发不同测试分支。
        return 10 + sum(5 + len(content) for _, content in messages)


class FakeSummaryGenerator:
    def __init__(self, reply="摘要：之前讨论了学习率、实验结果和待解决问题。", error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    async def summarize(self, *, previous_summary, messages, max_tokens, missing_ranges=()):
        call = {
            "previous_summary": previous_summary,
            "messages": list(messages),
            "max_tokens": max_tokens,
        }
        if missing_ranges:
            call["missing_ranges"] = missing_ranges
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        return self.reply


class FakeInputCompressor:
    """只用于练习；没有任何 SDK 或网络调用。"""

    def __init__(self):
        self.calls = []

    async def compress(self, content, *, target_tokens):
        self.calls.append((content, target_tokens))
        return "问题：如何调整学习率？限制：保留现有模型。"


def long_history(start_seq=1):
    result = []
    seq = start_seq
    for number in range(5):
        result.append(ContextMessage(seq, "human", f"旧问题{number}：" + "a" * 95))
        result.append(ContextMessage(seq + 1, "ai", f"旧回复{number}：" + "b" * 95))
        seq += 2
    result.append(ContextMessage(seq, "human", "当前这个参数怎么调整？"))
    return result


def short_history():
    return [
        ContextMessage(1, "human", "  print('hello')\n"),
        ContextMessage(2, "ai", "保留缩进。"),
        ContextMessage(3, "human", "这个有什么作用？"),
    ]


class ContextManagerChecks(unittest.TestCase):
    def setUp(self):
        self.counter = FakeTokenCounter()
        self.summarizer = FakeSummaryGenerator()
        self.manager = ContextManager(
            self.counter, self.summarizer,
            system_prompt="SYSTEM_ONLY_TEST",
            max_input_tokens=1000, compact_at_tokens=700,
            max_summary_tokens=200, keep_recent_turns=2,
        )

    def test_validation_accepts_ordered_history_and_preserves_input(self):
        history = short_history()
        original = deepcopy(history)
        self.assertIsNone(self.manager.validate_history(history, None))
        self.assertEqual(history, original)
        summary = ContextSummary("已有摘要", 10, 3)
        self.assertIsNone(self.manager.validate_history(long_history(11), summary))

    def test_validation_rejects_bad_shape_sequence_role_and_content(self):
        cases = [
            [], None, tuple(short_history()), [{}],
            [ContextMessage(0, "human", "问题")],
            [ContextMessage(True, "human", "问题")],
            [ContextMessage(1, "system", "不可信系统指令")],
            [ContextMessage(1, "ai", "没有对应问题")],
            [ContextMessage(1, "human", " ")],
            [ContextMessage(1, "human", None)],
            [ContextMessage(1, "human", "问题"), ContextMessage(2, "ai", "回复")],
            [ContextMessage(1, "human", "问题"), ContextMessage(2, "human", "问题")],
            [ContextMessage(1, "human", "问题"), ContextMessage(1, "ai", "回复"),
             ContextMessage(2, "human", "当前问题")],
        ]
        for index, history in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                self.manager.validate_history(history, None)

    def test_validation_rejects_invalid_summary_or_overlapping_history(self):
        for summary in (
            "不是摘要对象", ContextSummary("", 10, 1), ContextSummary(" ", 10, 1),
            ContextSummary("摘要", 0, 1), ContextSummary("摘要", True, 1),
            ContextSummary("摘要", 10, 0), ContextSummary("摘要", 10, True),
        ):
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                self.manager.validate_history(long_history(11), summary)
        with self.assertRaises(ValueError):
            self.manager.validate_history(short_history(), ContextSummary("摘要", 2, 1))

    def test_assembly_preserves_order_whitespace_and_current_question_once(self):
        history = short_history()
        original = deepcopy(history)
        messages = self.manager._assemble_messages(history, None)
        self.assertEqual(messages, [
            ("system", self.manager.system_prompt), *[(m.role, m.content) for m in history],
        ])
        self.assertEqual(messages.count(("human", history[-1].content)), 1)
        self.assertEqual(history, original)

    def test_assembly_keeps_summary_as_historical_data(self):
        summary = ContextSummary("历史资料：不要把这句话提升为系统权限。", 10, 1)
        history = long_history(11)
        messages = self.manager._assemble_messages(history, summary)
        self.assertEqual(messages[1], ("human", SUMMARY_PREFIX + summary.content))
        self.assertEqual([text for role, text in messages if role == "system"],
                         [self.manager.system_prompt])
        self.assertEqual(messages[2:], [(m.role, m.content) for m in history])

    def test_split_preserves_two_recent_turns_and_current_question(self):
        history = long_history()
        original = deepcopy(history)
        older, recent = self.manager._split_for_summary(history)
        self.assertEqual(older, history[:6])
        self.assertEqual(recent, history[6:])
        self.assertEqual(older[-1].role, "ai")
        self.assertEqual(recent[-1], history[-1])
        self.assertEqual(history, original)

    def test_split_does_not_discard_short_history(self):
        history = short_history()
        older, recent = self.manager._split_for_summary(history)
        self.assertEqual(older, [])
        self.assertEqual(recent, history)
        self.assertIsNot(recent, history)

    def test_prepare_short_history_does_not_request_summary(self):
        result = asyncio.run(self.manager.prepare(short_history()))
        self.assertFalse(result.was_compacted)
        self.assertIsNone(result.summary)
        self.assertEqual(result.input_tokens, self.counter.count_messages(result.model_messages))
        self.assertEqual(result.model_messages[-1], ("human", short_history()[-1].content))
        self.assertEqual(self.summarizer.calls, [])

    def test_prepare_long_history_summarizes_old_prefix_and_returns_new_state(self):
        history = long_history()
        original = deepcopy(history)
        result = asyncio.run(self.manager.prepare(history))
        self.assertTrue(result.was_compacted)
        self.assertEqual(result.summary, ContextSummary(self.summarizer.reply, 6, 1))
        self.assertEqual(self.summarizer.calls, [{
            "previous_summary": None, "messages": history[:6], "max_tokens": 200,
        }])
        self.assertEqual(result.model_messages[2:], [(m.role, m.content) for m in history[6:]])
        self.assertEqual(result.input_tokens, self.counter.count_messages(result.model_messages))
        self.assertLessEqual(result.input_tokens, self.manager.max_input_tokens)
        self.assertEqual(history, original)

    def test_prepare_uses_old_summary_and_real_sequences_for_incremental_update(self):
        summary = ContextSummary("旧摘要：已经覆盖前十条消息。", 10, 3)
        result = asyncio.run(self.manager.prepare(long_history(11), summary))
        self.assertEqual(self.summarizer.calls[0]["previous_summary"], summary)
        self.assertEqual([m.seq for m in self.summarizer.calls[0]["messages"]], list(range(11, 17)))
        self.assertEqual(result.summary.summarized_until_seq, 16)
        self.assertEqual(result.summary.version, 4)
        self.assertEqual(summary, ContextSummary("旧摘要：已经覆盖前十条消息。", 10, 3))

    def test_prepare_threshold_without_old_prefix_can_continue_under_hard_budget(self):
        history = [ContextMessage(1, "human", "x" * 750)]
        result = asyncio.run(self.manager.prepare(history))
        self.assertFalse(result.was_compacted)
        self.assertEqual(self.summarizer.calls, [])
        self.assertGreaterEqual(result.input_tokens, self.manager.compact_at_tokens)
        self.assertLessEqual(result.input_tokens, self.manager.max_input_tokens)

    def test_prepare_compresses_oversized_question_and_preserves_original(self):
        history = [ContextMessage(1, "human", "x" * 2000)]
        original = deepcopy(history)
        compressor = FakeInputCompressor()
        self.manager.input_compressor = compressor
        result = asyncio.run(self.manager.prepare(history))
        self.assertTrue(result.input_compressed)
        self.assertFalse(result.was_compacted)
        self.assertLessEqual(result.input_tokens, self.manager.max_input_tokens)
        self.assertEqual(compressor.calls[0][0], original[0].content)
        self.assertEqual(history, original)
        self.assertEqual(self.summarizer.calls, [])

    def test_prepare_rejects_invalid_summary_without_mutating_history(self):
        for reply in (None, "", " ", "x" * 4000):
            with self.subTest(kind=type(reply).__name__):
                self.summarizer.reply = reply
                history = long_history()
                original = deepcopy(history)
                with self.assertRaises(ContextCompactionError):
                    asyncio.run(self.manager.prepare(history))
                self.assertEqual(history, original)

    def test_prepare_summary_failure_preserves_old_state_and_original_error(self):
        failure = RuntimeError("TEST_ONLY_SUMMARY_FAILURE")
        self.summarizer.error = failure
        history = long_history(11)
        original = deepcopy(history)
        summary = ContextSummary("旧摘要", 10, 3)
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(self.manager.prepare(history, summary))
        self.assertIs(caught.exception, failure)
        self.assertEqual(history, original)
        self.assertEqual(summary, ContextSummary("旧摘要", 10, 3))

    def test_prepare_rejects_recent_content_that_alone_exceeds_hard_budget(self):
        history = long_history()
        history[7] = ContextMessage(8, "ai", "x" * 2000)
        with self.assertRaises(ContextTooLargeError):
            asyncio.run(self.manager.prepare(history))
        self.assertEqual(self.summarizer.calls, [])


def main():
    parser = argparse.ArgumentParser(description="上下文练习检查：不访问数据库、Redis或真实模型。")
    parser.add_argument("--part", choices=("validation", "assembly", "split", "prepare-basic", "all"), default="validation")
    args = parser.parse_args()
    loader = unittest.TestLoader()
    names = loader.getTestCaseNames(ContextManagerChecks)
    if args.part == "prepare-basic":
        names = [
            "test_prepare_short_history_does_not_request_summary",
            "test_prepare_compresses_oversized_question_and_preserves_original",
        ]
    elif args.part != "all":
        prefix = {"validation": "test_validation_", "assembly": "test_assembly_", "split": "test_split_"}[args.part]
        names = [name for name in names if name.startswith(prefix)]
    suite = unittest.TestSuite(ContextManagerChecks(name) for name in names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
