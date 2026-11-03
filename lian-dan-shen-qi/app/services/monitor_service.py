"""有界监控：查询状态、发布终态产物、采集日志及生成报告，不调用 start 或 stop。"""
import asyncio
import logging

from mysql.connector import Error as MySQLError

from app.errors import (MetricSourceChangedError,MetricSourceUnavailableError,MetricSyncConflictError,
    MetricStorageError,RunnerConflictError,RunnerProtocolError,ServerAuthenticationError,ServerHostKeyError,
    ServerAddressNotAllowedError,ServerCredentialDecryptionError,ServerStorageError,RunStorageError,
    ServerConnectionError,ServerBusyError)
from app.logging_config import logged_operation,log_failure
from app.models.monitor import MonitorLeaseLostError,MonitorResponse,MonitorSettings
from app.services.metric_service import MetricService

logger=logging.getLogger(__name__)
TERMINAL={'succeeded','failed','cancelled'}


def error_policy(error):
    permanent=((MetricSourceChangedError,'SOURCE_CHANGED'),(ServerAuthenticationError,'AUTHENTICATION_FAILED'),
        (ServerHostKeyError,'HOST_KEY_MISMATCH'),(ServerAddressNotAllowedError,'ADDRESS_NOT_ALLOWED'),
        (ServerCredentialDecryptionError,'CREDENTIALS_UNAVAILABLE'),(RunnerConflictError,'CONFIGURATION_CHANGED'),
        (RunnerProtocolError,'PROTOCOL_ERROR'))
    for kind,code in permanent:
        if isinstance(error,kind):return code,True
    for kind,code in ((TimeoutError,'TIMEOUT'),(MetricSourceUnavailableError,'SOURCE_UNAVAILABLE'),
        (MetricSyncConflictError,'SYNC_CONFLICT'),(ServerConnectionError,'CONNECTION_ERROR'),
        (ServerBusyError,'CONNECTION_ERROR'),(MySQLError,'STORAGE_ERROR'),(MetricStorageError,'STORAGE_ERROR'),
        (ServerStorageError,'STORAGE_ERROR'),(RunStorageError,'STORAGE_ERROR')):
        if isinstance(error,kind):return code,False
    return 'INTERNAL_ERROR',False


def public_monitor(record):return MonitorResponse.model_validate(record).model_dump()


class LeaseRunner:
    """状态/日志操作前续租；状态可发布终态产物，不暴露训练启动/停止方法。"""
    def __init__(self,runner,repository,lease,settings):
        self.runner,self.repository,self.lease,self.settings=runner,repository,lease,settings

    async def renew(self):
        row=self.lease
        saved=await asyncio.to_thread(self.repository.renew,row['user_id'],row['run_id'],row['lease_token'],self.settings.lease_seconds)
        if not saved:raise MonitorLeaseLostError('监控租约已失效，旧 Worker 不再推进任务。')

    async def operate(self,user_id,run_id,action,**options):
        if action not in {'status','logs'} or user_id!=self.lease['user_id'] or run_id!=self.lease['run_id']:
            raise ValueError('监控只能读取已领取的本人运行状态或日志。')
        await self.renew()
        return await self.runner.operate(user_id,run_id,action,**options)


class MonitorService:
    def __init__(self,repository,executions,metric_repository,*,runner_factory,settings=None):
        self.repository,self.executions,self.metric_repository=repository,executions,metric_repository
        self.runner_factory=runner_factory
        self.settings=settings or MonitorSettings()

    @logged_operation('monitor_service.get')
    async def get(self,user_id,run_id):
        return public_monitor(await asyncio.to_thread(self.repository.get,user_id,run_id))

    @logged_operation('monitor_service.retry')
    async def retry(self,user_id,run_id):
        # 先校验本人执行绑定；重试只登记采集，不重建运行、不重新训练。
        await asyncio.to_thread(self.executions.get,user_id,run_id)
        return public_monitor(await asyncio.to_thread(self.repository.retry,user_id,run_id))

    async def _perform(self,lease):
        user_id,run_id=lease['user_id'],lease['run_id']
        reader=LeaseRunner(self.runner_factory(),self.repository,lease,self.settings)
        observed=await reader.operate(user_id,run_id,'status')
        if observed['observation']['status']=='missing' or observed['observation']['stale']:
            raise MetricSourceUnavailableError('远端运行暂不可确认，不修改训练结果或重新启动。')
        metrics=MetricService(self.metric_repository,reader)
        result=await metrics.sync(user_id,run_id,max_pages=self.settings.max_pages)
        stream=result['stream']
        if stream['stale']:raise MetricSourceUnavailableError('指标来源暂不可确认。')
        execution=await asyncio.to_thread(self.executions.get,user_id,run_id)
        if execution['status'] in TERMINAL and stream['caught_up']:
            await reader.renew()
            report=await metrics.report(user_id,run_id,generate=True)
            if observed['observation'].get('outputs',{}).get('status')=='unavailable':
                return dict(status='attention',delay=0,error_code='OUTPUT_PUBLICATION_FAILED',
                            report_version=report['version'],success=True)
            if report['data']['collection_complete'] and not report['is_stale']:
                return dict(status='completed',delay=0,report_version=report['version'],success=True)
            if stream['truncated']:
                return dict(status='attention',delay=0,error_code='LOG_TRUNCATED',report_version=report['version'],success=True)
        return dict(status='scheduled',delay=self.settings.poll_seconds if stream['caught_up'] else 1,success=True)

    @logged_operation('monitor_service.tick',log_success=False)
    async def tick(self):
        lease=await asyncio.to_thread(self.repository.claim,self.settings.lease_seconds)
        if lease is None:return False
        identity=(lease['user_id'],lease['run_id'],lease['lease_token'])
        try:
            async with asyncio.timeout(self.settings.step_timeout_seconds):
                outcome=await self._perform(lease)
        except MonitorLeaseLostError:
            return True  # 已失效的令牌不能确认、失败或延后新 Worker 的任务。
        except asyncio.CancelledError:
            try:
                await asyncio.shield(asyncio.to_thread(self.repository.settle,*identity,status='scheduled',delay=1,
                    failure_count=lease['failure_count'],error_code='WORKER_CANCELLED'))
            except Exception as error:log_failure(logger,error,operation='monitor_service.cancel_cleanup')
            raise
        except Exception as error:
            code,permanent=error_policy(error)
            failures=min(50,lease['failure_count']+1)
            attention=permanent or failures>=self.settings.max_failures
            outcome=dict(status='attention' if attention else 'scheduled',delay=0 if attention else self.settings.backoff(failures),
                failure_count=failures,error_code=code)
            log_failure(logger,error,operation='monitor_service.step',level=logging.ERROR if attention else logging.WARNING)
        # 只有仍持有有效租约的 Worker 可以写任务终态；事实层 CAS/报告事务独立幂等。
        await asyncio.to_thread(self.repository.settle,*identity,**outcome)
        return True
