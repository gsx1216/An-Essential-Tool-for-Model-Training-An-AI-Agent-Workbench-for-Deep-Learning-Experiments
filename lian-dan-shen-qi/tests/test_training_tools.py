"""通用工具契约、原文启动授权、不可变计划、版本隔离和副作用边界。"""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,MagicMock

from app.models.context_snapshot import ContextAttempt
from app.models.context_state import ContextConflictError
from app.models.runner import RunnerSettings
from app.models.training_task import (TaskRequest,TRAINING_ARGUMENTS,TrainingConflictError,
    TrainingInstructionRequired,TrainingNotFoundError,digest,require_start_instruction,training_mode)
from app.models.chat_tools import tool_definitions,bounded_result,compact_json
from app.services.training_task_service import TrainingTaskService
from app.services.read_only_chat_service import ReadOnlyChatService
from app.errors import ServerConnectionTimeoutError

TASK='a'*32;SESSION='b'*32;RUN='c'*32;EXP='d'*32;SERVER='e'*32;PLAN='f'*32
ATTEMPT=ContextAttempt('1'*32,'2'*32)


class Contracts(unittest.TestCase):
    def test_training_migration_is_additive_and_upgrades_old_tool_limit(self):
        from app import database
        from unittest.mock import patch
        connection=MagicMock();cursor=connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value=('call_index between 1 and 4',)
        database._ensure_training_tools(connection)
        sql='\n'.join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn('training_tasks',sql);self.assertIn('chat_training_plans',sql)
        self.assertIn('BETWEEN 1 AND 6',sql);self.assertNotRegex(sql,r'(?m)^(DELETE|UPDATE)\s')
        cursor.reset_mock();cursor.fetchone.return_value=('call_index between 1 and 6',)
        database._ensure_training_tools(connection)
        self.assertNotIn('ALTER TABLE','\n'.join(call.args[0] for call in cursor.execute.call_args_list))

    def test_full_initialization_includes_training_migration(self):
        from app import database
        from contextlib import ExitStack
        from unittest.mock import patch
        connection=MagicMock();connection.__enter__.return_value=connection
        connection.cursor.return_value.__enter__.return_value.nextset.return_value=None
        with ExitStack() as stack:
            stack.enter_context(patch.object(database,'get_connection',return_value=connection))
            helpers={name:stack.enter_context(patch.object(database,name)) for name in vars(database) if name.startswith('_ensure_')}
            database.initialize_database('unused')
            helpers['_ensure_training_tools'].assert_called_once_with(connection)

    def test_tools_have_no_identity_commands_or_credentials(self):
        tools=tool_definitions(include_training=True)
        self.assertEqual(len(tools),10)
        for name,model in TRAINING_ARGUMENTS.items():
            for key in ('user_id','command','password','script_path','authorized'):
                args={'task_id':TASK} if name in {'get_training_task','prepare_training_run'} else {}
                with self.subTest(name=name,key=key),self.assertRaises(ValueError):model.model_validate({**args,key:'x'})

    def test_parameters_reject_secret_nonfinite_and_complex_values(self):
        for params in ({'api_key':'private'},{'lr':float('nan')},{'x':object()}):
            with self.assertRaises(ValueError):TRAINING_ARGUMENTS['prepare_training_run'](task_id=TASK,parameters=params)

    def test_schema_dump_is_json_serializable(self):
        for name,model in TRAINING_ARGUMENTS.items():
            args={'task_id':TASK} if name in {'get_training_task','prepare_training_run'} else {}
            if name in {'list_training_project_files','read_training_project_file'}:
                args={'server_id':SERVER}
                if name=='read_training_project_file':args['path']='train.py'
            json.dumps(model.model_validate(args).model_dump(mode='json'))

    def test_task_name_and_parameter_limits(self):
        for name in ('',' '*4,'bad\nname'):
            with self.assertRaises(ValueError):TaskRequest(name=name,experiment_id=EXP,server_id=SERVER,script_id='any_train')
        with self.assertRaises(ValueError):TaskRequest(name='task',experiment_id=EXP,server_id=SERVER,script_id='any_train',default_parameters={'x':'a'*1601})

    def test_explicit_instruction_allows_chinese_and_english(self):
        for text in ('启动 PRNet，batch size 4','请开始训练 PRNet','帮我训练 PRNet','执行 PRNet','现在在A服务器上启动 PRNet','start PRNet','please train PRNet'):
            with self.subTest(text=text):require_start_instruction(text,'PRNet',TASK,same_turn=True)

    def test_questions_negation_examples_and_quotes_never_authorize(self):
        for text in ('PRNet 最近训练结果是什么','如何启动 PRNet','如果启动 PRNet会怎样','不要启动 PRNet',
                     '请勿执行 PRNet','先不训练PRNet','比如启动 PRNet','日志里说启动 PRNet',
                     'PRNet 的命令是启动','“启动 PRNet”','`启动 PRNet`','> 启动 PRNet',
                     '启动 PRNet？','how to train PRNet','do not start PRNet','start YOLO','start PRNet2','启动 MyPRNet'):
            with self.subTest(text=text),self.assertRaises(TrainingInstructionRequired):
                require_start_instruction(text,'PRNet',TASK,same_turn=True)

    def test_referential_instruction_needs_previous_plan_not_current_invention(self):
        for text in ('执行这个训练','启动那个任务','就按刚才的参数执行'):
            with self.subTest(text=text):
                require_start_instruction(text,'PRNet',TASK,same_turn=False)
                with self.assertRaises(TrainingInstructionRequired):require_start_instruction(text,'PRNet',TASK,same_turn=True)
        with self.assertRaises(TrainingInstructionRequired):require_start_instruction('启动 YOLO 那个训练','PRNet',TASK,same_turn=False)

    def test_catalog_result_truncation_preserves_next_offset(self):
        value=bounded_result({'ok':True,'code':'OK','data':{'items':[{'name':'a'*100} for _ in range(5)],'offset':10,'next_offset':None}},420)
        self.assertEqual(value['data']['next_offset'],10+len(value['data']['items']))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo=MagicMock()
        spec={'path':'train_anything.py','sha256':'0'*64,'parameters':{'epochs':{'type':'int','min':1,'max':10,'required':True}},
            'max_runtime_seconds':30,'max_log_bytes':10000}
        self.settings=RunnerSettings(json.dumps({'other_project':spec,'point_cloud':{**spec,'path':'point_cloud.py'}}))
        self.experiment=SimpleNamespace(kind='training',evaluation=None)
        self.experiments=MagicMock();self.experiments.get_experiment.return_value=self.experiment
        self.server=SimpleNamespace(binding=lambda:'fixed_binding')
        self.servers=SimpleNamespace(get=AsyncMock(return_value=self.server))
        self.runner=SimpleNamespace(settings=self.settings,start=AsyncMock(return_value={'execution':{'run_id':RUN,'status':'running'}}))
        self.runs=SimpleNamespace(create_run=AsyncMock(return_value=SimpleNamespace(run=SimpleNamespace(run_id=RUN))))
        self.configuration={'spec':spec,'default_parameters':{'epochs':1},'server_binding':'fixed_binding','evaluation_sha256':digest(None)}
        self.task={'task_id':TASK,'name':'任意项目','experiment_id':EXP,'server_id':SERVER,'script_id':'other_project','configuration':self.configuration}
        snapshot={key:self.task[key] for key in ('task_id','name','experiment_id','server_id','script_id')}
        snapshot.update(parameters={'epochs':2},configuration_sha256=digest(self.configuration))
        self.plan={'id':PLAN,'task_id':TASK,'prepared_turn_id':ATTEMPT.turn_id,'launch_turn_id':ATTEMPT.turn_id,
            'snapshot':snapshot,'run_request_id':'3'*32,'run_id':None}
        self.repo.get_task.return_value=self.task;self.repo.get_plan.return_value=self.plan
        self.repo.claim.return_value=self.plan;self.repo.prepare.return_value=self.plan
        self.service=TrainingTaskService(self.repo,self.experiments,self.runs,self.servers,self.runner)

    async def test_create_works_for_multiple_registered_projects(self):
        self.repo.create_task.return_value=self.task
        for script in ('other_project','point_cloud'):
            result=await self.service.create(7,{'name':'任意项目','experiment_id':EXP,'server_id':SERVER,'script_id':script,'default_parameters':{'epochs':1}})
            self.assertEqual(result['name'],'任意项目')
            self.assertEqual(self.repo.create_task.call_args.args[4],script)
        self.runner.start.assert_not_awaited()

    async def test_prepare_merges_user_override_and_never_starts(self):
        await self.service.prepare(7,SESSION,ATTEMPT,TASK,{'epochs':2})
        self.assertEqual(self.repo.prepare.call_args.args[3]['parameters'],{'epochs':2})
        self.runner.start.assert_not_awaited();self.runs.create_run.assert_not_awaited()

    async def test_unknown_or_out_of_range_parameters_never_create_plan(self):
        for overrides in ({'command':'rm'},{'epochs':11},{'epochs':True}):
            with self.assertRaises(ValueError):await self.service.prepare(7,SESSION,ATTEMPT,TASK,overrides)
        self.repo.prepare.assert_not_called()

    async def test_foreign_task_never_reads_server_or_runs(self):
        self.repo.get_task.side_effect=TrainingNotFoundError()
        result=await self.service.execute(7,SESSION,ATTEMPT,'prepare_training_run',TRAINING_ARGUMENTS['prepare_training_run'](task_id=TASK))
        self.assertEqual(result['code'],'NOT_FOUND');self.servers.get.assert_not_awaited();self.runner.start.assert_not_awaited()

    async def test_source_server_or_evaluation_version_change_prevents_claim(self):
        for field,value in (('server_binding','other'),('evaluation_sha256','other'),('spec',{})):
            old=self.configuration[field];self.configuration[field]=value
            with self.assertRaises(TrainingConflictError):await self.service.start(7,SESSION,ATTEMPT,PLAN)
            self.configuration[field]=old
        self.repo.claim.assert_not_called();self.runner.start.assert_not_awaited()

    async def test_current_human_authorization_rejection_prevents_run_creation(self):
        self.repo.claim.side_effect=TrainingInstructionRequired()
        result=await self.service.execute(7,SESSION,ATTEMPT,'start_training_run',TRAINING_ARGUMENTS['start_training_run'](plan_id=PLAN))
        self.assertEqual(result['code'],'START_INSTRUCTION_REQUIRED');self.runs.create_run.assert_not_awaited()

    async def test_start_and_retry_reuse_plan_request_id(self):
        for _ in range(2):
            result=await self.service.start(7,SESSION,ATTEMPT,PLAN)
            self.assertEqual(result['run_id'],RUN)
        self.assertEqual([call.args[2] for call in self.runs.create_run.await_args_list],['3'*32,'3'*32])
        self.runner.start.assert_awaited_with(7,RUN,SERVER,'other_project')

    async def test_network_uncertainty_returns_existing_run_and_plan(self):
        self.runner.start.side_effect=ServerConnectionTimeoutError()
        result=await self.service.start(7,SESSION,ATTEMPT,PLAN)
        self.assertEqual(result['code'],'START_UNCONFIRMED');self.assertEqual(result['run_id'],RUN)
        self.assertEqual(result['data']['plan_id'],PLAN)

    async def test_start_receipt_does_not_leak_non_json_datetime_objects(self):
        from datetime import datetime,timezone
        self.runner.start.return_value={'execution':{'run_id':RUN,'status':'running','created_at':datetime.now(timezone.utc)}}
        result=await self.service.start(7,SESSION,ATTEMPT,PLAN)
        json.dumps(result)
        self.assertNotIn('created_at',result['data']['execution'])

    async def test_expired_attempt_after_persist_prevents_ssh(self):
        self.repo.check_attempt.side_effect=ContextConflictError()
        with self.assertRaises(ContextConflictError):await self.service.start(7,SESSION,ATTEMPT,PLAN)
        self.repo.bind_run.assert_called_once();self.runner.start.assert_not_awaited()

    async def test_prepare_same_turn_conflict_does_not_start(self):
        self.repo.prepare.side_effect=TrainingConflictError()
        args=TRAINING_ARGUMENTS['prepare_training_run'](task_id=TASK,parameters={'epochs':2})
        result=await self.service.execute(7,SESSION,ATTEMPT,'prepare_training_run',args)
        self.assertEqual(result['code'],'TRAINING_CONFLICT');self.runner.start.assert_not_awaited()


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_training_mode_dispatches_tools_but_research_is_not_constructed(self):
        repo=MagicMock();repo.binding.return_value=None
        training=SimpleNamespace(mode=AsyncMock(return_value=True),execute=AsyncMock(return_value={'ok':True,'code':'OK','data':{'items':[]}}))
        research=MagicMock()
        service=ReadOnlyChatService(repo,MagicMock(),metrics_factory=MagicMock(),runner_factory=MagicMock(),training_factory=lambda:training,research_factory=research)
        gateway=SimpleNamespace(invoke=AsyncMock(side_effect=[{'content':'','tool_calls':[{'id':'c1','name':'list_training_tasks','args':{}}]},{'content':'没有配置任务'}]))
        answer=await service.answer([('system','助手'),('human','有哪些训练任务')],user_id=7,session_id=SESSION,attempt=ATTEMPT,gateway=gateway,max_input_tokens=12000)
        self.assertEqual(answer,'没有配置任务');training.execute.assert_awaited_once();research.assert_not_called()
        self.assertEqual(repo.record.call_args.kwargs['name'],'list_training_tasks')
        self.assertEqual(service.input_reserve,11000)


if __name__=='__main__':unittest.main()
