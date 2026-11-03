import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.errors import NoPendingReplyError
from app.models.chat_turn import ChatTurn, TurnAlreadyCompletedError
from app.repositories.message_repository import MessageRepository
from app.services.chat_service import ChatService


SESSION_ID = "b4c4e67554ee4c1981740a1e901ec614"
TURN_ID = "430390d71ece4e63ae202043b057e9c2"
ATTEMPT_ID = "a" * 32
HISTORY = [("human", "之前的提问"), ("ai", "之前的回复"), ("human", "本轮提问")]


class ChatServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = MagicMock(spec=MessageRepository)
        self.repository.start_turn.return_value = TURN_ID
        self.repository.claim_turn.return_value = (ATTEMPT_ID, HISTORY)
        self.repository.get_unfinished_turn.return_value = ChatTurn(
            TURN_ID, "failed", 3, "b" * 32, False, "MODEL_REQUEST_FAILED",
        )
        self.gateway = SimpleNamespace(invoke=AsyncMock(return_value={"content": "本轮回复"}))
        self.service = ChatService(self.gateway, self.repository)

    async def test_send_preserves_question_before_model_and_returns_only_after_save(self):
        events = []

        def start(*args):
            events.append("question_saved")
            return TURN_ID

        async def invoke(messages):
            events.append("model_called")
            return {"content": "本轮回复"}

        self.repository.start_turn.side_effect = start
        self.gateway.invoke.side_effect = invoke
        self.repository.complete_turn.side_effect = lambda *args: events.append("reply_saved")
        result = await self.service.send_message(1, SESSION_ID, "本轮提问", TURN_ID)
        self.assertEqual(result, "本轮回复")
        self.assertEqual(events, ["question_saved", "model_called", "reply_saved"])
        self.repository.start_turn.assert_called_once_with(1, SESSION_ID, "本轮提问", TURN_ID)
        expected = [("system", "你是一个乐于助人的ai助手"), *HISTORY]
        self.gateway.invoke.assert_awaited_once_with(expected)
        self.repository.complete_turn.assert_called_once_with(1, SESSION_ID, TURN_ID, ATTEMPT_ID, "本轮回复")
        self.repository.fail_turn.assert_not_called()

    async def test_model_error_marks_failure_without_writing_reply(self):
        failure = RuntimeError("simulated model timeout")
        self.gateway.invoke.side_effect = failure
        with self.assertRaises(RuntimeError) as caught:
            await self.service.send_message(1, SESSION_ID, "本轮提问")
        self.assertIs(caught.exception, failure)
        self.repository.start_turn.assert_called_once()
        self.repository.fail_turn.assert_called_once_with(1, SESSION_ID, TURN_ID, ATTEMPT_ID, "MODEL_REQUEST_FAILED")
        self.repository.complete_turn.assert_not_called()

    async def test_invalid_model_reply_is_failed_before_database_completion(self):
        for content in ("", " \n", None, ["unexpected block"]):
            with self.subTest(content=content):
                self.repository.reset_mock()
                self.gateway.invoke.return_value = {"content": content}
                with self.assertRaises(ValueError):
                    await self.service.send_message(1, SESSION_ID, "本轮提问")
                self.repository.fail_turn.assert_called_once()
                self.repository.complete_turn.assert_not_called()

    async def test_database_completion_error_does_not_trigger_regeneration_or_failure_status(self):
        failure = RuntimeError("simulated writeback failure")
        self.repository.complete_turn.side_effect = failure
        with self.assertRaises(RuntimeError) as caught:
            await self.service.send_message(1, SESSION_ID, "本轮提问")
        self.assertIs(caught.exception, failure)
        self.gateway.invoke.assert_awaited_once()
        self.repository.fail_turn.assert_not_called()

    async def test_failed_state_write_preserves_original_error_and_cause(self):
        model_error = RuntimeError("simulated model timeout")
        state_error = RuntimeError("simulated state write failure")
        self.gateway.invoke.side_effect = model_error
        self.repository.fail_turn.side_effect = state_error
        with self.assertRaises(RuntimeError) as caught:
            await self.service.send_message(1, SESSION_ID, "本轮提问")
        self.assertIs(caught.exception, model_error)
        self.assertIs(caught.exception.__cause__, state_error)

    async def test_retry_reuses_saved_question_without_starting_another_turn(self):
        self.assertEqual(await self.service.retry_message(1, SESSION_ID), "本轮回复")
        self.repository.start_turn.assert_not_called()
        self.repository.claim_turn.assert_called_once_with(1, SESSION_ID, TURN_ID)
        messages = self.gateway.invoke.await_args.args[0]
        self.assertEqual(messages.count(("human", "本轮提问")), 1)
        self.assertNotIn(("human", "继续"), messages)

    async def test_retry_without_unfinished_question_does_not_call_model(self):
        self.repository.get_unfinished_turn.return_value = None
        with self.assertRaisesRegex(NoPendingReplyError, "没有等待回复的问题"):
            await self.service.retry_message(1, SESSION_ID)
        self.gateway.invoke.assert_not_awaited()
        self.repository.claim_turn.assert_not_called()

    async def test_active_task_error_does_not_call_model_or_mark_another_attempt_failed(self):
        self.repository.claim_turn.side_effect = ValueError("模型正在回复")
        with self.assertRaisesRegex(ValueError, "正在回复"):
            await self.service.retry_message(1, SESSION_ID)
        self.gateway.invoke.assert_not_awaited()
        self.repository.fail_turn.assert_not_called()

    async def test_repeated_completed_request_returns_saved_reply_without_model(self):
        self.repository.claim_turn.side_effect = TurnAlreadyCompletedError("already completed")
        self.repository.get_completed_reply.return_value = "以前已经保存的回复"
        self.assertEqual(await self.service.send_message(1, SESSION_ID, "本轮提问", TURN_ID), "以前已经保存的回复")
        self.gateway.invoke.assert_not_awaited()
        self.repository.complete_turn.assert_not_called()

    async def test_invalid_input_stops_before_saving_question_or_calling_model(self):
        for user_id, session_id, content in ((0, SESSION_ID, "hi"), (1, "bad", "hi"), (1, SESSION_ID, " ")):
            with self.subTest(user_id=user_id, session_id=session_id, content=content):
                with self.assertRaises(ValueError):
                    await self.service.send_message(user_id, session_id, content)
        self.repository.start_turn.assert_not_called()
        self.gateway.invoke.assert_not_awaited()

    async def test_cancelled_generation_marks_cancelled_failure_and_propagates_cancellation(self):
        self.gateway.invoke.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.send_message(1, SESSION_ID, "本轮提问")
        self.repository.fail_turn.assert_called_once_with(1, SESSION_ID, TURN_ID, ATTEMPT_ID, "MODEL_REQUEST_CANCELLED")
        self.repository.complete_turn.assert_not_called()
