"""真实 Fernet 加解密与用户绑定测试；只用随机临时主密钥和假 Key。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.fernet import Fernet

from app.logging_config import DEFAULT_LOG_DIR, configure_logging
from app.security.api_key_cipher import ApiKeyCipher, validate_api_key


class ApiKeyCipherTests(unittest.TestCase):
    def setUp(self):
        self.master_key = Fernet.generate_key().decode("ascii")
        self.cipher = ApiKeyCipher(self.master_key)
        self.api_key = "sk-TEST_ONLY_FAKE_KEY_NEVER_SEND_TO_PROVIDER"

    def test_round_trip_returns_original_key(self):
        encrypted = self.cipher.encrypt(7, self.api_key)
        self.assertIsInstance(encrypted, str)
        self.assertNotEqual(encrypted, self.api_key)
        self.assertNotIn(self.api_key, encrypted)
        self.assertEqual(self.cipher.decrypt(7, encrypted), self.api_key)

    def test_same_key_encrypted_twice_has_different_ciphertext(self):
        first = self.cipher.encrypt(7, self.api_key)
        second = self.cipher.encrypt(7, self.api_key)
        self.assertNotEqual(first, second)
        self.assertEqual(self.cipher.decrypt(7, first), self.api_key)
        self.assertEqual(self.cipher.decrypt(7, second), self.api_key)

    def test_another_instance_with_same_master_can_decrypt(self):
        encrypted = self.cipher.encrypt(7, self.api_key)
        self.assertEqual(ApiKeyCipher(self.master_key).decrypt(7, encrypted), self.api_key)

    def test_different_master_key_cannot_decrypt(self):
        encrypted = self.cipher.encrypt(7, self.api_key)
        other = ApiKeyCipher(Fernet.generate_key().decode("ascii"))
        with self.assertRaises(ValueError):
            other.decrypt(7, encrypted)

    def test_another_user_cannot_decrypt_swapped_ciphertext(self):
        encrypted = self.cipher.encrypt(7, self.api_key)
        with self.assertRaises(ValueError):
            self.cipher.decrypt(8, encrypted)

    def test_changed_ciphertext_is_rejected(self):
        encrypted = self.cipher.encrypt(7, self.api_key)
        index = len(encrypted) // 2
        changed = encrypted[:index] + ("A" if encrypted[index] != "A" else "B") + encrypted[index + 1:]
        with self.assertRaises(ValueError):
            self.cipher.decrypt(7, changed)

    def test_invalid_plain_api_keys_are_rejected(self):
        for value in (None, 123, "", " ", " key", "key ", "key\n", "key\t", "密钥", "a" * 4097):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(ValueError):
                    self.cipher.encrypt(7, value)

    def test_local_length_bound_is_supported(self):
        value = "A" * 4096
        encrypted = self.cipher.encrypt(7, value)
        self.assertLessEqual(len(encrypted), 8192)
        self.assertEqual(self.cipher.decrypt(7, encrypted), value)

    def test_invalid_user_ids_are_rejected(self):
        encrypted = self.cipher.encrypt(7, self.api_key)
        for owner in (True, 0, -1, "7", None, 2**64):
            with self.subTest(kind=type(owner).__name__):
                with self.assertRaises(ValueError):
                    self.cipher.encrypt(owner, self.api_key)
                with self.assertRaises(ValueError):
                    self.cipher.decrypt(owner, encrypted)

    def test_invalid_master_key_is_rejected(self):
        for value in (None, 123, "", "not-a-fernet-key", "界" * 44):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(ValueError):
                    ApiKeyCipher(value)

    def test_invalid_ciphertext_is_rejected(self):
        for value in (None, 123, "", "not-encrypted", "界", "a" * 8193):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(ValueError):
                    self.cipher.decrypt(7, value)

    def test_authenticated_payload_metadata_is_validated(self):
        original = {"version": 1, "provider": "deepseek", "user_id": 7, "api_key": self.api_key}
        invalid = [[], {**original, "version": True}, {**original, "version": 2},
                   {**original, "provider": "other"}, {**original, "user_id": True},
                   {**original, "user_id": 8}, {**original, "api_key": ""}]
        for payload in invalid:
            with self.subTest(kind=type(payload).__name__):
                encrypted = Fernet(self.master_key.encode("ascii")).encrypt(
                    json.dumps(payload).encode("utf-8"),
                ).decode("ascii")
                with self.assertRaises(ValueError):
                    self.cipher.decrypt(7, encrypted)

    def test_api_key_input_is_not_silently_trimmed(self):
        self.assertEqual(validate_api_key(self.api_key), self.api_key)
        with self.assertRaises(ValueError):
            validate_api_key(" " + self.api_key)

    def test_authenticated_malformed_json_is_rejected_safely(self):
        malformed = ('{"api_key":"' + self.api_key + '"').encode("utf-8")
        encrypted = Fernet(self.master_key.encode("ascii")).encrypt(malformed).decode("ascii")
        with self.assertRaises(ValueError) as caught:
            self.cipher.decrypt(7, encrypted)
        self.assertEqual(str(caught.exception), "模型凭据无法解密，请检查配置。")
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn(self.api_key, str(caught.exception))

    def test_authenticated_invalid_utf8_is_rejected(self):
        encrypted = Fernet(self.master_key.encode("ascii")).encrypt(b"\xff\xfe").decode("ascii")
        with self.assertRaisesRegex(ValueError, "模型凭据无法解密"):
            self.cipher.decrypt(7, encrypted)

    def test_largest_supported_user_id_round_trips(self):
        user_id = 2**64 - 1
        encrypted = self.cipher.encrypt(user_id, self.api_key)
        self.assertEqual(self.cipher.decrypt(user_id, encrypted), self.api_key)

    def test_printable_ascii_punctuation_is_preserved(self):
        value = "key-" + ''.join(chr(code) for code in range(33, 127))
        encrypted = self.cipher.encrypt(7, value)
        self.assertEqual(self.cipher.decrypt(7, encrypted), value)

    def test_missing_authenticated_fields_are_rejected(self):
        original = {"version": 1, "provider": "deepseek", "user_id": 7, "api_key": self.api_key}
        for field in original:
            with self.subTest(field=field):
                payload = {key: value for key, value in original.items() if key != field}
                encrypted = Fernet(self.master_key.encode("ascii")).encrypt(
                    json.dumps(payload).encode("utf-8"),
                ).decode("ascii")
                with self.assertRaises(ValueError):
                    self.cipher.decrypt(7, encrypted)

    def test_persistent_logs_do_not_include_keys_or_ciphertext(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            configure_logging(directory, console=False, force=True)
            try:
                encrypted = self.cipher.encrypt(7, self.api_key)
                self.assertEqual(self.cipher.decrypt(7, encrypted), self.api_key)
                with self.assertRaises(ValueError):
                    self.cipher.decrypt(8, encrypted)
                bad_master = "TEST_ONLY_PRIVATE_INVALID_MASTER_KEY"
                with self.assertRaises(ValueError):
                    ApiKeyCipher(bad_master)
                contents = "\n".join(path.read_text(encoding="utf-8") for path in directory.rglob("*.txt"))
                self.assertIn("api_key_cipher.encrypt", contents)
                self.assertIn("api_key_cipher.decrypt", contents)
                for secret in (self.master_key, self.api_key, encrypted, bad_master):
                    self.assertNotIn(secret, contents)
            finally:
                configure_logging(DEFAULT_LOG_DIR, console=False, force=True)

