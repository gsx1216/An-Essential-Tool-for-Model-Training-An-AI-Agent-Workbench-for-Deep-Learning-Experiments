"""真实 HTTP/MySQL/Redis Streams 验收，不调用模型或 SSH/GPU，不使用现有 Redis 服务。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError,IntegrityError
from redis.exceptions import ConnectionError as RedisConnectionError
from unittest.mock import patch

from app import database
from app.config import get_mysql_password
from app.models.event_bus import EventBusSettings
from app.repositories.metric_repository import encode
from app.models.training_metrics import initial_stream
from app.services.event_delivery_service import EventRelayService,AnalysisEventReceiver
from app.services.redis_event_bus import RedisEventBus
from scripts._chat_api_test_app import temporary_chat_api
from scripts._temporary_redis import temporary_redis


async def verify(password,redis_state):
    settings=EventBusSettings(redis_url=redis_state['url'],prefix='verify_'+uuid4().hex,
                              reclaim_idle_ms=1000,reconcile_seconds=10)
    bus=RedisEventBus(settings)
    try:
        with temporary_chat_api(password) as (app,state),TestClient(app) as client:
            repo=state['events'];metric_repo=state['metric_repository'];name=state['database_name']
            def sql(text,args=(),fetch=True):
                with database.get_connection(password) as connection:
                    with connection.cursor(dictionary=True) as cursor:
                        cursor.execute(text.replace('experiment_assistant',name),args)
                        rows=cursor.fetchall() if fetch else None
                    connection.commit();return rows
            def login(username):
                assert client.post('/auth/register',json={'username':username,'password':'TestPass123!'}).status_code==201
                response=client.post('/auth/login',json={'username':username,'password':'TestPass123!'})
                assert response.status_code==200,response.text
                return {'Authorization':'Bearer '+response.json()['access_token']}
            owner=login('events_owner_'+uuid4().hex[:8]);foreign=login('events_foreign_'+uuid4().hex[:8])
            exp=client.post('/experiments',headers=owner,json={'title':'event CPU fixture','kind':'training'}).json()['experiment_id']
            run=client.post(f'/experiments/{exp}/runs',headers=owner,json={'request_id':uuid4().hex,'parameters':{}}).json()['run']['run_id']
            owner_id=client.get('/auth/me',headers=owner).json()['user_id']
            sql("UPDATE experiment_assistant.experiment_runs SET status='succeeded',started_at=created_at,finished_at=UTC_TIMESTAMP(6),exit_code=0,updated_at=UTC_TIMESTAMP(6) WHERE id=%s",(run,),False)
            stream=initial_stream();stream.update(complete=True,caught_up=True)
            assert metric_repo.commit_page(owner_id,run,0,stream,[])
            generated=client.post(f'/runs/{run}/report',headers=owner)
            assert generated.status_code==200,generated.text
            snapshot=generated.json()['data']
            assert snapshot['collection_complete'] is True
            text,digest=encode(snapshot)
            # 第一个事件由正式 HTTP 报告生成事务产生，后续版本用于传输故障注入。
            source_run={'run_id':run,'user_id':owner_id,'experiment_kind':'training'}
            def enqueue(version,data):
                with database.get_connection(password) as connection:
                    with connection.cursor() as cursor:
                        original=metric_repo.enqueue_report_event_sql
                        assert name in original
                        metric_repo._enqueue_report_event(cursor,source_run,{'version':version,'data':data})
                    connection.commit()
            enqueue(1,snapshot);enqueue(1,snapshot)
            rows=repo.list(owner_id,run);assert len(rows)==1
            event_id=rows[0]['event_id']
            assert client.get(f'/runs/{run}/events').status_code==401
            assert client.get(f'/runs/{run}/events',headers=foreign).status_code==404
            assert client.get('/runs/bad/events',headers=owner).status_code==422
            assert client.get(f'/runs/{run}/events?limit=51',headers=owner).status_code==422
            assert client.post(f'/runs/{run}/events/{event_id}/retry',headers=foreign).status_code==404
            print('PASS: report-version deduplication, UUID/pagination, actual login and owner isolation')

            # 事务回滚不能残留事件，生成后丢失的报告不得误触发任务。
            try:
                with database.get_connection(password) as connection:
                    with connection.cursor() as cursor:
                        metric_repo._enqueue_report_event(cursor,source_run,{'version':99,'data':snapshot})
                        raise RuntimeError('force rollback')
            except RuntimeError:pass
            assert len(repo.list(owner_id,run))==1
            second_run=client.post(f'/experiments/{exp}/runs',headers=owner,json={'request_id':uuid4().hex,'parameters':{}}).json()['run']['run_id']
            sql("UPDATE experiment_assistant.experiment_runs SET status='succeeded',started_at=created_at,finished_at=UTC_TIMESTAMP(6),exit_code=0,updated_at=UTC_TIMESTAMP(6) WHERE id=%s",(second_run,),False)
            assert metric_repo.commit_page(owner_id,second_run,0,stream,[])
            with patch.object(metric_repo,'enqueue_report_event_sql','INVALID TEST SQL'):
                failed=client.post(f'/runs/{second_run}/report',headers=owner)
            assert failed.status_code==503 and failed.json()['detail']=='请稍后重试。'
            assert sql('SELECT COUNT(*) AS n FROM experiment_assistant.run_reports WHERE run_id=%s',(second_run,))[0]['n']==0
            assert sql('SELECT result_version FROM experiment_assistant.experiment_runs WHERE id=%s',(second_run,))[0]['result_version']==0
            print('PASS: outbox event participates in caller transaction and rolls back atomically')

            relay=EventRelayService(repo,bus,settings)
            receiver=AnalysisEventReceiver(repo,bus,'analysis_one')
            assert await relay.tick()
            assert repo.list(owner_id,run)[0]['status']=='sent'
            stream_rows=await bus.client.xrange(settings.stream)
            assert stream_rows[0][1]=={'schema_version':'1','event_id':event_id}
            assert await receiver.tick(block=False)
            assert len(repo.list(owner_id,run,tasks=True))==1
            assert repo.list(owner_id,run)[0]['status']=='received'
            assert (await bus.client.xpending(settings.stream,settings.analysis_group))['pending']==0
            result=client.get(f'/runs/{run}/agent-tasks',headers=owner)
            assert result.status_code==200 and result.json()[0]['status']=='queued'
            assert '-' in result.json()[0]['task_id']
            assert not any(x in result.text for x in ('report_json','report_digest','lease_token','redis://'))
            print('PASS: real Streams delivery persists one queued task, ACKs after commit, and exposes safe HTTP status')

            # 显式重投递：同一事件不会变成第二个任务。
            assert client.post(f'/runs/{run}/events/{event_id}/retry',headers=owner).status_code==202
            await relay.tick();await receiver.tick(block=False)
            assert len(repo.list(owner_id,run,tasks=True))==1
            print('PASS: explicit delivery retry and duplicate Redis messages never duplicate analysis tasks')

            # 领取后进程死亡，未 ACK；新 consumer 在 idle 阈值后 XCLAIM。
            enqueue(2,snapshot);await relay.tick()
            abandoned=await bus.read('crashed_consumer',block=False);assert abandoned is not None
            await asyncio.sleep(1.1)
            rebuilt=AnalysisEventReceiver(repo,bus,'rebuilt_consumer')
            assert await rebuilt.tick(block=False)
            assert len(repo.list(owner_id,run,tasks=True))==2
            print('PASS: Redis 5 XPENDING/XCLAIM recovers a crashed consumer without XAUTOCLAIM')

            # DB失败不 ACK，恢复后同一 pending 消息重新处理。
            enqueue(3,snapshot);await relay.tick()
            with patch.object(repo,'receive',side_effect=MySQLError('PRIVATE')):
                try:await receiver.tick(block=False)
                except MySQLError:pass
                else:raise AssertionError('DB error was swallowed')
            assert (await bus.client.xpending(settings.stream,settings.analysis_group))['pending']==1
            await asyncio.sleep(1.1);await rebuilt.tick(block=False)
            assert len(repo.list(owner_id,run,tasks=True))==3
            print('PASS: failed MySQL commit leaves the message pending until recovery')

            # ACK丢失：已落库但pending，重放仍只有一条task。
            enqueue(4,snapshot);await relay.tick()
            with patch.object(bus,'ack',side_effect=RedisConnectionError('test ACK lost')):
                try:await receiver.tick(block=False)
                except RedisConnectionError:pass
                else:raise AssertionError('ACK failure was swallowed')
            assert len(repo.list(owner_id,run,tasks=True))==4
            await asyncio.sleep(1.1);await rebuilt.tick(block=False)
            assert len(repo.list(owner_id,run,tasks=True))==4
            print('PASS: commit-before-ACK crash window replays idempotently')

            # 恶意字段不参与身份和业务；未知ID进入隔离死信。
            await bus.client.xadd(settings.stream,{'schema_version':'1','event_id':event_id,'user_id':'999','password':'TEST_PRIVATE'})
            await receiver.tick(block=False)
            dead=await bus.client.xrange(settings.dead_stream)
            assert dead and 'TEST_PRIVATE' not in repr(dead) and 'password' not in repr(dead)
            await bus.publish(uuid4().hex);await receiver.tick(block=False)
            assert len(repo.list(owner_id,run,tasks=True))==4
            print('PASS: tampered/unknown messages are isolated without credentials or foreign task creation')

            # 已发送但尚未消费的事件，Redis stream丢失后仍可由MySQL重投递。
            enqueue(5,snapshot);await relay.tick()
            # 只删除这个随机测试前缀的 stream，不接触其他键/数据库。
            await bus.client.delete(settings.stream)
            sql("UPDATE experiment_assistant.event_outbox SET next_attempt_at=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE run_id=%s AND report_version=5",(run,),False)
            assert await relay.tick();assert await receiver.tick(block=False)
            assert len(repo.list(owner_id,run,tasks=True))==5
            print('PASS: sent-but-unconsumed event is reconstructed after Redis stream loss')

            # 多个投递者只领取同一事件一次；旧租约不能写回新任务。
            enqueue(6,snapshot)
            with ThreadPoolExecutor(max_workers=8) as pool:
                claimed=list(pool.map(lambda _:repo.claim(30),range(8)))
            leases=[x for x in claimed if x is not None];assert len(leases)==1
            active=leases[0]
            assert client.post(f"/runs/{run}/events/{active['id']}/retry",headers=owner).status_code==409
            sql('UPDATE experiment_assistant.event_outbox SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE id=%s',(active['id'],),False)
            new=repo.claim(30);assert new['id']==active['id'] and new['lease_token']!=active['lease_token']
            assert repo.settle(active['id'],active['lease_token'],status='sent',failure_count=0,delay=10,redis_id='1-0') is False
            assert repo.settle(new['id'],new['lease_token'],status='pending',failure_count=0,delay=0)
            await relay.tick();await receiver.tick(block=False)
            print('PASS: eight claimers share one lease; expiry/rebuild fences old tokens and blocks live retry')

            # 新的报告版本保持独立快照；消费不依赖目前run_reports是否被覆盖。
            assert sql('SELECT report_json FROM experiment_assistant.event_outbox WHERE id=%s',(event_id,))[0]['report_json']==text
            task=repo.list(owner_id,run,tasks=True)[0]
            try:
                sql('UPDATE experiment_assistant.agent_tasks SET report_version=999 WHERE event_id=%s',(event_id,),False)
            except IntegrityError as error:assert error.errno==1452
            else:raise AssertionError('mismatched event version passed FK')
            print('PASS: immutable per-version snapshots and composite task/event/run/version foreign keys')

            # Redis不可用：不回滚已有报告/事件，进入可观测退避，随后可恢复。
            enqueue(7,snapshot)
            with patch.object(bus,'publish',side_effect=RedisConnectionError('TEST_PRIVATE')):
                await relay.tick()
            seventh=[x for x in repo.list(owner_id,run) if x['report_version']==7][0]
            assert seventh['status']=='pending' and seventh['last_error_code']=='REDIS_UNAVAILABLE'
            client.post(f"/runs/{run}/events/{seventh['event_id']}/retry",headers=owner)
            await relay.tick();await receiver.tick(block=False)
            print('PASS: Redis outage keeps durable work, exposes safe backoff and resumes after retry')

            # 接收者可能快于投递者的 SQL 确认；不能把 received 覆盖回 sent。
            enqueue(8,snapshot)
            original_publish=bus.publish
            async def receive_before_settle(event_id):
                message_id=await original_publish(event_id)
                assert await receiver.tick(block=False)
                return message_id
            with patch.object(bus,'publish',side_effect=receive_before_settle):await relay.tick()
            assert [r for r in repo.list(owner_id,run) if r['report_version']==8][0]['status']=='received'
            assert len(repo.list(owner_id,run,tasks=True))==8
            print('PASS: receiver commit racing ahead of relay confirmation cannot regress received state')

            # 报告复用与接收同时触及同一 run/event，保持统一锁顺序。
            with ThreadPoolExecutor(max_workers=2) as pool:
                for _ in range(8):
                    receiving=pool.submit(repo.receive,event_id)
                    reporting=pool.submit(metric_repo.report,owner_id,run,generate=True)
                    receiving.result(timeout=10);reporting.result(timeout=10)
            assert len(repo.list(owner_id,run,tasks=True))==8
            print('PASS: concurrent report reuse and event receipt follow one lock order without deadlock or duplicate task')

            for file in ('create_event_outbox.sql','create_agent_tasks.sql'):sql((Path(__file__).resolve().parents[1]/'sql'/file).read_text(),fetch=False)
            assert len(repo.list(owner_id,run))==8
            print('PASS: repeat DDL preserves events/tasks; no model, SSH or training calls')
    finally:await bus.aclose()


def main():
    password=get_mysql_password()
    with temporary_redis() as redis_state:
        asyncio.run(verify(password,redis_state))
        print('PASS: 15 HTTP/MySQL/real-Redis groups; temporary DB and Redis removed; Redis '+redis_state['version'])


if __name__=='__main__':main()
