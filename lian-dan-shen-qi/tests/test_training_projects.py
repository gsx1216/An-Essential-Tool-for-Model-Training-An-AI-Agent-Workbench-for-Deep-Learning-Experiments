"""通用项目配置、归属校验、API 和真实本机 CPU 子进程；不访问训练服务器或 GPU。"""
from copy import deepcopy
import hashlib
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_training_task_service
from app.api.schemas import UserResponse
from app.api.training_task_routes import router
from app.errors import RunnerConflictError, RunnerScriptNotAllowedError, ServerNotFoundError
from app.models.runner import RunnerSettings
from app.models.training_project import ProjectLaunch
from app.models.training_task import TaskRequest, TrainingNotFoundError, digest
from app.runner.remote_agent import MetricNormalizer
from app.services.runner_service import RunnerService
from app.services.training_task_service import TrainingTaskService
import tests.test_remote_runner as remote_fixture
from tests.test_server_connection_core import profile

SERVER = profile().server_id
EXPERIMENT = 'e'*32
RUN = 'b'*32
SCRIPT_ID = 'project_' + 'c'*32


def project_config():
    return {
        'entrypoint':'tools/train.py', 'python':'/opt/conda/envs/myenv/bin/python',
        'arguments':['--epochs','{epochs}','--lr','{learning_rate}',
                     '--data','{input_root}/dataset','--output','{artifact_dir}'],
        'parameter_rules':{
            'epochs':{'type':'int','required':True,'min':1,'max':200},
            'learning_rate':{'type':'float','required':True,'min':0.000001,'max':1.0}},
        'cuda_visible_devices':'1',
        'metric_rules':[{'pattern':r'Epoch (?P<epoch>\d+) loss=(?P<loss>[0-9.eE+-]+)',
                         'dataset':'custom','split':'train','metrics':['loss']}],
    }


class ProjectContracts(unittest.TestCase):
    def test_native_config_reuses_runner_parameter_validation(self):
        config=ProjectLaunch(**project_config())
        spec=config.spec('a'*64)
        settings=RunnerSettings(json.dumps({SCRIPT_ID:spec}))
        self.assertEqual(settings.resolve(SCRIPT_ID,{'epochs':50,'learning_rate':0.01})[0],spec)
        for params in ({'epochs':True,'learning_rate':0.01},{'epochs':201,'learning_rate':0.01},
                       {'epochs':1},{'epochs':1,'learning_rate':0.01,'command':'anything'}):
            with self.subTest(params=params),self.assertRaises(ValueError):
                settings.resolve(SCRIPT_ID,params)

    def test_main_network_is_separate_and_legacy_launch_spec_stays_compatible(self):
        from app.models.training_project import validate_project_spec
        legacy=ProjectLaunch(**project_config()).spec('a'*64)
        self.assertNotIn('main_network',legacy['project'])
        self.assertEqual(validate_project_spec(legacy).spec('a'*64),legacy)
        config=project_config();config['main_network']='models/network.py'
        spec=ProjectLaunch(**config).spec('a'*64)
        self.assertEqual(spec['path'],'tools/train.py')
        self.assertEqual(spec['project']['main_network'],'models/network.py')
        self.assertEqual(validate_project_spec(spec).spec('a'*64),spec)
        for path in ('../model.py','weights.pth'):
            with self.assertRaises(ValueError):ProjectLaunch(**(config|{'main_network':path}))

    def test_entry_is_user_choice_not_prnet_name(self):
        for entry in ('train.py','tools/train_detector.py','point_cloud/train.py','tracking/main.py'):
            config=project_config();config['entrypoint']=entry
            self.assertEqual(ProjectLaunch(**config).entrypoint,entry)

    def test_registered_and_owned_entry_are_mutually_exclusive(self):
        common={'name':'test','server_id':SERVER,'experiment_id':EXPERIMENT}
        with self.assertRaises(ValueError):TaskRequest(**common)
        with self.assertRaises(ValueError):TaskRequest(**common,script_id='old',project=project_config())
        self.assertIsNotNone(TaskRequest(**common,project=project_config()).project)
        self.assertEqual(TaskRequest(**common,script_id='prnet_train').script_id,'prnet_train')

    def test_templates_paths_and_interpreter_are_checked(self):
        cases=[{'entrypoint':'../train.py'},{'entrypoint':'/etc/train.py'},
               {'entrypoint':'train.sh'},{'python':'python; echo unsafe'},
               {'arguments':['{epochs.__class__}']},{'arguments':['{unknown}']},
               {'arguments':['{epochs!r}']},{'arguments':['{epochs:02d}']},
               {'cuda_visible_devices':'0;echo'}, {'cuda_visible_devices':'gpu-unknown'}]
        for changes in cases:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                ProjectLaunch(**(project_config()|changes))
        for devices in ('','0','2','1,3',None):
            ProjectLaunch(**(project_config()|{'cuda_visible_devices':devices}))

    def test_metrics_and_native_jsonl_share_existing_protocol(self):
        from app.models.training_metrics import metric_line
        normalizer=MetricNormalizer(ProjectLaunch(**project_config()).model_dump()['metric_rules'])
        first=normalizer.feed(b'Epoch 1 lo')
        self.assertEqual(first,b'')
        data=normalizer.feed(b'ss=0.5\nEpoch 2 loss=0.25',final=True)
        events=[metric_line(line) for line in data.splitlines()]
        events=[row for row in events if row]
        self.assertEqual([row['values']['loss'] for row in events],[0.5,0.25])
        self.assertEqual(events[0]['dataset'],'custom')
        raw=b'{"type":"metric","step":3,"loss":0.1}\n'
        self.assertEqual(normalizer.feed(raw),raw)

    def test_metrics_reject_invalid_configuration_and_skip_bad_values(self):
        for rule in ({'pattern':'('},{'pattern':r'loss=(?P<loss>.+)'},{'metrics':['missing']}):
            config=project_config();config['metric_rules'][0].update(rule)
            with self.assertRaises(ValueError):ProjectLaunch(**config)
        normalizer=MetricNormalizer(ProjectLaunch(**project_config()).model_dump()['metric_rules'])
        raw=b'Epoch 1 loss=1e999\n'
        self.assertEqual(normalizer.feed(raw),raw)
        raw=b'x'*10000+b'\n'
        self.assertEqual(normalizer.feed(raw),raw)


class OwnedProjectServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo=MagicMock()
        self.experiments=MagicMock()
        self.experiments.get_experiment.return_value=SimpleNamespace(kind='training',evaluation=None)
        self.profile=profile()
        self.servers=SimpleNamespace(get=AsyncMock(return_value=self.profile),cipher=MagicMock())
        self.transport=SimpleNamespace(request=AsyncMock(return_value={'path':'tools/train.py','file_sha256':'a'*64}))
        self.runner=SimpleNamespace(settings=RunnerSettings(),start=AsyncMock())
        self.service=TrainingTaskService(self.repo,self.experiments,MagicMock(),self.servers,self.runner,
                                         source_transport=self.transport)
        self.servers.cipher.decrypt.return_value={'password':'not-a-real-secret'}
        def saved(user,name,experiment,server,script,configuration):
            return {'task_id':'a'*32,'name':name,'experiment_id':experiment,'server_id':server,
                    'script_id':script,'configuration':configuration}
        self.repo.create_task.side_effect=saved

    async def create(self):
        return await self.service.create(7,{'name':'我的点云项目','experiment_id':EXPERIMENT,
            'server_id':SERVER,'project':project_config(),
            'default_parameters':{'epochs':20,'learning_rate':0.01}})

    async def test_self_service_without_global_registration_reads_entry_and_saves(self):
        result=await self.create()
        self.assertTrue(result['script_id'].startswith('project_'))
        self.assertEqual(result['project']['entrypoint'],'tools/train.py')
        self.assertEqual(result['project']['entry_sha256'],'a'*64)
        self.assertEqual(result['project']['cuda_visible_devices'],'1')
        self.assertEqual(result['project']['max_runtime_seconds'],86400)
        self.assertEqual(self.transport.request.call_args.args[2]['work_dir'],self.profile.work_dir)
        self.assertEqual(self.servers.cipher.decrypt.return_value,{})
        self.runner.start.assert_not_awaited()

    async def test_wrong_owner_never_opens_ssh_or_saves(self):
        self.servers.get.side_effect=ServerNotFoundError()
        with self.assertRaises(ServerNotFoundError):await self.create()
        self.transport.request.assert_not_awaited()
        self.repo.create_task.assert_not_called()

    async def test_read_error_and_timeout_clear_credentials(self):
        from app.runner.source_reader import SourceReadError
        for error in (SourceReadError('NOT_FOUND'),TimeoutError()):
            self.servers.cipher.decrypt.return_value={'password':'not-a-real-secret'}
            self.transport.request.side_effect=error
            with self.assertRaises(type(error)):await self.create()
            self.assertEqual(self.servers.cipher.decrypt.return_value,{})
        self.repo.create_task.assert_not_called()

    async def test_bad_parameters_do_not_open_project_read(self):
        with self.assertRaises(ValueError):
            await self.service.create(7,{'name':'bad','experiment_id':EXPERIMENT,'server_id':SERVER,
                'project':project_config(),'default_parameters':{'epochs':999,'learning_rate':0.01}})
        self.transport.request.assert_not_awaited()

    async def test_server_discovery_returns_only_needed_public_fields(self):
        self.servers.list=AsyncMock(return_value=SimpleNamespace(items=[self.profile],next_cursor=None))
        result=await self.service.list_servers(7)
        self.assertEqual(set(result['items'][0]),{'server_id','name','work_dir'})
        self.assertNotIn('password',json.dumps(result))

    async def test_plan_for_owned_project_works_with_empty_global_registry(self):
        await self.create()
        task=self.repo.create_task.call_args
        saved=self.repo.create_task.side_effect(*task.args)
        self.repo.get_task.return_value=saved
        self.repo.prepare.return_value={'id':'f'*32,'snapshot':{
            'task_id':saved['task_id'],'name':saved['name'],'experiment_id':EXPERIMENT,
            'server_id':SERVER,'script_id':saved['script_id'],'parameters':{'epochs':30,'learning_rate':0.01}},
            'run_id':None,'launch_turn_id':None}
        await self.service.prepare(7,'d'*32,SimpleNamespace(),saved['task_id'],{'epochs':30})
        self.assertEqual(self.repo.prepare.call_args.args[3]['parameters']['epochs'],30)
        self.runner.start.assert_not_awaited()

    async def test_runner_resolves_owned_script_and_checks_target(self):
        result=await self.create()
        task=self.repo.create_task.side_effect(*self.repo.create_task.call_args.args)
        self.repo.get_by_script.return_value=task
        runner=RunnerService(MagicMock(),MagicMock(),self.servers,MagicMock(),RunnerSettings(),tasks=self.repo)
        spec,params=await runner.resolve_script(7,result['script_id'],{'epochs':10,'learning_rate':0.02},server_id=SERVER)
        self.assertIn('project',spec)
        self.repo.get_by_script.assert_called_with(7,result['script_id'])
        with self.assertRaises(RunnerConflictError):
            await runner.resolve_script(7,result['script_id'],params,server_id='9'*32)
        self.repo.get_by_script.side_effect=TrainingNotFoundError()
        with self.assertRaises(RunnerScriptNotAllowedError):
            await runner.resolve_script(8,result['script_id'],params,server_id=SERVER)

    async def test_owned_project_start_freezes_native_config_before_remote_launch(self):
        result=await self.create()
        task=self.repo.create_task.side_effect(*self.repo.create_task.call_args.args)
        self.repo.get_by_script.return_value=task
        executions=MagicMock();executions.get.return_value=None
        runs=MagicMock();runs.get_run.return_value=SimpleNamespace(parameters={'epochs':3,'learning_rate':0.01})
        runner=RunnerService(executions,runs,self.servers,MagicMock(),RunnerSettings(),tasks=self.repo)
        runner._remote=AsyncMock(return_value={'execution':{'run_id':RUN}})
        await runner.start(7,RUN,SERVER,result['script_id'])
        launch=executions.claim.call_args.args[-1]
        self.assertEqual(launch['spec']['path'],'tools/train.py')
        self.assertEqual(launch['spec']['project']['python'],'/opt/conda/envs/myenv/bin/python')
        self.assertEqual(launch['parameters']['epochs'],3)
        executions.claim.assert_called_once()
        runner._remote.assert_awaited_once()

    async def test_removed_global_entry_is_not_restored_by_task_catalog(self):
        executions=MagicMock();executions.get.return_value={
            'server_id':SERVER,'script_id':'removed','launch':{'parameters':{}}}
        tasks=MagicMock();tasks.get_by_script.side_effect=TrainingNotFoundError()
        transport=SimpleNamespace(request=AsyncMock())
        runner=RunnerService(executions,MagicMock(),self.servers,transport,RunnerSettings(),tasks=tasks)
        with self.assertRaises(RunnerScriptNotAllowedError):
            await runner.start(7,RUN,SERVER,'removed')
        transport.request.assert_not_awaited()

    async def test_training_agent_can_inspect_before_any_run_exists(self):
        from app.models.context_snapshot import ContextAttempt
        from app.services.read_only_chat_service import ReadOnlyChatService
        repo=MagicMock();repo.binding.return_value=None
        training=SimpleNamespace(mode=AsyncMock(return_value=True),execute=AsyncMock(return_value={
            'ok':True,'code':'OK','data':{'server_id':SERVER,'path':'README.md',
                'file_sha256':'a'*64,'lines':[{'number':1,'text':'python tools/train.py --epochs 10'}]}}))
        gateway=SimpleNamespace(invoke=AsyncMock(side_effect=[
            {'content':'','tool_calls':[{'id':'inspect1','name':'read_training_project_file',
                'args':{'server_id':SERVER,'path':'README.md'}}]},
            {'content':'已读取 README，请确认训练入口和参数。'}]))
        service=ReadOnlyChatService(repo,MagicMock(),metrics_factory=MagicMock(),runner_factory=MagicMock(),
                                   training_factory=lambda:training)
        reply=await service.answer([('system','助手'),('human','接入我的训练项目，先读 README')],
            user_id=7,session_id='d'*32,attempt=ContextAttempt('1'*32,'2'*32),
            gateway=gateway,max_input_tokens=48000)
        self.assertIn('已读取',reply)
        self.assertEqual(training.execute.call_args.args[3],'read_training_project_file')
        repo.record.assert_called_once()


class ProjectApiTests(unittest.TestCase):
    def setUp(self):
        app=FastAPI();app.include_router(router)
        self.service=SimpleNamespace(create=AsyncMock(return_value={'script_id':SCRIPT_ID}),
                                     project_source=AsyncMock(return_value={'path':'README.md','lines':[]}))
        app.dependency_overrides[require_current_user]=lambda:UserResponse(user_id=7,username='tester')
        app.dependency_overrides[get_training_task_service]=lambda:self.service
        self.client=TestClient(app)

    def test_new_project_request_and_read_endpoints(self):
        response=self.client.post('/training-tasks',json={
            'name':'my project','experiment_id':EXPERIMENT,'server_id':SERVER,
            'project':project_config(),'default_parameters':{'epochs':10,'learning_rate':0.01}})
        self.assertEqual(response.status_code,201,response.text)
        response=self.client.post('/training-projects/file',json={'server_id':SERVER,'path':'README.md'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(self.service.project_source.call_args.args[0],7)

    def test_bad_schema_and_other_user_server(self):
        response=self.client.post('/training-tasks',json={'name':'x','experiment_id':EXPERIMENT,'server_id':SERVER})
        self.assertEqual(response.status_code,422)
        self.service.project_source.side_effect=ServerNotFoundError()
        self.assertEqual(self.client.post('/training-projects/files',json={'server_id':SERVER}).status_code,404)

    def test_read_timeout_returns_friendly_error(self):
        self.service.project_source.side_effect=TimeoutError('not exposed')
        response=self.client.post('/training-projects/file',json={'server_id':SERVER,'path':'README.md'})
        self.assertEqual(response.status_code,503)
        self.assertNotIn('not exposed',response.text)


class NativeCpuProcessTests(unittest.TestCase):
    setUp=remote_fixture.RemoteRunnerTests.setUp
    tearDown=remote_fixture.RemoteRunnerTests.tearDown
    request=remote_fixture.RemoteRunnerTests.request
    wait_terminal=remote_fixture.RemoteRunnerTests.wait_terminal

    def configure(self,*,fail=False):
        # 真正执行一个无依赖 CPU 梯度下降脚本，证明 argv、环境、日志、产物和退出码贯通。
        source='''import argparse,json,os
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--epochs',type=int,required=True)
p.add_argument('--lr',type=float,required=True)
p.add_argument('--output',required=True)
p.add_argument('--label',required=True)
a=p.parse_args()
w=0.0
with Path('launch_count.txt').open('a') as f:f.write('launch\\n')
for epoch in range(1,a.epochs+1):
    w-=a.lr*2*(w-3)
    print(f'Epoch {epoch} loss={(w-3)**2}',flush=True)
Path(a.output,'last.json').write_text(json.dumps({'weight':w,'gpu':os.environ.get('CUDA_VISIBLE_DEVICES'),'label':a.label}))
'''
        if fail:source+='raise SystemExit(7)\n'
        script=self.work/'train.py';script.write_text(source,encoding='utf-8')
        self.launch['spec'].update(sha256=hashlib.sha256(script.read_bytes()).hexdigest(),max_log_bytes=20000)
        self.launch['spec']['project']={
            'python':sys.executable,
            'arguments':['--epochs','{epochs}','--lr','{learning_rate}',
                         '--output','{artifact_dir}','--label','{label}'],
            'cuda_visible_devices':'',
            'metric_rules':[{'pattern':r'Epoch (?P<epoch>\d+) loss=(?P<loss>[0-9.eE+-]+)',
                             'dataset':'toy','split':'train','metrics':['loss']}],
        }
        self.launch['parameters']={'epochs':3,'learning_rate':0.1,'label':'literal ; & $() value'}

    def test_native_cpu_training_metrics_artifacts_and_idempotent_start(self):
        self.configure()
        self.request('start')
        result=self.wait_terminal()
        self.assertEqual(result['status'],'succeeded')
        destination=self.work/'training_outputs'/self.launch['run_id']
        artifact=json.loads((destination/'artifacts/last.json').read_text())
        self.assertGreater(artifact['weight'],0)
        self.assertEqual(artifact['gpu'],'')
        self.assertEqual(artifact['label'],'literal ; & $() value')
        self.assertEqual(result['outputs']['status'],'published')
        from app.models.training_metrics import metric_line
        events=[metric_line(line) for line in (destination/'output.log').read_bytes().splitlines()]
        self.assertEqual(len([row for row in events if row]),3)
        self.request('start')
        self.assertEqual((self.work/'launch_count.txt').read_text().count('launch'),1)

    def test_native_nonzero_exit_is_failure(self):
        self.configure(fail=True)
        self.request('start')
        result=self.wait_terminal()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['exit_code'],7)

    def test_entry_changed_before_start_does_not_execute(self):
        self.configure()
        (self.work/'train.py').write_text('raise RuntimeError("changed")',encoding='utf-8')
        self.request('start')
        result=self.wait_terminal()
        self.assertEqual(result['error_code'],'SCRIPT_CHANGED')
        self.assertFalse((self.work/'launch_count.txt').exists())

    def test_missing_python_is_reported_separately_from_missing_training_file(self):
        self.configure()
        self.launch['spec']['project']['python']=str(self.work/'python-that-does-not-exist')
        self.request('start')
        result=self.wait_terminal()
        self.assertEqual(result['error_code'],'PYTHON_MISSING')
        self.assertFalse((self.work/'launch_count.txt').exists())

    def test_native_revision_runs_modified_source_with_same_project_parameters(self):
        from app.runner import revision_source
        self.configure()
        original=(self.work/'train.py').read_bytes()
        change='d'*32
        candidate=self.work/'code_changes'/change/'project'
        candidate.mkdir(parents=True)
        (candidate/'train.py').write_bytes(original.replace(b'w=0.0',b'w=1.0'))
        rows=revision_source.inventory(candidate)
        self.launch['revision']={'mode':'cwd','change_id':change,'files':rows,
            'source_digest':revision_source.digest(revision_source.canonical(rows).encode())}
        self.launch['revision_helper_source']=(Path(__file__).resolve().parents[1]/'app/runner/revision_source.py').read_text(encoding='utf-8')
        self.request('start')
        result=self.wait_terminal()
        self.assertEqual(result['status'],'succeeded')
        destination=self.work/'training_outputs'/self.launch['run_id']
        artifact=json.loads((destination/'artifacts/last.json').read_text())
        self.assertGreater(artifact['weight'],1.9)
        self.assertEqual((self.work/'train.py').read_bytes(),original)

    def test_two_projects_with_different_paths_and_parameter_names_can_train(self):
        from uuid import uuid4
        for entry in ('detection/train_detector.py','point_cloud/fit.py'):
            with self.subTest(entry=entry):
                self.configure()
                target=self.work/entry;target.parent.mkdir(parents=True)
                target.write_bytes((self.work/'train.py').read_bytes())
                self.launch['spec']['path']=entry
                self.launch['parameters']={'iterations':2,'step_size':0.1,'experiment_name':entry}
                self.launch['spec']['project']['arguments']=[
                    '--epochs','{iterations}','--lr','{step_size}',
                    '--output','{artifact_dir}','--label','{experiment_name}']
                self.launch['run_id']=uuid4().hex;self.job_id=uuid4().hex
                self.request('start')
                result=self.wait_terminal()
                self.assertEqual(result['status'],'succeeded')
                destination=self.work/'training_outputs'/self.launch['run_id']/'artifacts/last.json'
                self.assertEqual(json.loads(destination.read_text())['label'],entry)


if __name__=='__main__':unittest.main()

