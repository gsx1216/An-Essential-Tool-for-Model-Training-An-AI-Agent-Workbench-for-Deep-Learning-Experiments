"""Runner 业务：先持久化绑定，再进行远端幂等启动；不把网络异常当作训练失败。"""

import asyncio
import json
from pathlib import PurePosixPath

from app.errors import RunnerConflictError
from app.logging_config import logged_operation
from app.models.experiment_run import normalize_run_uuid
from app.models.runner import RunnerSettings, script_identifier
from app.models.server_connection import normalize_server_id
from app.validators import validate_positive_integer


def public_execution(record):
    result = {key: record[key] for key in ('run_id', 'server_id', 'script_id', 'status', 'exit_code', 'error_code', 'created_at', 'checked_at')}
    launch = record.get('launch', {})
    work = launch.get('work_dir')
    result['output_directory'] = str(PurePosixPath(work) / launch.get('output_directory', 'training_outputs') / record['run_id']) if work else None
    return result


class RunnerService:
    def __init__(self, repository, runs, servers, transport, settings, *, revisions=None, tasks=None):
        self.repository, self.runs, self.servers = repository, runs, servers
        self.transport, self.settings = transport, settings
        self.revisions = revisions
        self.tasks = tasks

    async def resolve_script(self, user_id, script_id, parameters, *, server_id=None):
        if script_id in self.settings.registry():
            return self.settings.resolve(script_id, parameters)
        from app.models.training_task import TrainingNotFoundError
        from app.errors import RunnerScriptNotAllowedError
        if self.tasks is None:
            raise RunnerScriptNotAllowedError('训练入口尚未配置。')
        try:
            task = await asyncio.to_thread(self.tasks.get_by_script, user_id, script_id)
        except TrainingNotFoundError as error:
            raise RunnerScriptNotAllowedError('训练入口不存在或不可访问。') from error
        spec = task['configuration']['spec']
        if 'project' not in spec:
            raise RunnerScriptNotAllowedError('部署训练入口已撤销。')
        if server_id is not None and task['server_id'] != server_id:
            raise RunnerConflictError('用户项目必须使用保存配置时的服务器。')
        profile = await self.servers.get(user_id, task['server_id'])
        if task['configuration']['server_binding'] != profile.binding():
            raise RunnerConflictError('用户项目的服务器配置已经变化。')
        return RunnerSettings(json.dumps({script_id:spec})).resolve(script_id, parameters)

    @logged_operation('runner_service.start')
    async def start(self, user_id, run_id, server_id, script_id):
        user_id = validate_positive_integer(user_id, '用户 ID'); run_id = normalize_run_uuid(run_id)
        server_id = normalize_server_id(server_id); script_id = script_identifier(script_id)
        if script_id not in self.settings.registry() and self.tasks is None:
            from app.errors import RunnerScriptNotAllowedError
            raise RunnerScriptNotAllowedError('脚本尚未登记。')
        existing = await asyncio.to_thread(self.repository.get, user_id, run_id, required=False)
        if existing is not None:
            if existing['server_id'] != server_id or existing['script_id'] != script_id:
                raise RunnerConflictError('运行已绑定其他执行目标。')
            if script_id not in self.settings.registry():
                spec, _ = await self.resolve_script(user_id, script_id, existing['launch']['parameters'],
                                                    server_id=server_id)
                if spec != existing['launch']['spec']:
                    raise RunnerConflictError('用户项目配置与启动快照不一致。')
            record = existing
        else:
            run = await asyncio.to_thread(self.runs.get_run, user_id, run_id)
            profile = await self.servers.get(user_id, server_id)
            spec, parameters = await self.resolve_script(user_id, script_id, run.parameters, server_id=server_id)
            launch = {'version': 1, 'run_id': run_id, 'user_id': user_id, 'server_id': server_id,
                      'server_binding': profile.binding(), 'script_id': script_id, 'work_dir': profile.work_dir,
                      'spec': spec, 'parameters': parameters}
            launch['output_directory'] = self.settings.output_directory
            if self.revisions is not None:
                revision = await asyncio.to_thread(self.revisions.get_revision, user_id, run_id, required=False)
                if revision is not None:
                    binding = revision['binding']
                    if (binding['server_id'] != server_id or binding['script_id'] != script_id
                            or binding['spec'] != spec or binding['server_binding'] != profile.binding()
                            or binding['parameters'] != parameters):
                        raise RunnerConflictError('修改后运行必须使用已冻结的目标、入口和参数。')
                    for key in ('work_dir','output_directory','revision','revision_helper_source','revision_adapter_source'):
                        if key in binding: launch[key] = binding[key]
            record = await asyncio.to_thread(self.repository.claim, user_id, run_id, profile, script_id, launch)
        # 数据库已是终态则不再发起 start，更不重新启动。
        if record['status'] in {'succeeded', 'failed', 'cancelled'}:
            return {'execution': public_execution(record), 'observation': None}
        return await self._remote(user_id, run_id, record, 'start')

    @logged_operation('runner_service.get')
    async def get(self, user_id, run_id):
        return public_execution(await asyncio.to_thread(self.repository.get, user_id, run_id))

    @logged_operation('runner_service.operate')
    async def operate(self, user_id, run_id, action, *, offset=0, limit=4096):
        user_id = validate_positive_integer(user_id, '用户 ID'); run_id = normalize_run_uuid(run_id)
        if action not in {'status', 'stop', 'logs'}:
            raise ValueError('Runner 操作不受支持。')
        if type(offset) is not int or not 0 <= offset <= 10485760 or type(limit) is not int or not 1 <= limit <= 16384:
            raise ValueError('日志读取范围不符合要求。')
        record = await asyncio.to_thread(self.repository.get, user_id, run_id)
        return await self._remote(user_id, run_id, record, action, offset=offset, limit=limit)

    async def _remote(self, user_id, run_id, record, action, **options):
        profile = await self.servers.get(user_id, record['server_id'])
        if profile.binding() != record['launch']['server_binding']:
            raise RunnerConflictError('服务器配置与启动快照不一致。')
        credentials = self.servers.cipher.decrypt(user_id, profile)
        try:
            observation = await self.transport.request(profile, credentials,
                {'action': action, 'job_id': record['job_id'], 'digest': record['launch_digest'], 'launch': record['launch'], **options})
        finally:
            credentials.clear()
        await asyncio.to_thread(self.repository.observe, user_id, run_id, observation)
        current = await asyncio.to_thread(self.repository.get, user_id, run_id)
        return {'execution': public_execution(current), 'observation': observation}
