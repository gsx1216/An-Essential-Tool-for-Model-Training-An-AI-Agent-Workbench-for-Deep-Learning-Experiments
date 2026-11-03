"""从本人执行绑定取得服务器和项目根目录；网络读取不持有 SQL 事务。"""

import asyncio
from datetime import datetime, timezone

from app.errors import RunnerConflictError
from app.logging_config import logged_operation
from app.models.source_code import ListSourceArguments, ReadSourceArguments, FileResponse, DirectoryResponse
from app.models.experiment_run import normalize_run_uuid
from app.validators import validate_positive_integer


class SourceCodeService:
    def __init__(self, executions, servers, transport, network_repository=None):
        self.executions, self.servers, self.transport = executions, servers, transport
        self.network_repository=network_repository

    async def main_network(self,user_id,run_id):
        user_id=validate_positive_integer(user_id,'用户 ID');run_id=normalize_run_uuid(run_id)
        record=await asyncio.to_thread(self.executions.get,user_id,run_id)
        selected=await asyncio.to_thread(self.network_repository.get,user_id,run_id) if self.network_repository else None
        if selected:return selected
        return {'path':record['launch']['spec'].get('project',{}).get('main_network'),'file_sha256':None,'selected_at':None}

    async def select_network(self,user_id,run_id,path):
        from app.models.source_code import MainNetworkRequest
        user_id=validate_positive_integer(user_id,'用户 ID');run_id=normalize_run_uuid(run_id)
        path=MainNetworkRequest(path=path).path
        data=await self.query(user_id,run_id,'read',path=path,line_count=1)
        if self.network_repository is None:raise RuntimeError('未组装网络选择仓储。')
        return await asyncio.to_thread(self.network_repository.save,user_id,run_id,path,data['file_sha256'])

    @logged_operation('source_code_service.read')
    async def query(self, user_id, run_id, action, **options):
        user_id = validate_positive_integer(user_id, '用户 ID')
        run_id = normalize_run_uuid(run_id)
        if action not in {'list', 'read'}:
            raise ValueError('仅支持列目录和读取源码。')
        model = ListSourceArguments if action == 'list' else ReadSourceArguments
        args = model.model_validate(options).model_dump(exclude={'run_id'}, exclude_none=True)
        # get 同时校验当前用户、运行和启动快照，失败时不会构造 SSH 会话。
        record = await asyncio.to_thread(self.executions.get, user_id, run_id)
        launch = record['launch']
        profile = await self.servers.get(user_id, record['server_id'])
        if profile.binding() != launch['server_binding']:
            raise RunnerConflictError('服务器配置与运行绑定不一致。')
        entry = launch['spec']['path']
        work_dir = launch['work_dir']
        if 'revision' in launch:
            work_dir += '/.experiment-assistant/' + record['job_id'] + '/revision_source'
        if action == 'read' and 'path' not in args:
            selected=await asyncio.to_thread(self.network_repository.get,user_id,run_id) if self.network_repository else None
            args['path'] = (selected or {}).get('path') or launch['spec'].get('project',{}).get('main_network') or entry
            if selected and selected.get('file_sha256') and 'expected_sha256' not in args:
                args['expected_sha256']=selected['file_sha256']
        credentials = self.servers.cipher.decrypt(user_id, profile)
        try:
            data = await self.transport.request(profile, credentials, {'action': action, **args,
                'work_dir': work_dir, 'excluded_directory': launch.get('output_directory', 'training_outputs')})
        finally:
            credentials.clear()
        data.update(run_id=run_id, retrieved_at=datetime.now(timezone.utc))
        if action == 'list':
            return DirectoryResponse(**data, entry_path=entry).model_dump(mode='json')
        expected = launch.get('revision',{}).get('files',{}).get(data['path'], launch['spec']['sha256'] if data['path']==entry else None)
        frozen = data['file_sha256'] == expected if 'revision' in launch and expected is not None else None
        match = data['file_sha256'] == launch['spec']['sha256'] if data['path']==launch['spec']['path'] else None
        return FileResponse(**data, training_entry_match=match, frozen_source_match=frozen).model_dump(mode='json')
