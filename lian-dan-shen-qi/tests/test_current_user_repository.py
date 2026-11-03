"""当前用户查询的仓储回归；不连接真实 MySQL，不测试尚未填写的业务骨架。"""

import unittest
from unittest.mock import MagicMock, patch

from mysql.connector import Error as MySQLError

from app.repositories import login_session_repository
from app.repositories.login_session_repository import LoginSessionRepository


class CurrentUserRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = LoginSessionRepository("unused")
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.token_hash = "a" * 64

    def lookup(self, token_hash=None):
        with patch.object(login_session_repository, "get_connection", return_value=self.connection):
            return self.repository.get_active_user(self.token_hash if token_hash is None else token_hash)

    def test_valid_match_returns_only_public_identity(self):
        self.cursor.fetchone.return_value = (7, "alice")
        self.assertEqual(self.lookup(), (7, "alice"))
        self.cursor.execute.assert_called_once_with(
            self.repository.get_active_user_sql, (self.token_hash,),
        )
        self.connection.commit.assert_not_called()

    def test_no_match_returns_none(self):
        self.cursor.fetchone.return_value = None
        self.assertIsNone(self.lookup())

    def test_invalid_hash_is_rejected_before_opening_connection(self):
        for token_hash in (None, 12, "", "a" * 43, "A" * 64, "g" * 64, "a" * 63, "a" * 65, "' OR 1=1 --"):
            with self.subTest(token_hash=token_hash):
                with patch.object(login_session_repository, "get_connection") as connect:
                    with self.assertRaises(ValueError):
                        self.repository.get_active_user(token_hash)
                    connect.assert_not_called()

    def test_connection_failure_propagates_as_database_error(self):
        original = MySQLError("TEST_ONLY_CONNECTION_FAILURE", errno=2003)
        with patch.object(login_session_repository, "get_connection", side_effect=original):
            with self.assertRaises(MySQLError) as caught:
                self.repository.get_active_user(self.token_hash)
        self.assertIs(caught.exception, original)

    def test_query_failure_is_not_converted_to_invalid_token(self):
        original = MySQLError("TEST_ONLY_QUERY_FAILURE", errno=2013)
        self.cursor.execute.side_effect = original
        with self.assertRaises(MySQLError) as caught:
            self.lookup()
        self.assertIs(caught.exception, original)

    def test_fetch_failure_is_not_reported_as_missing_login(self):
        original = MySQLError("TEST_ONLY_FETCH_FAILURE", errno=2013)
        self.cursor.fetchone.side_effect = original
        with self.assertRaises(MySQLError) as caught:
            self.lookup()
        self.assertIs(caught.exception, original)
