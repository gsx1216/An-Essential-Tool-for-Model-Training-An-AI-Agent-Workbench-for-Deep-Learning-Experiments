"""页面明确点击后只处理本人的当前运行；不启用全局 Worker 或自动偏好。"""
import asyncio
from dataclasses import replace
import logging
from app.config import get_mysql_password,get_analysis_settings,get_suggestion_settings
from app.errors import ReportNotFoundError
from app.logging_config import log_failure,logged_operation
from app.models.analysis import AnalysisConflictError
from app.models.experiment_run import normalize_run_uuid
from app.repositories.metric_repository import MetricRepository
from app.repositories.event_repository import EventRepository,report_event_id
from app.validators import validate_positive_integer

logger=logging.getLogger(__name__)

class ManualAnalysisService:
    def __init__(self,metrics,events,source,analysis_builder=None,suggestion_builder=None):
        self.metrics,self.events,self.source=metrics,events,source
        self.analysis_builder,self.suggestion_builder=analysis_builder,suggestion_builder

    async def state(self,user_id,run_id):
        user_id=validate_positive_integer(user_id,'用户 ID');run_id=normalize_run_uuid(run_id)
        try:report=await asyncio.to_thread(self.metrics.report,user_id,run_id)
        except ReportNotFoundError:report=None
        network=await self.source.main_network(user_id,run_id)
        tasks=await asyncio.to_thread(self.events.list,user_id,run_id,tasks=True,limit=50)
        ready=bool(report and not report['is_stale'] and report['data'].get('collection_complete') is True and report['data'].get('status') in {'succeeded','failed','cancelled'})
        current=[t for t in tasks if report and t['report_version']==report['version']]
        running=any(t.get('lease_active') for t in current)
        message=('请等待训练结束并完成事实报告采集。' if not ready else
                 '请先选择主网络 Python 文件。' if not network['path'] else
                 '正在处理本次分析，请等待结果。' if running else
                 '已准备就绪。点击按钮后使用你已保存的模型 Key 生成本次代码建议。')
        return {'report_ready':ready,'main_network':network,'processing':running,'can_start':ready and bool(network['path']) and not running,'message':message}

    def _builders(self):
        from app.services.direct_code_advice import build_snapshot as build,build_direct as suggestions
        return self.analysis_builder or build,self.suggestion_builder or suggestions

    @logged_operation('manual_analysis.prepare')
    async def prepare(self,user_id,run_id):
        state=await self.state(user_id,run_id)
        if state['processing']:return {**state,'accepted':False}
        if not state['can_start']:raise AnalysisConflictError(state['message'])
        network=state['main_network']
        # 在入队前检查选定文件及哈希，读取不是模型调用。
        await self.source.query(user_id,run_id,'read',path=network['path'],line_count=1,
                                **({'expected_sha256':network['file_sha256']} if network.get('file_sha256') else {}))
        report=await asyncio.to_thread(self.metrics.report,user_id,run_id)
        event_id=report_event_id(run_id,report['version'])
        await asyncio.to_thread(self.events.receive,event_id)
        tasks=await asyncio.to_thread(self.events.list,user_id,run_id,tasks=True,limit=50)
        queued=False
        for task in tasks:
            if task['event_id']!=event_id:continue
            if task['status']=='queued':queued=True;continue
            if task.get('lease_active'):return {**state,'accepted':False,'processing':True}
            if task['status'] in {'failed','attention','running'} or task['agent_name']=='suggestion' and task['status']=='succeeded':
                build,build_suggestions=self._builders()
                stage=(build(replace(get_analysis_settings(),enabled=True)) if task['agent_name']=='analysis'
                       else build_suggestions(replace(get_suggestion_settings(),enabled=True)))
                repository=stage.repository.manual_for(user_id,run_id,event_id)
                try:await asyncio.to_thread(repository.task,user_id,run_id,task['task_id'],retry=True)
                except AnalysisConflictError:continue  # 已有具体结果时不覆盖、不重复调用。
                queued=True
        return {**state,'accepted':queued,'event_id':event_id,'message':'已提交代码建议，训练不会重启。' if queued else '本次已有分析与代码诊断结果，无需重复提交。'}

    @logged_operation('manual_analysis.execute')
    async def execute(self,user_id,run_id,event_id):
        try:
            build,build_suggestions=self._builders()
            analysis=build(replace(get_analysis_settings(),enabled=True))
            analysis.repository.manual_for(user_id,run_id,event_id)
            await analysis.tick(user_id=user_id,run_id=run_id)
            suggestions=build_suggestions(replace(get_suggestion_settings(),enabled=True))
            suggestions.repository.manual_for(user_id,run_id,event_id)
            await suggestions.tick(user_id=user_id,run_id=run_id)
        except Exception as error:
            log_failure(logger,error,operation='manual_analysis.execute')


def build_manual_service():
    from app.api.dependencies import get_source_code_service
    password=get_mysql_password()
    return ManualAnalysisService(MetricRepository(password),EventRepository(password),get_source_code_service())
