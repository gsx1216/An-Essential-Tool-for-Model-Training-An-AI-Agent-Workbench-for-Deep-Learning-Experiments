"""TODO 4: uncompressed context, mandatory input budget and safe failure logs; fake dependencies only."""

import asyncio
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from app.logging_config import DEFAULT_LOG_DIR, configure_logging
from app.models.context_state import ContextMessage, ContextSummary, ContextTooLargeError, HistoryGap, InputCompressionError
from app.services.context_manager import ContextManager
from scripts.check_context_manager import FakeSummaryGenerator, FakeTokenCounter, short_history


SECRET = "TEST_ONLY_PRIVATE_CONTEXT"


class ContextPrepareBasicTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        configure_logging(self.directory, console=False, force=True, log_format="text")
        self.counter = FakeTokenCounter()
        self.summarizer = FakeSummaryGenerator()

    def tearDown(self):
        configure_logging(DEFAULT_LOG_DIR, console=False, force=True)
        self.temporary.cleanup()

    def manager(self, **options):
        settings = dict(system_prompt="SYSTEM_ONLY_TEST", max_input_tokens=1000,
                        compact_at_tokens=900, max_summary_tokens=200)
        settings.update(options)
        return ContextManager(self.counter, self.summarizer, **settings)

    def test_short_history_returns_flat_messages_once_without_summary_call_or_mutation(self):
        history = short_history()
        original = deepcopy(history)
        manager = self.manager()
        result = asyncio.run(manager.prepare(history))
        self.assertFalse(result.was_compacted)
        self.assertIsNone(result.summary)
        self.assertEqual(result.model_messages, [
            ("system", manager.system_prompt), *[(m.role, m.content) for m in history],
        ])
        self.assertEqual(result.input_tokens, self.counter.count_messages(result.model_messages))
        self.assertEqual(result.missing_ranges, ())
        self.assertEqual(result.model_messages.count(("human", history[-1].content)), 1)
        self.assertEqual(history, original)
        self.assertEqual(self.summarizer.calls, [])

    def test_existing_summary_is_reused_and_not_regenerated(self):
        summary = ContextSummary("已确认目标和限制。", 10, 3)
        history = [ContextMessage(11, "human", "历史问题"), ContextMessage(12, "ai", "历史回复"),
                   ContextMessage(13, "human", "当前问题")]
        result = asyncio.run(self.manager().prepare(history, summary))
        self.assertIs(result.summary, summary)
        self.assertFalse(result.was_compacted)
        self.assertEqual(result.model_messages[-3:], [(m.role, m.content) for m in history])
        self.assertEqual(result.missing_ranges, ())
        self.assertEqual(self.summarizer.calls, [])

    def test_uncompressed_result_preserves_old_and_new_gaps_and_counts_notice_text(self):
        summary = ContextSummary("旧摘要", 10, 2, (HistoryGap(2, 3),))
        history = [ContextMessage(13, "human", "当前问题")]
        result = asyncio.run(self.manager().prepare(history, summary))
        self.assertEqual(result.missing_ranges, (HistoryGap(2, 3), HistoryGap(11, 12)))
        self.assertTrue(any("上下文完整性说明" in text for _, text in result.model_messages))
        self.assertTrue(any("历史缺失提示" in text for _, text in result.model_messages))
        self.assertEqual(result.input_tokens, self.counter.count_messages(result.model_messages))
        self.assertIs(result.summary, summary)
        self.assertEqual(self.summarizer.calls, [])

    def test_oversized_question_stops_before_history_assembly_and_logs_no_private_content(self):
        history = [ContextMessage(1, "human", SECRET * 100)]
        original = deepcopy(history)
        manager = self.manager()
        with patch.object(manager, "_assemble_messages") as assemble:
            with self.assertRaises(InputCompressionError):
                asyncio.run(manager.prepare(history))
            assemble.assert_not_called()
        self.assertEqual(history, original)
        self.assertEqual(self.summarizer.calls, [])
        text = "\n".join(p.read_text(encoding="utf-8") for p in self.directory.rglob("*.txt"))
        self.assertIn("ERROR", text)
        self.assertIn("error_type=InputCompressionError", text)
        self.assertIn("当前输入精简未配置或执行失败", text)
        self.assertNotIn(SECRET, text)

    def test_mandatory_budget_includes_system_prompt(self):
        manager = self.manager(system_prompt="S" * 200, max_input_tokens=100,
                               compact_at_tokens=80, max_summary_tokens=20)
        with self.assertRaises(ContextTooLargeError):
            asyncio.run(manager.prepare([ContextMessage(1, "human", "简短问题")]))
        self.assertEqual(self.summarizer.calls, [])

    def test_invalid_history_is_rejected_before_counter_or_summary(self):
        manager = self.manager()
        manager.counter = Mock()
        with self.assertRaises(ValueError):
            asyncio.run(manager.prepare([]))
        manager.counter.count_messages.assert_not_called()
        self.assertEqual(self.summarizer.calls, [])

    def test_invalid_counter_values_do_not_become_a_prepared_context(self):
        for count in (True, -1, 1.5, None):
            with self.subTest(count=count):
                manager = self.manager()
                manager.counter = Mock(count_messages=Mock(return_value=count))
                with self.assertRaises(ValueError):
                    asyncio.run(manager.prepare([ContextMessage(1, "human", "问题")]))
        self.assertEqual(self.summarizer.calls, [])

    def test_input_one_unit_below_trigger_returns_without_compaction(self):
        history = [ContextMessage(1, "human", "问题")]
        expected = self.counter.count_messages([("system", "S"), ("human", "问题")])
        manager = self.manager(system_prompt="S", max_input_tokens=expected + 10,
                               compact_at_tokens=expected + 1, max_summary_tokens=1)
        result = asyncio.run(manager.prepare(history))
        self.assertEqual(result.input_tokens, manager.compact_at_tokens - 1)
        self.assertFalse(result.was_compacted)
        self.assertEqual(self.summarizer.calls, [])


if __name__ == "__main__":
    unittest.main()
