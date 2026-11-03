"""事件配置、Outbox 原子入队条件、投递/ACK 边界及安全 HTTP；外部依赖模拟。"""
import asyncio
from datetime import datetime,timezone
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,MagicMock,patch

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError
from redis.exceptions import ConnectionError as RedisConnectionError

from app import config,database
from app.api.auth_dependencies import require_current_user
from app.api.error_handlers import request_validation_exception_handler
from app.api.event_routes import router,get_event_repository
from app.api.schemas import UserResponse
from app.errors import RunNotFoundError
from app.models.event_bus import EventBusSettings,EventPayloadError,EventBusyError,with_redis_password
from app.repositories.event_repository import decode_event,report_event_id
from app.repositories.metric_repository import MetricRepository
from app.services.event_delivery_service import EventRelayService,AnalysisEventReceiver

RUN='a'*32;EVENT=report_event_id(RUN,1);TOKEN='b'*32


def event():
    data={'schema_version':2,'run_id':RUN,'status':'succeeded','collection_complete':True}
    text=json.dumps(data)
    return {'id':EVENT,'user_id':7,'run_id':RUN,'event_type':'training.report_ready','report_version':1,
            'report_json':text,'report_digest':hashlib.sha256(text.encode()).hexdigest(),
            'lease_token':TOKEN,'failure_count':0}


class ContractTests(unittest.TestCase):
    def test_settings_hide_connection_secret_and_validate_bounds(self):
        settings=EventBusSettings(redis_url='redis://:TEST_PRIVATE@localhost:6379/0')
        self.assertNotIn('TEST_PRIVATE',repr(settings))
        for options in ({'redis_url':'http://bad'},{'prefix':'../x'},{'lease_seconds':True},{'block_ms':0}):
            with self.subTest(options=options),self.assertRaises(ValueError):EventBusSettings(**options)

    def test_config_reads_env_without_logging_url(self):
        with patch.object(config,'load_dotenv'),patch.dict('os.environ',{
            'REDIS_URL':'redis://:PRIVATE@localhost/2','AGENT_EVENT_PREFIX':'test_events','AGENT_EVENT_BLOCK_MS':'100'},clear=True):
            settings=config.get_event_bus_settings()
        self.assertEqual(settings.block_ms,100);self.assertEqual(settings.stream,'test_events:agent-events')
        self.assertNotIn('PRIVATE',repr(settings))

    def test_prompt_password_is_encoded_only_in_new_in_memory_settings(self):
        from urllib.parse import unquote,urlsplit
        original=EventBusSettings(redis_url='redis://localhost:6379/2')
        updated=with_redis_password(original,'TEST:#@ password')
        self.assertEqual(original.redis_url,'redis://localhost:6379/2')
        self.assertEqual(unquote(urlsplit(updated.redis_url).password),'TEST:#@ password')
        self.assertNotIn('TEST',repr(updated))
        self.assertEqual(urlsplit(updated.redis_url).path,'/2')

    def test_report_snapshot_digest_run_binding_and_completeness_are_enforced(self):
        self.assertEqual(decode_event(event())['report']['run_id'],RUN)
        for update in ({'report_digest':'0'*64},{'run_id':'c'*32},{'event_type':'other'}):
            with self.subTest(update=update),self.assertRaises(EventPayloadError):decode_event({**event(),**update})

    def test_event_id_is_stable_per_report_version(self):
        self.assertEqual(report_event_id(RUN,1),EVENT)
        self.assertNotEqual(report_event_id(RUN,2),EVENT)

    def test_only_complete_terminal_training_report_is_enqueued(self):
        repo=MetricRepository('TEST_ONLY');cursor=MagicMock()
        run={'run_id':RUN,'user_id':7,'experiment_kind':'training'}
        report={'version':1,'data':{'schema_version':2,'run_id':RUN,'status':'succeeded','collection_complete':True}}
        repo._enqueue_report_event(cursor,run,report)
        self.assertEqual(cursor.execute.call_count,1)
        self.assertEqual(cursor.execute.call_args.args[1][0],EVENT)
        for changed_run,changed_data in (({'experiment_kind':'script'},{}),({}, {'status':'running'}),({}, {'collection_complete':False})):
            cursor.reset_mock()
            repo._enqueue_report_event(cursor,{**run,**changed_run},{**report,'data':{**report['data'],**changed_data}})
            cursor.execute.assert_not_called()

    def test_selective_migration_never_runs_full_init_or_redis(self):
        connection=MagicMock();connection.__enter__.return_value=connection
        with patch('sys.argv',['app.database','--event-bus-schema']),patch.object(database,'get_mysql_password',return_value='TEST'), \
             patch.object(database,'get_connection',return_value=connection),patch.object(database,'_ensure_event_bus') as ensure, \
             patch.object(database,'initialize_database') as full,patch('builtins.print'):
            database.main()
        ensure.assert_called_once_with(connection);full.assert_not_called()


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo=MagicMock();self.repo.claim.return_value=event()
        self.bus=SimpleNamespace(publish=AsyncMock(return_value='123-0'),read=AsyncMock(),ack=AsyncMock(),dead_letter=AsyncMock())
        self.relay=EventRelayService(self.repo,self.bus,EventBusSettings())
        self.receiver=AnalysisEventReceiver(self.repo,self.bus,'analysis_test')
        self.bus.read.return_value=('123-0',{'schema_version':'1','event_id':EVENT})

    async def test_no_due_event_does_not_touch_redis(self):
        self.repo.claim.return_value=None
        self.assertFalse(await self.relay.tick());self.bus.publish.assert_not_awaited()

    async def test_relay_publishes_reference_only_and_settles_after_response(self):
        await self.relay.tick()
        self.bus.publish.assert_awaited_once_with(EVENT)
        self.assertEqual(self.repo.settle.call_args.kwargs['redis_id'],'123-0')
        self.assertEqual(self.repo.settle.call_args.kwargs['status'],'sent')

    async def test_redis_failure_retries_then_attention(self):
        self.bus.publish.side_effect=RedisConnectionError('PRIVATE')
        await self.relay.tick()
        self.assertEqual(self.repo.settle.call_args.kwargs['status'],'pending')
        self.assertEqual(self.repo.settle.call_args.kwargs['error_code'],'REDIS_UNAVAILABLE')
        self.repo.claim.return_value={**event(),'failure_count':7}
        await self.relay.tick()
        self.assertEqual(self.repo.settle.call_args.kwargs['status'],'attention')

    async def test_cancel_never_marks_event_sent(self):
        self.bus.publish.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.relay.tick()
        self.repo.settle.assert_not_called()

    async def test_bad_db_snapshot_is_not_published(self):
        self.repo.claim.return_value={**event(),'report_digest':'0'*64}
        await self.relay.tick();self.bus.publish.assert_not_awaited()
        self.assertEqual(self.repo.settle.call_args.kwargs['error_code'],'PAYLOAD_INVALID')

    async def test_receiver_commits_before_ack(self):
        calls=[];self.repo.receive.side_effect=lambda *args:calls.append('commit')
        async def ack(*args):calls.append('ack')
        self.bus.ack.side_effect=ack
        await self.receiver.tick()
        self.assertEqual(calls,['commit','ack'])
        self.repo.receive.assert_called_once_with(EVENT)

    async def test_database_failure_leaves_pending_without_ack_or_deadletter(self):
        self.repo.receive.side_effect=MySQLError('PRIVATE')
        with self.assertRaises(MySQLError):await self.receiver.tick()
        self.bus.ack.assert_not_awaited();self.bus.dead_letter.assert_not_awaited()

    async def test_ack_failure_can_replay_the_same_event(self):
        self.bus.ack.side_effect=[RedisConnectionError(),1]
        with self.assertRaises(RedisConnectionError):await self.receiver.tick()
        await self.receiver.tick()
        self.assertEqual(self.repo.receive.call_count,2)

    async def test_tampered_identity_fields_are_rejected_before_database(self):
        self.bus.read.return_value=('123-0',{'schema_version':'1','event_id':EVENT,'user_id':'99'})
        await self.receiver.tick()
        self.repo.receive.assert_not_called();self.bus.dead_letter.assert_awaited_once()


class EventApiTests(unittest.TestCase):
    def setUp(self):
        self.repo=MagicMock();self.repo.list.return_value=[]
        self.app=FastAPI();self.app.include_router(router)
        self.app.add_exception_handler(RequestValidationError,request_validation_exception_handler)
        self.app.dependency_overrides[get_event_repository]=lambda:self.repo
        self.app.dependency_overrides[require_current_user]=lambda:UserResponse(user_id=7,username='test')
        self.client=TestClient(self.app);self.addCleanup(self.client.close)

    def test_lists_only_use_current_identity_and_validate_pagination(self):
        response=self.client.get(f'/runs/{RUN}/events?user_id=99')
        self.assertEqual(response.status_code,200)
        self.repo.list.assert_called_with(7,RUN,limit=20,offset=0)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(self.client.get(f'/runs/{RUN}/agent-tasks?limit=51').status_code,422)
        self.assertEqual(self.client.get('/runs/bad/events').status_code,422)

    def test_retry_returns_standard_uuid_and_does_not_accept_task_parameters(self):
        response=self.client.post(f'/runs/{RUN}/events/{EVENT}/retry')
        self.assertEqual(response.status_code,202)
        self.assertEqual(response.json()['event_id'],str(__import__('uuid').UUID(EVENT)))
        self.repo.retry.assert_called_once_with(7,RUN,EVENT)

    def test_errors_are_safe_and_ownership_is_not_disclosed(self):
        for error,status in ((RunNotFoundError('PRIVATE'),404),(EventBusyError('PRIVATE'),409),(MySQLError('PRIVATE'),503)):
            self.repo.retry.side_effect=error
            response=self.client.post(f'/runs/{RUN}/events/{EVENT}/retry')
            self.assertEqual(response.status_code,status);self.assertNotIn('PRIVATE',response.text)

    def test_no_login_rejects_before_repository(self):
        self.app.dependency_overrides.pop(require_current_user)
        self.assertEqual(self.client.get(f'/runs/{RUN}/events').status_code,401)
        self.repo.list.assert_not_called()
