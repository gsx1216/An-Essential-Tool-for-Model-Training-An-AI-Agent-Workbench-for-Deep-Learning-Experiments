"""已有 Token 的业务校验：模拟仓储，不连接 MySQL，也不调用模型。"""

import asyncio
import threading
import unittest
from unittest.mock import Mock

from mysql.connector import Error as MySQLError

from app.errors import InvalidTokenError
from app.repositories.login_session_repository import LoginSessionRepository
from app.security.tokens import TokenService
from app.services.current_user_service import CurrentUserService


class CurrentUserServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.real_tokens = TokenService()
        self.access_token = self.real_tokens.create_token()
        self.tokens = Mock(spec=TokenService)
        self.tokens.hash_token.side_effect = self.real_tokens.hash_token
        self.repository = Mock(spec=LoginSessionRepository)
        self.repository.get_active_user.return_value = (7, "alice")
        self.service = CurrentUserService(self.repository, self.tokens)

    async def assert_rejected_before_hash_and_query(self, value):
        self.tokens.hash_token.reset_mock()
        self.repository.get_active_user.reset_mock()
        with self.assertRaises(InvalidTokenError):
            await self.service.get_current_user(value)
        self.tokens.hash_token.assert_not_called()
        self.repository.get_active_user.assert_not_called()

    async def test_valid_token_returns_authoritative_user_and_queries_only_hash(self):
        user = await self.service.get_current_user(self.access_token)
        self.assertEqual(user, (7, "alice"))
        self.tokens.hash_token.assert_called_once_with(self.access_token)
        self.repository.get_active_user.assert_called_once_with(
            self.real_tokens.hash_token(self.access_token),
        )
        self.repository.save_session.assert_not_called()
        self.tokens.create_token.assert_not_called()
        self.tokens.get_expires_at.assert_not_called()

    async def test_unknown_expired_or_revoked_lookup_raises_invalid_token(self):
        self.repository.get_active_user.return_value = None
        with self.assertRaises(InvalidTokenError):
            await self.service.get_current_user(self.access_token)

    async def test_non_string_input_is_rejected_before_hash_and_query(self):
        for label, value in (("none", None), ("integer", 123), ("bool", True),
                             ("bytes", b"A" * 43), ("list", []), ("dict", {})):
            with self.subTest(case=label):
                await self.assert_rejected_before_hash_and_query(value)

    async def test_empty_string_raises_invalid_token_before_hashing(self):
        await self.assert_rejected_before_hash_and_query("")

    async def test_wrong_length_is_rejected_before_hash_and_query(self):
        for length in (1, 42, 44, 10000):
            with self.subTest(length=length):
                await self.assert_rejected_before_hash_and_query("A" * length)

    async def test_invalid_characters_are_rejected_without_normalization(self):
        for label, value in (("symbol", "A" * 42 + "!"),
                             ("trailing_newline", "A" * 42 + "\n"),
                             ("trailing_space", "A" * 42 + " "),
                             ("non_ascii", "中" * 43),
                             ("bearer_prefix", "Bearer " + self.access_token)):
            with self.subTest(case=label):
                await self.assert_rejected_before_hash_and_query(value)

    async def test_database_error_propagates_without_becoming_invalid_token(self):
        original = MySQLError("TEST_ONLY_CONNECTION_FAILURE", errno=2003)
        self.repository.get_active_user.side_effect = original
        with self.assertRaises(MySQLError) as caught:
            await self.service.get_current_user(self.access_token)
        self.assertIs(caught.exception, original)

    async def test_internal_hash_failure_propagates_without_query(self):
        original = RuntimeError("TEST_ONLY_HASH_FAILURE")
        self.tokens.hash_token.side_effect = original
        with self.assertRaises(RuntimeError) as caught:
            await self.service.get_current_user(self.access_token)
        self.assertIs(caught.exception, original)
        self.repository.get_active_user.assert_not_called()

    async def test_sync_repository_lookup_runs_in_worker_thread(self):
        loop_thread = threading.get_ident()
        worker_threads = []

        def lookup(token_hash):
            worker_threads.append(threading.get_ident())
            return 7, "alice"

        self.repository.get_active_user.side_effect = lookup
        self.assertEqual(await self.service.get_current_user(self.access_token), (7, "alice"))
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], loop_thread)

    async def test_reused_service_requeries_identity_on_each_request(self):
        self.repository.get_active_user.side_effect = [(7, "alice"), (9, "bob")]
        second_token = self.real_tokens.create_token()
        first = await self.service.get_current_user(self.access_token)
        second = await self.service.get_current_user(second_token)
        self.assertEqual((first, second), ((7, "alice"), (9, "bob")))
        self.assertEqual(self.repository.get_active_user.call_count, 2)
        self.tokens.create_token.assert_not_called()
        self.tokens.get_expires_at.assert_not_called()
        self.repository.save_session.assert_not_called()
