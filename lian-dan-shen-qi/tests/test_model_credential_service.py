"""个人凭据业务回归：验证加密存储顺序、隔离、异常、线程调度和日志。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import create_autospec

from mysql.connector import Error as MySQLError

from app.errors import ModelCredentialDecryptionError, ModelCredentialNotConfiguredError
from app.logging_config import DEFAULT_LOG_DIR, configure_logging
from app.repositories.model_credential_repository import ModelCredentialRepository
from app.security.api_key_cipher import ApiKeyCipher
from app.services.model_credential_service import ModelCredentialService


class ModelCredentialServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = create_autospec(ModelCredentialRepository, instance=True, spec_set=True)
        self.cipher = create_autospec(ApiKeyCipher, instance=True, spec_set=True)
        self.key = "sk-TEST_ONLY_FAKE_KEY"
        self.encrypted = "TEST_ONLY_MOCK_CIPHERTEXT"
        self.cipher.encrypt.return_value = self.encrypted
        self.cipher.decrypt.return_value = self.key
        self.repository.get_key.return_value = self.encrypted
        self.repository.save_key.return_value = None
        self.repository.delete_key.return_value = True
        self.service = ModelCredentialService(self.repository, self.cipher)

    def test_constructor_keeps_injected_collaborators(self):
        self.assertIs(self.service.repository, self.repository)
        self.assertIs(self.service.cipher, self.cipher)

    async def test_save_encrypts_before_storing_and_returns_none(self):
        events = []
        self.cipher.encrypt.side_effect = lambda *args, **kwargs: events.append("encrypt") or self.encrypted
        self.repository.save_key.side_effect = lambda *args, **kwargs: events.append("save")
        self.assertIsNone(await self.service.save_api_key(7, self.key))
        self.assertEqual(events, ["encrypt", "save"])
        self.cipher.encrypt.assert_called_once_with(7, self.key)
        self.repository.save_key.assert_called_once_with(7, self.encrypted)

    async def test_invalid_user_ids_stop_all_work(self):
        for owner in (None, True, "7", 0, -1, 2**64):
            for operation, rest in ((self.service.save_api_key, (self.key,)),
                                    (self.service.is_configured, ()),
                                    (self.service.delete_api_key, ()),
                                    (self.service.get_api_key, ())):
                with self.subTest(operation=operation.__name__, kind=type(owner).__name__):
                    with self.assertRaises(ValueError):
                        await operation(owner, *rest)
        self.cipher.encrypt.assert_not_called()
        self.cipher.decrypt.assert_not_called()
        self.repository.save_key.assert_not_called()
        self.repository.get_key.assert_not_called()
        self.repository.delete_key.assert_not_called()

    async def test_invalid_keys_stop_before_encryption_and_storage(self):
        for key in (None, 123, "", " ", " key", "key ", "key\n", "界", "a" * 4097):
            with self.subTest(kind=type(key).__name__):
                with self.assertRaises(ValueError):
                    await self.service.save_api_key(7, key)
        self.cipher.encrypt.assert_not_called()
        self.repository.save_key.assert_not_called()

    async def test_encrypt_failure_never_saves_plaintext(self):
        error = ValueError("TEST_ONLY_ENCRYPT_FAILURE")
        self.cipher.encrypt.side_effect = error
        with self.assertRaises(ValueError) as caught:
            await self.service.save_api_key(7, self.key)
        self.assertIs(caught.exception, error)
        self.repository.save_key.assert_not_called()

    async def test_save_database_failure_propagates(self):
        error = MySQLError("TEST_ONLY_SAVE_FAILURE", errno=2013)
        self.repository.save_key.side_effect = error
        with self.assertRaises(MySQLError) as caught:
            await self.service.save_api_key(7, self.key)
        self.assertIs(caught.exception, error)

    async def test_status_returns_true_without_decrypting_or_exposing_ciphertext(self):
        self.assertIs(await self.service.is_configured(7), True)
        self.repository.get_key.assert_called_once_with(7)
        self.cipher.decrypt.assert_not_called()
        self.cipher.encrypt.assert_not_called()

    async def test_status_returns_false_when_not_configured(self):
        self.repository.get_key.return_value = None
        self.assertIs(await self.service.is_configured(7), False)

    async def test_status_database_failure_is_not_false(self):
        self.repository.get_key.side_effect = MySQLError("TEST_ONLY_READ_FAILURE", errno=2013)
        with self.assertRaises(MySQLError):
            await self.service.is_configured(7)

    async def test_delete_passes_owner_and_returns_true(self):
        self.assertIs(await self.service.delete_api_key(7), True)
        self.repository.delete_key.assert_called_once_with(7)
        self.cipher.encrypt.assert_not_called()
        self.cipher.decrypt.assert_not_called()

    async def test_delete_returns_false_for_already_missing_configuration(self):
        self.repository.delete_key.return_value = False
        self.assertIs(await self.service.delete_api_key(7), False)

    async def test_delete_database_failure_is_not_success(self):
        error = MySQLError("TEST_ONLY_DELETE_FAILURE", errno=2013)
        self.repository.delete_key.side_effect = error
        with self.assertRaises(MySQLError) as caught:
            await self.service.delete_api_key(7)
        self.assertIs(caught.exception, error)

    async def test_internal_get_decrypts_only_the_owners_value(self):
        self.assertEqual(await self.service.get_api_key(7), self.key)
        self.repository.get_key.assert_called_once_with(7)
        self.cipher.decrypt.assert_called_once_with(7, self.encrypted)

    async def test_internal_get_missing_key_raises_without_fallback(self):
        self.repository.get_key.return_value = None
        with self.assertRaises(ModelCredentialNotConfiguredError):
            await self.service.get_api_key(7)
        self.cipher.decrypt.assert_not_called()

    async def test_internal_get_database_error_is_not_missing_configuration(self):
        error = MySQLError("TEST_ONLY_READ_FAILURE", errno=2013)
        self.repository.get_key.side_effect = error
        with self.assertRaises(MySQLError) as caught:
            await self.service.get_api_key(7)
        self.assertIs(caught.exception, error)
        self.cipher.decrypt.assert_not_called()

    async def test_decryption_failure_becomes_distinct_server_error(self):
        error = ValueError("TEST_ONLY_UNREADABLE_CREDENTIAL")
        self.cipher.decrypt.side_effect = error
        with self.assertRaises(ModelCredentialDecryptionError) as caught:
            await self.service.get_api_key(7)
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(str(caught.exception), "无法读取模型凭据。")

    async def test_same_user_reads_fresh_configuration_on_each_call(self):
        self.repository.get_key.side_effect = ["CIPHER_ONE", "CIPHER_TWO"]
        self.cipher.decrypt.side_effect = ["KEY_ONE", "KEY_TWO"]
        self.assertEqual(await self.service.get_api_key(7), "KEY_ONE")
        self.assertEqual(await self.service.get_api_key(7), "KEY_TWO")
        self.assertEqual(self.repository.get_key.call_count, 2)

    async def test_different_users_are_scoped_at_read_and_decrypt(self):
        self.repository.get_key.side_effect = lambda user_id: {7: "CIPHER_ALICE", 8: "CIPHER_BOB"}[user_id]
        self.cipher.decrypt.side_effect = lambda user_id, encrypted_key: {
            (7, "CIPHER_ALICE"): "KEY_ALICE", (8, "CIPHER_BOB"): "KEY_BOB",
        }[(user_id, encrypted_key)]
        self.assertEqual(await self.service.get_api_key(7), "KEY_ALICE")
        self.assertEqual(await self.service.get_api_key(8), "KEY_BOB")

    async def test_synchronous_cipher_and_database_work_runs_off_event_loop(self):
        loop_thread = threading.get_ident()
        workers = []

        def track(result):
            def invoke(*args, **kwargs):
                workers.append(threading.get_ident())
                return result
            return invoke

        self.cipher.encrypt.side_effect = track(self.encrypted)
        self.cipher.decrypt.side_effect = track(self.key)
        self.repository.save_key.side_effect = track(None)
        self.repository.get_key.side_effect = track(self.encrypted)
        self.repository.delete_key.side_effect = track(True)
        await self.service.save_api_key(7, self.key)
        await self.service.is_configured(7)
        await self.service.delete_api_key(7)
        await self.service.get_api_key(7)
        self.assertEqual(len(workers), 6)
        self.assertTrue(all(worker != loop_thread for worker in workers))

    async def test_logs_exclude_plaintext_ciphertext_and_exception_secrets(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            configure_logging(directory, console=False, force=True)
            try:
                await self.service.save_api_key(7, self.key)
                self.assertEqual(await self.service.get_api_key(7), self.key)
                self.cipher.decrypt.side_effect = ValueError(self.key)
                with self.assertRaises(ModelCredentialDecryptionError):
                    await self.service.get_api_key(7)
                contents = "\n".join(path.read_text(encoding="utf-8") for path in directory.rglob("*.txt"))
                self.assertIn("model_credential_service.save_api_key", contents)
                self.assertIn("model_credential_service.get_api_key", contents)
                self.assertNotIn(self.key, contents)
                self.assertNotIn(self.encrypted, contents)
            finally:
                configure_logging(DEFAULT_LOG_DIR, console=False, force=True)

