"""模拟仓储边界；验证历史分页、归属与空会话的区别，不连接数据库。"""

import unittest
from unittest.mock import MagicMock, patch

from mysql.connector import Error as MySQLError

from app.errors import SessionNotFoundError
from app.repositories import message_repository
from app.repositories.message_repository import MessageRepository


SESSION_ID = "b4c4e67554ee4c1981740a1e901ec614"


class HistoryPageRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = MessageRepository("unused")
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.side_effect = [(7, "alice"), (SESSION_ID, "会话")]
        self.cursor.fetchall.return_value = [(1, "human", "问题"), (2, "ai", "回复")]

    def read(self, **kwargs):
        with patch.object(message_repository, "get_connection", return_value=self.connection):
            return self.repository.load_history_page(7, SESSION_ID, **kwargs)

    def test_default_page_preserves_sequence_and_message_data(self):
        self.assertEqual(self.read(), [(1, "human", "问题"), (2, "ai", "回复")])
        self.cursor.execute.assert_any_call(self.repository.load_history_page_sql, (7, SESSION_ID, 0, 50))

    def test_cursor_and_limit_are_passed_as_parameters(self):
        self.read(after_seq=12, limit=10)
        self.cursor.execute.assert_any_call(self.repository.load_history_page_sql, (7, SESSION_ID, 12, 10))

    def test_owned_empty_conversation_returns_empty_list(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.read(), [])

    def test_missing_or_unowned_conversation_raises_before_message_query(self):
        self.cursor.fetchone.side_effect = [(7, "alice"), None]
        with self.assertRaises(SessionNotFoundError):
            self.read()
        self.cursor.fetchall.assert_not_called()
        self.assertNotIn(self.repository.load_history_page_sql,
                         [call.args[0] for call in self.cursor.execute.call_args_list])

    def test_invalid_pagination_is_rejected_without_opening_connection(self):
        cases = [{"after_seq": value} for value in (None, True, "1", -1, 2**64)]
        cases += [{"limit": value} for value in (None, True, "1", 0, -1, 101)]
        for options in cases:
            with self.subTest(fields=sorted(options), value_type=type(next(iter(options.values()))).__name__):
                with patch.object(message_repository, "get_connection") as connect:
                    with self.assertRaises(ValueError):
                        self.repository.load_history_page(7, SESSION_ID, **options)
                    connect.assert_not_called()

    def test_uuid_is_normalized(self):
        with patch.object(message_repository, "get_connection", return_value=self.connection):
            self.repository.load_history_page(7, " B4C4E675-54EE-4C19-8174-0A1E901EC614 ")
        self.cursor.execute.assert_any_call(self.repository.load_history_page_sql, (7, SESSION_ID, 0, 50))

    def test_database_failure_is_not_reported_as_empty_history(self):
        error = MySQLError("TEST_ONLY_HISTORY_FAILURE", errno=2013)
        self.cursor.execute.side_effect = error
        with self.assertRaises(MySQLError) as caught:
            self.read()
        self.assertIs(caught.exception, error)

    def test_history_query_is_scoped_ordered_and_bounded(self):
        sql = self.repository.load_history_page_sql
        self.assertIn("s.user_id = %s", sql)
        self.assertIn("s.id = %s", sql)
        self.assertIn("m.seq > %s", sql)
        self.assertIn("ORDER BY m.seq ASC", sql)
        self.assertIn("LIMIT %s", sql)
