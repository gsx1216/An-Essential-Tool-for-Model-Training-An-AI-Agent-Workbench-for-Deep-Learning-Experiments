"""源码工具、只读通道、引用与权限回归；不连接数据库、真实 SSH 或模型。"""

from contextlib import asynccontextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock, MagicMock, patch

from app.errors import ChatToolProtocolError, RunnerConflictError, RunNotFoundError
from app.models.chat_tools import tool_definitions, compact_json
from app.models.context_snapshot import ContextAttempt
from app.models.source_code import SOURCE_ARGUMENTS, SourceProtocolError, attach_code_sources
from app.runner.source_reader import SourceReadError, excerpt, relative_path
from app.services.read_only_chat_service import ReadOnlyChatService
from app.services.source_code_service import SourceCodeService
from app.services.source_transport import SourceTransport, validate_response

RID, SID, SERVER = 'a'*32, 'b'*32, 'c'*32
ATTEMPT = ContextAttempt('d'*32, 'e'*32)


def source(start=1):
    return {'path': 'train.py', 'file_sha256': 'f'*64, 'file_bytes': 100, 'modified_ns': 123,
            'lines': [{'number': start, 'text': 'value = 1'}], 'total_lines': 10,
            'start_line': start, 'end_line': start, 'next_line': start + 1}


def displayed(start=1):
    return {**source(start), 'run_id': RID, 'retrieved_at': '2026-10-28T00:00:00Z',
            'version_scope': 'current_file', 'training_entry_match': True}


class SourceContracts(unittest.TestCase):
    def test_inspecting_training_implementation_does_not_select_execution_mode(self):
        from app.models.training_task import training_mode
        for question in ('检查最近训练的入口代码', '读取训练源码', '查看 PRNet_train_cod.py', '分析训练模型实现'):
            self.assertFalse(training_mode(question))
        self.assertTrue(training_mode('启动 PRNet'))
        self.assertTrue(training_mode('有哪些训练任务'))

    def test_relative_paths_reject_escapes_credentials_data_and_binaries(self):
        for path in ('../train.py', '/etc/passwd', 'a/../train.py', 'x\\train.py', '.env', 'config/api_key.json',
                     'checkpoints/model.py', 'model.pth', 'data/labels.txt', 'x%2fy.py', 'bad\n.py'):
            with self.subTest(path=path), self.assertRaises(SourceReadError): relative_path(path)
        self.assertEqual(relative_path('data/loaders.py'), 'data/loaders.py')
        self.assertEqual(relative_path('.', directory=True), '.')

    def test_tools_cannot_supply_identity_root_or_commands(self):
        for model in SOURCE_ARGUMENTS.values():
            for key in ('user_id', 'server_id', 'work_dir', 'password', 'command'):
                with self.subTest(key=key), self.assertRaises(ValueError): model.model_validate({key: 'private'})
        for options in ({'start_line': True}, {'line_count': 61}, {'expected_sha256': 'bad'}):
            with self.assertRaises(ValueError): SOURCE_ARGUMENTS['read_project_file'].model_validate(options)

    def test_source_tools_are_opt_in_and_definitions_have_no_credentials(self):
        self.assertFalse(set(SOURCE_ARGUMENTS) & {x['function']['name'] for x in tool_definitions()})
        names = {x['function']['name'] for x in tool_definitions(include_source=True)}
        self.assertTrue(set(SOURCE_ARGUMENTS) <= names)
        self.assertNotIn('password', compact_json(tool_definitions(include_source=True)))

    def test_excerpt_preserves_complete_lines_and_crlf_line_numbers(self):
        value = excerpt(b'first\r\n    second\r\nthird\r\n', 2, 1)
        self.assertEqual(value['lines'], [{'number': 2, 'text': '    second'}])
        self.assertEqual(value['next_line'], 3)
        self.assertEqual(excerpt(b'', 1, 10)['lines'], [])
        self.assertEqual(excerpt(b'one\n', 2, 10)['total_lines'], 1)

    def test_secret_outside_requested_window_is_rejected(self):
        for data in (b'normal = 1\npassword = "secret"\n', b'api_key =\n "secret"\n',
                     b'safe\n-----BEGIN OPENSSH PRIVATE KEY-----\nprivate\n'):
            with self.assertRaises(SourceReadError) as caught: excerpt(data, 1, 1)
            self.assertEqual(caught.exception.code, 'SENSITIVE_CONTENT')
        for data in (b'\xff', b'hello\x00world'):
            with self.assertRaises(SourceReadError): excerpt(data, 1, 1)

    def test_protocol_checks_path_line_range_cursor_and_expected_version(self):
        payload = {'action': 'read', 'path': 'train.py', 'start_line': 1, 'line_count': 2}
        self.assertEqual(validate_response({'ok': True, 'data': source()}, payload), source())
        for fields in ({'path': 'other.py'}, {'end_line': 3}, {'next_line': 5}, {'start_line': 2},
                       {'lines': [{'number': 2, 'text': 'invented'}]}, {'private_key': 'private'}):
            with self.subTest(fields=fields), self.assertRaises(SourceProtocolError):
                validate_response({'ok': True, 'data': {**source(), **fields}}, payload)
        with self.assertRaises(SourceProtocolError):
            validate_response({'ok': True, 'data': source()}, {**payload, 'expected_sha256': '0'*64})
        raw = b'\n' * 200061
        high_lines = {**source(), **excerpt(raw, 200000, 60), 'file_bytes': len(raw)}
        validated = validate_response({'ok': True, 'data': high_lines},
                                      {**payload, 'start_line': 200000, 'line_count': 60})
        self.assertGreater(validated['end_line'], 200000)

    def test_protocol_rejects_invalid_errors_and_remote_credential_text(self):
        payload = {'action': 'read', 'path': 'train.py', 'start_line': 1, 'line_count': 2}
        for response in ({'ok': False, 'code': {}}, {'ok': False, 'code': 'PRIVATE_RAW_ERROR'}, {'ok': 1}):
            with self.assertRaises(SourceProtocolError): validate_response(response, payload)
        data = source(); data['lines'][0]['text'] = 'password = "secret"'
        with self.assertRaises(SourceReadError): validate_response({'ok': True, 'data': data}, payload)

    def test_directory_protocol_cannot_return_another_directory_or_false_cursor(self):
        payload = {'action': 'list', 'path': '.', 'offset': 0, 'limit': 25}
        data = {'path': '.', 'items': [{'path': 'model.py', 'kind': 'file'}], 'offset': 0, 'next_offset': None}
        self.assertEqual(validate_response({'ok': True, 'data': data}, payload), data)
        for fields in ({'next_offset': 3}, {'items': [{'path': 'other/model.py', 'kind': 'file'}]},
                       {'items': [{'path': '.env', 'kind': 'file'}]}):
            with self.assertRaises(SourceProtocolError): validate_response({'ok': True, 'data': {**data, **fields}}, payload)

    def test_program_sources_and_invalid_citations_do_not_fabricate_files(self):
        evidence = {key: displayed()[key] for key in ('run_id', 'path', 'file_sha256', 'retrieved_at', 'start_line', 'end_line', 'lines', 'version_scope', 'training_entry_match')}
        evidence['citation_id'] = 'code_1'
        text = attach_code_sources('value 在片段中初始化 [code_1]。', [evidence])
        self.assertIn('train.py', text); self.assertIn('第 1～1 行', text)
        with self.assertRaises(ChatToolProtocolError): attach_code_sources('编造引用 [code_2]', [evidence])


class SourceServiceChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        guard = patch('app.logging_config.configure_logging'); guard.start(); self.addCleanup(guard.stop)
        self.executions = MagicMock()
        self.executions.get.return_value = {'server_id': SERVER, 'launch': {'work_dir': '/owned/project',
            'server_binding': 'binding', 'spec': {'path': 'train.py', 'sha256': 'f'*64}}}
        self.credentials = {'password': 'not-for-output'}
        self.profile = SimpleNamespace(binding=lambda: 'binding')
        self.servers = SimpleNamespace(get=AsyncMock(return_value=self.profile),
                                       cipher=SimpleNamespace(decrypt=MagicMock(return_value=self.credentials)))
        self.transport = SimpleNamespace(request=AsyncMock(return_value=source()))
        self.service = SourceCodeService(self.executions, self.servers, self.transport)

    async def test_owned_record_supplies_root_and_default_entry_without_any_runner_action(self):
        result = await self.service.query(7, RID, 'read')
        self.assertTrue(result['training_entry_match'])
        payload = self.transport.request.call_args.args[2]
        self.assertEqual(payload['work_dir'], '/owned/project')
        self.assertEqual(payload['path'], 'train.py')
        self.assertEqual(payload['action'], 'read')
        self.assertEqual(self.credentials, {})

    async def test_selected_main_network_is_read_with_selected_hash(self):
        repo=Mock();repo.get.return_value={'path':'model.py','file_sha256':'f'*64}
        self.service.network_repository=repo
        self.transport.request.return_value={**source(),'path':'model.py'}
        data=await self.service.query(7,RID,'read')
        payload=self.transport.request.call_args.args[2]
        self.assertEqual(payload['path'],'model.py')
        self.assertEqual(payload['expected_sha256'],'f'*64)
        self.assertIsNone(data['training_entry_match'])

    async def test_network_selection_validates_real_owned_file_before_save(self):
        repo=Mock();repo.save.return_value={'path':'model.py','file_sha256':'f'*64}
        self.service.network_repository=repo
        self.transport.request.return_value={**source(),'path':'model.py'}
        await self.service.select_network(7,RID,'model.py')
        repo.save.assert_called_once_with(7,RID,'model.py','f'*64)
        for path in ('../model.py','weights.pth'):
            with self.assertRaises(ValueError):await self.service.select_network(7,RID,path)

    async def test_foreign_run_stops_before_profile_decryption_or_connection(self):
        self.executions.get.side_effect = RunNotFoundError()
        with self.assertRaises(RunNotFoundError): await self.service.query(8, RID, 'read')
        self.servers.get.assert_not_awaited(); self.transport.request.assert_not_awaited()

    async def test_changed_server_binding_stops_before_decryption(self):
        self.servers.get.return_value = SimpleNamespace(binding=lambda: 'changed')
        with self.assertRaises(RunnerConflictError): await self.service.query(7, RID, 'read')
        self.servers.cipher.decrypt.assert_not_called(); self.transport.request.assert_not_awaited()

    async def test_credentials_are_released_when_cancelled(self):
        import asyncio
        self.transport.request.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await self.service.query(7, RID, 'read')
        self.assertEqual(self.credentials, {})

    async def test_other_files_are_not_labelled_as_matching_training_version(self):
        self.transport.request.return_value = {**source(), 'path': 'model.py'}
        result = await self.service.query(7, RID, 'read', path='model.py')
        self.assertIsNone(result['training_entry_match'])


class SourceTransportChecks(unittest.IsolatedAsyncioTestCase):
    async def test_stdin_only_payload_and_process_cleanup(self):
        process = MagicMock(); process.stdin.drain = AsyncMock(); process.wait_closed = AsyncMock()
        process.stdout.read = AsyncMock(return_value=json.dumps({'ok': True, 'data': source()}).encode())
        process.stderr.read = AsyncMock(return_value=b''); process.exit_status = 0
        connection = SimpleNamespace(create_process=AsyncMock(return_value=process))
        @asynccontextmanager
        async def connect(*args): yield connection
        transport = SourceTransport(SimpleNamespace(connection=connect), 'python3')
        payload = {'action': 'read', 'path': 'train.py', 'start_line': 1, 'line_count': 1, 'work_dir': '/owned/project'}
        await transport.request(None, {}, payload)
        self.assertNotIn('train.py', connection.create_process.call_args.args[0])
        self.assertEqual(json.loads(process.stdin.write.call_args.args[0])['path'], 'train.py')
        process.close.assert_called_once()
        process.stderr.read.return_value = b'private raw traceback'
        with self.assertRaises(SourceProtocolError): await transport.request(None, {}, payload)
        self.assertEqual(process.close.call_count, 2)


class SourceChatChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        guard = patch('app.logging_config.configure_logging'); guard.start(); self.addCleanup(guard.stop)
        self.repo = MagicMock(); self.repo.binding.return_value = RID
        self.runs = MagicMock()
        self.source = SimpleNamespace(query=AsyncMock(return_value=displayed()))
        self.factory = MagicMock(return_value=self.source)
        self.service = ReadOnlyChatService(self.repo, self.runs, metrics_factory=MagicMock(),
            runner_factory=MagicMock(), source_factory=self.factory)

    async def answer(self, replies):
        gateway = SimpleNamespace(invoke=AsyncMock(side_effect=replies))
        result = await self.service.answer([('system', 'assistant'), ('human', 'inspect training code')],
            user_id=7, session_id=SID, attempt=ATTEMPT, gateway=gateway, max_input_tokens=49152)
        return result, gateway

    def call(self, index=1, **args):
        return {'content': '', 'tool_calls': [{'id': 'read_'+str(index), 'name': 'read_project_file', 'args': args}]}

    async def test_ordinary_chat_never_constructs_source_service(self):
        result, _ = await self.answer([{'content': 'hello'}])
        self.assertEqual(result, 'hello'); self.factory.assert_not_called()

    async def test_code_evidence_is_audited_before_delivery_and_paths_are_program_generated(self):
        result, gateway = await self.answer([self.call(path='train.py'), {'content': '初始化 value [code_1]。'}])
        evidence = self.repo.record.call_args.kwargs['metadata']['code_evidence']
        self.assertEqual(evidence['lines'], displayed()['lines'])
        self.assertEqual(evidence['file_sha256'], 'f'*64)
        self.assertLessEqual(len(compact_json(self.repo.record.call_args.kwargs['metadata']).encode()), 4096)
        self.assertIn('train.py', result)
        tool = next(m for m in gateway.invoke.call_args.args[0] if m['role'] == 'tool')
        self.assertEqual(json.loads(tool['content'])['data']['citation_id'], 'code_1')

    async def test_next_read_automatically_reuses_delivered_file_version(self):
        self.source.query.side_effect = [displayed(1), displayed(2)]
        await self.answer([self.call(path='train.py'), self.call(2, path='train.py', start_line=2), {'content': '两个片段 [code_1] [code_2]'}])
        self.assertEqual(self.source.query.call_args.kwargs['expected_sha256'], 'f'*64)
        self.assertEqual(self.repo.record.call_count, 2)
        self.repo.reset_mock()
        self.source.query.side_effect = [displayed(1), {**displayed(2), 'file_sha256': '1'*64}]
        result, _ = await self.answer([self.call(path='train.py'), self.call(2, start_line=2), {'content': '文件已变化。'}])
        self.assertEqual(self.repo.record.call_args.kwargs['outcome'], 'SOURCE_SOURCE_CHANGED')
        self.assertNotIn('code_evidence', self.repo.record.call_args.kwargs['metadata'])
        self.assertNotIn('版本 `'+ '1'*64, result)

    async def test_foreign_run_cannot_construct_source_reader(self):
        self.runs.get_run.side_effect = RunNotFoundError()
        result, _ = await self.answer([self.call(), {'content': '不可访问'}])
        self.factory.assert_not_called()
        self.assertNotIn('代码读取依据', result)

    async def test_failed_read_does_not_invent_or_persist_source_evidence(self):
        self.source.query.side_effect = SourceReadError('SENSITIVE_CONTENT')
        result, _ = await self.answer([self.call(), {'content': '模型错误地声称已经读完 train.py，代码没有问题。'}])
        self.assertNotIn('code_evidence', self.repo.record.call_args.kwargs['metadata'])
        self.assertNotIn('代码读取依据', result)
        self.assertNotIn('已经读完', result)
        self.assertIn('没有取得', result)
        self.assertEqual(self.repo.record.call_args.kwargs['outcome'], 'SOURCE_SENSITIVE_CONTENT')

    async def test_audit_failure_prevents_unaudited_source_from_reaching_model(self):
        self.repo.record.side_effect = RuntimeError('test-only storage error')
        with self.assertRaises(RuntimeError): await self.answer([self.call(), {'content': 'must not return'}])

    async def test_source_evidence_can_be_combined_with_verified_paper_reply(self):
        from app.models.research import ResearchReply, reference_row
        from app.services.research_service import ResearchService
        paper = reference_row(url='https://arxiv.org/html/2401.12345', title='offline fixture',
                              text='This is an offline fixture excerpt for checking citations.', kind='paper_html')
        repository = MagicMock(); repository.list_saved.return_value = {'items': []}
        research = ResearchService(repository, SimpleNamespace(read=AsyncMock(return_value=paper)))
        self.service.research_factory = lambda: research
        answer = {'facts': ['代码初始化 value [code_1]。'], 'hypotheses': [],
                  'recommendations': [{'change': '进行一次受控对照', 'why': '需要验证假设', 'conditions': '固定其他配置',
                      'validation': '比较同协议指标', 'citations': [{'reference_id': paper['reference_id'], 'quote': paper['content'][:30]}]}],
                  'limitations': ['模拟资料，仅验证格式和引用流程。']}
        result, _ = await self.answer([self.call(),
            {'content': '', 'tool_calls': [{'id': 'paper', 'name': 'read_reference', 'args': {'url': paper['url']}}]},
            {'content': json.dumps(answer, ensure_ascii=False)}])
        self.assertIsInstance(result, ResearchReply)
        self.assertIn(paper['reference_id'], result.reference_ids)
        self.assertIn('train.py', result.text)
        self.assertIn('代码读取依据', result.text)


if __name__ == '__main__':
    unittest.main()
