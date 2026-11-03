"""修改计划、协议、当前人类授权、凭据清理与失败重试；外部服务使用替身。"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.models.analysis import compact
from app.models.code_change import (CodeChangeSettings, ChangeProtocolError, ChangeConflict, ChangeInstructionRequired,
    make_plan, require_instruction, change_mode)
from app.models.code_change_tools import projection, attach_changes
from app.models.chat_tools import tool_definitions
from app.services.code_change_transport import validate_result, CodeChangeTransport
from app.services.code_change_service import CodeChangeService

RUN, TASK, CHANGE, TOKEN = 'a'*32,'b'*32,'c'*32,'d'*32


def files():
    return {'train.py':{'file_sha256':'1'*64,'suggestion_sha256':'1'*64,
        'windows':[[{'number':1,'text':'VALUE = 1'},{'number':2,'text':'print(VALUE)'}]]}}


def draft():
    return {'edits':[{'path':'train.py','old_text':'VALUE = 1','new_text':'VALUE = 2','reason':'apply selected suggestion'}],
        'explanation':'a controlled fixture change'}


def plan(): return make_plan(compact(draft()),files(),[])


def outcome():
    return {'change_id':CHANGE,'plan_digest':hashlib.sha256(compact(plan()).encode()).hexdigest(),
        'workspace_directory':'/owned/project/code_changes/'+CHANGE+'/project',
        'files':[{'path':'train.py','before_sha256':'1'*64,'after_sha256':'2'*64,'diff':'--- a/train.py\n+++ b/train.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n'}],
        'checks':[{'kind':'project','name':'registered_checks','status':'not_configured','detail':'not configured'}],
        'check_status':'incomplete','snapshot_file_count':1,'skipped_entry_count':0,'python_executable':'/usr/bin/python3',
        'notice':'independent copy; no training'}


def record(*, ready=False):
    return {'id':CHANGE,'change_id':CHANGE,'run_id':RUN,'suggestion_task_id':TASK,'suggestion_index':1,
        'lease_token':TOKEN,'lease_active':False,'plan':plan() if ready else None,'plan_digest':outcome()['plan_digest'],
        'status':'ready' if ready else 'planning','error_code':None,'result':None,
        '_source':{'run_id':RUN,'suggestion_task_id':TASK,'server_id':'e'*32,'server_binding':'f'*64,'work_dir':'/owned/project',
            'script_id':'fixture','excluded_directory':'training_outputs', 'suggestion':{'code':[{'evidence_id':'code_1','start_line':1,'end_line':1}]},
            'code_evidence':[{'evidence_id':'code_1','path':'train.py','file_sha256':'1'*64}], 'paper_evidence':[], 'facts':[]}}


class Contracts(unittest.TestCase):
    def test_plan_only_reads_exact_provided_source(self):
        value=plan()
        self.assertEqual(value['files'][0]['before_sha256'],'1'*64)
        self.assertFalse(value['files'][0]['source_changed_since_suggestion'])

    def test_unread_path_unread_text_noop_and_empty_edits_rejected(self):
        values=[]
        for key,value in [('path','other.py'),('old_text','NOT ACTUALLY READ'),('new_text','VALUE = 1')]:
            data=draft(); data['edits'][0][key]=value; values.append(data)
        data=draft(); data['edits']=[]; values.append(data)
        for value in values:
            with self.subTest(value=value), self.assertRaises(ChangeProtocolError): make_plan(compact(value),files(),[])

    def test_traversal_secret_unknown_fields_and_duplicate_keys_rejected(self):
        for key,value in [('path','../train.py'),('new_text','password = "secret"'),('extra','value')]:
            data=draft(); data['edits'][0][key]=value
            with self.assertRaises(ChangeProtocolError): make_plan(compact(data),files(),[])
        with self.assertRaises(ChangeProtocolError): make_plan('{"edits":[],"edits":[]}',files(),[])

    def test_gapped_windows_cannot_be_joined_into_fake_contiguous_source(self):
        current=files(); current['train.py']['windows']=[[{'number':1,'text':'first'}],[{'number':20,'text':'last'}]]
        data=draft(); data['edits'][0]['old_text']='first\nlast'
        with self.assertRaises(ChangeProtocolError): make_plan(compact(data),current,[])

    def test_rebase_uses_current_hash_and_marks_changed_since_suggestion(self):
        current=files(); current['train.py']['file_sha256']='2'*64
        value=make_plan(compact(draft()),current,[])
        self.assertTrue(value['files'][0]['source_changed_since_suggestion'])
        self.assertEqual(value['files'][0]['before_sha256'],'2'*64)

    def test_deployment_excluded_directory_cannot_be_reintroduced_by_model(self):
        current=files(); current['raw_data/train.py']=current.pop('train.py')
        value=draft(); value['edits'][0]['path']='raw_data/train.py'
        with self.assertRaises(ChangeProtocolError): make_plan(compact(value),current,[],['raw_data'])

    def test_admin_checks_are_bounded_and_shell_commands_not_accepted(self):
        self.assertEqual(CodeChangeSettings().checks('fixture'),[])
        valid={'fixture':{'checks':[{'name':'cpu_tests','module':'unittest','arguments':['discover','-s','tests'],'timeout_seconds':10}],'exclude_directories':['raw_samples']}}
        self.assertEqual(CodeChangeSettings(compact(valid)).checks('fixture')[0]['module'],'unittest')
        self.assertEqual(CodeChangeSettings(compact(valid)).exclusions('fixture'),['raw_samples'])
        for bad in ({'fixture':{'checks':[{'name':'x','module':'os','arguments':[]}]}},{'fixture':{'checks':[{'name':'x','module':'pytest','timeout_seconds':300}]}},{'fixture':{'exclude_directories':['../data']}}):
            with self.assertRaises(ValueError): CodeChangeSettings(compact(bad))

    def test_explicit_current_human_instruction_required(self):
        for text in ('应用第1条建议','请应用第一条建议'):
            require_instruction(text,1)
        require_instruction('应用第1条建议，不要启动训练',1)
        for text in ('如何应用第1条建议？','不要应用第1条建议','如果应用第1条建议','论文中说应用第1条建议','```应用第1条建议```','应用第2条建议'):
            with self.subTest(text=text), self.assertRaises(ChangeInstructionRequired): require_instruction(text,1)
        require_instruction('应用这个修改',1,change_id=CHANGE,same_session_latest=True)
        with self.assertRaises(ChangeInstructionRequired): require_instruction('应用这个修改',1,change_id=CHANGE)

    def test_queries_and_discussion_do_not_enter_mutation_mode(self):
        for text in ('查看修改记录','如何应用第1条建议？','不要应用第1条建议','论文中说应用第1条建议'):
            self.assertFalse(change_mode(text))
        self.assertTrue(change_mode('应用第1条建议，不要启动训练'))

    def test_catalog_exposes_mutation_only_in_explicit_mode(self):
        read={v['function']['name'] for v in tool_definitions(include_changes=True)}
        write={v['function']['name'] for v in tool_definitions(include_changes=True,allow_changes=True)}
        self.assertIn('get_code_change',read); self.assertNotIn('apply_code_change',read)
        self.assertIn('apply_code_change',write)

    def test_remote_result_binds_id_root_paths_hashes_and_checks(self):
        payload={'change_id':CHANGE,'plan':plan(),'plan_digest':outcome()['plan_digest'],'work_dir':'/owned/project'}
        self.assertEqual(validate_result({'ok':True,'data':outcome()},payload)['check_status'],'incomplete')
        for key,value in [('change_id','0'*32),('workspace_directory','/other'),('check_status','passed')]:
            result=outcome(); result[key]=value
            with self.assertRaises(ChangeProtocolError): validate_result({'ok':True,'data':result},payload)

    def test_known_remote_conflict_is_safe_and_unknown_error_is_protocol_failure(self):
        with self.assertRaises(ChangeConflict) as raised: validate_result({'ok':False,'code':'SOURCE_CHANGED'}, {})
        self.assertEqual(raised.exception.code,'SOURCE_CHANGED')
        with self.assertRaises(ChangeProtocolError): validate_result({'ok':False,'code':'private traceback'}, {})

    def test_projection_marks_previews_and_footer_records_actual_checks(self):
        row=record(ready=True); row['result']=outcome(); row['status']='applied'
        value=projection(row)
        self.assertEqual(value['check_status'],'incomplete')
        self.assertIn(CHANGE,attach_changes('result',[value]))

    def test_pure_editor_preserves_crlf_and_rejects_ambiguous_replacements(self):
        spec=importlib.util.spec_from_file_location('editor_fixture',Path(__file__).resolve().parents[1]/'app/runner/code_editor.py')
        module=importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules',{'fcntl':SimpleNamespace()}): spec.loader.exec_module(module)
        self.assertEqual(module._replacements(b'\xef\xbb\xbfVALUE = 1\r\n',draft()['edits']),b'\xef\xbb\xbfVALUE = 2\r\n')
        with self.assertRaises(module.EditError): module._replacements(b'VALUE = 1\nVALUE = 1\n',draft()['edits'])


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo=Mock(); self.repo.public.side_effect=lambda value:value
        self.execution=SimpleNamespace(get=Mock(return_value={'launch':{'server_binding':'f'*64,'work_dir':'/owned/project'},'server_id':'e'*32}))
        self.credentials={'password':'TEST_ONLY_SECRET'}
        profile=SimpleNamespace(binding=lambda:'f'*64)
        self.servers=SimpleNamespace(get=AsyncMock(return_value=profile),cipher=SimpleNamespace(decrypt=Mock(return_value=self.credentials)))
        self.transport=SimpleNamespace(request=AsyncMock(return_value=outcome()))
        self.gateway=SimpleNamespace(invoke=AsyncMock(return_value={'content':compact(draft())}),aclose=AsyncMock())
        self.planner=SimpleNamespace(create_for_user=AsyncMock(return_value=self.gateway))
        self.reader=SimpleNamespace(query=AsyncMock(return_value={'path':'train.py','file_sha256':'1'*64,'lines':files()['train.py']['windows'][0]}))
        self.service=CodeChangeService(self.repo,self.execution,servers_factory=lambda:self.servers,transport_factory=lambda:self.transport,
            planner_factory=lambda:self.planner,source_factory=lambda:self.reader)

    async def test_apply_stores_result_and_clears_credentials(self):
        self.repo.claim_apply.return_value=(record(ready=True),True)
        self.repo.get.return_value=record(ready=True)
        await self.service.apply(7,CHANGE)
        self.repo.finish.assert_called_once_with(7,CHANGE,TOKEN,result=outcome())
        self.assertEqual(self.credentials,{})
        self.planner.create_for_user.assert_not_awaited()

    async def test_completed_apply_replay_does_not_connect_or_generate(self):
        self.repo.claim_apply.return_value=(record(ready=True),False)
        await self.service.apply(7,CHANGE)
        self.servers.get.assert_not_awaited(); self.transport.request.assert_not_awaited()

    async def test_changed_server_binding_stops_before_decryption(self):
        self.repo.claim_apply.return_value=(record(ready=True),True)
        self.execution.get.return_value['launch']['server_binding']='0'*64
        with self.assertRaises(ChangeConflict): await self.service.apply(7,CHANGE)
        self.servers.cipher.decrypt.assert_not_called()
        self.assertEqual(self.repo.finish.call_args.kwargs['error_code'],'TARGET_CHANGED')

    async def test_remote_failure_keeps_plan_and_records_recoverable_error(self):
        self.repo.claim_apply.return_value=(record(ready=True),True)
        self.transport.request.side_effect=ChangeConflict('changed')
        with self.assertRaises(ChangeConflict): await self.service.apply(7,CHANGE)
        self.assertEqual(self.credentials,{})
        self.assertEqual(self.repo.finish.call_args.kwargs['error_code'],'SOURCE_CHANGED')

    async def test_prepare_replay_requires_no_key_source_or_model(self):
        self.repo.by_request.return_value=record(ready=True)
        await self.service.prepare(7,RUN,TASK,1,'e'*32)
        self.reader.query.assert_not_awaited(); self.planner.create_for_user.assert_not_awaited()

    async def test_cancel_apply_clears_credentials_and_records_attention(self):
        started=asyncio.Event()
        async def wait(*args): started.set(); await asyncio.sleep(10)
        self.repo.claim_apply.return_value=(record(ready=True),True)
        self.transport.request.side_effect=wait
        task=asyncio.create_task(self.service.apply(7,CHANGE))
        await started.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.credentials,{})
        self.assertEqual(self.repo.finish.call_args.kwargs['error_code'],'WORKER_CANCELLED')

    async def test_transport_uses_fixed_bootstrap_stdin_and_closes_on_cancel(self):
        process=SimpleNamespace(stdin=SimpleNamespace(write=Mock(),drain=AsyncMock(),write_eof=Mock()),
            stdout=SimpleNamespace(read=AsyncMock(return_value=b'')),stderr=SimpleNamespace(read=AsyncMock(return_value=b'')),
            wait_closed=AsyncMock(),close=Mock(),exit_status=0)
        connection=SimpleNamespace(create_process=AsyncMock(return_value=process))
        class Context:
            async def __aenter__(self): return connection
            async def __aexit__(self,*args): pass
        transport=CodeChangeTransport(SimpleNamespace(connection=lambda *args:Context()),'python3')
        with self.assertRaises(ChangeProtocolError): await transport.request(Mock(),{}, {'change_id':CHANGE})
        self.assertNotIn(CHANGE,connection.create_process.call_args.args[0])
        process.stdin.write.assert_called_once(); process.close.assert_called_once()

    async def test_http_maps_missing_execution_and_ssh_policy_without_leaking_details(self):
        from fastapi import Response,HTTPException
        from app.api.code_change_routes import operate
        from app.errors import RunnerNotStartedError,ServerAddressNotAllowedError
        for error,status in ((RunnerNotStartedError('PRIVATE'),409),(ServerAddressNotAllowedError('PRIVATE'),403)):
            with self.assertRaises(HTTPException) as raised:
                await operate(Response(),AsyncMock(side_effect=error))
            self.assertEqual(raised.exception.status_code,status)
            self.assertNotIn('PRIVATE',raised.exception.detail)

    async def test_http_rejects_provider_key_with_safe_message(self):
        import httpx
        from openai import APIStatusError
        from fastapi import Response,HTTPException
        from app.api.code_change_routes import operate
        error=APIStatusError('PRIVATE_PROVIDER_BODY',response=httpx.Response(401,request=httpx.Request('POST','https://example.invalid')),body={})
        with self.assertRaises(HTTPException) as raised:
            await operate(Response(),AsyncMock(side_effect=error))
        self.assertEqual(raised.exception.status_code,400)
        self.assertNotIn('PRIVATE',raised.exception.detail)


if __name__=='__main__': unittest.main()
