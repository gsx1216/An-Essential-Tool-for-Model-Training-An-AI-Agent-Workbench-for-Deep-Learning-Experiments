"""退出登录业务回归：模拟仓储、使用真实哈希计算，不连接 MySQL。"""

import threading
import unittest
from unittest.mock import Mock

from mysql.connector import Error as MySQLError

from app.errors import InvalidTokenError
from app.repositories.login_session_repository import LoginSessionRepository
from app.security.tokens import TokenService
from app.services.logout_service import LogoutService


class LogoutServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.real_tokens = TokenService()
        self.token = self.real_tokens.create_token()
        self.tokens = Mock(spec=TokenService)
        self.tokens.hash_token.side_effect = self.real_tokens.hash_token
        self.repository = Mock(spec=LoginSessionRepository)
        self.repository.revoke_session.return_value = True
        self.service = LogoutService(self.repository, self.tokens)

    async def test_success_hashes_current_token_and_returns_none(self):
        self.assertIsNone(await self.service.logout(self.token))
        self.tokens.hash_token.assert_called_once_with(self.token)
        self.repository.revoke_session.assert_called_once_with(self.real_tokens.hash_token(self.token))
        self.repository.get_active_user.assert_not_called()
        self.repository.save_session.assert_not_called()
        self.tokens.create_token.assert_not_called()
        self.tokens.get_expires_at.assert_not_called()

    async def test_non_string_input_uses_invalid_token_error_before_any_work(self):
        for label, value in (("none", None), ("integer", 123), ("bool", True),
                             ("bytes", b"A" * 43), ("list", []), ("dict", {})):
            with self.subTest(case=label):
                with self.assertRaises(InvalidTokenError):
                    await self.service.logout(value)
                self.tokens.hash_token.assert_not_called()
                self.repository.revoke_session.assert_not_called()

    async def test_bad_string_format_is_rejected_before_any_work(self):
        for label, value in (("empty", ""), ("short", "A" * 42), ("long", "A" * 44),
                             ("symbol", "A" * 42 + "!"), ("unicode", "中" * 43),
                             ("space", "A" * 42 + " "), ("newline", "A" * 42 + "\n"),
                             ("bearer_prefix", "Bearer " + self.token)):
            with self.subTest(case=label):
                with self.assertRaises(InvalidTokenError):
                    await self.service.logout(value)
                self.tokens.hash_token.assert_not_called()
                self.repository.revoke_session.assert_not_called()

    async def test_no_active_record_raises_invalid_token(self):
        self.repository.revoke_session.return_value = False
        with self.assertRaises(InvalidTokenError):
            await self.service.logout(self.token)

    async def test_database_failure_is_not_converted_to_success_or_invalid_token(self):
        original = MySQLError("TEST_ONLY_DATABASE_FAILURE", errno=2013)
        self.repository.revoke_session.side_effect = original
        with self.assertRaises(MySQLError) as caught:
            await self.service.logout(self.token)
        self.assertIs(caught.exception, original)

    async def test_hash_failure_never_reaches_repository(self):
        original = RuntimeError("TEST_ONLY_HASH_FAILURE")
        self.tokens.hash_token.side_effect = original
        with self.assertRaises(RuntimeError) as caught:
            await self.service.logout(self.token)
        self.assertIs(caught.exception, original)
        self.repository.revoke_session.assert_not_called()

    async def test_sync_revocation_runs_in_worker_thread(self):
        loop_thread = threading.get_ident()
        worker_threads = []

        def revoke(token_hash):
            worker_threads.append(threading.get_ident())
            return True

        self.repository.revoke_session.side_effect = revoke
        self.assertIsNone(await self.service.logout(self.token))
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], loop_thread)

    async def test_repeated_call_respects_repository_revocation_result(self):
        self.repository.revoke_session.side_effect = [True, False]
        self.assertIsNone(await self.service.logout(self.token))
        with self.assertRaises(InvalidTokenError):
            await self.service.logout(self.token)
        self.assertEqual(self.repository.revoke_session.call_count, 2)
