"""已选建议到独立代码副本；计划和应用分开，重试复用同一记录及远端结果。"""
import asyncio
import logging
from mysql.connector import Error as MySQLError
from openai import OpenAIError
from app.errors import ModelCredentialNotConfiguredError, ModelCredentialDecryptionError, ModelCredentialConfigurationError

from app.logging_config import logged_operation, log_failure
from app.models.analysis import compact
from app.models.code_change import (CodeChangeSettings, ChangeConflict, ChangeProtocolError,
    ChangeStorageError, POLICY, make_plan, change_mode)
from app.models.experiment_run import normalize_run_uuid
from app.models.research import content_hash
from app.models.chat_tools import request_size
from app.validators import validate_positive_integer
from app.runner.source_reader import SourceReadError

logger = logging.getLogger(__name__)


class CodeChangeService:
    def __init__(self, repository, executions, *, source_factory=None, servers_factory=None,
                 transport_factory=None, planner_factory=None, settings=None):
        self.repository, self.executions = repository, executions
        self.source_factory, self.servers_factory = source_factory, servers_factory
        self.transport_factory, self.planner_factory = transport_factory, planner_factory
        self.settings = settings or CodeChangeSettings()

    async def mode(self, user_id, chat):
        return change_mode(await asyncio.to_thread(self.repository.question, user_id, chat))

    async def get(self, user_id, change_id):
        return self.repository.public(await asyncio.to_thread(self.repository.get, user_id, change_id))

    async def list(self, user_id, run_id, **options):
        return await asyncio.to_thread(self.repository.list, user_id, run_id, **options)

    async def _fail(self, user_id, record, error):
        code = 'WORKER_CANCELLED' if isinstance(error, asyncio.CancelledError) else getattr(error, 'code', None)
        if code is None:
            code = ('KEY_UNAVAILABLE' if isinstance(error,(ModelCredentialNotConfiguredError,ModelCredentialDecryptionError,ModelCredentialConfigurationError))
                else 'STORAGE_ERROR' if isinstance(error,(MySQLError,ChangeStorageError)) else 'INVALID_MODEL_OUTPUT'
                if isinstance(error,ChangeProtocolError) else 'OPERATION_TIMEOUT' if isinstance(error,TimeoutError)
                else 'MODEL_UNAVAILABLE' if isinstance(error,OpenAIError) else 'EDIT_UNAVAILABLE' if record['plan'] is not None else 'INTERNAL_ERROR')
        try:
            await asyncio.shield(asyncio.to_thread(self.repository.finish, user_id, record['id'], record['lease_token'], error_code=code))
        except Exception as store_error:
            log_failure(logger, store_error, operation='code_change_service.save_failure')

    @logged_operation('code_change_service.prepare')
    async def prepare(self, user_id, run_id, task_id, index, request_id, *, chat=None):
        user_id = validate_positive_integer(user_id, '用户 ID')
        run_id, task_id, request_id = map(normalize_run_uuid, (run_id, task_id, request_id))
        if type(index) is not int or not 1 <= index <= 3: raise ValueError('建议编号应为 1～3。')
        # 重放不要求个人 Key、联网或再读远端，也不能换目标复用请求编号。
        prior = await asyncio.to_thread(self.repository.by_request, user_id, request_id, run_id, task_id, index)
        if prior and (prior['plan'] is not None or prior['lease_active']):
            return self.repository.public(prior)
        content = await asyncio.to_thread(self.repository.suggestion, user_id, run_id, task_id, index)
        selected = content['suggestions'][index-1]
        code_ids = {citation['evidence_id'] for citation in selected['code']}
        paper_ids = {citation['evidence_id'] for citation in selected['papers']}
        execution = await asyncio.to_thread(self.executions.get, user_id, run_id)
        launch = execution['launch']
        if 'revision' in launch:
            raise ChangeConflict('本阶段请在原始运行上准备独立修改；暂不对派生运行连续嵌套修改。')
        source = {'run_id': run_id, 'suggestion_task_id': task_id, 'suggestion_index': index, 'suggestion': selected,
            'provenance': content['source'], 'code_evidence': [v for v in content['code_evidence'] if v['evidence_id'] in code_ids],
            'paper_evidence': [v for v in content['paper_evidence'] if v['evidence_id'] in paper_ids],
            'facts': [v for v in content['facts'] if v['path'] in selected['evidence_paths']],
            'server_id': execution['server_id'], 'server_binding': launch['server_binding'], 'work_dir': launch['work_dir'],
            'script_id': launch['script_id'], 'excluded_directory': launch.get('output_directory', 'training_outputs'),
            'project_exclusions':launch['spec'].get('project',{}).get('exclude_directories',[])}
        record, claimed = await asyncio.to_thread(self.repository.prepare, user_id, run_id, task_id, index, request_id, source, chat=chat)
        if not claimed: return self.repository.public(record)
        try:
            async with asyncio.timeout(150):
                source = record['_source']
                codes = {row['evidence_id']: row for row in source['code_evidence']}
                files = {}
                for citation in source['suggestion']['code']:
                    old = codes[citation['evidence_id']]
                    path = old['path']
                    current = await self.source_factory().query(user_id, run_id, 'read', path=path,
                        start_line=citation['start_line'], line_count=60,
                        **({'expected_sha256': files[path]['file_sha256']} if path in files else {}))
                    if not current['lines']: raise ChangeConflict('当前源码窗口为空，请重新检查建议对应实现。')
                    row = files.setdefault(path, {key: current[key] for key in ('path', 'file_sha256')})
                    row['suggestion_sha256'] = old['file_sha256']
                    row.setdefault('windows', []).append(current['lines'])
                messages = [('system', POLICY), ('human', compact({'selected_suggestion': source['suggestion'],
                    'facts': source['facts'], 'paper_evidence': source['paper_evidence'], 'files': list(files.values())}))]
                if request_size(messages, []) > 49152: raise ChangeProtocolError('修改输入材料超过预算。')
                gateway = await self.planner_factory().create_for_user(user_id)
                try:
                    async with asyncio.timeout(self.settings.model_timeout_seconds):
                        response = await gateway.invoke(messages, json_mode=True, max_tokens=4096)
                    if type(response) is not dict: raise ChangeProtocolError('修改模型没有返回有效结构。')
                    exclusions = list(dict.fromkeys(self.settings.exclusions(source['script_id']) + source.get('project_exclusions',[])))
                    plan = make_plan(response.get('content'), files, self.settings.checks(source['script_id']),exclusions)
                finally:
                    try:
                        async with asyncio.timeout(5): await gateway.aclose()
                    except asyncio.CancelledError: raise
                    except Exception as close_error: log_failure(logger, close_error, operation='code_change_service.close_planner')
                await asyncio.to_thread(self.repository.finish, user_id, record['id'], record['lease_token'], plan=plan)
        except BaseException as error:
            await self._fail(user_id, record, error)
            raise
        return await self.get(user_id, record['id'])

    @logged_operation('code_change_service.apply')
    async def apply(self, user_id, change_id, *, chat=None):
        record, claimed = await asyncio.to_thread(self.repository.claim_apply, user_id, change_id, chat=chat)
        if not claimed: return self.repository.public(record)
        try:
            async with asyncio.timeout(120):
                source = record['_source']
                execution = await asyncio.to_thread(self.executions.get, user_id, record['run_id'])
                launch = execution['launch']
                servers = self.servers_factory()
                profile = await servers.get(user_id, source['server_id'])
                if (profile.binding() != source['server_binding'] or launch['server_binding'] != source['server_binding']
                    or launch['work_dir'] != source['work_dir'] or execution['server_id'] != source['server_id']):
                    raise ChangeConflict('服务器绑定已变化，不能向另一目标写入修改。','TARGET_CHANGED')
                credentials = servers.cipher.decrypt(user_id, profile)
                try:
                    result = await self.transport_factory().request(profile, credentials, {
                        'change_id': record['id'], 'plan': record['plan'], 'plan_digest': record['plan_digest'],
                        'work_dir': source['work_dir'], 'excluded_directory': source['excluded_directory']})
                finally:
                    credentials.clear()
                await asyncio.to_thread(self.repository.finish, user_id, record['id'], record['lease_token'], result=result)
        except BaseException as error:
            await self._fail(user_id, record, error)
            raise
        return await self.get(user_id, record['id'])
