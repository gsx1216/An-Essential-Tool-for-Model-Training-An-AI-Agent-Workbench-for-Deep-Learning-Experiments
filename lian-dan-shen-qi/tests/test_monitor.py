"""监控策略、租约边界、取消恢复、无启动权限以及公开 API 契约。"""
import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,MagicMock,patch

from mysql.connector import Error as MySQLError

from app import database
from app.errors import (MetricSourceChangedError,MetricSourceUnavailableError,ServerAuthenticationError,
    ServerConnectionTimeoutError,RunnerConflictError,MetricSyncConflictError)
from app.models.monitor import MonitorSettings,MonitorLeaseLostError,MonitorBusyError
from app.services.monitor_service import MonitorService,LeaseRunner,error_policy,public_monitor
from app.workers.monitor_worker import run_worker

USER=7;RUN='a'*32;TOKEN='b'*32


def lease():return {'user_id':USER,'run_id':RUN,'lease_token':TOKEN,'failure_count':0}


class SettingsTests(unittest.TestCase):
    def test_bounds_types_and_timeout_lease_relationship(self):
        for values in ({'poll_seconds':True},{'max_pages':9},{'max_failures':0},{'step_timeout_seconds':115},
                       {'lease_seconds':20},{'max_backoff_seconds':1}):
            with self.subTest(values=values),self.assertRaises(ValueError):MonitorSettings(**values)

    def test_backoff_increases_and_caps(self):
        settings=MonitorSettings()
        self.assertEqual([settings.backoff(n) for n in range(1,9)],[5,10,20,40,80,160,300,300])

    def test_error_codes_classify_transient_and_configuration_errors(self):
        for error,code,permanent in ((ServerAuthenticationError(),'AUTHENTICATION_FAILED',True),
            (MetricSourceChangedError(),'SOURCE_CHANGED',True),(RunnerConflictError(),'CONFIGURATION_CHANGED',True),
            (ServerConnectionTimeoutError(),'TIMEOUT',False),(MetricSyncConflictError(),'SYNC_CONFLICT',False),
            (MySQLError(),'STORAGE_ERROR',False)):
            self.assertEqual(error_policy(error),(code,permanent))

    def test_monitor_public_projection_never_exposes_lease_or_owner(self):
        now=datetime.now(timezone.utc)
        row={'run_id':RUN,'status':'scheduled','attempts':0,'failure_count':0,'next_poll_at':now,'updated_at':now,
            'last_attempt_at':None,'last_success_at':None,'last_error_code':None,'report_version':None,
            'lease_active':False,'completed_at':None,'lease_token':TOKEN,'user_id':USER}
        result=public_monitor(row)
        self.assertNotIn('lease_token',result);self.assertNotIn('user_id',result)

    def test_migration_only_adds_queue_and_backfills_active_not_terminal_runs(self):
        connection=MagicMock();cursor=connection.cursor.return_value.__enter__.return_value
        database._ensure_run_monitors(connection)
        sql='\n'.join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn('CREATE TABLE IF NOT EXISTS',sql)
        self.assertIn("WHERE r.status IN ('queued','running')",sql)
        self.assertNotRegex(sql,r'(?m)^(DELETE|UPDATE)\s')


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo=MagicMock();self.repo.claim.return_value=lease();self.repo.renew.return_value=True;self.repo.settle.return_value=True
        self.execution=MagicMock();self.execution.get.return_value={'status':'running'}
        self.metrics=MagicMock()
        self.runner=SimpleNamespace(operate=AsyncMock(return_value={'observation':{'status':'running','stale':False}}),start=AsyncMock(),stop=AsyncMock())
        self.factory=MagicMock(return_value=self.runner)
        self.service=MonitorService(self.repo,self.execution,self.metrics,runner_factory=self.factory)

    async def test_no_job_does_not_construct_ssh(self):
        self.repo.claim.return_value=None
        self.assertFalse(await self.service.tick());self.factory.assert_not_called();self.repo.settle.assert_not_called()

    async def test_running_success_reschedules_with_normal_interval(self):
        metrics=SimpleNamespace(sync=AsyncMock(return_value={'stream':{'caught_up':True,'stale':False,'truncated':False}}),report=AsyncMock())
        with patch('app.services.monitor_service.MetricService',return_value=metrics):await self.service.tick()
        self.assertEqual(self.repo.settle.call_args.kwargs['status'],'scheduled')
        self.assertEqual(self.repo.settle.call_args.kwargs['delay'],15)
        metrics.report.assert_not_awaited();self.runner.start.assert_not_awaited();self.runner.stop.assert_not_awaited()

    async def test_backlog_reschedules_soon_instead_of_reading_unbounded_logs(self):
        metrics=SimpleNamespace(sync=AsyncMock(return_value={'stream':{'caught_up':False,'stale':False,'truncated':False}}),report=AsyncMock())
        with patch('app.services.monitor_service.MetricService',return_value=metrics):await self.service.tick()
        metrics.sync.assert_awaited_once_with(USER,RUN,max_pages=4)
        self.assertEqual(self.repo.settle.call_args.kwargs['delay'],1)

    async def test_failed_training_still_completes_monitor_with_factual_report(self):
        self.execution.get.return_value={'status':'failed'}
        metrics=SimpleNamespace(sync=AsyncMock(return_value={'stream':{'caught_up':True,'stale':False,'truncated':False}}),
            report=AsyncMock(return_value={'version':2,'is_stale':False,'data':{'collection_complete':True}}))
        with patch('app.services.monitor_service.MetricService',return_value=metrics):await self.service.tick()
        outcome=self.repo.settle.call_args.kwargs
        self.assertEqual(outcome['status'],'completed');self.assertEqual(outcome['report_version'],2)
        self.runner.start.assert_not_awaited()

    async def test_truncated_terminal_log_saves_partial_report_and_needs_attention(self):
        self.execution.get.return_value={'status':'succeeded'}
        metrics=SimpleNamespace(sync=AsyncMock(return_value={'stream':{'caught_up':True,'stale':False,'truncated':True}}),
            report=AsyncMock(return_value={'version':1,'is_stale':False,'data':{'collection_complete':False}}))
        with patch('app.services.monitor_service.MetricService',return_value=metrics):await self.service.tick()
        self.assertEqual(self.repo.settle.call_args.kwargs['status'],'attention')
        self.assertEqual(self.repo.settle.call_args.kwargs['error_code'],'LOG_TRUNCATED')

    async def test_missing_or_stale_status_retries_without_start_or_failure_of_training(self):
        for observed in ({'status':'missing','stale':True},{'status':'succeeded','stale':True}):
            self.runner.operate.return_value={'observation':observed}
            with patch('app.services.monitor_service.MetricService') as metrics:await self.service.tick();metrics.assert_not_called()
            self.assertEqual(self.repo.settle.call_args.kwargs['error_code'],'SOURCE_UNAVAILABLE')
            self.assertEqual(self.repo.settle.call_args.kwargs['status'],'scheduled')
        self.runner.start.assert_not_awaited()

    async def test_transient_error_backoff_then_attention(self):
        self.runner.operate.side_effect=ServerConnectionTimeoutError()
        await self.service.tick()
        self.assertEqual(self.repo.settle.call_args.kwargs['delay'],5)
        self.repo.claim.return_value={**lease(),'failure_count':7}
        await self.service.tick()
        self.assertEqual(self.repo.settle.call_args.kwargs['status'],'attention')
        self.assertEqual(self.repo.settle.call_args.kwargs['failure_count'],8)

    async def test_output_publication_failure_preserves_report_and_requires_attention(self):
        self.runner.operate.return_value={'observation':{'status':'succeeded','stale':False,
            'outputs':{'status':'unavailable','error_code':'OUTPUT_PUBLICATION_FAILED'}}}
        self.execution.get.return_value={'status':'succeeded'}
        with patch('app.services.monitor_service.MetricService') as factory:
            metrics=factory.return_value
            metrics.sync=AsyncMock(return_value={'stream':{'stale':False,'caught_up':True,'truncated':False}})
            metrics.report=AsyncMock(return_value={'version':2,'data':{'collection_complete':True},'is_stale':False})
            await self.service.tick()
        outcome=self.repo.settle.call_args.kwargs
        self.assertEqual(outcome['status'],'attention')
        self.assertEqual(outcome['error_code'],'OUTPUT_PUBLICATION_FAILED')
        self.assertEqual(outcome['report_version'],2)
        self.runner.start.assert_not_awaited()

    async def test_authentication_error_does_not_retry_credentials_repeatedly(self):
        self.runner.operate.side_effect=ServerAuthenticationError()
        await self.service.tick()
        self.assertEqual(self.repo.settle.call_args.kwargs['status'],'attention')
        self.assertEqual(self.repo.settle.call_args.kwargs['error_code'],'AUTHENTICATION_FAILED')

    async def test_lost_lease_stops_before_ssh_and_does_not_settle(self):
        self.repo.renew.return_value=False
        await self.service.tick()
        self.runner.operate.assert_not_awaited();self.repo.settle.assert_not_called()

    async def test_cancel_reschedules_only_the_current_lease(self):
        self.runner.operate.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.service.tick()
        self.repo.settle.assert_called_once_with(USER,RUN,TOKEN,status='scheduled',delay=1,failure_count=0,error_code='WORKER_CANCELLED')

    async def test_outer_timeout_is_retriable_and_does_not_start(self):
        self.service._perform=AsyncMock(side_effect=TimeoutError())
        await self.service.tick()
        self.assertEqual(self.repo.settle.call_args.kwargs['error_code'],'TIMEOUT');self.runner.start.assert_not_awaited()

    async def test_lease_runner_cannot_operate_other_user_or_start_stop(self):
        reader=LeaseRunner(self.runner,self.repo,lease(),MonitorSettings())
        for user,run,action in ((USER,RUN,'start'),(USER,RUN,'stop'),(8,RUN,'status'),(USER,'c'*32,'logs')):
            with self.assertRaises(ValueError):await reader.operate(user,run,action)
        self.runner.operate.assert_not_awaited()

    async def test_get_and_retry_metadata_do_not_construct_runner(self):
        self.repo.get.side_effect=RuntimeError('fake lookup')
        with self.assertRaises(RuntimeError):await self.service.get(USER,RUN)
        self.repo.retry.side_effect=MonitorBusyError()
        with self.assertRaises(MonitorBusyError):await self.service.retry(USER,RUN)
        self.execution.get.assert_called_once_with(USER,RUN);self.factory.assert_not_called()

    async def test_worker_once_returns_without_sleep_and_never_loops_forever(self):
        self.service.tick=AsyncMock(return_value=False)
        with patch('app.workers.monitor_worker.asyncio.sleep',new=AsyncMock()) as sleep:
            self.assertFalse(await run_worker(self.service,once=True));sleep.assert_not_awaited()
        self.repo.reconcile.assert_called_once_with()

    async def test_worker_once_surfaces_database_failure_as_nonzero_exit(self):
        self.service.tick=AsyncMock(side_effect=MySQLError())
        with self.assertRaises(MySQLError):await run_worker(self.service,once=True)


if __name__=='__main__':unittest.main()
