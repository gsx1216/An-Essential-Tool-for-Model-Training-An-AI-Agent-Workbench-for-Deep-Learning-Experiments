"""验证已完成的 Token 工具和登录记录输入校验，不调用模型、不写数据库。"""

import base64
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from app.repositories import login_session_repository
from app.security.tokens import TokenService


class TokenServiceTests(unittest.TestCase):
    def test_token_is_url_safe_and_has_32_random_bytes(self):
        service = TokenService()
        first, second = service.create_token(), service.create_token()
        self.assertRegex(first, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(len(base64.urlsafe_b64decode(first + "=")), 32)
        self.assertNotEqual(first, second)

    def test_hash_matches_known_sha256_result(self):
        self.assertEqual(
            TokenService().hash_token("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )

    def test_hash_does_not_remove_spaces_or_change_case(self):
        service = TokenService()
        self.assertNotEqual(service.hash_token("abc"), service.hash_token(" abc "))
        self.assertNotEqual(service.hash_token("abc"), service.hash_token("ABC"))

    def test_hash_rejects_empty_or_non_string_input(self):
        for value in ("", None, 123, True, b"abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TokenService().hash_token(value)

    def test_default_expiry_is_three_days_in_utc(self):
        service = TokenService()
        before = datetime.now(timezone.utc)
        expiry = service.get_expires_at()
        after = datetime.now(timezone.utc)
        self.assertEqual(service.ttl_seconds, 259200)
        self.assertEqual(expiry.tzinfo, timezone.utc)
        self.assertGreaterEqual(expiry, before + timedelta(days=3))
        self.assertLessEqual(expiry, after + timedelta(days=3))

    def test_custom_expiry_is_used(self):
        before = datetime.now(timezone.utc)
        expiry = TokenService(60).get_expires_at()
        after = datetime.now(timezone.utc)
        self.assertGreaterEqual(expiry, before + timedelta(seconds=60))
        self.assertLessEqual(expiry, after + timedelta(seconds=60))

    def test_invalid_lifetime_is_rejected(self):
        for value in (0, -1, 259201, True, 1.5, "3600", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TokenService(value)


class LoginSessionParameterTests(unittest.TestCase):
    def setUp(self):
        self.repository = login_session_repository.LoginSessionRepository("unused")
        self.token_hash = TokenService().hash_token("test-token")
        self.expiry = datetime.now(timezone.utc) + timedelta(days=1)

    def test_aware_datetime_is_converted_to_utc_before_mysql_storage(self):
        beijing = self.expiry.astimezone(timezone(timedelta(hours=8)))
        parameters = login_session_repository._validate_login_session_parameters(
            7, self.token_hash, beijing,
        )
        self.assertEqual(parameters, (7, self.token_hash, self.expiry.replace(tzinfo=None)))

    def assert_invalid_before_database_connection(self, user_id, token_hash, expires_at):
        with patch.object(login_session_repository, "get_connection") as connect:
            with self.assertRaises(ValueError):
                self.repository.save_session(user_id, token_hash, expires_at)
            connect.assert_not_called()

    def test_invalid_user_id_is_rejected(self):
        for user_id in (0, -1, True, "1", 2**64):
            with self.subTest(user_id=user_id):
                self.assert_invalid_before_database_connection(user_id, self.token_hash, self.expiry)

    def test_plaintext_and_malformed_hash_are_rejected(self):
        for value in (TokenService().create_token(), None, "g" * 64, "A" * 64, "a" * 63):
            with self.subTest(length=len(value) if isinstance(value, str) else None):
                self.assert_invalid_before_database_connection(7, value, self.expiry)

    def test_naive_expired_or_overlong_expiry_is_rejected(self):
        for value in (
            self.expiry.replace(tzinfo=None), None, "tomorrow",
            datetime.now(timezone.utc) - timedelta(seconds=1),
            datetime.now(timezone.utc) + timedelta(days=4),
        ):
            with self.subTest(value=value):
                self.assert_invalid_before_database_connection(7, self.token_hash, value)
