"""工具参数、隔离、循环、预算、脱敏、实际 SDK 线协议与迁移边界。"""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,MagicMock,patch

import httpx
from langchain_deepseek import ChatDeepSeek as RealModel

from app import database
from app.errors import ChatToolBudgetError,ChatToolProtocolError,RunNotFoundError,ReportNotFoundError
from app.models.chat_tools import (ARGUMENTS,INPUT_RESERVE,bounded_result,compact_json,redact_log,request_size,tool_definitions)
from app.models.context_snapshot import ContextAttempt
from app.models.context_state import ContextConflictError
from app.models.context_settings import ContextSettings
from app.services.chat_context_factory import ChatContextFactory
from app.services.model_gateway import ModelGateway
from app.services.read_only_chat_service import ReadOnlyChatService

RUN='b'*32;SESSION='a'*32;ATTEMPT=ContextAttempt('c'*32,'d'*32)


def call(name='get_run_report',args=None,identifier='call_1'):
    return {'id':identifier,'name':name,'args':args or {}}


class ToolContractTests(unittest.TestCase):
    def test_tool_registry_contains_only_three_read_queries_without_identity(self):
        self.assertEqual(set(ARGUMENTS),{'get_latest_training_run','get_run_report','get_run_logs'})
        for tool in tool_definitions():
            self.assertNotIn('user_id',tool['function']['parameters']['properties'])

    def test_identity_and_commands_are_rejected_for_every_tool(self):
        for model in ARGUMENTS.values():
            for extra in ('user_id','password','command','server_id'):
                with self.assertRaises(ValueError):model.model_validate({extra:'x'})

    def test_log_limits_and_types_rejected(self):
        model=ARGUMENTS['get_run_logs']
        for args in ({'offset':-1},{'offset':True},{'limit':True},{'limit':16385},{'run_id':'2'}):
            with self.assertRaises(ValueError):model.model_validate(args)

    def test_report_filters_and_paging_rejected(self):
        model=ARGUMENTS['get_run_report']
        for args in ({'dataset':'../other'},{'split':'all'},{'metric_name':'Loss'},{'offset':65}):
            with self.assertRaises(ValueError):model.model_validate(args)

    def test_definitions_are_new_objects(self):
        first=tool_definitions();first[0]['function']['name']='changed'
        self.assertEqual(tool_definitions()[0]['function']['name'],'get_latest_training_run')

    def test_redaction_removes_common_credentials_and_control_sequences(self):
        source='password=unsafe\nAPI_KEY: sk-secret123456789\nAuthorization: Bearer abc123\n\x1b[31mfailed\x00'
        output=redact_log(source)
        for secret in ('unsafe','sk-secret123456789','abc123','\x1b','\x00'):self.assertNotIn(secret,output)
        self.assertIn('failed',output)

    def test_private_key_block_redacted(self):
        self.assertNotIn('payload',redact_log('-----BEGIN RSA PRIVATE KEY-----\npayload\n-----END RSA PRIVATE KEY-----'))

    def test_result_trims_whole_series_and_keeps_paging_and_numbers(self):
        rows=[{'metric_name':'m'+str(i),'last_value':.25,'note':'x'*100} for i in range(8)]
        result=bounded_result({'ok':True,'code':'OK','data':{'series':rows,'offset':2,'next_offset':None}},600)
        self.assertLessEqual(len(compact_json(result).encode()),600)
        self.assertTrue(result['truncated']);self.assertEqual(result['data']['next_offset'],2+len(result['data']['series']))
        self.assertTrue(all(row['last_value']==.25 for row in result['data']['series']))
        self.assertEqual(len(rows),8)

    def test_new_migration_checks_index_and_adds_tables_only(self):
        connection=MagicMock();cursor=connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value=[];database._ensure_chat_tools(connection)
        sql='\n'.join(c.args[0] for c in cursor.execute.call_args_list)
        self.assertIn('ADD UNIQUE INDEX',sql);self.assertIn('chat_tool_calls',sql)
        self.assertNotRegex(sql,r'(?m)^(DELETE|UPDATE)\s')

    def test_migration_repeats_without_alter_and_rejects_wrong_index(self):
        connection=MagicMock();cursor=connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.side_effect=[[(1,'id',0),(2,'user_id',0)],[(1,'session_id',0),(2,'id',0)]]
        database._ensure_chat_tools(connection)
        self.assertNotIn('ALTER TABLE','\n'.join(c.args[0] for c in cursor.execute.call_args_list))
        cursor.fetchall.side_effect=None
        cursor.fetchall.return_value=[(1,'id',1)]
        with self.assertRaises(ValueError):database._ensure_chat_tools(connection)

    def test_context_reserves_tool_space_before_compaction(self):
        factory=ChatContextFactory(None,MagicMock(),ContextSettings(),input_reserve=INPUT_RESERVE)
        context,_=factory.create(1,SESSION,ATTEMPT)
        self.assertEqual(context.manager.max_input_tokens,ContextSettings().max_input_tokens-INPUT_RESERVE)

    def test_insufficient_tool_context_is_explicit(self):
        settings=ContextSettings(max_input_tokens=3000,compact_at_tokens=2000,max_summary_tokens=1000)
        with self.assertRaises(ChatToolBudgetError):ChatContextFactory(None,MagicMock(),settings,input_reserve=INPUT_RESERVE).create(1,SESSION,ATTEMPT)

    def test_audit_dependency_does_not_require_model_credentials_or_ssh(self):
        from app.api import dependencies
        dependencies.get_chat_tool_service.cache_clear()
        try:
            with patch.object(dependencies,'get_mysql_password',return_value='TEST_ONLY'), \
                 patch.object(dependencies,'get_model_credential_service') as credentials, \
                 patch.object(dependencies,'get_runner_service') as ssh:
                service=dependencies.get_chat_tool_service()
                self.assertIsInstance(service,ReadOnlyChatService)
                self.assertIs(service,dependencies.get_chat_tool_service())
                credentials.assert_not_called();ssh.assert_not_called()
        finally:dependencies.get_chat_tool_service.cache_clear()

    def test_audit_configuration_failure_is_generic(self):
        from app.api import dependencies
        from fastapi import HTTPException
        dependencies.get_chat_tool_service.cache_clear()
        with patch.object(dependencies,'get_mysql_password',side_effect=ValueError('test-only')):
            with self.assertRaises(HTTPException) as caught:dependencies.get_chat_tool_service()
        self.assertEqual(caught.exception.status_code,503);self.assertEqual(caught.exception.detail,'请稍后重试。')
        dependencies.get_chat_tool_service.cache_clear()


class ToolLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo=MagicMock();self.repo.binding.return_value=RUN
        self.runs=MagicMock();self.metrics=SimpleNamespace(report=AsyncMock(side_effect=ReportNotFoundError()))
        self.metrics_factory=MagicMock(return_value=self.metrics);self.runner_factory=MagicMock()
        self.service=ReadOnlyChatService(self.repo,self.runs,metrics_factory=self.metrics_factory,runner_factory=self.runner_factory)
        self.gateway=SimpleNamespace(invoke=AsyncMock())

    async def answer(self,responses,maximum=12000):
        self.gateway.invoke.side_effect=responses
        return await self.service.answer([('system','助手'),('human','问题')],user_id=7,session_id=SESSION,
                    attempt=ATTEMPT,gateway=self.gateway,max_input_tokens=maximum)

    async def test_ordinary_chat_has_no_tool_queries_or_audit(self):
        self.assertEqual(await self.answer([{'content':'你好'}]),'你好')
        self.runs.get_run.assert_not_called();self.metrics_factory.assert_not_called();self.runner_factory.assert_not_called();self.repo.record.assert_not_called()

    async def test_missing_report_is_evidence_and_binds_owned_run(self):
        result=await self.answer([{'content':'','tool_calls':[call()]},{'content':'报告尚未生成'}])
        self.assertEqual(result,'报告尚未生成');self.runs.get_run.assert_called_once_with(7,RUN)
        self.metrics.report.assert_awaited_once_with(7,RUN)
        self.assertEqual(self.repo.record.call_args.kwargs['outcome'],'REPORT_NOT_GENERATED')
        self.assertEqual(self.repo.record.call_args.kwargs['binding'],'set')
        self.runner_factory.assert_not_called()

    async def test_foreign_run_never_constructs_ssh_or_report_service(self):
        self.runs.get_run.side_effect=RunNotFoundError()
        await self.answer([{'content':'','tool_calls':[call('get_run_logs',{'run_id':RUN})]},{'content':'无法访问'}])
        self.runner_factory.assert_not_called();self.metrics_factory.assert_not_called()
        self.assertEqual(self.repo.record.call_args.kwargs['outcome'],'NOT_FOUND')

    async def test_unknown_tool_is_rejected_and_not_dispatched(self):
        await self.answer([{'content':'','tool_calls':[call('start_training',{'command':'secret'})]},{'content':'无法执行'}])
        self.assertEqual(self.repo.record.call_args.kwargs['name'],'unsupported')
        self.assertEqual(self.repo.record.call_args.kwargs['arguments'],{})
        self.runs.get_run.assert_not_called()

    async def test_identity_injection_is_rejected_before_read(self):
        await self.answer([{'content':'','tool_calls':[call(args={'user_id':2})]},{'content':'参数无效'}])
        self.runs.get_run.assert_not_called()
        self.assertEqual(self.repo.record.call_args.kwargs['outcome'],'INVALID_ARGUMENTS')

    async def test_failed_attempt_prevents_tool_execution(self):
        self.repo.check_attempt.side_effect=ContextConflictError()
        with self.assertRaises(ContextConflictError):await self.answer([{'content':'','tool_calls':[call()]}])
        self.runs.get_run.assert_not_called();self.repo.record.assert_not_called()

    async def test_audit_failure_does_not_deliver_unrecorded_evidence(self):
        self.repo.record.side_effect=RuntimeError('test-only')
        with self.assertRaises(RuntimeError):await self.answer([{'content':'','tool_calls':[call()]},{'content':'unused'}])
        self.assertEqual(self.gateway.invoke.await_count,1)

    async def test_duplicate_call_id_is_not_executed_again(self):
        with self.assertRaises(ChatToolProtocolError):
            await self.answer([{'content':'','tool_calls':[call()]},{'content':'','tool_calls':[call()]}])
        self.assertEqual(self.repo.record.call_count,1)

    async def test_more_than_four_calls_is_rejected_before_any_dispatch(self):
        with self.assertRaises(ChatToolProtocolError):
            await self.answer([{'content':'','tool_calls':[call(identifier='c'+str(i)) for i in range(5)]}])
        self.repo.record.assert_not_called()

    async def test_after_four_queries_final_call_disables_tools(self):
        responses=[{'content':'','tool_calls':[call('start_training',identifier='c'+str(i))]} for i in range(4)]
        await self.answer(responses+[{'content':'查询结束'}])
        self.assertEqual(self.repo.record.call_count,4)
        self.assertEqual(self.gateway.invoke.call_args.kwargs['tool_choice'],'none')

    async def test_budget_prevents_oversized_model_request(self):
        with self.assertRaises(ChatToolBudgetError):await self.answer([{'content':'unused'}],maximum=100)
        self.gateway.invoke.assert_not_awaited()

    async def test_no_binding_does_not_guess_run_id(self):
        self.repo.binding.return_value=None
        await self.answer([{'content':'','tool_calls':[call()]},{'content':'需要先查询运行'}])
        self.runs.get_run.assert_not_called()
        self.assertEqual(self.repo.record.call_args.kwargs['outcome'],'NO_LINKED_RUN')

    async def test_empty_model_answer_is_upstream_protocol_failure(self):
        with self.assertRaises(ChatToolProtocolError):await self.answer([{'content':''}])

    async def test_malformed_call_names_and_nonfinite_arguments_never_execute(self):
        for bad in (call('bad/name'),call(args={'offset':float('nan')})):
            with self.assertRaises(ChatToolProtocolError):await self.answer([{'content':'','tool_calls':[bad]}])
        self.repo.record.assert_not_called()

    async def test_tool_timeout_is_audited_without_binding_change(self):
        with patch.object(self.service,'_execute',new=AsyncMock(side_effect=TimeoutError())):
            await self.answer([{'content':'','tool_calls':[call()]},{'content':'查询超时。'}])
        self.assertEqual(self.repo.record.call_args.kwargs['outcome'],'TOOL_TIMEOUT')
        self.assertEqual(self.repo.record.call_args.kwargs['binding'],'keep')

    async def test_two_calls_in_one_model_response_are_processed_sequentially(self):
        await self.answer([{'content':'','tool_calls':[call(identifier='one'),call(identifier='two')]},{'content':'完成。'}])
        self.assertEqual(self.repo.record.call_count,2)
        final_messages=self.gateway.invoke.call_args.args[0]
        self.assertEqual([x['tool_call_id'] for x in final_messages if x['role']=='tool'],['one','two'])

    async def test_legacy_report_does_not_return_merged_dataset_numbers(self):
        self.metrics.report.side_effect=None
        self.metrics.report.return_value={'version':1,'data':{'series':[{'last_value':999}]}}
        await self.answer([{'content':'','tool_calls':[call()]},{'content':'请先重建旧版报告。'}])
        evidence=json.loads(self.gateway.invoke.call_args.args[0][-1]['content'])
        self.assertEqual(evidence['code'],'REPORT_REBUILD_REQUIRED')
        self.assertNotIn('999',compact_json(evidence))


class ToolWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_sends_assistant_calls_and_tool_results_with_thinking_disabled(self):
        payloads=[]
        def handler(request):
            body=json.loads(request.content);payloads.append(body)
            message=({'role':'assistant','content':'','tool_calls':[{'id':'call_wire','type':'function','function':{'name':'get_latest_training_run','arguments':'{}'}}]}
                     if len(payloads)==1 else {'role':'assistant','content':'没有训练记录'})
            return httpx.Response(200,json={'id':'offline','object':'chat.completion','created':0,'model':'deepseek-flash',
                'choices':[{'index':0,'message':message,'finish_reason':'tool_calls' if len(payloads)==1 else 'stop'}],
                'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
        transport=httpx.MockTransport(handler);sync=httpx.Client(transport=transport,trust_env=False);asynchronous=httpx.AsyncClient(transport=transport,trust_env=False)
        with patch('app.services.model_gateway.ChatDeepSeek',side_effect=lambda **kwargs:RealModel(**{**kwargs,'http_client':sync,'http_async_client':asynchronous})):
            gateway=ModelGateway('sk-TEST_ONLY_TOOL_WIRE')
        try:
            definitions=tool_definitions();messages=[{'role':'system','content':'助手'},{'role':'user','content':'最近训练？'}]
            first=await gateway.invoke(messages,tools=definitions)
            self.assertEqual(first['tool_calls'][0]['name'],'get_latest_training_run')
            messages += [{'role':'assistant','content':'','tool_calls':[{'id':'call_wire','type':'function','function':{'name':'get_latest_training_run','arguments':'{}'}}]},
                         {'role':'tool','tool_call_id':'call_wire','content':'{"code":"NO_RUN"}'}]
            final=await gateway.invoke(messages,tools=definitions,tool_choice='none')
            self.assertEqual(final['content'],'没有训练记录')
            self.assertEqual(payloads[1]['messages'][-1]['tool_call_id'],'call_wire')
            self.assertEqual(payloads[1]['thinking'],{'type':'disabled'})
            self.assertEqual(payloads[0]['parallel_tool_calls'],False)
        finally:
            await gateway.aclose();await asynchronous.aclose();sync.close()
