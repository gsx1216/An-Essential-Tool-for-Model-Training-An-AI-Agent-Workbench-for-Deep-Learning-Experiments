"""历史缺口降级：保留真实消息，附加资料标记，不伪造 AI 回复或修改序号。"""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.logging_config import DEFAULT_LOG_DIR, configure_logging
from app.models.context_state import ContextMessage, ContextSummary, HistoryGap
from app.services.context_manager import ContextManager
from scripts.check_context_manager import FakeSummaryGenerator, FakeTokenCounter


SECRET = "TEST_ONLY_PRIVATE_HISTORY_NEVER_LOG"


class ContextGapRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        configure_logging(self.directory, console=False, force=True, log_format="text")
        self.manager = ContextManager(FakeTokenCounter(), FakeSummaryGenerator())

    def tearDown(self):
        configure_logging(DEFAULT_LOG_DIR, console=False, force=True)
        self.temporary.cleanup()

    def test_continuous_history_has_no_notice_or_gap(self):
        history = [ContextMessage(1, "human", "问题"), ContextMessage(2, "ai", "回复"),
                   ContextMessage(3, "human", "当前问题")]
        self.assertEqual(self.manager.find_history_gaps(history), ())
        self.assertEqual(self.manager.build_history_messages(history),
                         [(m.role, m.content) for m in history])

    def test_missing_reply_adds_explicit_data_marker_and_preserves_both_questions(self):
        history = [ContextMessage(1, "human", "早先问题"), ContextMessage(3, "human", "当前问题")]
        original = deepcopy(history)
        messages = self.manager.build_history_messages(history)
        self.assertEqual(self.manager.find_history_gaps(history), (HistoryGap(2, 2),))
        self.assertEqual(messages[0], ("human", "早先问题"))
        self.assertEqual(messages[-1], ("human", "当前问题"))
        self.assertEqual(messages[1][0], "human")
        self.assertIn("历史缺失提示", messages[1][1])
        self.assertIn("并非用户新提问", messages[1][1])
        self.assertIn("2～2", messages[1][1])
        self.assertEqual([m for m in messages if m[0] == "ai"], [])
        self.assertEqual(history, original)

    def test_missing_prefix_can_keep_real_orphan_reply_and_current_question(self):
        history = [ContextMessage(3, "ai", "实际存在的回复"), ContextMessage(4, "human", "当前问题")]
        messages = self.manager.build_history_messages(history)
        self.assertEqual(self.manager.find_history_gaps(history), (HistoryGap(1, 2),))
        self.assertIn("1～2", messages[0][1])
        self.assertEqual(messages[1:], [("ai", "实际存在的回复"), ("human", "当前问题")])

    def test_missing_range_after_summary_does_not_require_renumbering(self):
        summary = ContextSummary("旧摘要", 10, 2)
        history = [ContextMessage(13, "human", "当前问题")]
        self.assertEqual(self.manager.find_history_gaps(history, summary), (HistoryGap(11, 12),))
        messages = self.manager.build_history_messages(history, summary)
        self.assertIn("11～12", messages[0][1])
        self.assertEqual(history[0].seq, 13)
        self.assertEqual(summary.summarized_until_seq, 10)

    def test_large_gap_produces_one_range_and_one_notice(self):
        history = [ContextMessage(10**12, "human", "当前问题")]
        self.assertEqual(self.manager.find_history_gaps(history), (HistoryGap(1, 10**12 - 1),))
        self.assertEqual(len(self.manager.build_history_messages(history)), 2)

    def test_multiple_gaps_keep_source_order_and_original_ai_messages_only(self):
        history = [ContextMessage(1, "human", "问题1"), ContextMessage(3, "ai", "回复3"),
                   ContextMessage(6, "human", "问题6"), ContextMessage(7, "ai", "回复7"),
                   ContextMessage(9, "human", "当前问题")]
        messages = self.manager.build_history_messages(history)
        self.assertEqual(self.manager.find_history_gaps(history),
                         (HistoryGap(2, 2), HistoryGap(4, 5), HistoryGap(8, 8)))
        actual = [item for item in messages if "【历史缺失提示：" not in item[1]]
        self.assertEqual(actual, [(m.role, m.content) for m in history])
        self.assertEqual([item for item in messages if item[0] == "ai"],
                         [("ai", "回复3"), ("ai", "回复7")])

    def test_summary_keeps_missing_information_warning_after_originals_are_condensed(self):
        summary = ContextSummary("旧摘要", 10, 2, (HistoryGap(3, 4),))
        history = [ContextMessage(11, "human", "当前问题")]
        messages = self.manager.build_history_messages(history, summary)
        self.assertIn("上下文完整性说明", messages[0][1])
        self.assertIn("原文存在缺失", messages[0][1])
        self.assertEqual(messages[-1], ("human", "当前问题"))

    def test_summary_missing_ranges_must_be_valid_ordered_and_within_coverage(self):
        invalid = [[], (None,), (HistoryGap(True, 2),), (HistoryGap(0, 2),),
                   (HistoryGap(4, 3),), (HistoryGap(1, 11),),
                   (HistoryGap(3, 5), HistoryGap(4, 6))]
        for missing_ranges in invalid:
            with self.subTest(ranges=missing_ranges), self.assertRaises(ValueError):
                self.manager.validate_history([ContextMessage(11, "human", "当前问题")],
                                              ContextSummary("摘要", 10, 1, missing_ranges))

    def test_duplicate_or_reverse_sequence_is_not_silently_repaired(self):
        for history in (
            [ContextMessage(1, "human", "问题"), ContextMessage(1, "human", "当前问题")],
            [ContextMessage(3, "human", "问题"), ContextMessage(2, "human", "当前问题")],
        ):
            with self.subTest(history=history), self.assertRaises(ValueError):
                self.manager.build_history_messages(history)

    def test_missing_current_question_is_not_replaced_with_a_fake_question(self):
        with self.assertRaises(ValueError):
            self.manager.build_history_messages([ContextMessage(3, "ai", "实际回复")])

    def test_text_warning_contains_counts_without_source_content(self):
        self.manager.build_history_messages([ContextMessage(3, "human", SECRET)])
        text = "\n".join(p.read_text(encoding="utf-8") for p in self.directory.rglob("*.txt"))
        self.assertIn("WARNING", text)
        self.assertIn("历史消息存在缺口，已标记不完整上下文并继续", text)
        self.assertIn("gap_count=1", text)
        self.assertIn("missing_message_count=2", text)
        self.assertNotIn(SECRET, text)

    def test_json_warning_uses_safe_fixed_event_and_numeric_metadata(self):
        configure_logging(self.directory, console=False, force=True, log_format="json")
        self.manager.build_history_messages([ContextMessage(3, "human", SECRET)])
        contents = "\n".join(p.read_text(encoding="utf-8") for p in self.directory.rglob("*.txt"))
        record = json.loads(contents)
        self.assertEqual(record["event"], "context_history_incomplete")
        self.assertEqual(record["gap_count"], 1)
        self.assertEqual(record["missing_message_count"], 2)
        self.assertNotIn(SECRET, contents)


if __name__ == "__main__":
    unittest.main()
