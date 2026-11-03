"""聊天会话业务回归：模拟同步仓储，验证结果、校验、异常和工作线程。"""

import threading
import unittest
from unittest.mock import create_autospec

from mysql.connector import Error as MySQLError

from app.errors import SessionNotFoundError
from app.repositories.session_repository import SessionRepository
from app.services.chat_session_service import ChatSessionService


SESSION_ID = "b4c4e67554ee4c1981740a1e901ec614"


class ChatSessionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # autospec 检查真实方法的参数名，防止普通 Mock 隐藏 tilte 之类拼写错误。
        self.repository = create_autospec(SessionRepository, instance=True, spec_set=True)
        self.repository.create_session.return_value = SESSION_ID
        self.repository.list_sessions.return_value = [(SESSION_ID, "新聊天")]
        self.repository.delete_session.return_value = True
        self.service = ChatSessionService(self.repository)

    def test_constructor_keeps_the_injected_repository(self):
        self.assertIs(self.service.repository, self.repository)

    async def test_create_returns_repository_uuid_with_default_title(self):
        self.assertEqual(await self.service.create_session(7), SESSION_ID)
        self.repository.create_session.assert_called_once_with(user_id=7, title="新聊天")

    async def test_create_passes_normalized_title_to_repository(self):
        self.assertEqual(await self.service.create_session(7, "  实验一  "), SESSION_ID)
        self.repository.create_session.assert_called_once_with(user_id=7, title="实验一")

    async def test_list_returns_every_session(self):
        expected = [(SESSION_ID, "会话一"), ("a" * 32, "会话二")]
        self.repository.list_sessions.return_value = expected
        self.assertEqual(await self.service.list_sessions(7), expected)
        self.repository.list_sessions.assert_called_once_with(7)

    async def test_list_preserves_empty_result(self):
        self.repository.list_sessions.return_value = []
        self.assertEqual(await self.service.list_sessions(7), [])

    async def test_delete_returns_none_on_success(self):
        self.assertIsNone(await self.service.delete_session(7, SESSION_ID))
        self.repository.delete_session.assert_called_once_with(7, SESSION_ID)

    async def test_delete_missing_or_unowned_session_raises_business_error(self):
        self.repository.delete_session.return_value = False
        with self.assertRaises(SessionNotFoundError):
            await self.service.delete_session(7, SESSION_ID)

    async def test_delete_passes_canonical_uuid_to_repository(self):
        await self.service.delete_session(7, " B4C4E675-54EE-4C19-8174-0A1E901EC614 ")
        self.repository.delete_session.assert_called_once_with(7, SESSION_ID)

    async def test_invalid_user_ids_stop_before_repository(self):
        operations = ((self.service.create_session, ()), (self.service.list_sessions, ()),
                      (self.service.delete_session, (SESSION_ID,)))
        for operation, remaining in operations:
            for user_id in (None, True, "7", 0, -1, 2**64):
                with self.subTest(operation=operation.__name__, input_type=type(user_id).__name__):
                    with self.assertRaises(ValueError):
                        await operation(user_id, *remaining)
                    self.repository.create_session.assert_not_called()
                    self.repository.list_sessions.assert_not_called()
                    self.repository.delete_session.assert_not_called()

    async def test_invalid_titles_stop_before_repository(self):
        for title in (None, 1, "", "   ", "字" * 201):
            with self.subTest(input_type=type(title).__name__):
                with self.assertRaises(ValueError):
                    await self.service.create_session(7, title)
                self.repository.create_session.assert_not_called()

    async def test_invalid_uuid_stops_before_delete_repository(self):
        for session_id in (None, 1, "", "bad-id"):
            with self.subTest(input_type=type(session_id).__name__):
                with self.assertRaises(ValueError):
                    await self.service.delete_session(7, session_id)
                self.repository.delete_session.assert_not_called()

    async def test_create_database_failure_propagates(self):
        original = MySQLError("TEST_ONLY_CREATE_FAILURE", errno=2013)
        self.repository.create_session.side_effect = original
        with self.assertRaises(MySQLError) as caught:
            await self.service.create_session(7)
        self.assertIs(caught.exception, original)

    async def test_list_database_failure_is_not_an_empty_result(self):
        original = MySQLError("TEST_ONLY_LIST_FAILURE", errno=2013)
        self.repository.list_sessions.side_effect = original
        with self.assertRaises(MySQLError) as caught:
            await self.service.list_sessions(7)
        self.assertIs(caught.exception, original)

    async def test_delete_database_failure_is_not_success_or_missing(self):
        original = MySQLError("TEST_ONLY_DELETE_FAILURE", errno=2013)
        self.repository.delete_session.side_effect = original
        with self.assertRaises(MySQLError) as caught:
            await self.service.delete_session(7, SESSION_ID)
        self.assertIs(caught.exception, original)

    async def test_sync_repository_calls_use_worker_threads(self):
        loop_thread = threading.get_ident()
        cases = ((self.service.create_session, (7,), self.repository.create_session, SESSION_ID),
                 (self.service.list_sessions, (7,), self.repository.list_sessions, []),
                 (self.service.delete_session, (7, SESSION_ID), self.repository.delete_session, True))
        for operation, arguments, repository_method, result in cases:
            with self.subTest(operation=operation.__name__):
                workers = []

                def invoke(*args, **kwargs):
                    workers.append(threading.get_ident())
                    return result

                repository_method.side_effect = invoke
                await operation(*arguments)
                self.assertEqual(len(workers), 1)
                self.assertNotEqual(workers[0], loop_thread)
