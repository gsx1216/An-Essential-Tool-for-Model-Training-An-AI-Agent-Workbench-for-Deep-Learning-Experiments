"""Context history validation: ordered coverage, safe type failures and original text preservation."""

from copy import deepcopy
import unittest

from app.models.context_state import ContextMessage, ContextSummary
from app.services.context_manager import ContextManager
from scripts.check_context_manager import FakeSummaryGenerator, FakeTokenCounter, long_history, short_history


class ContextHistoryValidationTests(unittest.TestCase):
    def setUp(self):
        self.manager = ContextManager(FakeTokenCounter(), FakeSummaryGenerator())

    def test_single_current_question_is_valid(self):
        self.assertIsNone(self.manager.validate_history([ContextMessage(1, "human", "当前问题")], None))

    def test_original_whitespace_and_input_objects_are_preserved(self):
        history = short_history()
        original = deepcopy(history)
        self.assertIsNone(self.manager.validate_history(history, None))
        self.assertEqual(history, original)
        self.assertEqual(history[0].content, "  print('hello')\n")

    def test_summary_coverage_and_version_are_validated_without_mutation(self):
        summary = ContextSummary("已有摘要", 10, 3)
        history = long_history(11)
        original = deepcopy(history)
        self.assertIsNone(self.manager.validate_history(history, summary))
        self.assertEqual(history, original)
        self.assertEqual(summary, ContextSummary("已有摘要", 10, 3))
        # 原文可以缺号，但不能与已经总结的消息重叠。
        self.assertIsNone(self.manager.validate_history(long_history(12), summary))
        for start in (1, 10):
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.manager.validate_history(long_history(start), summary)

    def test_invalid_container_or_element_raises_value_error_instead_of_attribute_error(self):
        for history in (None, 1, "history", [], tuple(short_history()), [None], [{}], [1]):
            with self.subTest(history=history), self.assertRaises(ValueError):
                self.manager.validate_history(history, None)

    def test_summary_fields_reject_bool_float_missing_text_and_invalid_types(self):
        summaries = [False, {}, "summary"]
        summaries += [ContextSummary(content, 10, 1) for content in (None, 1, "", " \n")]
        summaries += [ContextSummary("摘要", seq, 1) for seq in (True, False, 0, -1, 10.0, "10", None)]
        summaries += [ContextSummary("摘要", 10, version) for version in (True, False, 0, -1, 1.0, "1", None)]
        for summary in summaries:
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                self.manager.validate_history(long_history(11), summary)

    def test_message_sequence_rejects_non_integer_duplicate_and_reverse_order(self):
        for seq in (True, False, 0, -1, 1.0, "1", None):
            with self.subTest(seq=seq), self.assertRaises(ValueError):
                self.manager.validate_history([ContextMessage(seq, "human", "问题")], None)
        for sequences in ((1, 1, 2), (1, 0, 3), (1, 2, 2)):
            history = [ContextMessage(seq, role, "正文")
                       for seq, role in zip(sequences, ("human", "ai", "human"))]
            with self.subTest(sequences=sequences), self.assertRaises(ValueError):
                self.manager.validate_history(history, None)

    def test_roles_and_content_are_checked_before_accepting_history(self):
        for role in ("ai", "system", "tool", "", None, 1, []):
            with self.subTest(role=role), self.assertRaises(ValueError):
                self.manager.validate_history([ContextMessage(1, role, "正文")], None)
        for content in (None, 1, [], {}, "", " \n\t"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.manager.validate_history([ContextMessage(1, "human", content)], None)
        for history in (
            [ContextMessage(1, "human", "问题"), ContextMessage(2, "human", "问题")],
            [ContextMessage(1, "human", "问题"), ContextMessage(2, "ai", "回复")],
        ):
            with self.subTest(history=history), self.assertRaises(ValueError):
                self.manager.validate_history(history, None)


if __name__ == "__main__":
    unittest.main()
