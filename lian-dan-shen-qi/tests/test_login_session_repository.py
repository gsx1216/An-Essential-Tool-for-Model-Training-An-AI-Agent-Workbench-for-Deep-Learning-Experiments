"""登录保存事务回归：模拟连接，验证提交、回滚和错误传播，不写真实用户表。"""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, patch

from mysql.connector import Error as MySQLError

from app.repositories import login_session_repository


class LoginSessionRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = login_session_repository.LoginSessionRepository("unused")
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.lastrowid = 17
        self.token_hash = "a" * 64
        self.expiry = datetime.now(timezone.utc) + timedelta(days=1)

    def save(self):
        with patch.object(login_session_repository, "get_connection", return_value=self.connection):
            return self.repository.save_session(7, self.token_hash, self.expiry)

    def test_successful_save_returns_id_after_commit(self):
        self.assertEqual(self.save(), 17)
        self.cursor.execute.assert_called_once_with(
            self.repository.save_session_sql,
            (7, self.token_hash, self.expiry.replace(tzinfo=None)),
        )
        self.connection.commit.assert_called_once_with()
        self.connection.rollback.assert_not_called()

    def test_insert_failure_rolls_back_and_preserves_original_error(self):
        original = MySQLError("TEST_ONLY_INSERT_FAILURE", errno=1452)
        self.cursor.execute.side_effect = original
        with self.assertRaises(MySQLError) as caught:
            self.save()
        self.assertIs(caught.exception, original)
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_commit_failure_cannot_return_a_successful_id(self):
        original = MySQLError("TEST_ONLY_COMMIT_FAILURE", errno=2013)
        self.connection.commit.side_effect = original
        with self.assertRaises(MySQLError) as caught:
            self.save()
        self.assertIs(caught.exception, original)
        self.connection.rollback.assert_called_once_with()

    def test_missing_generated_id_rolls_back_and_raises_runtime_error(self):
        self.cursor.lastrowid = None
        with self.assertRaises(RuntimeError):
            self.save()
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()
