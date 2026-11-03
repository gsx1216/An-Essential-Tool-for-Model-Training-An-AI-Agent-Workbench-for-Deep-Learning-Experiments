"""个人聊天业务回归：真实ChatService搭配假仓储、假网关；无数据库或模型调用。"""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, create_autospec

from app.errors import ModelCredentialNotConfiguredError, SessionNotFoundError
from app.models.chat_turn import ChatTurn, TurnAlreadyCompletedError
from app.repositories.message_repository import MessageRepository
from app.services.user_chat_service import UserChatService
from app.services.user_model_gateway_factory import UserModelGatewayFactory


SESSION = 'b4c4e67554ee4c1981740a1e901ec614'
TURN = '430390d71ece4e63ae202043b057e9c2'
ATTEMPT = 'a' * 32
HISTORY = [('human', '服务器中的最新问题')]


class UserChatServiceChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = create_autospec(MessageRepository, instance=True, spec_set=True)
        self.repository.start_turn.return_value = TURN
        self.repository.find_request_reply.return_value = None
        self.repository.claim_turn.return_value = (ATTEMPT, HISTORY)
        self.repository.get_unfinished_turn.return_value = ChatTurn(TURN, 'failed', 1, None, False, 'MODEL_REQUEST_FAILED')
        self.gateway = SimpleNamespace(invoke=AsyncMock(return_value={'content': '假回复'}), aclose=AsyncMock())
        self.factory = create_autospec(UserModelGatewayFactory, instance=True, spec_set=True)
        self.factory.create_for_user.return_value = self.gateway
        self.service = UserChatService(self.factory, self.repository)

    async def test_send_saves_question_and_reply_and_returns_content(self):
        result = await self.service.send_message(7, SESSION, '假问题', TURN)
        self.assertEqual(result, '假回复')
        self.factory.create_for_user.assert_awaited_once_with(7)
        self.repository.start_turn.assert_called_once_with(7, SESSION, '假问题', TURN)
        self.repository.complete_turn.assert_called_once_with(7, SESSION, TURN, ATTEMPT, '假回复')
        self.gateway.aclose.assert_awaited_once()

    async def test_question_is_saved_before_invoke_and_close_after_reply_commit(self):
        events = []
        self.repository.start_turn.side_effect = lambda *args: events.append('question') or TURN
        self.repository.complete_turn.side_effect = lambda *args: events.append('reply')

        async def invoke(messages):
            events.append('invoke')
            return {'content': '假回复'}

        async def close():
            events.append('close')

        self.gateway.invoke.side_effect = invoke
        self.gateway.aclose.side_effect = close
        await self.service.send_message(7, SESSION, '假问题')
        self.assertEqual(events, ['question', 'invoke', 'reply', 'close'])

    async def test_uses_fresh_database_context(self):
        await self.service.send_message(7, SESSION, '假问题')
        self.gateway.invoke.assert_awaited_once_with([('system', '你是一个乐于助人的ai助手'), *HISTORY])
        self.repository.load_history_page.assert_not_called()

    async def test_retry_reuses_question_and_releases_gateway(self):
        self.assertEqual(await self.service.retry_message(7, SESSION), '假回复')
        self.repository.start_turn.assert_not_called()
        self.repository.get_unfinished_turn.assert_called_once_with(7, SESSION)
        self.gateway.aclose.assert_awaited_once()

    async def test_new_gateway_is_requested_each_time_for_the_correct_user(self):
        second = SimpleNamespace(invoke=AsyncMock(return_value={'content': '另一回复'}), aclose=AsyncMock())
        self.factory.create_for_user.side_effect = [self.gateway, second]
        await self.service.send_message(7, SESSION, '第一问题')
        await self.service.send_message(8, SESSION, '第二问题')
        self.assertEqual([call.args for call in self.factory.create_for_user.await_args_list], [(7,), (8,)])
        self.gateway.aclose.assert_awaited_once()
        second.aclose.assert_awaited_once()

    async def test_invalid_send_input_stops_before_key_lookup(self):
        for user, session, content in [(0, SESSION, 'hi'), (True, SESSION, 'hi'), (7, 'bad', 'hi'), (7, SESSION, ' '), (7, SESSION, None)]:
            with self.subTest(user=user, session=session):
                with self.assertRaises(ValueError):
                    await self.service.send_message(user, session, content)
        self.factory.create_for_user.assert_not_awaited()
        self.repository.start_turn.assert_not_called()

    async def test_invalid_retry_input_stops_before_key_lookup(self):
        for user, session in [(0, SESSION), (7, 'bad')]:
            with self.subTest(user=user):
                with self.assertRaises(ValueError):
                    await self.service.retry_message(user, session)
        self.factory.create_for_user.assert_not_awaited()

    async def test_missing_key_prevents_submission(self):
        self.factory.create_for_user.side_effect = ModelCredentialNotConfiguredError('TEST_ONLY_MISSING')
        with self.assertRaises(ModelCredentialNotConfiguredError):
            await self.service.send_message(7, SESSION, '假问题')
        self.repository.start_turn.assert_not_called()
        self.gateway.invoke.assert_not_awaited()

    async def test_session_ownership_rejection_does_not_invoke_model_and_closes_gateway(self):
        self.repository.start_turn.side_effect = SessionNotFoundError('TEST_ONLY_NOT_OWNED')
        with self.assertRaises(SessionNotFoundError):
            await self.service.send_message(7, SESSION, '假问题')
        self.gateway.invoke.assert_not_awaited()
        self.gateway.aclose.assert_awaited_once()

    async def test_model_failure_preserves_saved_question_and_records_failure(self):
        error = RuntimeError('TEST_ONLY_MODEL_FAILURE')
        self.gateway.invoke.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            await self.service.send_message(7, SESSION, '假问题')
        self.assertIs(caught.exception, error)
        self.repository.start_turn.assert_called_once()
        self.repository.fail_turn.assert_called_once_with(7, SESSION, TURN, ATTEMPT, 'MODEL_REQUEST_FAILED')
        self.repository.complete_turn.assert_not_called()
        self.gateway.aclose.assert_awaited_once()

    async def test_cancellation_records_failure_and_closes_gateway(self):
        self.gateway.invoke.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.send_message(7, SESSION, '假问题')
        self.repository.fail_turn.assert_called_once_with(7, SESSION, TURN, ATTEMPT, 'MODEL_REQUEST_CANCELLED')
        self.gateway.aclose.assert_awaited_once()

    async def test_retry_without_pending_question_does_not_invoke_model(self):
        self.repository.get_unfinished_turn.return_value = None
        with self.assertRaises(ValueError):
            await self.service.retry_message(7, SESSION)
        self.gateway.invoke.assert_not_awaited()
        self.gateway.aclose.assert_awaited_once()

    async def test_completed_request_reads_saved_reply_without_regeneration(self):
        self.repository.claim_turn.side_effect = TurnAlreadyCompletedError('TEST_ONLY_COMPLETED')
        self.repository.get_completed_reply.return_value = '已保存回复'
        self.assertEqual(await self.service.send_message(7, SESSION, '假问题', TURN), '已保存回复')
        self.gateway.invoke.assert_not_awaited()
        self.gateway.aclose.assert_awaited_once()

    async def test_cleanup_failure_preserves_original_model_error(self):
        operation, cleanup = RuntimeError('TEST_ONLY_OPERATION'), RuntimeError('TEST_ONLY_CLEANUP')
        self.gateway.invoke.side_effect = operation
        self.gateway.aclose.side_effect = cleanup
        with self.assertRaises(RuntimeError) as caught:
            await self.service.send_message(7, SESSION, '假问题')
        self.assertIs(caught.exception, operation)
        self.assertIs(caught.exception.__cause__, cleanup)

    async def test_cleanup_failure_after_success_propagates_without_regeneration(self):
        self.gateway.aclose.side_effect = RuntimeError('TEST_ONLY_CLEANUP')
        with self.assertRaises(RuntimeError):
            await self.service.send_message(7, SESSION, '假问题', TURN)
        self.repository.complete_turn.assert_called_once()
        self.gateway.invoke.assert_awaited_once()
        self.repository.fail_turn.assert_not_called()

