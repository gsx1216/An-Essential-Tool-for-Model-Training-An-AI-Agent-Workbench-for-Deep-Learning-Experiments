"""冗余清理回归：保留归属/幂等/脱敏，验证读取与预算的实际行为。"""

import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.errors import ChatToolBudgetError, ModelCredentialNotConfiguredError, RequestIdConflictError, SessionNotFoundError
from app.logging_config import SafeJsonFormatter, SafeTextFormatter
from app.models.chat_tools import bounded_result, compact_json, request_size, tool_result_budget
from app.models.context_snapshot import ContextAttempt
from app.models.context_state import ContextMessage, HistoryGap
from app.repositories.message_repository import MessageRepository
from app.services.context_manager import ContextManager
from app.services.read_only_chat_service import ReadOnlyChatService
from app.services.token_budget import Utf8BudgetCounter
from app.services.user_chat_service import UserChatService

SID, RID, TID = 'a'*32, 'b'*32, 'c'*32
ATTEMPT = ContextAttempt(TID, 'd'*32)
SECRET = 'PRIVATE_TEST_VALUE_NOT_FOR_LOGS'


class CleanupContracts(unittest.TestCase):
    def test_safe_log_metadata_is_visible_but_message_args_and_secrets_are_not(self):
        record = logging.LogRecord('app.test', logging.WARNING, __file__, 1, 'cache_bypassed', (SECRET,), None)
        record.operation, record.error_type, record.fallback = 'context_cache.read', 'ConnectionError', 'mysql'
        record.session_id, record.event_id, record.message_id = SID, RID, '123-0'
        record.delivery_status, record.state_saved, record.password = 'pending', False, SECRET
        data = SafeJsonFormatter().as_dict(record)
        self.assertEqual(data['event'], 'cache_bypassed')
        self.assertEqual(data['operation'], 'context_cache.read')
        self.assertEqual(data['fallback'], 'mysql')
        self.assertEqual(data['session_id'], SID)
        self.assertEqual(data['message_id'], '123-0')
        self.assertEqual(data['delivery_status'], 'pending')
        self.assertFalse(data['state_saved'])
        text = SafeTextFormatter().format(record)
        self.assertIn('fallback=mysql', text)
        self.assertIn('event_id='+RID, text)
        self.assertNotIn(SECRET, text + json.dumps(data))

    def test_invalid_new_log_fields_are_dropped(self):
        record = logging.LogRecord('app.test', logging.INFO, __file__, 1, 'cache_reconciled', (), None)
        record.message_id, record.session_id = 'not-an-id\nsecret', SECRET
        record.delivery_status, record.cache_mode, record.fallback = {}, ['delta'], 'password'
        record.user_id, record.tail_seq = True, -1
        data = SafeJsonFormatter().as_dict(record)
        for key in ('message_id', 'session_id', 'delivery_status', 'cache_mode', 'fallback', 'user_id', 'tail_seq'):
            self.assertNotIn(key, data)

    def test_tool_budget_scales_and_cannot_exceed_its_available_bytes(self):
        self.assertEqual(tool_result_budget(49152), 16384)
        self.assertEqual(tool_result_budget(12000), 4000)
        for maximum in (100, 400, 2000):
            result = bounded_result({'ok': True, 'code': 'OK', 'data': {'text': '中'*2000}}, maximum)
            self.assertLessEqual(len(compact_json(result).encode()), maximum)
        with self.assertRaises(ChatToolBudgetError):
            bounded_result({'ok': True, 'code': 'OK', 'data': {'text': 'test'}}, 0)

    def test_completed_request_read_is_owned_content_checked_and_only_one_query(self):
        repo = MessageRepository('test-only-placeholder')
        cases = [
            (('completed', 'human', 'question', 'saved answer'), 'saved answer', None),
            ((None, None, None, None), None, None),
            (('failed', 'human', 'question', None), None, None),
            (None, None, SessionNotFoundError),
            (('completed', 'human', 'different question', 'saved answer'), None, RequestIdConflictError),
            (('completed', 'human', 'question', None), None, ValueError),
        ]
        for row, expected, error in cases:
            with self.subTest(row=row):
                connection = MagicMock()
                connection.__enter__.return_value = connection
                cursor = connection.cursor.return_value.__enter__.return_value
                cursor.fetchone.return_value = row
                with patch('app.repositories.message_repository.get_connection', return_value=connection):
                    if error:
                        with self.assertRaises(error): repo.find_request_reply(7, SID, TID, 'question')
                    else:
                        self.assertEqual(repo.find_request_reply(7, SID, TID, 'question'), expected)
                cursor.execute.assert_called_once_with(repo.find_request_reply_sql, (TID, 7, SID))
                self.assertIn('s.user_id = %s', repo.find_request_reply_sql)
                self.assertNotIn('FOR UPDATE', repo.find_request_reply_sql)
                connection.commit.assert_not_called()


class CleanupFlows(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 本文件使用替身，不创建日志文件、连接数据库或调用模型。
        self.patch = patch('app.logging_config.configure_logging')
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_saved_reply_is_returned_when_personal_key_has_been_deleted(self):
        repository = MagicMock()
        repository.find_request_reply.return_value = 'saved answer'
        factory = SimpleNamespace(create_for_user=AsyncMock(side_effect=ModelCredentialNotConfiguredError('test')))
        service = UserChatService(factory, repository)
        self.assertEqual(await service.send_message(7, SID, 'question', TID), 'saved answer')
        factory.create_for_user.assert_not_awaited()
        repository.start_turn.assert_not_called()

    async def test_foreign_session_and_changed_question_stop_before_key_loading(self):
        for error in (SessionNotFoundError(), RequestIdConflictError()):
            repository = MagicMock()
            repository.find_request_reply.side_effect = error
            factory = SimpleNamespace(create_for_user=AsyncMock())
            with self.assertRaises(type(error)):
                await UserChatService(factory, repository).send_message(7, SID, 'question', TID)
            factory.create_for_user.assert_not_awaited()
            repository.start_turn.assert_not_called()

    async def test_new_question_without_key_is_not_saved(self):
        repository = MagicMock()
        repository.find_request_reply.return_value = None
        factory = SimpleNamespace(create_for_user=AsyncMock(side_effect=ModelCredentialNotConfiguredError('test')))
        with self.assertRaises(ModelCredentialNotConfiguredError):
            await UserChatService(factory, repository).send_message(7, SID, 'question', TID)
        repository.start_turn.assert_not_called()

    async def test_history_validation_runs_once_and_gap_marker_is_preserved(self):
        manager = ContextManager(Utf8BudgetCounter(), MagicMock())
        history = [ContextMessage(1, 'human', 'q1'), ContextMessage(2, 'ai', 'a1'), ContextMessage(4, 'human', 'q2')]
        with patch.object(manager, 'validate_history', wraps=manager.validate_history) as validate:
            prepared = await manager.prepare(history)
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(prepared.missing_ranges, (HistoryGap(3, 3),))
        self.assertEqual(prepared.model_messages[-1], ('human', 'q2'))
        self.assertIn('3～3', compact_json(prepared.model_messages))
        # 独立公开入口仍拒绝非法输入。
        with self.assertRaises(ValueError): manager.build_history_messages([])

    async def run_tools(self, texts, maximum):
        repo = MagicMock(); repo.binding.return_value = RID
        service = ReadOnlyChatService(repo, MagicMock(), metrics_factory=MagicMock(), runner_factory=MagicMock())
        responses = [{'content': '', 'tool_calls': [{'id': 'call_'+str(i), 'name': 'get_run_logs', 'args': {}}]}
                     for i in range(len(texts))]
        gateway = SimpleNamespace(invoke=AsyncMock(side_effect=responses+[{'content': 'done'}]))
        results = [({'ok': True, 'code': 'OK', 'run_id': RID, 'data': {'text': text}}, 'set', RID) for text in texts]
        with patch.object(service, '_execute', new=AsyncMock(side_effect=results)) as execute:
            answer = await service.answer([('system', 'assistant'), ('human', 'question')], user_id=7,
                session_id=SID, attempt=ATTEMPT, gateway=gateway, max_input_tokens=maximum)
        return repo, gateway, execute, answer

    async def test_three_excerpts_fit_48k_without_old_4500_byte_cutoff(self):
        repo, gateway, execute, answer = await self.run_tools(['x'*2200]*3, 49152)
        self.assertEqual(answer, 'done')
        self.assertEqual(execute.await_count, 3)
        self.assertTrue(all(call.kwargs['outcome']=='OK' for call in repo.record.call_args_list))
        messages = gateway.invoke.call_args.args[0]
        self.assertEqual([len(json.loads(m['content'])['data']['text']) for m in messages if m['role']=='tool'], [2200]*3)
        self.assertLessEqual(request_size(messages, gateway.invoke.call_args.kwargs['tools']), 49152)
        self.assertNotIn('result_sha256', repo.record.call_args.kwargs['metadata'])

    async def test_exhausted_result_budget_prevents_next_read(self):
        repo, gateway, execute, _ = await self.run_tools(['x'*7700, 'not read'], 24000)
        execute.assert_awaited_once()
        self.assertEqual(repo.record.call_args.kwargs['outcome'], 'RESULT_BUDGET')
        self.assertEqual(gateway.invoke.call_args.kwargs['tool_choice'], 'none')


if __name__ == '__main__':
    unittest.main()
