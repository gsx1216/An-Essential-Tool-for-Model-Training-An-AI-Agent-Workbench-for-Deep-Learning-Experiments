"""修改后训练的真实源码固定、授权与数值对比；不调用模型或 GPU。"""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.models.revision_run import RevisionSettings, RevisionConflict, PrepareRevision, require_revision_start, compare_reports
from app.models.training_task import TrainingInstructionRequired
from app.runner import revision_source as remote
from app.services.revision_transport import validate_inspection
from app.services.runner_service import RunnerService
from app.models.runner import RunnerSettings
from app.models.chat_tools import tool_definitions
from tests.test_remote_runner import RemoteRunnerTests as _RunnerFixture


class ContractTests(unittest.TestCase):
    def test_registry_is_explicit(self):
        with self.assertRaises(RevisionConflict):RevisionSettings().mode('prnet_train')
        self.assertEqual(RevisionSettings('{"a":"cwd"}').mode('a'),'cwd')
        for text in ('{"a":"shell"}','[]','{"bad name":"prnet"}'):
            with self.subTest(text=text),self.assertRaises(ValueError):RevisionSettings(text)

    def test_request_has_no_commands_or_identity(self):
        for key in ('user_id','server_id','work_dir','command','source_digest'):
            with self.subTest(key=key),self.assertRaises(ValueError):
                PrepareRevision.model_validate({'request_id':uuid4().hex,key:'x'})

    def test_only_current_explicit_named_command(self):
        key=uuid4().hex
        for text in ('启动修改副本 '+key+' 的训练','开始训练 '+key+' 修改副本'):
            require_revision_start(text,key,'')
        for text in ('启动修改副本训练','如何启动修改副本 '+key+'？','不要启动修改副本 '+key,
                     '如果可以就启动修改副本 '+key,'“启动修改副本 '+key+'”','日志里启动修改副本 '+key):
            with self.subTest(text=text),self.assertRaises(TrainingInstructionRequired):require_revision_start(text,key,'')
        with self.assertRaises(TrainingInstructionRequired):require_revision_start('启动任意训练','','')

    def test_write_tools_not_offered_for_reading(self):
        names=lambda **kw:{row['function']['name'] for row in tool_definitions(**kw)}
        self.assertIn('compare_revision_run',names(include_revisions=True))
        self.assertNotIn('start_revision_run',names(include_revisions=True))
        self.assertIn('start_revision_run',names(include_revisions=True,allow_revisions=True))

    def test_only_generic_revision_mode_is_available_for_new_tasks(self):
        with self.assertRaises(ValueError):
            RevisionSettings('{"legacy":"prnet"}')


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.change=uuid4().hex
        (self.root/'train.py').write_bytes(b'value = 1\n')
        self.candidate=self.root/'code_changes'/self.change/'project';self.candidate.mkdir(parents=True)
        (self.candidate/'train.py').write_bytes(b'value = 2\n')
        self.payload={'work_dir':str(self.root),'change_id':self.change,'mode':'cwd',
            'changed_files':[{'path':'train.py','after_sha256':remote.digest(b'value = 2\n')}],
            'spec':{'path':'train.py','sha256':remote.digest(b'value = 1\n')}}

    def inspect(self):return remote.inspect(self.payload)

    def test_inspection_reads_real_modified_entry_without_writing(self):
        data=self.inspect()
        self.assertEqual(data['files']['train.py'],remote.digest(b'value = 2\n'))
        self.assertEqual((self.root/'train.py').read_bytes(),b'value = 1\n')
        self.assertEqual(validate_inspection({'ok':True,'data':data},self.payload),data)

    def test_manual_modification_after_apply_not_misattributed(self):
        (self.candidate/'train.py').write_bytes(b'value = 3\n')
        with self.assertRaises(remote.RevisionError):self.inspect()

    def test_registered_original_entry_changed(self):
        (self.root/'train.py').write_bytes(b'changed\n')
        with self.assertRaises(remote.RevisionError):self.inspect()

    def test_inspection_rejects_hidden_and_binary_inputs(self):
        for name in ('.env','weights.pth'):
            target=self.candidate/name;target.write_bytes(b'x')
            with self.subTest(name=name),self.assertRaises(remote.RevisionError):self.inspect()
            target.unlink()

    def test_freeze_binds_all_files_and_not_live_candidate(self):
        data=self.inspect();job=self.root/'job';job.mkdir()
        launch={'revision':{**data,'change_id':self.change},'spec':self.payload['spec']}
        script,directory=remote.freeze(self.root,job,launch)
        (self.candidate/'train.py').write_bytes(b'value = 9\n')
        self.assertEqual(script.read_bytes(),b'value = 2\n')
        self.assertEqual(directory,job/'revision_source')
        self.assertEqual((self.root/'train.py').read_bytes(),b'value = 1\n')

    def test_freeze_rejects_added_or_changed_dependencies(self):
        (self.candidate/'helper.py').write_bytes(b'value = 5\n')
        data=self.inspect();(self.candidate/'helper.py').write_bytes(b'value = 6\n')
        job=self.root/'job';job.mkdir()
        with self.assertRaises(remote.RevisionError):remote.freeze(self.root,job,{'revision':{**data,'change_id':self.change}})
        (self.candidate/'helper.py').write_bytes(b'value = 5\n');(self.candidate/'extra.py').write_bytes(b'x=1')
        with self.assertRaises(remote.RevisionError):remote.freeze(self.root,job,{'revision':{**data,'change_id':self.change}})

    def test_no_overwriting_existing_runtime_source(self):
        data=self.inspect();job=self.root/'job';job.mkdir();(job/'revision_source').mkdir()
        keep=job/'revision_source'/'keep';keep.write_bytes(b'manual')
        with self.assertRaises(remote.RevisionError):remote.freeze(self.root,job,{'revision':{**data,'change_id':self.change}})
        self.assertEqual(keep.read_bytes(),b'manual')

    def test_response_does_not_accept_wrong_modified_hash(self):
        data=self.inspect();data['files']['train.py']='f'*64
        from app.errors import RunnerProtocolError
        with self.assertRaises(RunnerProtocolError):validate_inspection({'ok':True,'data':data},self.payload)

    def test_path_escape_rejected(self):
        for value in ('../outside','/outside','a\\b'):
            with self.subTest(value=value),self.assertRaises(remote.RevisionError):remote.plain(self.root,value)


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.spec={'metrics':[{'name':'mae','direction':'minimize'}], 'datasets':[{'id':'cod'}]}
        self.run=SimpleNamespace(status='succeeded',evaluation=self.spec,parameters={'epochs':1})
        self.a={'version':1,'is_stale':False,'data':{'schema_version':2,'status':'succeeded','evaluation':self.spec,
            'collection_complete':True,'warnings':[],'series':[{'dataset':'cod','split':'test','metric_name':'mae',
                'last_value':.3,'last_step':1,'quality_flags':[]}]}}
        self.b=deepcopy(self.a);self.b['data']['series'][0]['last_value']=.2

    def compare(self,a=None,b=None,run=None):return compare_reports(a or self.a,b or self.b,self.run,run or self.run)

    def test_minimize_last_value_delta(self):
        row=self.compare();self.assertTrue(row['comparable']);self.assertAlmostEqual(row['series'][0]['delta'],-.1)
        self.assertEqual(row['series'][0]['outcome'],'improved')

    def test_maximize_and_unspecified_direction(self):
        for direction,outcome in [('maximize','worsened'),('none','direction_unspecified')]:
            self.spec['metrics'][0]['direction']=direction
            self.b['data']['evaluation']=deepcopy(self.spec)
            self.assertEqual(self.compare()['series'][0]['outcome'],outcome)

    def test_missing_report_returns_not_ready(self):
        self.assertIn('candidate:REPORT_NOT_READY',compare_reports(self.a,None,self.run,self.run)['reasons'])

    def test_bad_report_blocks_numeric_conclusion(self):
        for field,value in [('is_stale',True),('schema_version',1),('collection_complete',False),('warnings',['LOG_TRUNCATED'])]:
            b=deepcopy(self.b)
            (b if field=='is_stale' else b['data'])[field]=value
            with self.subTest(field=field):
                result=self.compare(b=b);self.assertFalse(result['comparable']);self.assertEqual(result['series'],[])

    def test_protocol_difference_blocks_comparison(self):
        other=SimpleNamespace(status='succeeded',evaluation={'different':'protocol'},parameters={})
        self.assertIn('EVALUATION_MISMATCH_OR_UNSPECIFIED',self.compare(run=other)['reasons'])

    def test_missing_split_not_mixed_or_imputed(self):
        self.b['data']['series'][0]['split']='validation'
        rows=self.compare()['series'];self.assertEqual(len(rows),2)
        self.assertTrue(all(row['reason']=='SERIES_MISSING' and row['delta'] is None for row in rows))

    def test_step_mismatch_or_quality_not_compared(self):
        for field,value in [('last_step',2),('quality_flags',['VALUE_OUT_OF_RANGE'])]:
            b=deepcopy(self.b);b['data']['series'][0][field]=value
            with self.subTest(field=field):self.assertIsNone(self.compare(b=b)['series'][0]['delta'])

    def test_parameter_changes_exposed(self):
        other=SimpleNamespace(status='succeeded',evaluation=self.spec,parameters={'epochs':2})
        self.assertEqual(self.compare(run=other)['parameter_changes']['epochs'],{'original':1,'candidate':2})


class RunnerBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_start_interface_uses_revision_binding(self):
        uid=1;rid=uuid4().hex;sid=uuid4().hex
        spec={'path':'train.py','sha256':'a'*64,'parameters':{},'max_runtime_seconds':10,'max_log_bytes':100}
        profile=SimpleNamespace(server_id=sid,work_dir='/owned/project',binding=lambda:'b'*64)
        repo=MagicMock();repo.get.return_value=None
        runs=MagicMock();runs.get_run.return_value=SimpleNamespace(parameters={})
        revisions=MagicMock();revisions.get_revision.return_value={'binding':{'server_id':sid,'script_id':'train','spec':spec,
            'server_binding':'b'*64,'parameters':{},'work_dir':'/owned/project','revision':{'change_id':'c'*32},'revision_helper_source':'helper'}}
        runner=RunnerService(repo,runs,SimpleNamespace(get=AsyncMock(return_value=profile)),None,RunnerSettings(json.dumps({'train':spec})),revisions=revisions)
        runner._remote=AsyncMock(return_value={'ok':True})
        await runner.start(uid,rid,sid,'train')
        launch=repo.claim.call_args.args[-1]
        self.assertIn('revision',launch);self.assertEqual(launch['revision_helper_source'],'helper')


class RemoteRevisionTests(unittest.TestCase):
    request=_RunnerFixture.request
    wait_terminal=_RunnerFixture.wait_terminal
    tearDown=_RunnerFixture.tearDown
    _base_setup=_RunnerFixture.setUp
    def setUp(self):
        self._base_setup()
        self.change=uuid4().hex
        project=self.work/'code_changes'/self.change/'project';project.mkdir(parents=True)
        script='''import argparse,json,os
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--config');a=p.parse_args()
out=Path(os.environ['EXPERIMENT_ARTIFACT_DIR']);out.mkdir()
(out/'candidate.txt').write_text('modified version 2')
with (Path(os.environ['EXPERIMENT_INPUT_ROOT'])/'revision_count.txt').open('a') as f:f.write('one\\n')
print('{"type":"metric","step":1,"dataset":"cod","split":"test","mae":0.2}',flush=True)
'''
        (project/'train.py').write_text(script,encoding='utf-8')
        rows=remote.inventory(project)
        self.project=project
        self.launch['revision']={'change_id':self.change,'mode':'cwd','files':rows,'source_digest':remote.digest(remote.canonical(rows).encode())}
        self.launch['revision_helper_source']=(Path(__file__).resolve().parents[1]/'app/runner/revision_source.py').read_text(encoding='utf-8')

    def test_revision_executes_modified_entry_and_publishes(self):
        self.request('start');observation=self.wait_terminal()
        self.assertEqual(observation['status'],'succeeded')
        output=self.work/'training_outputs'/self.launch['run_id']/'artifacts/candidate.txt'
        self.assertEqual(output.read_text(),'modified version 2')
        (self.project/'train.py').write_text('raise Exception("later edit")',encoding='utf-8')
        self.request('start')
        self.assertEqual((self.work/'revision_count.txt').read_text().count('one'),1)
        self.assertNotIn('modified version',(self.work/'train.py').read_text())

    def test_revision_changed_after_prepare_never_executes(self):
        (self.project/'other.py').write_text('value=1',encoding='utf-8')
        self.request('start');observation=self.wait_terminal()
        self.assertEqual(observation['error_code'],'REVISION_SOURCE_CHANGED')
        self.assertFalse((self.work/'revision_count.txt').exists())


del _RunnerFixture


if __name__=='__main__':unittest.main()
