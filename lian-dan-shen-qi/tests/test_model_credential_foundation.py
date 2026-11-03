"""验证已提供的密文检查、SQL加载和建表接入；不调用尚未完成的仓储方法。"""

import base64
from contextlib import ExitStack
import unittest
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from mysql.connector import Error as MySQLError

from app import database
from app.repositories.model_credential_repository import ModelCredentialRepository, validate_encrypted_key
from app.security.api_key_cipher import ApiKeyCipher


class ModelCredentialFoundationTests(unittest.TestCase):
    def setUp(self):
        self.cipher = ApiKeyCipher(Fernet.generate_key().decode("ascii"))
        self.encrypted = self.cipher.encrypt(7, "sk-TEST_ONLY_FAKE_KEY")

    def test_real_ciphertext_is_accepted_unchanged(self):
        self.assertEqual(validate_encrypted_key(self.encrypted), self.encrypted)

    def test_largest_api_key_ciphertext_is_accepted(self):
        encrypted = self.cipher.encrypt(2**64 - 1, "a" * 4096)
        self.assertEqual(validate_encrypted_key(encrypted), encrypted)

    def test_invalid_inputs_and_plaintext_are_rejected(self):
        for value in (None, 123, b"not-a-string", "", "sk-TEST_ONLY_FAKE_KEY", "a" * 8193,
                      "A" * 100, "界" * 100, "!" * 100, self.encrypted + "\n"):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(ValueError):
                    validate_encrypted_key(value)

    def test_wrong_version_or_structure_is_rejected(self):
        original = base64.urlsafe_b64decode(self.encrypted)
        for raw in (b"\x81" + original[1:], original[:-1], b"\x80" * 57):
            with self.subTest(length=len(raw)):
                with self.assertRaises(ValueError):
                    validate_encrypted_key(base64.urlsafe_b64encode(raw).decode("ascii"))

    def test_shape_check_does_not_replace_signature_verification(self):
        raw = bytearray(base64.urlsafe_b64decode(self.encrypted))
        raw[-1] ^= 1
        changed = base64.urlsafe_b64encode(raw).decode("ascii")
        self.assertEqual(validate_encrypted_key(changed), changed)
        with self.assertRaises(ValueError):
            self.cipher.decrypt(7, changed)

    def test_repository_loads_prepared_sql_without_connecting(self):
        with patch("app.repositories.model_credential_repository.get_connection") as connect:
            repository = ModelCredentialRepository("unused")
        connect.assert_not_called()
        self.assertIn("ON DUPLICATE KEY UPDATE", repository.save_key_sql)
        self.assertIn("SELECT encrypted_key", repository.get_key_sql)
        self.assertIn("WHERE user_id = %s", repository.delete_key_sql)
        self.assertIn("WHERE id = %s", repository.get_user_sql)


class ModelCredentialMigrationTests(unittest.TestCase):
    def test_new_table_script_is_executed_then_committed(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        with patch.object(database, "_read_sql", return_value="TEST_ONLY_CREATE_TABLE") as read:
            database._ensure_model_credentials(connection)
        read.assert_called_once_with("create_model_credentials.sql")
        cursor.execute.assert_called_once_with("TEST_ONLY_CREATE_TABLE")
        connection.commit.assert_called_once_with()

    def test_create_failure_is_propagated_without_reporting_commit(self):
        connection = MagicMock()
        error = MySQLError("TEST_ONLY_CREATE_FAILURE", errno=1142)
        connection.cursor.return_value.__enter__.return_value.execute.side_effect = error
        with patch.object(database, "_read_sql", return_value="TEST_ONLY_CREATE_TABLE"):
            with self.assertRaises(MySQLError) as caught:
                database._ensure_model_credentials(connection)
        self.assertIs(caught.exception, error)
        connection.commit.assert_not_called()

    def test_default_initialization_includes_new_table_migration(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value.nextset.return_value = False
        with ExitStack() as stack:
            stack.enter_context(patch.object(database, '_ensure_evaluation'))
            stack.enter_context(patch.object(database, '_ensure_chat_tools'))
            stack.enter_context(patch.object(database, '_ensure_research'))
            stack.enter_context(patch.object(database, '_ensure_training_tools'))
            stack.enter_context(patch.object(database, '_ensure_run_monitors'))
            stack.enter_context(patch.object(database, '_ensure_event_bus'))
            stack.enter_context(patch.object(database, '_ensure_analysis'))
            stack.enter_context(patch.object(database, '_ensure_code_changes'))
            stack.enter_context(patch.object(database, '_ensure_revision_runs'))
            stack.enter_context(patch.object(database, '_ensure_papers'))
            stack.enter_context(patch.object(database, '_ensure_skills'))
            stack.enter_context(patch.object(database, "get_connection", return_value=connection))
            stack.enter_context(patch.object(database, "_read_sql", return_value="TEST_ONLY_INIT"))
            for name in ("_ensure_user_password_column", "_ensure_session_user_fk",
                         "_ensure_chat_turns", "_ensure_login_sessions"):
                stack.enter_context(patch.object(database, name, return_value=None))
            ensure = stack.enter_context(patch.object(database, "_ensure_model_credentials"))
            database.initialize_database("unused")
        ensure.assert_called_once_with(connection)
