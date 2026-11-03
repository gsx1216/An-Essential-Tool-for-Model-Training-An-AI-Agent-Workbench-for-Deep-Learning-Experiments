"""本人项目、快捷模板与对话执行；资源发现只读，启动参数不解释 shell。"""
import asyncio
import json
from uuid import uuid4

from mysql.connector import Error as MySQLError

from app.errors import (ExperimentNotFoundError,ServerNotFoundError,RunnerScriptNotAllowedError,
    RunnerConflictError,RunStorageError,RunRequestConflictError,ServerConnectionError,ServerConnectionTimeoutError,
    ServerHostKeyError,ServerAuthenticationError,ServerBusyError,ServerAddressNotAllowedError,
    ServerCredentialDecryptionError,ServerStorageError,RunnerProtocolError)
from app.logging_config import logged_operation,log_failure
from app.models.training_task import (TaskRequest,TrainingNotFoundError,TrainingConflictError,
    TrainingInstructionRequired,digest,training_mode)
from app.models.runner import RunnerSettings
from app.models.training_project import ProjectDirectoryRequest, ProjectFileRequest, ProjectEnvironmentRequest
from app.models.source_code import SourceProtocolError
from app.runner.source_reader import SourceReadError
import logging

logger=logging.getLogger(__name__)
UNAVAILABLE=(MySQLError,RunStorageError,ServerStorageError,ServerConnectionError,ServerConnectionTimeoutError,
    ServerHostKeyError,ServerAuthenticationError,ServerBusyError,ServerAddressNotAllowedError,
    ServerCredentialDecryptionError,RunnerProtocolError)


def public_task(task):
    result = {key:task[key] for key in ('task_id','name','experiment_id','server_id','script_id')} | {
        'default_parameters':task['configuration']['default_parameters'],
        'parameter_rules':task['configuration']['spec']['parameters']}
    spec = task['configuration']['spec']
    result['project'] = ({'entrypoint':spec['path'], **spec['project'],
        'entry_sha256':spec['sha256'], 'max_runtime_seconds':spec['max_runtime_seconds'],
        'max_log_bytes':spec['max_log_bytes']} if 'project' in spec else None)
    return result


def public_plan(plan):
    snapshot=plan['snapshot']
    return {'plan_id':plan['id'],'task_id':snapshot['task_id'],'name':snapshot['name'],
        'experiment_id':snapshot['experiment_id'],'server_id':snapshot['server_id'],
        'script_id':snapshot['script_id'],'parameters':snapshot['parameters'],'run_id':plan['run_id'],
        'launch_claimed':plan['launch_turn_id'] is not None}


class TrainingTaskService:
    def __init__(self,repository,experiments,runs,servers,runner,*,source_transport=None):
        self.repository,self.experiments,self.runs,self.servers,self.runner=repository,experiments,runs,servers,runner
        self.source_transport = source_transport

    @logged_operation('training_task_service.project_source')
    async def project_source(self, user_id, action, request):
        model = {'list':ProjectDirectoryRequest,'read':ProjectFileRequest,'environments':ProjectEnvironmentRequest,'resources':ProjectEnvironmentRequest}.get(action)
        if model is None:
            raise ValueError('只允许列目录、读取文件或发现 Python 环境。')
        args = model.model_validate(request)
        profile = await self.servers.get(user_id, args.server_id.hex)
        if self.source_transport is None:
            raise SourceProtocolError('项目读取通道尚未配置。')
        credentials = self.servers.cipher.decrypt(user_id, profile)
        try:
            async with asyncio.timeout(30):
                data = await self.source_transport.request(profile, credentials, {
                    'action':action, 'work_dir':profile.work_dir,
                    **args.model_dump(exclude={'server_id'}, exclude_none=True)})
        finally:
            credentials.clear()
        return {'server_id':profile.server_id, 'work_dir':profile.work_dir, **data}

    @logged_operation('training_task_service.list_servers')
    async def list_servers(self, user_id, *, before_id=None, limit=5):
        page = await self.servers.list(user_id, limit=limit, before_id=before_id.hex if before_id else None)
        return {'items':[{'server_id':row.server_id, 'name':row.name, 'work_dir':row.work_dir} for row in page.items],
                'next_cursor':page.next_cursor}

    @logged_operation('training_task_service.create')
    async def create(self,user_id,request):
        request=TaskRequest.model_validate(request)
        experiment=await asyncio.to_thread(self.experiments.get_experiment,user_id,request.experiment_id.hex)
        if experiment.kind!='training':raise ValueError('训练任务需要关联 training 类型的实验。')
        server=await self.servers.get(user_id,request.server_id.hex)
        script_id = request.script_id
        if request.project is not None:
            script_id = 'project_' + uuid4().hex
            preliminary = request.project.spec('0'*64)
            RunnerSettings(json.dumps({script_id:preliminary})).resolve(script_id, request.default_parameters)
            entry = await self.project_source(user_id, 'read', {
                'server_id':request.server_id, 'path':request.project.entrypoint, 'line_count':1})
            if request.project.main_network:
                await self.project_source(user_id,'read',{'server_id':request.server_id,'path':request.project.main_network,'line_count':1})
            spec = request.project.spec(entry['file_sha256'])
            parameters = request.default_parameters
        else:
            spec,parameters=self.runner.settings.resolve(script_id,request.default_parameters)
        configuration={'spec':spec,'default_parameters':parameters,'server_binding':server.binding(),
            'evaluation_sha256':digest(experiment.evaluation)}
        if len(json.dumps(configuration, ensure_ascii=False).encode()) > 32768:
            raise ValueError('项目配置超过任务存储预算。')
        task=await asyncio.to_thread(self.repository.create_task,user_id,request.name,request.experiment_id.hex,
            request.server_id.hex,script_id,configuration)
        return public_task(task)

    @logged_operation('training_task_service.get')
    async def get(self,user_id,task_id):
        return public_task(await asyncio.to_thread(self.repository.get_task,user_id,str(task_id)))

    @logged_operation('training_task_service.list')
    async def list(self,user_id,**options):
        return await asyncio.to_thread(self.repository.list_tasks,user_id,**options)

    @logged_operation('training_task_service.delete')
    async def delete(self,user_id,task_id):
        await asyncio.to_thread(self.repository.delete_task,user_id,str(task_id))

    async def mode(self,user_id,session_id,attempt):
        question=await asyncio.to_thread(self.repository.question,user_id,session_id,attempt)
        return training_mode(question)

    async def _current(self,user_id,task):
        # 所有权与版本必须重新查询；注册过不等于永远有效。
        server=await self.servers.get(user_id,task['server_id'])
        experiment=await asyncio.to_thread(self.experiments.get_experiment,user_id,task['experiment_id'])
        configuration=task['configuration']
        current=(configuration['spec'] if 'project' in configuration['spec']
                 else self.runner.settings.registry().get(task['script_id']))
        if (current!=configuration['spec'] or server.binding()!=configuration['server_binding']
                or digest(experiment.evaluation)!=configuration['evaluation_sha256'] or experiment.kind!='training'):
            raise TrainingConflictError('任务配置已变化，请配置新的任务版本。')

    @logged_operation('training_task_service.prepare')
    async def prepare(self,user_id,session_id,attempt,task_id,overrides):
        task=await asyncio.to_thread(self.repository.get_task,user_id,str(task_id))
        await self._current(user_id,task)
        parameters={**task['configuration']['default_parameters'],**overrides}
        _,parameters=RunnerSettings(json.dumps({task['script_id']:task['configuration']['spec']})).resolve(task['script_id'],parameters)
        snapshot={key:task[key] for key in ('task_id','name','experiment_id','server_id','script_id')}
        snapshot.update(parameters=parameters,configuration_sha256=digest(task['configuration']))
        plan=await asyncio.to_thread(self.repository.prepare,user_id,session_id,attempt,snapshot)
        return public_plan(plan)

    @logged_operation('training_task_service.start')
    async def start(self,user_id,session_id,attempt,plan_id=None):
        plan=await asyncio.to_thread(self.repository.get_plan,user_id,session_id,str(plan_id) if plan_id else None)
        task=await asyncio.to_thread(self.repository.get_task,user_id,plan['task_id'])
        await self._current(user_id,task)
        if digest(task['configuration'])!=plan['snapshot']['configuration_sha256']:
            raise TrainingConflictError('训练计划对应的任务配置已变化。')
        snapshot=plan['snapshot']
        RunnerSettings(json.dumps({snapshot['script_id']:task['configuration']['spec']})).resolve(snapshot['script_id'],snapshot['parameters'])
        # 原文授权 + 当前尝试校验 + 原子领取在同一个短事务内提交。
        plan=await asyncio.to_thread(self.repository.claim,user_id,session_id,attempt,plan['id'])
        created=await self.runs.create_run(user_id,snapshot['experiment_id'],plan['run_request_id'],snapshot['parameters'])
        run_id=created.run.run_id
        await asyncio.to_thread(self.repository.bind_run,user_id,session_id,plan['id'],run_id)
        await asyncio.to_thread(self.repository.check_attempt,user_id,session_id,attempt)
        try:
            execution=await self.runner.start(user_id,run_id,snapshot['server_id'],snapshot['script_id'])
        except UNAVAILABLE as error:
            log_failure(logger,error,operation='training_task_service.start_unconfirmed')
            return {'ok':False,'code':'START_UNCONFIRMED','run_id':run_id,'data':{
                **public_plan(plan),'run_id':run_id,'hint':'启动结果暂未确认；查询此 run 或重试同一计划，不能新建运行重跑。'}}
        return {'ok':True,'code':'STARTED','run_id':run_id,'data':{**public_plan(plan),
            'run_id':run_id,'execution':{key:execution['execution'].get(key) for key in ('run_id','status','exit_code','error_code')},
            'notice':'这是启动请求的结果，不代表训练已完成；重复此计划复用同一个运行。'}}

    async def execute(self,user_id,session_id,attempt,name,args):
        try:
            if name=='list_training_servers':data=await self.list_servers(user_id,**args.model_dump())
            elif name in {'list_training_project_files','read_training_project_file'}:
                data=await self.project_source(user_id,'list' if name=='list_training_project_files' else 'read',args.model_dump())
            elif name=='list_training_tasks':data=await self.list(user_id,**args.model_dump())
            elif name=='get_training_task':data=await self.get(user_id,args.task_id.hex)
            elif name=='prepare_training_run':data=await self.prepare(user_id,session_id,attempt,args.task_id.hex,args.parameters)
            else:return await self.start(user_id,session_id,attempt,args.plan_id.hex if args.plan_id else None)
            return {'ok':True,'code':'PLAN_READY' if name=='prepare_training_run' else 'OK','data':data}
        except TrainingInstructionRequired:
            return {'ok':False,'code':'START_INSTRUCTION_REQUIRED','data':{'hint':'请明确说“启动 任务名称”；已有计划后可说“执行这个训练”。'}}
        except (TrainingNotFoundError,ExperimentNotFoundError,ServerNotFoundError):
            return {'ok':False,'code':'NOT_FOUND','data':{'hint':'训练任务或计划不存在或不可访问。'}}
        except (TrainingConflictError,RunnerConflictError,RunRequestConflictError):
            return {'ok':False,'code':'TRAINING_CONFLICT','data':{'hint':'计划或配置冲突；查询已有运行，修改配置请用新的提问准备新计划。'}}
        except RunnerScriptNotAllowedError:
            return {'ok':False,'code':'SCRIPT_NOT_ALLOWED','data':{'hint':'该训练入口尚未配置，不能执行任意命令。'}}
        except SourceReadError as error:
            return {'ok':False,'code':'SOURCE_'+error.code,'data':{'hint':'项目文件未读取成功，不能猜测入口或参数。'}}
        except ValueError:
            return {'ok':False,'code':'INVALID_ARGUMENTS','data':{'hint':'训练参数不符合任务约束，请查询任务详情。'}}
        except (*UNAVAILABLE, SourceProtocolError, TimeoutError) as error:
            log_failure(logger,error,operation='training_task_service.execute')
            return {'ok':False,'code':'TEMPORARILY_UNAVAILABLE','data':{'hint':'暂时无法操作，请稍后重试同一计划。'}}
