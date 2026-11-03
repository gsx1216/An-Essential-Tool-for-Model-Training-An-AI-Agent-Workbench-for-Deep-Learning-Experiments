"""个人凭据仓储回归：模拟连接，检查SQL参数、返回值、校验和事务。"""

import unittest
from unittest.mock import MagicMock, call, patch

from cryptography.fernet import Fernet
from mysql.connector import Error as MySQLError, IntegrityError

from app.repositories import model_credential_repository
from app.repositories.model_credential_repository import ModelCredentialRepository
from app.security.api_key_cipher import ApiKeyCipher


class ModelCredentialRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = ModelCredentialRepository("unused")
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = (7, "alice")
        self.cursor.rowcount = 1
        self.encrypted = ApiKeyCipher(Fernet.generate_key().decode("ascii")).encrypt(7, "sk-TEST_ONLY_FAKE_KEY")

    def invoke(self, operation, *args):
        with patch.object(model_credential_repository, "get_connection", return_value=self.connection):
            return operation(*args)

    def test_save_binds_only_owner_and_ciphertext_then_commits(self):
        self.assertIsNone(self.invoke(self.repository.save_key, 7, self.encrypted))
        self.assertEqual(self.cursor.execute.call_args_list, [
            call(self.repository.get_user_sql, (7,)),
            call(self.repository.save_key_sql, (7, self.encrypted)),
        ])
        self.connection.commit.assert_called_once_with()
        self.connection.rollback.assert_not_called()

    def test_save_statement_failure_rolls_back_and_propagates(self):
        error = MySQLError("TEST_ONLY_WRITE_FAILURE", errno=2013)
        self.cursor.execute.side_effect = [None, error]
        with self.assertRaises(MySQLError) as caught:
            self.invoke(self.repository.save_key, 7, self.encrypted)
        self.assertIs(caught.exception, error)
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_save_commit_failure_cannot_report_success(self):
        error = MySQLError("TEST_ONLY_COMMIT_FAILURE", errno=2013)
        self.connection.commit.side_effect = error
        with self.assertRaises(MySQLError):
            self.invoke(self.repository.save_key, 7, self.encrypted)
        self.connection.rollback.assert_called_once_with()

    def test_user_deleted_after_precheck_is_rejected_by_foreign_key(self):
        self.cursor.execute.side_effect = [None, IntegrityError("TEST_ONLY_MISSING_USER", errno=1452)]
        with self.assertRaisesRegex(ValueError, "用户不存在"):
            self.invoke(self.repository.save_key, 7, self.encrypted)
        self.connection.rollback.assert_called_once_with()

    def test_get_returns_ciphertext_not_row_tuple(self):
        self.cursor.fetchone.side_effect = [(7, "alice"), (self.encrypted,)]
        self.assertEqual(self.invoke(self.repository.get_key, 7), self.encrypted)
        self.cursor.execute.assert_called_with(self.repository.get_key_sql, (7,))

    def test_get_returns_none_when_existing_user_has_no_key(self):
        self.cursor.fetchone.side_effect = [(7, "alice"), None]
        self.assertIsNone(self.invoke(self.repository.get_key, 7))

    def test_get_database_failure_is_not_treated_as_missing_key(self):
        error = MySQLError("TEST_ONLY_READ_FAILURE", errno=2013)
        self.cursor.execute.side_effect = [None, error]
        with self.assertRaises(MySQLError) as caught:
            self.invoke(self.repository.get_key, 7)
        self.assertIs(caught.exception, error)

    def test_delete_one_match_returns_true_after_commit(self):
        self.assertIs(self.invoke(self.repository.delete_key, 7), True)
        self.cursor.execute.assert_called_with(self.repository.delete_key_sql, (7,))
        self.connection.commit.assert_called_once_with()

    def test_delete_no_match_returns_false(self):
        self.cursor.rowcount = 0
        self.assertIs(self.invoke(self.repository.delete_key, 7), False)
        self.connection.commit.assert_called_once_with()

    def test_delete_does_not_fetch_a_deleted_row(self):
        # SELECT用户存在性有一行；DELETE本身不会返回可供fetchone读取的行。
        self.cursor.fetchone.side_effect = [(7, "alice"), None]
        self.assertIs(self.invoke(self.repository.delete_key, 7), True)
        self.assertEqual(self.cursor.fetchone.call_count, 1)

    def test_delete_statement_failure_rolls_back(self):
        error = MySQLError("TEST_ONLY_DELETE_FAILURE", errno=2013)
        self.cursor.execute.side_effect = [None, error]
        with self.assertRaises(MySQLError):
            self.invoke(self.repository.delete_key, 7)
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_delete_commit_failure_cannot_return_true(self):
        self.connection.commit.side_effect = MySQLError("TEST_ONLY_COMMIT_FAILURE", errno=2013)
        with self.assertRaises(MySQLError):
            self.invoke(self.repository.delete_key, 7)
        self.connection.rollback.assert_called_once_with()

    def test_unknown_user_stops_all_operations_before_credential_sql(self):
        for operation, args in ((self.repository.save_key, (7, self.encrypted)),
                                (self.repository.get_key, (7,)), (self.repository.delete_key, (7,))):
            with self.subTest(operation=operation.__name__):
                self.cursor.reset_mock()
                self.cursor.fetchone.return_value = None
                with self.assertRaisesRegex(ValueError, "用户不存在"):
                    self.invoke(operation, *args)
                self.cursor.execute.assert_called_once_with(self.repository.get_user_sql, (7,))

    def test_invalid_user_id_never_opens_connection(self):
        for owner in (True, 0, -1, "7", None, 2**64):
            for operation, args in ((self.repository.save_key, (owner, self.encrypted)),
                                    (self.repository.get_key, (owner,)), (self.repository.delete_key, (owner,))):
                with self.subTest(operation=operation.__name__, kind=type(owner).__name__):
                    with patch.object(model_credential_repository, "get_connection") as connect:
                        with self.assertRaises(ValueError):
                            operation(*args)
                        connect.assert_not_called()

    def test_plaintext_key_is_rejected_before_connection(self):
        with patch.object(model_credential_repository, "get_connection") as connect:
            with self.assertRaises(ValueError):
                self.repository.save_key(7, "sk-TEST_ONLY_PLAINTEXT")
            connect.assert_not_called()

    def test_save_connection_failure_propagates(self):
        error = MySQLError("TEST_ONLY_CONNECTION_FAILURE", errno=2003)
        with patch.object(model_credential_repository, "get_connection", side_effect=error):
            with self.assertRaises(MySQLError) as caught:
                self.repository.save_key(7, self.encrypted)
        self.assertIs(caught.exception, error)

    def test_reads_do_not_commit_or_delete_records(self):
        self.cursor.fetchone.side_effect = [(7, "alice"), None]
        self.invoke(self.repository.get_key, 7)
        self.connection.commit.assert_not_called()
        self.assertEqual(self.cursor.execute.call_args_list, [
            call(self.repository.get_user_sql, (7,)), call(self.repository.get_key_sql, (7,)),
        ])

