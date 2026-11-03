"""建议链路：实际三类证据、失败/取消、版本、预算、审计与只读聊天边界。"""
import asyncio
from datetime import datetime, timezone
import hashlib
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from app import config
from app.models.analysis import (AnalysisSettings, AnalysisProtocolError, AnalysisStorageError, AnalysisConflictError,
    AnalysisLeaseLostError, compact, evidence_messages, render_analysis)
from app.models.suggestion import (SuggestionSettings, render_suggestions,
    verify_suggestions, analysis_input)
from app.models.suggestion_tools import SuggestionArguments, suggestion_projection
from app.models.chat_tools import tool_definitions
from app.models.research import reference_row
from app.runner.source_reader import excerpt, SourceReadError
from app.services.suggestion_service import SuggestionService, tools
from app.services.read_only_chat_service import ReadOnlyChatService
from app.workers.suggestion_worker import run as worker_run
from app.repositories.suggestion_repository import SuggestionRepository
from tests.test_analysis import source as analysis_source, response as analysis_response, RUN, TASK, TOKEN


def source():
    row = analysis_source()
    _, facts = evidence_messages(row, AnalysisSettings())
    row['analysis_json'] = compact(render_analysis(compact(analysis_response()), facts))
    row['analysis_digest'] = hashlib.sha256(row['analysis_json'].encode()).hexdigest()
    row['analysis_task_id'] = 'e' * 32
    return row


def code():
    raw = b'loss = criterion(prediction, target)\nloss.backward()\n'
    return {'path': 'train.py', 'file_sha256': hashlib.sha256(raw).hexdigest(), 'file_bytes': len(raw),
        'modified_ns': 1, **excerpt(raw, 1, 30), 'retrieved_at': datetime.now(timezone.utc).isoformat(),
        'version_scope': 'current_file', 'training_entry_match': True}


def paper():
    row = reference_row(url='https://arxiv.org/html/2401.12345', title='Fixture paper',
        text='A controlled ablation keeps the data split fixed and compares the validation metric.', kind='paper_html')
    return {**{key: row[key] for key in ('reference_id', 'url', 'title', 'kind', 'content_sha256')},
        'offset': 0, 'text': row['content'], 'retrieved_at': datetime.now(timezone.utc).isoformat(), 'abstract_only': False}


def answer():
    return {'suggestions': [{'problem': '报告只有一次观测。', 'hypothesis': '损失项可能需要对照验证，原因未证实。',
        'change': '在固定条件下对损失权重做消融。', 'conditions': '保持数据划分和其他参数一致。',
        'validation': '对比完整验证曲线和多次重复结果。', 'evidence_paths': ['/series/0/points'],
        'code': [{'evidence_id': 'code_1', 'start_line': 1, 'end_line': 2}],
        'papers': [{'evidence_id': 'paper_2', 'quote': 'A controlled ablation keeps the data split fixed'}]}],
        'limitations': ['这是测试资料；建议尚未应用，原因需要实验验证。']}


def fixtures():
    return [{**code(), 'evidence_id': 'code_1'}], [{**paper(), 'evidence_id': 'paper_2'}]


class Contracts(unittest.TestCase):
    def test_three_evidence_types_saved_and_verified(self):
        codes, papers = fixtures()
        value = render_suggestions(compact(answer()), source(), codes, papers)
        self.assertEqual(value['evidence_status'], 'supported')
        self.assertEqual(verify_suggestions(value, source())['suggestions'], value['suggestions'])
        self.assertEqual(len(value['facts']), len(analysis_input(source())['facts']))

    def test_code_diagnosis_requires_read_code_and_fact_but_not_paper(self):
        codes,_=fixtures()
        finding=answer()['suggestions'][0].copy();finding.pop('papers')
        body={'suggestions':[],'code_findings':[finding],'limitations':['代码假设仍需对照验证。']}
        result=render_suggestions(compact(body),source(),codes,[])
        self.assertEqual(verify_suggestions(result,source())['code_findings'],result['code_findings'])
        with self.assertRaises(AnalysisProtocolError):render_suggestions(compact(body),source(),[],[])
        body['code_findings'][0]['code'][0]['end_line']=999
        with self.assertRaises(AnalysisProtocolError):render_suggestions(compact(body),source(),codes,[])

    def test_missing_fact_code_or_paper_rejected(self):
        for change in ('fact', 'code', 'paper', 'quote', 'range'):
            codes, papers = fixtures()
            value = answer()
            if change == 'fact': value['suggestions'][0]['evidence_paths'] = ['/not-provided']
            if change == 'code': codes = []
            if change == 'paper': papers = []
            if change == 'quote': value['suggestions'][0]['papers'][0]['quote'] = 'THIS QUOTE WAS NOT READ'
            if change == 'range': value['suggestions'][0]['code'][0]['end_line'] = 100
            with self.subTest(change=change), self.assertRaises(AnalysisProtocolError):
                render_suggestions(compact(value), source(), codes, papers)

    def test_abstract_is_not_implementation_evidence(self):
        codes, papers = fixtures()
        papers[0].update(kind='search_abstract', abstract_only=True)
        with self.assertRaises(AnalysisProtocolError):
            render_suggestions(compact(answer()), source(), codes, papers)

    def test_unknown_field_duplicate_keys_urls_and_blank_text_rejected(self):
        codes, papers = fixtures()
        values = [compact(answer()).replace('"suggestions":', '"suggestions":[],"suggestions":', 1)]
        for key, text in [('unexpected', 'field'), ('problem', '  '), ('change', 'https://invented.invalid')]:
            value = answer(); value['suggestions'][0][key] = text; values.append(compact(value))
        for value in values:
            with self.subTest(value=value[:60]), self.assertRaises(AnalysisProtocolError):
                render_suggestions(value, source(), codes, papers)

    def test_saved_fact_and_analysis_digest_cannot_change(self):
        codes, papers = fixtures()
        value = render_suggestions(compact(answer()), source(), codes, papers)
        value['facts'][0]['value'] = 'changed'
        with self.assertRaises(AnalysisStorageError): verify_suggestions(value, source())
        row = source(); row['analysis_digest'] = '0'*64
        with self.assertRaises(AnalysisStorageError): analysis_input(row)

    def test_empty_is_insufficient_not_fabricated_success(self):
        value = render_suggestions(compact({'suggestions': [], 'limitations': ['未读到证据。']}), source(), [], [])
        self.assertEqual(value['evidence_status'], 'insufficient')
        self.assertEqual(value['suggestions'], [])

    def test_default_off_bounded_settings_and_config(self):
        self.assertFalse(SuggestionSettings().enabled)
        for kw in ({'max_calls': 9}, {'max_calls': True}, {'step_timeout_seconds': 700}):
            with self.assertRaises(ValueError): SuggestionSettings(**kw)
        with patch.object(config, 'load_dotenv'), patch.dict('os.environ', {}, clear=True):
            self.assertEqual(config.get_suggestion_settings(), SuggestionSettings())
            with patch.dict('os.environ', {'SUGGESTION_WORKER_ENABLED': 'yes'}), self.assertRaises(ValueError):
                config.get_suggestion_settings()

    def test_tool_catalog_only_read_operations(self):
        names = {v['function']['name'] for v in tools()}
        self.assertEqual(names, {'list_project_files', 'read_project_file', 'search_references', 'read_reference'})
        default = {v['function']['name'] for v in tool_definitions()}
        optional = {v['function']['name'] for v in tool_definitions(include_suggestions=True)}
        self.assertNotIn('get_run_suggestions', default)
        self.assertIn('get_run_suggestions', optional)

    def test_projection_preserves_stale_and_actual_source(self):
        codes, papers = fixtures()
        value = render_suggestions(compact(answer()), source(), codes, papers)
        row = {'task_id': TASK, 'report_version': 1, 'is_stale': True, 'content': value}
        overview = suggestion_projection(row, SuggestionArguments())
        self.assertTrue(overview['is_stale']); self.assertEqual(overview['suggestion_count'], 1)
        item = suggestion_projection(row, SuggestionArguments(section='suggestions'))['items'][0]
        self.assertEqual(item['code']['path'], 'train.py')
        self.assertEqual(item['paper']['quote'], answer()['suggestions'][0]['papers'][0]['quote'])

    def test_retry_insufficient_clears_only_zero_suggestion_result(self):
        repo, cursor = SuggestionRepository('TEST_ONLY'), Mock()
        value = compact(render_suggestions(compact({'suggestions': [], 'limitations': ['缺少证据。']}), source(), [], []))
        cursor.fetchone.return_value = {'content_json': value, 'content_digest': hashlib.sha256(value.encode()).hexdigest()}
        repo.prepare_retry(cursor, {'task_id': TASK, 'status': 'succeeded', 'lease_active': False})
        self.assertIn(unittest.mock.call(repo.clear_insufficient_suggestion_sql, (TASK,)), cursor.execute.call_args_list)

    def test_retry_supported_or_running_does_not_clear_result(self):
        repo, cursor = SuggestionRepository('TEST_ONLY'), Mock()
        codes, papers = fixtures()
        value = compact(render_suggestions(compact(answer()), source(), codes, papers))
        cursor.fetchone.return_value = {'content_json': value, 'content_digest': hashlib.sha256(value.encode()).hexdigest()}
        for active in (True, False):
            with self.assertRaises(AnalysisConflictError):
                repo.prepare_retry(cursor, {'task_id': TASK, 'status': 'succeeded', 'lease_active': active})
        self.assertNotIn(unittest.mock.call(repo.clear_insufficient_suggestion_sql, (TASK,)), cursor.execute.call_args_list)

    def test_retry_corrupted_result_is_storage_failure(self):
        repo, cursor = SuggestionRepository('TEST_ONLY'), Mock()
        cursor.fetchone.return_value = {'content_json': '{}', 'content_digest': '0'*64}
        with self.assertRaises(AnalysisStorageError):
            repo.prepare_retry(cursor, {'task_id': TASK, 'status': 'succeeded', 'lease_active': False})


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lease = {'user_id': 7, 'run_id': RUN, 'task_id': TASK, 'lease_token': TOKEN}
        self.repo = Mock()
        self.repo.claim.return_value = self.lease
        self.repo.source.return_value = source()
        self.repo.renew.return_value = True
        self.gateway = SimpleNamespace(invoke=AsyncMock(), aclose=AsyncMock())
        self.factory = SimpleNamespace(create_for_user=AsyncMock(return_value=self.gateway))
        self.reader = SimpleNamespace(query=AsyncMock(return_value=code()))
        self.client = SimpleNamespace(read=AsyncMock(), search=AsyncMock(return_value=[]))
        p = paper()
        self.client.read.return_value = {'reference_id': p['reference_id'], 'url': p['url'], 'title': p['title'],
            'kind': p['kind'], 'content_sha256': p['content_sha256'], 'content': p['text']}
        self.service = SuggestionService(self.repo, gateway_factory=self.factory, settings=SuggestionSettings(enabled=True),
            source_factory=lambda: self.reader, research_factory=lambda: self.client)
        self.gateway.invoke.side_effect = [
            {'tool_calls': [{'id': 'read_code', 'name': 'read_project_file', 'args': {'path': 'train.py'}}]},
            {'tool_calls': [{'id': 'read_paper', 'name': 'read_reference', 'args': {'url': p['url']}}]},
            {'content': compact(answer())}]

    async def test_success_records_before_delivery_then_saves(self):
        await self.service.tick()
        self.assertEqual(self.repo.record.call_count, 2)
        self.assertEqual(self.repo.record.call_args_list[0].args[-1]['data']['evidence_id'], 'code_1')
        self.repo.complete.assert_called_once()
        self.repo.fail.assert_not_called()
        self.assertEqual(self.repo.complete.call_args.args[-2]['evidence_status'], 'supported')
        self.gateway.aclose.assert_awaited_once()
        self.reader.query.assert_awaited_once_with(7, RUN, 'read', path='train.py', start_line=1, line_count=30)

    async def test_native_protocol_correction_does_not_replay_read_tools(self):
        from app.services.tool_protocol import ToolTextProtocolError
        replies=list(self.gateway.invoke.side_effect)
        self.gateway.invoke.side_effect=[replies[0],ToolTextProtocolError('invalid'),replies[1],replies[2]]
        await self.service.tick()
        self.assertEqual(self.gateway.invoke.await_count,4)
        self.assertEqual(self.repo.record.call_count,2)
        self.reader.query.assert_awaited_once()
        self.repo.complete.assert_called_once()
        self.repo.fail.assert_not_called()

    async def test_no_paper_at_budget_end_can_produce_source_backed_diagnosis(self):
        self.service.settings=SuggestionSettings(enabled=True,max_calls=1)
        first=list(self.gateway.invoke.side_effect)[0]
        finding=answer()['suggestions'][0].copy();finding.pop('papers')
        self.gateway.invoke.side_effect=[first,{'content':compact({'suggestions':[],'code_findings':[finding],'limitations':['未取得论文正文，仅做代码诊断。']}),'tool_calls':[]}]
        await self.service.tick()
        self.assertEqual(self.gateway.invoke.await_count,2)
        self.assertTrue(self.gateway.invoke.call_args.kwargs['json_mode'])
        saved=self.repo.complete.call_args.args[-2]
        self.assertEqual(saved['evidence_status'],'supported')
        self.assertEqual(saved['suggestions'],[])
        self.assertEqual(len(saved['code_findings']),1)
        self.repo.fail.assert_not_called()

    async def test_supported_evidence_finalizes_with_json_without_tools(self):
        self.service.settings=SuggestionSettings(enabled=True,max_calls=2)
        await self.service.tick()
        options=self.gateway.invoke.await_args.kwargs
        self.assertTrue(options['json_mode'])
        self.assertNotIn('tools',options)
        self.assertEqual(self.repo.complete.call_args.args[-2]['evidence_status'],'supported')

    async def test_repeated_protocol_error_stops_after_one_correction(self):
        from app.services.tool_protocol import ToolTextProtocolError
        self.gateway.invoke.side_effect=ToolTextProtocolError('invalid')
        await self.service.tick()
        self.assertEqual(self.gateway.invoke.await_count,2)
        self.repo.record.assert_not_called()
        self.repo.complete.assert_not_called()
        self.assertEqual(self.repo.fail.call_args.kwargs['error_code'],'INVALID_MODEL_OUTPUT')

    async def test_uploaded_pdf_method_is_not_clipped_to_old_tiny_excerpt(self):
        from uuid import uuid4
        p=paper();cid=uuid4().hex
        text=('Motivation and method design details. '*120)+p['text']
        data={'reference_id':cid,'chunk_id':cid,'paper_id':uuid4().hex,'kind':'uploaded_pdf','url':None,
            'title':'Complete method','content_sha256':'a'*64,'file_sha256':'b'*64,'page_number':2,'page_char_start':0,
            'offset':0,'text':text,'content_length':len(text),'next_offset':None,'next_chunk_index':None,
            'retrieved_at':p['retrieved_at'],'abstract_only':False}
        self.service.paper_factory=lambda:SimpleNamespace(tool=AsyncMock(return_value={'ok':True,'code':'OK','data':data}))
        self.gateway.invoke.side_effect=[
            {'tool_calls':[{'id':'code','name':'read_project_file','args':{'path':'train.py'}}]},
            {'tool_calls':[{'id':'pdf','name':'read_paper_chunk','args':{'chunk_id':cid}}]},
            {'content':compact(answer())}]
        await self.service.tick()
        self.repo.fail.assert_not_called()
        saved=self.repo.complete.call_args.args[-2]['paper_evidence'][0]
        self.assertEqual(saved['kind'],'uploaded_pdf')
        self.assertEqual(saved['text'],text)
        self.assertGreater(len(saved['text']),4000)
        self.assertEqual(self.repo.record.call_args_list[-1].args[-1]['data']['text'],text)

    async def test_default_disabled_does_not_claim_or_construct_model(self):
        self.service.settings = SuggestionSettings()
        self.assertFalse(await self.service.tick())
        self.repo.claim.assert_not_called(); self.factory.create_for_user.assert_not_awaited()

    async def test_failed_source_cannot_accept_claimed_suggestion(self):
        self.reader.query.side_effect = SourceReadError('NOT_FOUND')
        await self.service.tick()
        saved = self.repo.complete.call_args.args[-2]
        self.assertEqual(saved['evidence_status'], 'insufficient')
        self.assertFalse(saved['suggestions'])
        self.assertEqual(self.repo.record.call_args_list[0].args[-1]['code'], 'SOURCE_NOT_FOUND')

    async def test_failed_audit_does_not_send_unrecorded_code_to_model(self):
        self.repo.record.side_effect = AnalysisStorageError('test fixture')
        await self.service.tick()
        self.assertEqual(self.gateway.invoke.await_count, 1)
        self.repo.complete.assert_not_called()
        self.assertEqual(self.repo.fail.call_args.kwargs['error_code'], 'STORAGE_ERROR')

    async def test_unknown_tool_and_foreign_run_never_connect(self):
        self.gateway.invoke.side_effect = [
            {'tool_calls': [{'id': 'x', 'name': 'start_training_run', 'args': {}}]},
            {'tool_calls': [{'id': 'y', 'name': 'read_project_file', 'args': {'run_id': 'f'*32}}]},
            {'content': 'already started and read all files'}]
        await self.service.tick()
        self.reader.query.assert_not_awaited()
        self.assertEqual(self.repo.record.call_args_list[0].args[-1]['code'], 'UNSUPPORTED_TOOL')
        self.assertEqual(self.repo.record.call_args_list[1].args[-1]['code'], 'INVALID_ARGUMENTS')

    async def test_bad_citation_marks_attention_and_does_not_save_result(self):
        value = answer(); value['suggestions'][0]['papers'][0]['quote'] = 'unread fabricated quotation'
        self.gateway.invoke.side_effect = [
            {'tool_calls': [{'id': 'a', 'name': 'read_project_file', 'args': {}}]},
            {'tool_calls': [{'id': 'b', 'name': 'read_reference', 'args': {'url': paper()['url']}}]},
            {'content': compact(value)}]
        await self.service.tick()
        self.repo.complete.assert_not_called()
        self.assertEqual(self.repo.fail.call_args.kwargs['error_code'], 'INVALID_MODEL_OUTPUT')

    async def test_read_code_version_cannot_mix_default_path_alias(self):
        args = __import__('app.models.source_code', fromlist=['ReadSourceArguments']).ReadSourceArguments
        versions = {}; refs = {}
        await self.service._tool(self.lease, 'read_project_file', args(path='train.py'), refs, versions)
        changed = code(); changed['file_sha256'] = 'f'*64
        self.reader.query.return_value = changed
        value = await self.service._tool(self.lease, 'read_project_file', args(), refs, versions)
        self.assertEqual(value, {'ok': False, 'code': 'SOURCE_CHANGED'})

    async def test_disabling_preference_during_read_prevents_delivery(self):
        self.repo.record.side_effect = AnalysisLeaseLostError('disabled')
        await self.service.tick()
        self.assertEqual(self.gateway.invoke.await_count, 1)
        self.repo.complete.assert_not_called()

    async def test_cancel_closes_gateway_without_completing(self):
        started = asyncio.Event()
        async def wait(*args, **kwargs):
            started.set(); await asyncio.sleep(10)
        self.gateway.invoke.side_effect = wait
        task = asyncio.create_task(self.service.tick())
        await started.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.gateway.aclose.assert_awaited_once()
        self.repo.complete.assert_not_called()
        self.assertEqual(self.repo.fail.call_args.kwargs['error_code'], 'WORKER_CANCELLED')

    async def test_no_saved_suggestions_is_not_generated_implicitly(self):
        runs = SimpleNamespace(get_run=Mock(return_value=SimpleNamespace(status='succeeded')))
        saved = SimpleNamespace(results=AsyncMock(return_value=[]))
        service = ReadOnlyChatService(Mock(), runs, metrics_factory=Mock(), runner_factory=Mock(), suggestion_factory=lambda: saved)
        value, binding, target = await service._execute(7, 'get_run_suggestions', SuggestionArguments(run_id=RUN), None)
        self.assertEqual(value['code'], 'SUGGESTIONS_NOT_READY')
        saved.results.assert_awaited_once_with(7, RUN, limit=1)

    async def test_disabled_worker_does_not_build_key_or_source_dependencies(self):
        with patch('app.workers.suggestion_worker.config.get_suggestion_settings', return_value=SuggestionSettings()), \
             patch('app.workers.suggestion_worker.build') as build:
            self.assertEqual((await worker_run(once=True))['reason'], 'ADMIN_DISABLED')
            build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
