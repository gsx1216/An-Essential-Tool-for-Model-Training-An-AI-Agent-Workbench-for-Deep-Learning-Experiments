"""撤销单个登录凭证的事务与参数校验；模拟数据库，不改真实登录记录。"""

import unittest
from unittest.mock import MagicMock, patch

from mysql.connector import Error as MySQLError

from app.repositories import login_session_repository
from app.repositories.login_session_repository import LoginSessionRepository


class LogoutRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = LoginSessionRepository("unused")
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.rowcount = 1
        self.token_hash = "a" * 64

    def revoke(self):
        with patch.object(login_session_repository, "get_connection", return_value=self.connection):
            return self.repository.revoke_session(self.token_hash)

    def test_matched_token_commits_and_returns_true(self):
        self.assertIs(self.revoke(), True)
        self.cursor.execute.assert_called_once_with(self.repository.revoke_session_sql, (self.token_hash,))
        self.connection.commit.assert_called_once_with()
        self.connection.rollback.assert_not_called()

    def test_no_active_match_commits_and_returns_false(self):
        self.cursor.rowcount = 0
        self.assertIs(self.revoke(), False)
        self.connection.commit.assert_called_once_with()

    def test_update_failure_rolls_back_and_propagates(self):
        original = MySQLError("TEST_ONLY_UPDATE_FAILURE", errno=2013)
        self.cursor.execute.side_effect = original
        with self.assertRaises(MySQLError) as caught:
            self.revoke()
        self.assertIs(caught.exception, original)
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_commit_failure_cannot_report_success(self):
        original = MySQLError("TEST_ONLY_COMMIT_FAILURE", errno=2013)
        self.connection.commit.side_effect = original
        with self.assertRaises(MySQLError) as caught:
            self.revoke()
        self.assertIs(caught.exception, original)
        self.connection.rollback.assert_called_once_with()

    def test_connection_failure_propagates(self):
        original = MySQLError("TEST_ONLY_CONNECTION_FAILURE", errno=2003)
        with patch.object(login_session_repository, "get_connection", side_effect=original):
            with self.assertRaises(MySQLError) as caught:
                self.repository.revoke_session(self.token_hash)
        self.assertIs(caught.exception, original)

    def test_bad_hash_never_opens_connection(self):
        for value in (None, 12, "", "a" * 43, "A" * 64, "g" * 64):
            with self.subTest(kind=type(value).__name__):
                with patch.object(login_session_repository, "get_connection") as connect:
                    with self.assertRaises(ValueError):
                        self.repository.revoke_session(value)
                    connect.assert_not_called()
