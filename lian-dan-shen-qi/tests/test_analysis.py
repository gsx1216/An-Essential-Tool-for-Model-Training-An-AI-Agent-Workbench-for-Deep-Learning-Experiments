"""分析契约、模型边界、租约取消和默认开关；外部服务全部替身。"""

import asyncio
from dataclasses import replace
import hashlib
import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, MagicMock, patch

from mysql.connector import Error as MySQLError
from pydantic import ValidationError

from app import config
from app import database
from app.logging_config import SafeJsonFormatter, SafeTextFormatter
from app.errors import ModelCredentialNotConfiguredError
from app.models.analysis import (AnalysisSettings, AnalysisProtocolError, AnalysisStorageError, AnalysisLeaseLostError,
    AnalysisPreference, compact, strict_json, evidence_messages, render_analysis, verify_saved_content)
from app.models.event_bus import EventPayloadError
from app.models.analysis_tools import AnalysisArguments
from app.models.chat_tools import tool_definitions
from app.repositories.event_repository import decode_event
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories import analysis_repository
from app.services.analysis_service import AnalysisService
from app.workers.analysis_worker import run as run_worker

RUN, EVENT, TASK, TOKEN = 'a'*32, 'b'*32, 'c'*32, 'd'*32


def source():
    report = {'schema_version': 2, 'run_id': RUN, 'status': 'succeeded', 'exit_code': 0,
        'collection_complete': True, 'parameters': {'epochs': 1}, 'warnings': ['SINGLE_EPOCH'],
        'series': [{'dataset': 'cod10k', 'split': 'validation', 'metric_name': 'mae', 'points': 1, 'last_value': 0.03}]}
    text = compact(report)
    return {'id': EVENT, 'event_id': EVENT, 'user_id': 7, 'run_id': RUN, 'report_version': 1,
            'event_type': 'training.report_ready', 'report_json': text,
            'report_digest': hashlib.sha256(text.encode()).hexdigest(), 'report': report}


def response():
    return {'inferences': [{'text': '只有一次观测，暂不能判断趋势。', 'evidence_paths': ['/series/0/points']}],
            'recommendations': [{'text': '建议补充同协议下的多轮验证观测。', 'evidence_paths': ['/parameters/epochs'],
                                 'verification': '固定数据划分和其余参数，再对比验证指标曲线。'}],
            'limitations': ['尚未验证论文依据，也没有完整训练曲线。']}


class ContractTests(unittest.TestCase):
    def test_prompt_provides_exact_output_limits_and_extra_field_policy(self):
        messages,_=evidence_messages(source(),AnalysisSettings())
        prompt=messages[0][1]
        self.assertIn('"maxItems":6',prompt)
        self.assertIn('"maxItems":4',prompt)
        self.assertIn('"additionalProperties":false',prompt)

    def test_structure_failure_log_identifies_field_without_model_text(self):
        _,evidence=evidence_messages(source(),AnalysisSettings())
        value=response()
        value['inferences']*=7
        value['PRIVATE_MODEL_FIELD']='PRIVATE_MODEL_BODY'
        with self.assertLogs('app.models.analysis',level='WARNING') as captured,self.assertRaises(AnalysisProtocolError):
            render_analysis(compact(value),evidence)
        text='\n'.join(SafeTextFormatter().format(record) for record in captured.records)
        self.assertIn('inferences:too_long',text)
        self.assertNotIn('PRIVATE_MODEL',text)

    def test_default_disabled_and_invalid_configuration(self):
        self.assertFalse(AnalysisSettings().enabled)
        for options in ({'enabled': 1}, {'lease_seconds': True}, {'heartbeat_seconds': 40},
                        {'max_attempts': 4}, {'model_name': '../command'}, {'max_input_tokens': 4000},
                        {'step_timeout_seconds': 30}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                AnalysisSettings(**options)

    def test_config_defaults_explicit_switch_and_invalid_values(self):
        with patch.object(config, 'load_dotenv'), patch.dict('os.environ', {}, clear=True):
            self.assertEqual(config.get_analysis_settings(), AnalysisSettings())
            with patch.dict('os.environ', {'ANALYSIS_WORKER_ENABLED': 'true', 'ANALYSIS_MAX_ATTEMPTS': '1'}):
                settings = config.get_analysis_settings()
                self.assertTrue(settings.enabled)
                self.assertEqual(settings.max_attempts, 1)
            with patch.dict('os.environ', {'ANALYSIS_WORKER_ENABLED': 'yes'}), self.assertRaises(ValueError):
                config.get_analysis_settings()

    def test_facts_come_from_report_and_model_cannot_supply_facts(self):
        row = source()
        messages, evidence = evidence_messages(row, AnalysisSettings())
        self.assertEqual(next(f['value'] for f in evidence['facts'] if f['path']=='/series/0/last_value'), 0.03)
        result = render_analysis(compact(response()), evidence)
        self.assertEqual(verify_saved_content(result, row), result)
        with self.assertRaises(AnalysisProtocolError):
            render_analysis(compact({**response(), 'facts': ['invented fact']}), evidence)
        self.assertNotIn('tools', compact(messages))

    def test_unknown_duplicate_and_unprovided_citations_are_rejected(self):
        _, evidence = evidence_messages(source(), AnalysisSettings())
        for paths in (['/series/1/points'], ['/series/0/points', '/series/0/points'], []):
            value = response()
            value['inferences'][0]['evidence_paths'] = paths
            with self.assertRaises(AnalysisProtocolError):
                render_analysis(compact(value), evidence)

    def test_invalid_json_links_and_unbounded_output_are_rejected(self):
        _, evidence = evidence_messages(source(), AnalysisSettings())
        for value in ('not json', '{"inferences":[],"inferences":[]}', 'x'*24001,
                      compact({**response(), 'limitations': ['https://invented.example/paper']})):
            with self.subTest(size=len(value)), self.assertRaises(AnalysisProtocolError):
                render_analysis(value, evidence)

    def test_modified_saved_fact_or_source_is_rejected(self):
        row = source()
        _, evidence = evidence_messages(row, AnalysisSettings())
        result = render_analysis(compact(response()), evidence)
        altered = json.loads(compact(result))
        altered['facts'][0]['value'] = 'failed'
        with self.assertRaises(AnalysisStorageError):
            verify_saved_content(altered, row)
        altered = json.loads(compact(result))
        altered['source']['report_version'] = 2
        with self.assertRaises(AnalysisStorageError):
            verify_saved_content(altered, row)

    def test_budget_omits_whole_series_and_marks_incomplete_evidence(self):
        row = source()
        row['report']['series'] *= 100
        messages, evidence = evidence_messages(row, replace(AnalysisSettings(), max_input_tokens=4096))
        from app.services.token_budget import Utf8BudgetCounter
        self.assertLessEqual(Utf8BudgetCounter().count_messages(messages), 4096)
        self.assertGreater(evidence['omitted_fact_count'], 0)
        paths = {fact['path'] for fact in evidence['facts']}
        for path in paths:
            if path.startswith('/series/'):
                group = '/'.join(path.split('/')[:3])
                self.assertTrue({group+'/dataset', group+'/split', group+'/metric_name'} <= paths)

    def test_credentials_in_report_parameters_are_not_sent(self):
        row = source()
        row['report']['parameters']['api_key'] = 'sk-PRIVATE_NOT_FOR_MODEL'
        messages, evidence = evidence_messages(row, AnalysisSettings())
        self.assertNotIn('PRIVATE_NOT_FOR_MODEL', compact(messages))
        self.assertGreater(evidence['omitted_fact_count'], 0)

    def test_malformed_report_shape_does_not_reach_provider(self):
        row = source()
        row['report']['series'] = 'not a list'
        with self.assertRaises(EventPayloadError):
            evidence_messages(row, AnalysisSettings())
        row = source()
        row['report_json'] = '[]'
        row['report_digest'] = hashlib.sha256(b'[]').hexdigest()
        with self.assertRaises(EventPayloadError):
            decode_event(row)

    def test_preference_and_chat_tool_reject_identity_or_commands(self):
        for value in ('true', 1, None):
            with self.assertRaises(ValidationError):
                AnalysisPreference(enabled=value)
        for values in ({'user_id': 7}, {'command': 'train'}, {'section': 'all'}, {'offset': True}):
            with self.assertRaises(ValidationError):
                AnalysisArguments.model_validate(values)
        names = {tool['function']['name'] for tool in tool_definitions(include_analysis=True)}
        self.assertIn('get_run_analysis', names)
        self.assertNotIn('start_analysis', names)


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lease = {'task_id': TASK, 'run_id': RUN, 'user_id': 7, 'lease_token': TOKEN, 'report_version': 1, 'attempts': 1}
        self.repo = SimpleNamespace(claim=Mock(return_value=self.lease), source=Mock(return_value=source()),
            renew=Mock(return_value=True), complete=Mock(), fail=Mock(return_value=True))
        self.gateway = SimpleNamespace(invoke=AsyncMock(return_value={'content': compact(response())}), aclose=AsyncMock())
        self.factory = SimpleNamespace(create_for_user=AsyncMock(return_value=self.gateway))
        self.service = AnalysisService(self.repo, gateway_factory=self.factory, settings=AnalysisSettings(enabled=True))

    async def test_disabled_worker_never_claims_or_reads_personal_key(self):
        self.service.settings = AnalysisSettings()
        self.assertFalse(await self.service.tick())
        self.repo.claim.assert_not_called()
        self.factory.create_for_user.assert_not_awaited()
        with patch('app.workers.analysis_worker.build') as build:
            result = await run_worker(once=True, service=self.service)
        self.assertFalse(result['enabled'])
        build.assert_not_called()

    async def test_success_is_one_call_with_no_tools_then_atomic_complete(self):
        self.assertTrue(await self.service.tick())
        self.factory.create_for_user.assert_awaited_once_with(7)
        self.gateway.invoke.assert_awaited_once()
        self.assertEqual(self.gateway.invoke.await_args.kwargs, {'max_tokens': 2048, 'json_mode': True})
        self.repo.complete.assert_called_once()
        self.repo.fail.assert_not_called()
        self.gateway.aclose.assert_awaited_once()

    async def test_no_task_does_not_create_gateway(self):
        self.repo.claim.return_value = None
        self.assertFalse(await self.service.tick())
        self.factory.create_for_user.assert_not_awaited()

    async def test_missing_key_marks_attention_without_calling_model(self):
        self.factory.create_for_user.side_effect = ModelCredentialNotConfiguredError('PRIVATE')
        await self.service.tick()
        self.repo.fail.assert_called_once_with(7, TASK, TOKEN, status='attention', error_code='KEY_UNAVAILABLE')
        self.gateway.invoke.assert_not_awaited()

    async def test_invalid_output_does_not_commit_and_closes_gateway(self):
        self.gateway.invoke.return_value = {'content': 'PRIVATE_INVALID_BODY'}
        await self.service.tick()
        self.repo.complete.assert_not_called()
        self.repo.fail.assert_called_once_with(7, TASK, TOKEN, status='attention', error_code='INVALID_MODEL_OUTPUT')
        self.gateway.aclose.assert_awaited_once()

    async def test_schema_error_is_corrected_once_then_saved(self):
        invalid=response()
        invalid['inferences']*=7
        self.gateway.invoke.side_effect=[{'content':compact(invalid)},{'content':compact(response())}]
        await self.service.tick()
        self.assertEqual(self.gateway.invoke.await_count,2)
        self.assertIn('inferences:too_long',self.gateway.invoke.await_args.args[0][-1][1])
        self.repo.complete.assert_called_once()
        self.repo.fail.assert_not_called()

    async def test_repeated_schema_error_stops_after_one_correction(self):
        invalid=response()
        invalid['inferences']*=7
        self.gateway.invoke.return_value={'content':compact(invalid)}
        await self.service.tick()
        self.assertEqual(self.gateway.invoke.await_count,2)
        self.repo.complete.assert_not_called()
        self.repo.fail.assert_called_once_with(7,TASK,TOKEN,status='attention',error_code='INVALID_MODEL_OUTPUT')

    async def test_lease_lost_before_invoke_stops_model(self):
        self.repo.renew.return_value = False
        await self.service.tick()
        self.gateway.invoke.assert_not_awaited()
        self.repo.complete.assert_not_called()
        self.gateway.aclose.assert_awaited_once()

    async def test_heartbeat_loss_cancels_inflight_call_and_rejects_old_result(self):
        self.service.settings = replace(self.service.settings, heartbeat_seconds=1)
        self.repo.renew.side_effect = [True, False]
        cancelled = asyncio.Event()
        async def hanging(*args, **kwargs):
            try:
                await asyncio.sleep(20)
            finally:
                cancelled.set()
        self.gateway.invoke.side_effect = hanging
        await self.service.tick()
        self.assertTrue(cancelled.is_set())
        self.repo.complete.assert_not_called()
        self.gateway.aclose.assert_awaited_once()

    async def test_model_timeout_requires_explicit_retry_and_keeps_training_separate(self):
        self.service.settings = replace(self.service.settings, model_timeout_seconds=1)
        self.gateway.invoke.side_effect = lambda *args, **kwargs: None
        async def hanging(*args, **kwargs):
            await asyncio.sleep(20)
        self.gateway.invoke.side_effect = hanging
        await self.service.tick()
        self.repo.fail.assert_called_once_with(7, TASK, TOKEN, status='failed', error_code='MODEL_TIMEOUT')
        self.repo.complete.assert_not_called()

    async def test_cancellation_marks_attention_and_does_not_requeue_model_automatically(self):
        started = asyncio.Event()
        async def hanging(*args, **kwargs):
            started.set()
            await asyncio.sleep(20)
        self.gateway.invoke.side_effect = hanging
        task = asyncio.create_task(self.service.tick())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.repo.fail.assert_called_once_with(7, TASK, TOKEN, status='attention', error_code='WORKER_CANCELLED')
        self.repo.complete.assert_not_called()

    async def test_result_commit_failure_does_not_invoke_model_twice(self):
        self.repo.complete.side_effect = MySQLError('PRIVATE_STORAGE')
        await self.service.tick()
        self.gateway.invoke.assert_awaited_once()
        self.repo.fail.assert_called_once_with(7, TASK, TOKEN, status='attention', error_code='STORAGE_ERROR')


class StorageAndLoggingTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value

    def test_partial_migration_refuses_overwriting_existing_columns(self):
        self.cursor.fetchall.return_value = [('attempts', 'bigint unsigned', 'NO')]
        with patch.object(database, '_ensure_event_bus'), self.assertRaises(ValueError):
            database._ensure_analysis(self.connection)
        statements = [call.args[0] for call in self.cursor.execute.call_args_list]
        self.assertFalse(any('ALTER TABLE' in text for text in statements))

    def test_full_install_calls_analysis_migration(self):
        from contextlib import ExitStack
        self.cursor.nextset.return_value = False
        names = ('_ensure_user_password_column', '_ensure_session_user_fk', '_ensure_chat_turns', '_ensure_login_sessions',
                 '_ensure_model_credentials', '_ensure_context_summaries', '_ensure_experiments', '_ensure_experiment_runs',
                 '_ensure_server_connections', '_ensure_run_executions', '_ensure_run_monitors', '_ensure_metrics',
                 '_ensure_event_bus', '_ensure_analysis', '_ensure_code_changes', '_ensure_revision_runs', '_ensure_papers', '_ensure_skills', '_ensure_evaluation', '_ensure_chat_tools', '_ensure_research', '_ensure_training_tools')
        with ExitStack() as stack:
            stack.enter_context(patch.object(database, 'get_connection', return_value=self.connection))
            mocks = {name: stack.enter_context(patch.object(database, name)) for name in names}
            database.initialize_database('TEST_ONLY')
            mocks['_ensure_analysis'].assert_called_once_with(self.connection)

    def test_old_lease_cannot_insert_result(self):
        repo = AnalysisRepository('TEST_ONLY')
        self.cursor.fetchone.side_effect = [{'id': RUN}, {'status': 'running', 'lease_token': 'e'*32, 'lease_active': True}]
        with patch.object(analysis_repository, 'get_connection', return_value=self.connection), self.assertRaises(AnalysisLeaseLostError):
            repo.complete(7, RUN, TASK, TOKEN, {}, 'deepseek-flash')
        self.assertNotIn(repo.save_analysis_result_sql, [call.args[0] for call in self.cursor.execute.call_args_list])
        self.connection.rollback.assert_called_once()

    def test_log_has_safe_identifiers_but_never_raw_args_or_secret_metadata(self):
        record = logging.LogRecord('app.analysis_test', logging.INFO, __file__, 1, 'analysis_started',
                                   ('PRIVATE_TEXT',), None)
        record.task_id, record.run_id, record.event_id = TASK, RUN, EVENT
        record.report_version, record.attempts, record.analysis_code = 1, 2, 'MODEL_TIMEOUT'
        record.api_key, record.lease_token, record.analysis_text = 'PRIVATE_KEY', TOKEN, 'PRIVATE_ANALYSIS'
        value = strict_json(SafeJsonFormatter().format(record))
        self.assertEqual(value['task_id'], TASK)
        self.assertEqual(value['analysis_code'], 'MODEL_TIMEOUT')
        text = SafeTextFormatter().format(record)
        self.assertIn('task_id='+TASK, text)
        for private in ('PRIVATE_', TOKEN, 'lease_token'):
            self.assertNotIn(private, compact(value)+text)

    def test_invalid_identifier_and_unknown_error_code_are_not_logged(self):
        record = logging.LogRecord('app.analysis_test', logging.INFO, __file__, 1, 'analysis_started', (), None)
        record.task_id, record.analysis_code = 'PRIVATE_TASK', {'secret': 'PRIVATE'}
        value = strict_json(SafeJsonFormatter().format(record))
        self.assertNotIn('task_id', value)
        self.assertNotIn('analysis_code', value)


class AnalysisWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_json_mode_zero_retries_and_timeout_without_network(self):
        import httpx
        from langchain_deepseek import ChatDeepSeek as RealModel
        from app.services.model_gateway import ModelGateway
        payloads = []
        def handler(request):
            payloads.append(strict_json(request.content.decode()))
            return httpx.Response(200, json={'id': 'offline', 'object': 'chat.completion', 'created': 0,
                'model': 'deepseek-flash', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': compact(response())},
                                                     'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
        transport = httpx.MockTransport(handler)
        sync, asynchronous = httpx.Client(transport=transport, trust_env=False), httpx.AsyncClient(transport=transport, trust_env=False)
        with patch('app.services.model_gateway.ChatDeepSeek', side_effect=lambda **kwargs: RealModel(**{**kwargs,'http_client':sync,'http_async_client':asynchronous})):
            gateway = ModelGateway('sk-TEST_ONLY_ANALYSIS_WIRE', max_retries=0, timeout_seconds=10)
        try:
            await gateway.invoke([('system', 'Return JSON only'), ('human', 'Return JSON')], json_mode=True, max_tokens=512)
            self.assertEqual(payloads[0]['response_format'], {'type': 'json_object'})
            self.assertNotIn('tools', payloads[0])
            self.assertEqual(gateway.model.max_retries, 0)
            with self.assertRaises(ValueError):
                await gateway.invoke([], tools=tool_definitions(), json_mode=True)
        finally:
            await gateway.aclose()
            await asynchronous.aclose()
            sync.close()


if __name__ == '__main__':
    unittest.main()
