"""按近期组数切分历史：保留真实对象、序号和缺口两侧片段，不访问外部依赖。"""

from copy import deepcopy
import unittest

from app.models.context_state import ContextMessage
from app.services.context_manager import ContextManager
from scripts.check_context_manager import FakeSummaryGenerator, FakeTokenCounter


def normal_history(turns):
    history = []
    for number in range(turns):
        history.extend([
            ContextMessage(2 * number + 1, "human", f"问题{number + 1}"),
            ContextMessage(2 * number + 2, "ai", f"回复{number + 1}"),
        ])
    history.append(ContextMessage(2 * turns + 1, "human", "当前问题"))
    return history


class ContextSummarySplitTests(unittest.TestCase):
    def manager(self, keep):
        return ContextManager(FakeTokenCounter(), FakeSummaryGenerator(), keep_recent_turns=keep)

    def assert_preserved(self, history, original, older, recent):
        self.assertEqual(history, original)
        self.assertEqual(older + recent, history)
        self.assertTrue(all(actual is expected for actual, expected in zip(older + recent, history)))
        self.assertIsNot(recent, history)
        self.assertIs(recent[-1], history[-1])

    def test_continuous_history_keeps_recent_complete_turns_and_question(self):
        history = normal_history(3)
        original = deepcopy(history)
        older, recent = self.manager(2)._split_for_summary(history)
        self.assertEqual(older, history[:2])
        self.assertEqual(recent, history[2:])
        self.assert_preserved(history, original, older, recent)

    def test_short_history_returns_no_old_messages_and_new_recent_list(self):
        history = normal_history(1)
        older, recent = self.manager(4)._split_for_summary(history)
        self.assertEqual(older, [])
        self.assertEqual(recent, history)
        self.assertIsNot(recent, history)

    def test_only_current_question_returns_empty_old_history(self):
        question = ContextMessage(11, "human", "摘要后的当前问题")
        history = [question]
        older, recent = self.manager(4)._split_for_summary(history)
        self.assertEqual(older, [])
        self.assertEqual(recent, [question])
        self.assertIsNot(recent, history)

    def test_exact_retained_group_count_does_not_summarize_any_group(self):
        history = normal_history(2)
        older, recent = self.manager(2)._split_for_summary(history)
        self.assertEqual(older, [])
        self.assertEqual(recent, history)

    def test_gap_starts_a_group_but_does_not_itself_determine_cut_position(self):
        history = [
            ContextMessage(1, "human", "问题1"), ContextMessage(2, "ai", "回复1"),
            ContextMessage(3, "human", "缺少回复的问题"),
            ContextMessage(5, "ai", "缺口后的孤立回复"),
            ContextMessage(6, "human", "问题6"), ContextMessage(7, "ai", "回复7"),
            ContextMessage(8, "human", "当前问题"),
        ]
        original = deepcopy(history)
        manager = self.manager(2)
        manager.validate_history(history, None)
        older, recent = manager._split_for_summary(history)
        self.assertEqual([m.seq for m in older], [1, 2, 3])
        self.assertEqual([m.seq for m in recent], [5, 6, 7, 8])
        self.assert_preserved(history, original, older, recent)

    def test_missing_replies_keep_real_question_fragments_without_fake_ai(self):
        history = [ContextMessage(1, "human", "旧问题"), ContextMessage(3, "human", "近期问题"),
                   ContextMessage(5, "human", "当前问题")]
        manager = self.manager(1)
        manager.validate_history(history, None)
        older, recent = manager._split_for_summary(history)
        self.assertEqual([m.seq for m in older], [1])
        self.assertEqual([m.seq for m in recent], [3, 5])
        self.assertTrue(all(m.role == "human" for m in older + recent))

    def test_leading_orphan_reply_is_preserved_as_its_own_group(self):
        history = [ContextMessage(5, "ai", "缺失前文的回复"), ContextMessage(6, "human", "近期问题"),
                   ContextMessage(7, "ai", "近期回复"), ContextMessage(8, "human", "当前问题")]
        manager = self.manager(1)
        manager.validate_history(history, None)
        older, recent = manager._split_for_summary(history)
        self.assertEqual([m.seq for m in older], [5])
        self.assertEqual([m.seq for m in recent], [6, 7, 8])

    def test_missing_input_or_current_question_is_rejected_clearly(self):
        for history in (None, [], (), [1], [ContextMessage(1, "ai", "只有回复")]):
            with self.subTest(history=history), self.assertRaises(ValueError):
                self.manager(2)._split_for_summary(history)


if __name__ == "__main__":
    unittest.main()
