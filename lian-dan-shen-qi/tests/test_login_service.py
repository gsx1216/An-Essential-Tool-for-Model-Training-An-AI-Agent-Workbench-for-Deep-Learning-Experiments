"""登录签发业务回归：模拟认证和仓储，使用真实 Token 工具，不写数据库。"""

from datetime import datetime, timezone
import threading
import unittest
from unittest.mock import AsyncMock, Mock

from mysql.connector import Error as MySQLError

from app.errors import InvalidCredentialsError
from app.models.login_result import LoginResult
from app.repositories.login_session_repository import LoginSessionRepository, _validate_login_session_parameters
from app.security.tokens import TokenService
from app.services.auth_service import AuthService
from app.services.login_service import LoginService


class LoginServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.auth = Mock(spec=AuthService)
        self.auth.authenticate = AsyncMock(return_value=(7, "alice"))
        self.repository = Mock(spec=LoginSessionRepository)
        self.repository.save_session.return_value = 17
        self.real_tokens = TokenService()
        self.tokens = Mock(spec=TokenService)
        self.tokens.ttl_seconds = 259200
        self.tokens.create_token.side_effect = self.real_tokens.create_token
        self.tokens.hash_token.side_effect = self.real_tokens.hash_token
        self.tokens.get_expires_at.side_effect = self.real_tokens.get_expires_at
        self.service = LoginService(self.auth, self.repository, self.tokens)

    async def test_success_returns_token_lifetime_and_authoritative_user(self):
        result = await self.service.login("  ALICE  ", "Test1234")
        self.assertIsInstance(result, LoginResult)
        self.assertEqual((result.user_id, result.username), (7, "alice"))
        self.assertEqual(result.expires_in, 259200)
        self.assertEqual(result.token_type, "bearer")
        self.assertNotIn(result.access_token, repr(result))
        self.auth.authenticate.assert_awaited_once_with("  ALICE  ", "Test1234")
        self.tokens.hash_token.assert_called_once_with(result.access_token)
        user_id, stored_hash, expires_at = self.repository.save_session.call_args.args
        self.assertEqual(user_id, 7)
        self.assertEqual(stored_hash, self.real_tokens.hash_token(result.access_token))
        self.assertNotEqual(stored_hash, result.access_token)
        self.assertEqual(expires_at.tzinfo, timezone.utc)
        self.assertGreater(expires_at, datetime.now(timezone.utc))

    async def test_rejected_credentials_do_not_create_or_save_token(self):
        original = ValueError("用户名或密码错误")
        self.auth.authenticate.side_effect = original
        with self.assertRaises(InvalidCredentialsError) as caught:
            await self.service.login("missing", "WrongPassword8")
        self.assertIs(caught.exception.__cause__, original)
        self.tokens.create_token.assert_not_called()
        self.tokens.hash_token.assert_not_called()
        self.repository.save_session.assert_not_called()

    async def test_hash_failure_does_not_reach_database(self):
        original = RuntimeError("TEST_ONLY_HASH_FAILURE")
        self.tokens.hash_token.side_effect = original
        with self.assertRaises(RuntimeError) as caught:
            await self.service.login("alice", "Test1234")
        self.assertIs(caught.exception, original)
        self.repository.save_session.assert_not_called()

    async def test_generated_parameters_pass_real_repository_validation(self):
        def validate_and_save(user_id, token_hash, expires_at):
            _validate_login_session_parameters(user_id, token_hash, expires_at)
            return 17

        self.repository.save_session.side_effect = validate_and_save
        result = await self.service.login("alice", "Test1234")
        self.assertEqual(result.user_id, 7)

    async def test_database_failure_propagates_without_success_response(self):
        original = MySQLError("TEST_ONLY_DATABASE_FAILURE", errno=2003)
        self.repository.save_session.side_effect = original
        with self.assertRaises(MySQLError) as caught:
            await self.service.login("alice", "Test1234")
        self.assertIs(caught.exception, original)

    async def test_synchronous_database_operation_runs_in_worker_thread(self):
        event_loop_thread = threading.get_ident()
        worker_threads = []

        def save(user_id, token_hash, expires_at):
            worker_threads.append(threading.get_ident())
            return 17

        self.repository.save_session.side_effect = save
        await self.service.login("alice", "Test1234")
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], event_loop_thread)

    async def test_repeated_logins_get_distinct_tokens(self):
        first = await self.service.login("alice", "Test1234")
        second = await self.service.login("alice", "Test1234")
        self.assertNotEqual(first.access_token, second.access_token)
        self.assertEqual(self.repository.save_session.call_count, 2)
