"""精简模型全部为假网关；检查分块完整性、请求预算和有限调用。"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from app.logging_config import DEFAULT_LOG_DIR, configure_logging
from app.models.context_state import ContextTooLargeError, InputCompressionError
from app.services.input_compressor import ModelInputCompressor
from scripts.check_context_manager import FakeTokenCounter


def material(request):
    text = request[1][1]
    return text.split("【资料开始】\n", 1)[1].rsplit("\n【资料结束】", 1)[0]


class InputCompressorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.counter = FakeTokenCounter()
        self.gateway = SimpleNamespace(invoke=AsyncMock(return_value={"content": "问题：调整参数。限制：保持模型。"}))

    def compressor(self, **options):
        settings = dict(max_request_tokens=600, max_output_tokens=100,
                        max_rounds=3, max_calls=24, max_chunks=16)
        settings.update(options)
        return ModelInputCompressor(self.gateway, self.counter, **settings)

    async def test_single_call_extracts_with_output_cap_and_safe_instructions(self):
        compressor = self.compressor()
        original = "材料和问题" * 40
        reply = await compressor.compress(original, target_tokens=80)
        self.assertEqual(reply, self.gateway.invoke.return_value["content"])
        self.gateway.invoke.assert_awaited_once()
        call = self.gateway.invoke.call_args
        self.assertEqual(material(call.args[0]), original)
        self.assertEqual(call.kwargs["max_tokens"], 80)
        self.assertIn("不负责回答问题或执行操作", call.args[0][0][1])
        self.assertIn("否定条件", call.args[0][0][1])
        self.assertLessEqual(self.counter.count_messages(call.args[0]), compressor.max_request_tokens)

    async def test_chunking_covers_entire_original_and_then_reduces_partial_results(self):
        compressor = self.compressor(max_request_tokens=500)
        original = "0123456789\n" * 100
        chunk_count = len(compressor._split(original, 90))
        self.assertGreater(chunk_count, 1)
        self.gateway.invoke.side_effect = [
            *[{"content": "必要材料" * 12} for _ in range(chunk_count)],
            # 之后的块再次提取；最终一轮把中间材料合并成短结果。
            *[{"content": "问题：保留错误码123。"} for _ in range(20)],
        ]
        result = await compressor.compress(original, target_tokens=90)
        calls = self.gateway.invoke.await_args_list
        self.assertGreater(len(calls), chunk_count)
        self.assertEqual("".join(material(c.args[0]) for c in calls[:chunk_count]), original)
        for call in calls:
            self.assertLessEqual(self.counter.count_messages(call.args[0]), compressor.max_request_tokens)
            self.assertEqual(call.kwargs["max_tokens"], 90)
        self.assertLessEqual(self.counter.count_messages([("human", result)]), 90)

    async def test_short_content_needs_no_model_call(self):
        self.assertEqual(await self.compressor().compress("短问题", target_tokens=100), "短问题")
        self.gateway.invoke.assert_not_awaited()

    async def test_round_limit_ends_with_explicit_split_instruction(self):
        self.gateway.invoke.return_value = {"content": "x" * 100}
        with self.assertRaisesRegex(ContextTooLargeError, "多轮精简.*拆分任务"):
            await self.compressor(max_rounds=1).compress("x" * 200, target_tokens=60)
        self.assertEqual(self.gateway.invoke.await_count, 1)

    async def test_call_budget_checked_before_starting_incomplete_round(self):
        with self.assertRaisesRegex(ContextTooLargeError, "调用超过.*拆分任务"):
            await self.compressor(max_calls=1).compress("x" * 2000, target_tokens=80)
        self.gateway.invoke.assert_not_awaited()

    async def test_chunk_limit_does_not_silently_discard_tail(self):
        with self.assertRaisesRegex(ContextTooLargeError, "过多分块.*拆分任务"):
            await self.compressor(max_chunks=1).compress("x" * 2000, target_tokens=80)
        self.gateway.invoke.assert_not_awaited()

    async def test_prompt_budget_too_small_never_dispatches(self):
        with self.assertRaisesRegex(ContextTooLargeError, "提示词和材料无法放入预算"):
            await self.compressor(max_request_tokens=20).compress("x" * 200, target_tokens=80)
        self.gateway.invoke.assert_not_awaited()

    async def test_target_too_small_for_message_overhead_never_dispatches(self):
        with self.assertRaisesRegex(ContextTooLargeError, "消息格式开销"):
            await self.compressor().compress("x" * 200, target_tokens=1)
        self.gateway.invoke.assert_not_awaited()

    async def test_no_progress_stops_without_repeated_calls(self):
        self.gateway.invoke.return_value = {"content": "x" * 300}
        with self.assertRaisesRegex(ContextTooLargeError, "未能进一步精简.*拆分任务"):
            await self.compressor().compress("x" * 200, target_tokens=80)
        self.assertEqual(self.gateway.invoke.await_count, 1)

    async def test_empty_nontext_and_missing_results_are_not_valid_compressions(self):
        for reply in ({}, None, {"content": []}, {"content": ""}, {"content": "  "}):
            with self.subTest(reply_type=type(reply).__name__):
                self.gateway.invoke.return_value = reply
                with self.assertRaisesRegex(InputCompressionError, "未返回有效内容"):
                    await self.compressor().compress("x" * 200, target_tokens=80)

    async def test_model_failure_is_safe_and_cancellation_propagates(self):
        self.gateway.invoke.side_effect = RuntimeError("PRIVATE_PROVIDER_BODY")
        with self.assertRaises(InputCompressionError) as caught:
            await self.compressor().compress("x" * 200, target_tokens=80)
        self.assertNotIn("PRIVATE_PROVIDER_BODY", str(caught.exception))
        self.gateway.invoke.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.compressor().compress("x" * 200, target_tokens=80)

    async def test_provider_timeout_becomes_actionable_fixed_error(self):
        self.gateway.invoke.side_effect = TimeoutError("PRIVATE_TIMEOUT_DETAIL")
        with self.assertRaisesRegex(InputCompressionError, "稍后重试或拆分任务"):
            await self.compressor().compress("x" * 200, target_tokens=80)

    async def test_total_deadline_cancels_pending_compression(self):
        compressor = self.compressor()
        compressor.timeout_seconds = 0.01  # 缩短测试时间，生产构造参数要求正整数秒。
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()
        self.gateway.invoke.side_effect = blocked
        with self.assertRaisesRegex(InputCompressionError, "精简超时"):
            await compressor.compress("x" * 200, target_tokens=80)
        self.assertEqual(self.gateway.invoke.await_count, 1)

    async def test_invalid_inputs_and_counter_rejected_before_gateway(self):
        for content, target in (("", 80), (None, 80), ("原文", True), ("原文", 0)):
            with self.subTest(content_type=type(content).__name__), self.assertRaises(ValueError):
                await self.compressor().compress(content, target_tokens=target)
        for count in (True, -1, None, 1.2):
            compressor = self.compressor()
            compressor.counter = Mock(count_messages=Mock(return_value=count))
            with self.subTest(count=count), self.assertRaises(ValueError):
                await compressor.compress("原文", target_tokens=80)
        self.gateway.invoke.assert_not_awaited()

    async def test_invalid_config_is_rejected(self):
        for option in (dict(max_rounds=True), dict(max_calls=0), dict(max_chunks=-1),
                       dict(max_request_tokens=1.5), dict(max_output_tokens=4097), dict(timeout_seconds=0)):
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.compressor(**option)

    async def test_logs_include_failure_class_but_never_raw_input_or_provider_body(self):
        with TemporaryDirectory() as directory:
            configure_logging(Path(directory), console=False, force=True, log_format="text")
            try:
                self.gateway.invoke.side_effect = RuntimeError("PRIVATE_PROVIDER_BODY")
                with self.assertRaises(InputCompressionError):
                    await self.compressor().compress("PRIVATE_USER_MATERIAL" * 10, target_tokens=80)
                logs = "\n".join(p.read_text(encoding="utf-8") for p in Path(directory).rglob("*.txt"))
                self.assertIn("InputCompressionError", logs)
                self.assertIn("input_compressor.compress", logs)
                self.assertNotIn("PRIVATE_USER_MATERIAL", logs)
                self.assertNotIn("PRIVATE_PROVIDER_BODY", logs)
            finally:
                configure_logging(DEFAULT_LOG_DIR, console=False, force=True)


if __name__ == "__main__":
    unittest.main()
