"""上下文仓储单元测试：模拟连接，验证权限、事务、快照与摘要条件写入。"""

import json
import unittest
from unittest.mock import MagicMock, patch

from mysql.connector import Error as MySQLError

from app.errors import SessionNotFoundError
from app.models.context_state import ContextConflictError, ContextStorageError, ContextSummary, HistoryGap
from app.repositories import context_repository
from app.repositories.context_repository import ContextRepository


SID = "b4c4e67554ee4c1981740a1e901ec614"


class ContextRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = ContextRepository("TEST_ONLY_UNUSED")
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.rowcount = 1

    def arrange(self, summary=None, tail=5, rows=None):
        self.cursor.fetchone.side_effect = [(SID, "标题"), summary, (tail,) if tail else None]
        self.cursor.fetchall.return_value = rows if rows is not None else [(1, "human", "问题"), (2, "ai", "回复"), (5, "human", "当前问题")]

    def load(self, user_id=7, session_id=SID):
        with patch.object(context_repository, "get_connection", return_value=self.connection):
            return self.repository.load_context(user_id, session_id)

    def save(self, summary=None, **options):
        summary = ContextSummary("新摘要", 2, 1) if summary is None else summary
        settings = dict(expected_version=0, expected_tail_seq=5)
        settings.update(options)
        with patch.object(context_repository, "get_connection", return_value=self.connection):
            return self.repository.save_summary(7, SID, summary, **settings)

    def test_constructor_only_loads_sql_and_does_not_connect(self):
        with patch.object(context_repository, "get_connection") as connect:
            ContextRepository("unused")
        connect.assert_not_called()

    def test_load_without_summary_returns_real_seq_and_commits_before_return(self):
        self.arrange()
        result = self.load()
        self.assertIsNone(result.summary)
        self.assertEqual(result.version, 0)
        self.assertEqual(result.tail_seq, 5)
        self.assertEqual([m.seq for m in result.messages], [1, 2, 5])
        self.assertIsInstance(result.messages, tuple)
        self.cursor.execute.assert_any_call(self.repository.load_context_messages_sql, (7, SID, 0))
        self.connection.commit.assert_called_once_with()
        self.connection.rollback.assert_not_called()

    def test_load_saved_summary_restores_gap_objects_and_reads_only_uncovered_raw_messages(self):
        self.arrange(summary=("  摘要\n", 4, 3, '[{"start_seq": 2, "end_seq": 3}]'),
                     rows=[(5, "human", "当前问题")])
        result = self.load()
        self.assertEqual(result.summary, ContextSummary("  摘要\n", 4, 3, (HistoryGap(2, 3),)))
        self.assertEqual(result.version, 3)
        self.assertEqual([m.seq for m in result.messages], [5])
        self.cursor.execute.assert_any_call(self.repository.load_context_messages_sql, (7, SID, 4))

    def test_empty_owned_chat_is_a_valid_empty_snapshot(self):
        self.arrange(tail=0, rows=[])
        result = self.load()
        self.assertEqual(result.tail_seq, 0)
        self.assertEqual(result.messages, ())

    def test_missing_or_unowned_chat_never_reads_or_writes_summary(self):
        for operation in (self.load, self.save):
            self.cursor.reset_mock()
            self.cursor.fetchone.side_effect = [None]
            with self.assertRaises(SessionNotFoundError):
                operation()
            self.cursor.execute.assert_called_once_with(self.repository.lock_session_sql, (7, SID))
        self.connection.commit.assert_not_called()

    def test_uuid_normalization_happens_before_queries(self):
        self.arrange()
        self.load(session_id=" B4C4E675-54EE-4C19-8174-0A1E901EC614 ")
        self.cursor.execute.assert_any_call(self.repository.lock_session_sql, (7, SID))

    def test_invalid_identity_does_not_open_connection(self):
        for user_id, session_id in ((True, SID), (0, SID), ("7", SID), (7, "wrong"), (7, None)):
            with self.subTest(user_type=type(user_id).__name__), patch.object(context_repository, "get_connection") as connect:
                with self.assertRaises(ValueError):
                    self.repository.load_context(user_id, session_id)
                connect.assert_not_called()

    def test_bad_stored_summary_is_not_treated_as_no_summary(self):
        for row in (("摘要", 2, 1, "not-json"), ("摘要", 2, 1, '{}'),
                    ("", 2, 1, '[]'), ("摘要", 2, True, '[]'),
                    ("摘要", 2, 1, '[{"start_seq":1,"end_seq":3}]'),
                    ("摘要", 2, 1, '[{"start_seq":true,"end_seq":1}]')):
            self.arrange(summary=row)
            with self.subTest(row_type=type(row[3]).__name__), self.assertRaises(ContextStorageError):
                self.load()
        self.connection.commit.assert_not_called()

    def test_summary_cannot_cover_tail_or_missing_current_history(self):
        for row in (("摘要", 5, 1, '[]'), ("摘要", 6, 1, '[]')):
            self.arrange(summary=row)
            with self.assertRaises(ContextStorageError):
                self.load()
        self.arrange(summary=("摘要", 2, 1, '[]'), rows=[])
        with self.assertRaises(ContextStorageError):
            self.load()

    def test_invalid_stored_raw_rows_are_not_sent_to_model(self):
        for rows in ([(True, "human", "问题")], [(1, "system", "错误角色")],
                     [(1, "human", " ")], [(5, "human", "问题"), (4, "ai", "倒序")]):
            self.arrange(rows=rows)
            with self.subTest(rows_count=len(rows)), self.assertRaises(ContextStorageError):
                self.load()

    def test_first_summary_insert_is_scoped_and_uses_parameters(self):
        self.arrange(rows=[(1,), (2,)])
        self.save()
        self.cursor.execute.assert_any_call(self.repository.insert_context_summary_sql,
                                             ("新摘要", 2, 1, "[]", 7, SID))
        self.connection.commit.assert_called_once_with()
        self.assertIn("s.user_id = %s", self.repository.insert_context_summary_sql)
        self.assertIn("c.version = %s", self.repository.update_context_summary_sql)

    def test_existing_summary_updates_with_expected_version_and_advanced_boundary(self):
        self.arrange(summary=("旧摘要", 2, 1, '[]'), tail=7, rows=[(3,), (4,)])
        self.save(ContextSummary("新摘要", 4, 2), expected_version=1, expected_tail_seq=7)
        self.cursor.execute.assert_any_call(self.repository.update_context_summary_sql,
                                             ("新摘要", 4, 2, "[]", 7, SID, 1, 4))
        self.cursor.execute.assert_any_call(self.repository.load_context_covered_seqs_sql, (7, SID, 2, 4))

    def test_known_gap_metadata_must_equal_real_source_gaps(self):
        self.arrange(rows=[(1,), (4,)])
        summary = ContextSummary("缺失范围已标记", 4, 1, (HistoryGap(2, 3),))
        self.save(summary)
        values = self.cursor.execute.call_args.args[1]
        self.assertEqual(json.loads(values[3]), [{"start_seq": 2, "end_seq": 3}])
        self.connection.commit.reset_mock()
        self.arrange(rows=[(1,), (4,)])
        with self.assertRaisesRegex(ValueError, "缺失范围与原始消息不一致"):
            self.save(ContextSummary("遗漏缺口", 4, 1))
        self.connection.commit.assert_not_called()

    def test_old_version_and_changed_tail_reject_stale_result(self):
        self.arrange(summary=("已更新", 2, 1, '[]'))
        with self.assertRaises(ContextConflictError):
            self.save()
        self.arrange(tail=7, rows=[(1,), (2,)])
        with self.assertRaises(ContextConflictError):
            self.save()
        self.connection.commit.assert_not_called()

    def test_summary_boundary_must_advance_and_end_at_existing_old_message(self):
        self.arrange(summary=("旧摘要", 2, 1, '[]'), tail=7)
        with self.assertRaisesRegex(ValueError, "向前扩展"):
            self.save(ContextSummary("重复范围", 2, 2), expected_version=1, expected_tail_seq=7)
        self.arrange(rows=[(1,), (2,), (3,)])
        with self.assertRaisesRegex(ValueError, "覆盖终点不是现存"):
            self.save(ContextSummary("不存在的终点", 4, 1))

    def test_invalid_save_parameters_never_open_connection(self):
        cases = [(ContextSummary("", 2, 1), {}), (ContextSummary("摘要", 5, 1), {}),
                 (ContextSummary("摘要", 2, 2), {}), (ContextSummary("摘要", 2, 1), {"expected_version": True}),
                 (ContextSummary("摘要", 2, 1), {"expected_tail_seq": 0}),
                 (ContextSummary("摘要", 2, 1, (HistoryGap(2, 1),)), {})]
        for summary, options in cases:
            with self.subTest(options=options), patch.object(context_repository, "get_connection") as connect:
                with self.assertRaises(ValueError):
                    self.repository.save_summary(7, SID, summary,
                        expected_version=options.get("expected_version", 0),
                        expected_tail_seq=options.get("expected_tail_seq", 5))
                connect.assert_not_called()

    def test_zero_affected_rows_rolls_back_instead_of_reporting_success(self):
        self.arrange(rows=[(1,), (2,)])
        self.cursor.rowcount = 0
        with self.assertRaises(ContextConflictError):
            self.save()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once_with()

    def test_sql_and_commit_failure_roll_back(self):
        for during_commit in (False, True):
            self.arrange(rows=[(1,), (2,)])
            error = MySQLError("TEST_ONLY_FAILURE", errno=2013)
            if during_commit:
                self.cursor.execute.side_effect = None
                self.connection.commit.side_effect = error
            else:
                self.cursor.execute.side_effect = error
            with self.subTest(during_commit=during_commit), self.assertRaises(MySQLError) as caught:
                self.save()
            self.assertIs(caught.exception, error)
        self.assertEqual(self.connection.rollback.call_count, 2)


if __name__ == "__main__":
    unittest.main()
