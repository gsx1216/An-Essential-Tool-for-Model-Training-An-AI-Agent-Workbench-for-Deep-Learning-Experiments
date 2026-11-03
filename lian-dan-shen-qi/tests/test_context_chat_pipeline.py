"""发送/重试与上下文管线单元回归：正式估算器、任务栅栏、隔离及 HTTP 错误映射。"""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, create_autospec, patch

from fastapi import HTTPException
from mysql.connector import Error as MySQLError

from app import config
from app.api import dependencies
from app.api.chat_routes import _raise_chat_http_error
from app.errors import SessionNotFoundError
from app.models.context_settings import ContextSettings
from app.models.context_snapshot import ContextAttempt
from app.models.context_state import ContextCompactionError, ContextConflictError, ContextStorageError, ContextTooLargeError, InputCompressionError, PreparedContext
from app.repositories import context_repository, message_repository
from app.repositories.context_repository import ContextRepository
from app.repositories.message_repository import MessageRepository
from app.services.chat_context_factory import AttemptGateway, ChatContextFactory
from app.services.chat_service import ChatService
from app.services.history_summarizer import ModelHistorySummarizer
from app.services.input_compressor import ModelInputCompressor
from app.services.token_budget import Utf8BudgetCounter
from app.services.user_chat_service import UserChatService


SID, TURN, ATTEMPT = 'b4c4e67554ee4c1981740a1e901ec614', '430390d71ece4e63ae202043b057e9c2', 'a' * 32


class BudgetAndSettingsTests(unittest.TestCase):
    def test_utf8_counter_counts_multibyte_and_message_framing(self):
        counter = Utf8BudgetCounter()
        a = counter.count_messages([('human', 'a')])
        chinese = counter.count_messages([('human', '中')])
        self.assertEqual(chinese - a, 2)
        self.assertGreater(counter.count_messages([('human', ''), ('ai', '')]), counter.count_messages([('human', '')]))
        self.assertEqual(counter.count_messages([('human', '  a\n')]) - a, 3)

    def test_counter_rejects_nested_messages_nontext_and_invalid_unicode(self):
        for messages in (None, [('human', 1)], [[('human', 'a')]], [('tool', 'a')], [('human', '\ud800')]):
            with self.subTest(messages_type=type(messages).__name__), self.assertRaises(ValueError):
                Utf8BudgetCounter().count_messages(messages)

    def test_real_estimator_generates_small_output_cap_and_length_hint(self):
        counter = Utf8BudgetCounter()
        limit, hint = counter.output_budget(2000)
        self.assertLess(limit, 2000)
        self.assertGreater(limit, 0)
        self.assertIn('UTF-8', hint)
        with self.assertRaises(ValueError):
            counter.output_budget(1)

    def test_invalid_deployment_values_rejected(self):
        for values in (dict(max_input_tokens=True), dict(keep_recent_turns=0), dict(max_summary_tokens=5000),
                       dict(lease_seconds=3601), dict(compact_at_tokens=ContextSettings().max_input_tokens+1),
                       dict(max_summary_tokens=ContextSettings().compact_at_tokens)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ContextSettings(**values)

    def test_config_defaults_and_explicit_overrides_do_not_read_personal_key(self):
        with patch.object(config, 'load_dotenv'), patch.dict('os.environ', {}, clear=True):
            self.assertEqual(config.get_context_settings(), ContextSettings())
            with patch.dict('os.environ', {'CHAT_KEEP_RECENT_TURNS': '2', 'CHAT_CONTEXT_LEASE_SECONDS': '1200'}):
                settings = config.get_context_settings()
            self.assertEqual(settings.keep_recent_turns, 2)
            self.assertEqual(settings.lease_seconds, 1200)

    def test_config_rejects_empty_signed_and_nondecimal_values(self):
        for raw in ('', '0', '-1', '1.5', ' 2 ', '１２'):
            with patch.object(config, 'load_dotenv'), patch.dict('os.environ', {'CHAT_KEEP_RECENT_TURNS': raw}, clear=True):
                with self.subTest(raw=raw), self.assertRaises(ValueError):
                    config.get_context_settings()

    def test_attempt_normalizes_ids_and_rejects_bad_values(self):
        self.assertEqual(ContextAttempt(TURN.upper(), ATTEMPT).turn_id, TURN)
        for values in ((None, ATTEMPT), (TURN, 'bad')):
            with self.assertRaises(ValueError):
                ContextAttempt(*values)


class GuardedPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = create_autospec(ContextRepository, instance=True)
        self.gateway = SimpleNamespace(invoke=AsyncMock(return_value={'content': '假回复'}))
        self.attempt = ContextAttempt(TURN, ATTEMPT)

    async def test_guard_checks_current_attempt_before_every_model_call(self):
        events = []
        self.repo.check_attempt.side_effect = lambda *args: events.append('guard')
        async def invoke(*args, **kwargs):
            events.append('invoke')
            return {'content': '假摘要'}
        self.gateway.invoke.side_effect = invoke
        guarded = AttemptGateway(self.gateway, self.repo, 7, SID, self.attempt)
        await guarded.invoke([('human', '第一批')], max_tokens=20)
        await guarded.invoke([('human', '第二批')], max_tokens=10)
        self.assertEqual(events, ['guard', 'invoke', 'guard', 'invoke'])
        self.assertEqual(self.gateway.invoke.await_args.kwargs, {'max_tokens': 10})

    async def test_stale_or_deleted_attempt_never_invokes_provider(self):
        guarded = AttemptGateway(self.gateway, self.repo, 7, SID, self.attempt)
        for error in (ContextConflictError('PRIVATE'), SessionNotFoundError('PRIVATE'), MySQLError('PRIVATE')):
            self.repo.check_attempt.side_effect = error
            with self.assertRaises(type(error)):
                await guarded.invoke([('human', '资料')])
        self.gateway.invoke.assert_not_awaited()

    async def test_factory_builds_fresh_manager_and_guard_per_attempt_with_same_users_gateway(self):
        factory = ChatContextFactory(self.gateway, self.repo, ContextSettings())
        service_a, guard_a = factory.create(7, SID, self.attempt)
        service_b, guard_b = factory.create(8, SID, ContextAttempt(TURN, 'b' * 32))
        self.assertIsNot(service_a.manager, service_b.manager)
        self.assertIsNot(guard_a, guard_b)
        self.assertIs(guard_a.gateway, self.gateway)
        self.assertEqual((guard_a.user_id, guard_b.user_id), (7, 8))
        self.assertIsInstance(service_a.manager.counter, Utf8BudgetCounter)
        self.gateway.invoke.assert_not_awaited()

    async def test_generation_adapters_do_not_wrap_guard_failures_as_model_failures(self):
        from app.models.context_state import ContextMessage
        from scripts.check_context_manager import FakeTokenCounter
        guarded = AttemptGateway(self.gateway, self.repo, 7, SID, self.attempt)
        for error in (ContextConflictError('PRIVATE'), SessionNotFoundError('PRIVATE'), MySQLError('PRIVATE')):
            self.repo.check_attempt.side_effect = error
            with self.assertRaises(type(error)):
                await ModelHistorySummarizer(guarded, FakeTokenCounter()).summarize(
                    previous_summary=None, messages=[ContextMessage(1, 'human', '问题')], max_tokens=200)
            with self.assertRaises(type(error)):
                await ModelInputCompressor(guarded, FakeTokenCounter()).compress('x' * 500, target_tokens=100)
        self.gateway.invoke.assert_not_awaited()

    async def test_estimator_adapters_include_byte_hint_and_use_low_output_cap(self):
        from app.models.context_state import ContextMessage
        counter = Utf8BudgetCounter()
        self.gateway.invoke.return_value = {'content': '摘要'}
        await ModelHistorySummarizer(self.gateway, counter).summarize(
            previous_summary=None, messages=[ContextMessage(1, 'human', '问题')], max_tokens=600)
        call = self.gateway.invoke.await_args
        self.assertIn('UTF-8', call.args[0][0][1])
        self.assertLess(call.kwargs['max_tokens'], 100)


class ChatServiceContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_failure_has_own_error_code_and_never_starts_answer(self):
        repository = create_autospec(MessageRepository, instance=True)
        repository.start_turn.return_value = TURN
        repository.claim_turn.return_value = (ATTEMPT, [])
        gateway = SimpleNamespace(invoke=AsyncMock())
        context = SimpleNamespace(prepare=AsyncMock(side_effect=ContextCompactionError('PRIVATE')))
        factory = SimpleNamespace(settings=ContextSettings(), create=Mock(return_value=(context, gateway)))
        with self.assertRaises(ContextCompactionError):
            await ChatService(gateway, repository, context_factory=factory).send_message(7, SID, '原问题', TURN)
        repository.fail_turn.assert_called_once_with(7, SID, TURN, ATTEMPT, 'CONTEXT_PREPARATION_FAILED')
        gateway.invoke.assert_not_awaited()

    async def test_claim_skips_full_history_then_prepares_then_calls_guarded_answer_gateway(self):
        repository = create_autospec(MessageRepository, instance=True)
        repository.start_turn.return_value = TURN
        repository.claim_turn.return_value = (ATTEMPT, [])
        gateway = SimpleNamespace(invoke=AsyncMock())
        context = SimpleNamespace(prepare=AsyncMock(return_value=PreparedContext([('human', '准备好的输入')], None, 10, False)))
        guarded = SimpleNamespace(invoke=AsyncMock(return_value={'content': '已生成回复'}))
        factory = SimpleNamespace(settings=ContextSettings(), create=Mock(return_value=(context, guarded)))
        result = await ChatService(gateway, repository, context_factory=factory).send_message(7, SID, '原问题', TURN)
        self.assertEqual(result, '已生成回复')
        repository.claim_turn.assert_called_once_with(7, SID, TURN, lease_seconds=1800, load_history=False)
        context.prepare.assert_awaited_once_with(7, SID, attempt=ContextAttempt(TURN, ATTEMPT))
        guarded.invoke.assert_awaited_once_with([('human', '准备好的输入')])
        gateway.invoke.assert_not_awaited()
        repository.complete_turn.assert_called_once_with(7, SID, TURN, ATTEMPT, '已生成回复')

    async def test_http_service_provides_context_factory_and_still_closes_original_gateway(self):
        gateway = SimpleNamespace(invoke=AsyncMock(), aclose=AsyncMock())
        provider = SimpleNamespace(create_for_user=AsyncMock(return_value=gateway))
        repository = Mock()
        repository.find_request_reply.return_value = None
        service = UserChatService(provider, repository, context_repository=Mock(), context_settings=ContextSettings())
        with patch('app.services.user_chat_service.ChatService') as chat:
            chat.return_value.send_message = AsyncMock(return_value='结果')
            self.assertEqual(await service.send_message(7, SID, '问题', TURN), '结果')
            self.assertIsInstance(chat.call_args.kwargs['context_factory'], ChatContextFactory)
        gateway.aclose.assert_awaited_once()


class AttemptRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = ContextRepository('TEST_ONLY')
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.attempt = ContextAttempt(TURN, ATTEMPT)

    def check(self):
        with patch.object(context_repository, 'get_connection', return_value=self.connection):
            self.repo.check_attempt(7, SID, self.attempt)

    def test_valid_lease_and_tail_commit_short_check_transaction(self):
        self.cursor.fetchone.side_effect = [(SID, '标题'), (TURN, 'running', 5, ATTEMPT, True, None), (5,)]
        self.check()
        self.connection.commit.assert_called_once()

    def test_stale_status_attempt_lease_or_tail_is_rejected(self):
        for row, tail in (((TURN, 'running', 5, ATTEMPT, False, None), (5,)),
                          ((TURN, 'running', 5, 'b' * 32, True, None), (5,)),
                          ((TURN, 'completed', 5, ATTEMPT, True, None), (5,)),
                          ((TURN, 'running', 5, ATTEMPT, True, None), (7,)), (None, (5,))):
            self.cursor.fetchone.side_effect = [(SID, '标题'), row, tail]
            with self.subTest(row_type=type(row).__name__), self.assertRaises(ContextConflictError):
                self.check()
        self.connection.commit.assert_not_called()

    def test_no_history_claim_reads_only_saved_current_question(self):
        repo = MessageRepository('TEST_ONLY')
        self.cursor.fetchone.side_effect = [(7, '用户'), (SID, '标题'),
            (TURN, 'pending', 5, None, False, None), (5,), ('human', '当前问题')]
        self.cursor.rowcount = 1
        with patch.object(message_repository, 'get_connection', return_value=self.connection):
            attempt_id, messages = repo.claim_turn(7, SID, TURN, load_history=False)
        self.assertEqual(messages, [])
        self.assertEqual(len(attempt_id), 32)
        self.assertNotIn(repo.lock_messages_sql, [c.args[0] for c in self.cursor.execute.call_args_list])


class ContextHttpMappingTests(unittest.TestCase):
    def test_context_errors_have_fixed_safe_statuses(self):
        for error, status in ((ContextConflictError('PRIVATE'), 409), (ContextTooLargeError('PRIVATE'), 413),
                              (ContextCompactionError('PRIVATE'), 502), (InputCompressionError('PRIVATE'), 502),
                              (ContextStorageError('PRIVATE'), 503)):
            with self.subTest(error_type=type(error).__name__), self.assertRaises(HTTPException) as caught:
                _raise_chat_http_error(error, 'TEST_ONLY')
            self.assertEqual(caught.exception.status_code, status)
            self.assertNotIn('PRIVATE', caught.exception.detail)

    def test_formal_dependency_always_enables_context_without_creating_sdk_or_querying_database(self):
        dependencies.get_user_chat_service.cache_clear()
        try:
            with patch.object(dependencies, 'get_mysql_password', return_value='TEST_ONLY'), \
                 patch.object(dependencies, 'get_model_credential_service', return_value=Mock()), \
                 patch.object(dependencies, 'get_context_settings', return_value=ContextSettings()), \
                 patch('app.services.model_gateway.ChatDeepSeek') as sdk, \
                 patch.object(context_repository, 'get_connection') as connect:
                service = dependencies.get_user_chat_service()
            self.assertIsInstance(service.context_repository, ContextRepository)
            self.assertEqual(service.context_settings, ContextSettings())
            sdk.assert_not_called()
            connect.assert_not_called()
        finally:
            dependencies.get_user_chat_service.cache_clear()


if __name__ == '__main__':
    unittest.main()
