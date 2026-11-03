"""修改后运行：只读预检→保存计划→显式启动→复用监控报告→同协议对比。"""
import asyncio
import json
from pathlib import Path

from app.errors import ReportNotFoundError, RunnerConflictError
from app.logging_config import logged_operation
from app.models.experiment_run import normalize_run_uuid
from app.models.revision_run import RevisionConflict, compare_reports


class RevisionRunService:
    def __init__(self,repository,executions,runs,*,runner_factory,servers_factory,transport_factory,metrics_factory,settings):
        self.repository,self.executions,self.runs=repository,executions,runs
        self.runner_factory,self.servers_factory,self.transport_factory=runner_factory,servers_factory,transport_factory
        self.metrics_factory,self.settings=metrics_factory,settings

    async def public(self,user_id,record):
        run=await asyncio.to_thread(self.runs.get_run,user_id,record['run_id'])
        binding=record['binding'];revision=binding['revision']
        return {'run_id':record['run_id'],'original_run_id':record['original_run_id'],'change_id':record['change_id'],
            'status':run.status,'parameters':run.parameters,'evaluation':run.evaluation,
            'server_id':binding['server_id'],'script_id':binding['script_id'],'mode':revision['mode'],
            'candidate_directory':binding['work_dir'].rstrip('/')+'/code_changes/'+record['change_id']+'/project',
            'source_digest':revision['source_digest'],'source_file_count':len(revision['files']),
            'check_status':binding['check_status'],'created_at':record['created_at'],
            'notice':'准备不启动训练；incomplete 表示项目检查未全部完成。新运行使用冻结副本，原运行、原代码和评测快照保留。'}

    @logged_operation('revision_service.prepare')
    async def prepare(self,user_id,change_id,request_id,parameters,*,chat=None):
        change_id,request_id=normalize_run_uuid(change_id),normalize_run_uuid(request_id)
        from app.models.experiment_run import canonical_parameters
        from app.models.analysis import strict_json
        overrides=strict_json(canonical_parameters(parameters))
        saved=await asyncio.to_thread(self.repository.by_request,user_id,request_id,change_id,overrides)
        if saved:return await self.public(user_id,saved)
        change=await asyncio.to_thread(self.repository.get,user_id,change_id)
        if change['status']!='applied' or not change['result'] or change['result']['check_status']=='failed':
            raise RevisionConflict('需要已应用且没有失败检查的修改副本。')
        if chat:
            from app.models.revision_run import require_revision_start
            require_revision_start(await asyncio.to_thread(self.repository.question,user_id,chat),change_id,'')
        execution=await asyncio.to_thread(self.executions.get,user_id,change['run_id'])
        launch=execution['launch'];runner=self.runner_factory()
        mode='cwd' if 'project' in launch['spec'] else self.settings.mode(execution['script_id'])
        if 'revision' in launch:raise RevisionConflict('本阶段只从原始运行派生修改后训练；连续多级修改尚未支持。')
        original=await asyncio.to_thread(self.runs.get_run,user_id,change['run_id'])
        if original.status not in {'succeeded','failed','cancelled'} or original.evaluation is None:
            raise RevisionConflict('原运行必须已结束并有评测声明。')
        if 'project' in launch['spec']:
            spec,merged=await runner.resolve_script(user_id,execution['script_id'],original.parameters|overrides,
                                                   server_id=execution['server_id'])
        else:
            spec,merged=runner.settings.resolve(execution['script_id'],original.parameters|overrides)
        if spec!=launch['spec']:raise RevisionConflict('脚本登记版本已经改变，请重新核对原运行。')
        if launch['work_dir']!=change['_source']['work_dir'] or launch['server_binding']!=change['_source']['server_binding']:
            raise RevisionConflict('修改记录与执行目标不一致。')
        servers=self.servers_factory();profile=await servers.get(user_id,execution['server_id'])
        if profile.binding()!=launch['server_binding']:raise RunnerConflictError('服务器配置与原运行绑定不同。')
        credentials=servers.cipher.decrypt(user_id,profile)
        try:
            async with asyncio.timeout(45):
                inspected=await self.transport_factory().request(profile,credentials,{'work_dir':launch['work_dir'],
                    'change_id':change_id,'changed_files':change['result']['files'],'spec':spec,'mode':mode})
        finally:credentials.clear()
        helper=(Path(__file__).resolve().parents[1]/'runner/revision_source.py').read_text(encoding='utf-8')
        binding={key:launch[key] for key in ('server_id','server_binding','script_id','work_dir','spec')}
        binding.update(parameters=merged,overrides=overrides,output_directory=launch.get('output_directory','training_outputs'),
            check_status=change['result']['check_status'],revision={**inspected,'change_id':change_id,'original_run_id':change['run_id']},
            revision_helper_source=helper)
        # 与 Runner 的实际 ASCII 启动序列化口径一致，并为 manifest 外壳留出空间。
        launch_preview={key:value for key,value in binding.items() if key not in {'overrides','check_status'}}
        launch_preview.update(version=1,run_id='0'*32,user_id=user_id)
        if len(json.dumps(launch_preview,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode())>130816:
            raise ValueError('候选源码清单与启动配置超过执行器预算。')
        record=await asyncio.to_thread(self.repository.create_revision,user_id,change,request_id,overrides,binding,chat=chat)
        return await self.public(user_id,record)

    @logged_operation('revision_service.get')
    async def get(self,user_id,run_id):
        record=await asyncio.to_thread(self.repository.get_revision,user_id,normalize_run_uuid(run_id))
        return await self.public(user_id,record)

    @logged_operation('revision_service.list')
    async def list(self,user_id,change_id,limit=10):
        records=await asyncio.to_thread(self.repository.list_revisions,user_id,normalize_run_uuid(change_id),limit)
        return [await self.public(user_id,row) for row in records]

    @logged_operation('revision_service.start')
    async def start(self,user_id,run_id,*,chat=None):
        run_id=normalize_run_uuid(run_id)
        record=await asyncio.to_thread(self.repository.get_revision,user_id,run_id)
        if chat:await asyncio.to_thread(self.repository.claim_chat,user_id,run_id,chat)
        return await self.runner_factory().start(user_id,run_id,record['binding']['server_id'],record['binding']['script_id'])

    @logged_operation('revision_service.compare')
    async def compare(self,user_id,run_id):
        record=await asyncio.to_thread(self.repository.get_revision,user_id,normalize_run_uuid(run_id))
        original=await asyncio.to_thread(self.runs.get_run,user_id,record['original_run_id'])
        candidate=await asyncio.to_thread(self.runs.get_run,user_id,record['run_id'])
        reports=[]
        metrics=self.metrics_factory()
        for run in (original,candidate):
            try:reports.append(await metrics.report(user_id,run.run_id))
            except ReportNotFoundError:reports.append(None)
        return {'original_run_id':original.run_id,'run_id':candidate.run_id,'change_id':record['change_id'],
            **compare_reports(*reports,original,candidate)}
