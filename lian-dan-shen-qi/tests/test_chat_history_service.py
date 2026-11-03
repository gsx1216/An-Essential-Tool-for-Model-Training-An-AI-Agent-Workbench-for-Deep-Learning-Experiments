"""历史消息业务回归：模拟消息仓储，不连接数据库或模型。"""

import threading
import unittest
from unittest.mock import create_autospec

from mysql.connector import Error as MySQLError

from app.errors import SessionNotFoundError
from app.repositories.message_repository import MessageRepository
from app.services.chat_history_service import ChatHistoryService


SESSION_ID = "b4c4e67554ee4c1981740a1e901ec614"


class ChatHistoryServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = create_autospec(MessageRepository, instance=True, spec_set=True)
        self.rows = [(1, "human", "    print('hello')\n"), (2, "ai", "回复")]
        self.repository.load_history_page.return_value = self.rows
        self.service = ChatHistoryService(self.repository)

    def test_constructor_saves_injected_repository(self):
        self.assertIs(self.service.repository, self.repository)

    async def test_default_page_returns_sequence_role_and_content(self):
        self.assertEqual(await self.service.load_history_page(7, SESSION_ID), self.rows)
        self.repository.load_history_page.assert_called_once_with(7, SESSION_ID, 0, 50)

    async def test_custom_pagination_is_forwarded(self):
        self.repository.load_history_page.return_value = [(3, "human", "下一页")]
        result = await self.service.load_history_page(7, SESSION_ID, after_seq=2, limit=10)
        self.assertEqual(result, [(3, "human", "下一页")])
        self.repository.load_history_page.assert_called_once_with(7, SESSION_ID, 2, 10)

    async def test_empty_history_is_valid_empty_list(self):
        self.repository.load_history_page.return_value = []
        self.assertEqual(await self.service.load_history_page(7, SESSION_ID), [])

    async def test_uuid_is_normalized_before_repository_call(self):
        await self.service.load_history_page(7, " B4C4E675-54EE-4C19-8174-0A1E901EC614 ")
        self.repository.load_history_page.assert_called_once_with(7, SESSION_ID, 0, 50)

    async def test_invalid_user_ids_never_reach_repository(self):
        for value in (None, True, "7", 0, -1, 2**64):
            with self.subTest(input_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    await self.service.load_history_page(value, SESSION_ID)
                self.repository.load_history_page.assert_not_called()

    async def test_invalid_uuid_never_reaches_repository(self):
        for value in (None, 7, "", "bad-id"):
            with self.subTest(input_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    await self.service.load_history_page(7, value)
                self.repository.load_history_page.assert_not_called()

    async def test_missing_or_unowned_conversation_error_propagates(self):
        error = SessionNotFoundError("TEST_ONLY_NOT_OWNED")
        self.repository.load_history_page.side_effect = error
        with self.assertRaises(SessionNotFoundError) as caught:
            await self.service.load_history_page(7, SESSION_ID)
        self.assertIs(caught.exception, error)

    async def test_repository_pagination_rejection_is_not_empty_history(self):
        error = ValueError("TEST_ONLY_BAD_PAGINATION")
        self.repository.load_history_page.side_effect = error
        with self.assertRaises(ValueError) as caught:
            await self.service.load_history_page(7, SESSION_ID, limit=101)
        self.assertIs(caught.exception, error)

    async def test_database_error_is_not_swallowed(self):
        error = MySQLError("TEST_ONLY_HISTORY_DATABASE_FAILURE", errno=2013)
        self.repository.load_history_page.side_effect = error
        with self.assertRaises(MySQLError) as caught:
            await self.service.load_history_page(7, SESSION_ID)
        self.assertIs(caught.exception, error)

    async def test_repository_runs_in_worker_thread(self):
        loop_thread = threading.get_ident()
        workers = []

        def read(*args, **kwargs):
            workers.append(threading.get_ident())
            return self.rows

        self.repository.load_history_page.side_effect = read
        self.assertEqual(await self.service.load_history_page(7, SESSION_ID), self.rows)
        self.assertEqual(len(workers), 1)
        self.assertNotEqual(workers[0], loop_thread)

    async def test_reading_history_does_not_write_messages_or_turns(self):
        await self.service.load_history_page(7, SESSION_ID)
        self.repository.start_turn.assert_not_called()
        self.repository.claim_turn.assert_not_called()
        self.repository.complete_turn.assert_not_called()
        self.repository.fail_turn.assert_not_called()
