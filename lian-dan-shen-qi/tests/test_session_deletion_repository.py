"""模拟数据库，验证整段会话删除的权限边界、删除顺序和事务失败处理。"""

import unittest
from unittest.mock import MagicMock, patch

from mysql.connector import Error as MySQLError

from app.repositories import session_repository
from app.repositories.session_repository import SessionRepository


SESSION_ID = "b4c4e67554ee4c1981740a1e901ec614"


class SessionDeletionRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = SessionRepository("unused")
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = (SESSION_ID, "测试会话")
        self.cursor.rowcount = 1

    def delete(self, user_id=7, session_id=SESSION_ID):
        with patch.object(session_repository, "get_connection", return_value=self.connection):
            return self.repository.delete_session(user_id, session_id)

    def test_owned_session_deletes_children_then_parent_and_commits_once(self):
        self.assertIs(self.delete(), True)
        self.assertEqual([call.args for call in self.cursor.execute.call_args_list], [
            (self.repository.lock_session_sql, (7, SESSION_ID)),
            (self.repository.delete_session_turns_sql, (7, SESSION_ID)),
            (self.repository.delete_session_messages_sql, (7, SESSION_ID)),
            (self.repository.delete_session_sql, (7, SESSION_ID)),
        ])
        self.assertIn("FOR UPDATE", self.repository.lock_session_sql)
        self.connection.commit.assert_called_once_with()
        self.connection.rollback.assert_not_called()

    def test_missing_or_unowned_session_never_executes_delete(self):
        self.cursor.fetchone.return_value = None
        self.assertIs(self.delete(), False)
        self.cursor.execute.assert_called_once_with(self.repository.lock_session_sql, (7, SESSION_ID))
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once_with()

    def test_uuid_is_normalized_before_queries(self):
        self.assertIs(self.delete(session_id=" B4C4E675-54EE-4C19-8174-0A1E901EC614 "), True)
        for statement in self.cursor.execute.call_args_list:
            self.assertEqual(statement.args[1], (7, SESSION_ID))

    def test_invalid_ids_do_not_open_connection(self):
        invalid = [(value, SESSION_ID) for value in (None, True, 0, -1, "7", 2**64)]
        invalid += [(7, value) for value in (None, 1, "", "bad-id")]
        for user_id, session_id in invalid:
            with self.subTest(user_type=type(user_id).__name__, session_type=type(session_id).__name__):
                with patch.object(session_repository, "get_connection") as connect:
                    with self.assertRaises(ValueError):
                        self.repository.delete_session(user_id, session_id)
                    connect.assert_not_called()

    def test_failure_at_any_sql_step_rolls_back_and_stops(self):
        for failing_step in range(4):
            with self.subTest(step=failing_step):
                self.cursor.reset_mock()
                self.connection.commit.reset_mock()
                self.connection.rollback.reset_mock()
                error = MySQLError("TEST_ONLY_DELETE_FAILURE", errno=2013)
                self.cursor.execute.side_effect = [None] * failing_step + [error]
                with self.assertRaises(MySQLError) as caught:
                    self.delete()
                self.assertIs(caught.exception, error)
                self.assertEqual(self.cursor.execute.call_count, failing_step + 1)
                self.connection.commit.assert_not_called()
                self.connection.rollback.assert_called_once_with()

    def test_commit_failure_cannot_return_success(self):
        self.connection.commit.side_effect = MySQLError("TEST_ONLY_COMMIT_FAILURE", errno=2013)
        with self.assertRaises(MySQLError):
            self.delete()
        self.connection.rollback.assert_called_once_with()

    def test_missing_parent_after_children_rolls_back_all_deletes(self):
        self.cursor.rowcount = 0
        with self.assertRaises(ValueError):
            self.delete()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once_with()

    def test_connection_failure_propagates(self):
        error = MySQLError("TEST_ONLY_CONNECTION_FAILURE", errno=2003)
        with patch.object(session_repository, "get_connection", side_effect=error):
            with self.assertRaises(MySQLError) as caught:
                self.repository.delete_session(7, SESSION_ID)
        self.assertIs(caught.exception, error)

    def test_each_delete_is_scoped_to_user_and_session_with_parameters(self):
        for statement in (self.repository.delete_session_turns_sql,
                          self.repository.delete_session_messages_sql,
                          self.repository.delete_session_sql):
            with self.subTest(statement=statement.splitlines()[0]):
                self.assertEqual(statement.count("%s"), 2)
                self.assertIn("user_id = %s", statement)
                self.assertIn("id = %s", statement)
