"""后台分析真实 HTTP/MySQL/Redis 验收；模拟模型，随机临时库，不连接训练服务器。"""

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app import database
from app.config import get_mysql_password
from app.models.analysis import AnalysisSettings, AnalysisLeaseLostError, compact, strict_json, evidence_messages, render_analysis
from app.models.event_bus import EventBusSettings
from app.models.training_metrics import initial_stream, parse_page
from app.services.analysis_service import AnalysisService
from app.services.event_delivery_service import EventRelayService, AnalysisEventReceiver
from app.services.redis_event_bus import RedisEventBus
from app.services.user_model_gateway_factory import UserModelGatewayFactory
from scripts._chat_api_test_app import temporary_chat_api
from scripts._temporary_redis import temporary_redis


class FakeAnalysisGateway:
    def __init__(self, builder, api_key):
        self.builder, self.api_key = builder, api_key
        self.closed = False

    async def invoke(self, messages, **options):
        self.builder.calls.append((messages, options))
        assert not self.closed and 'tools' not in options
        if self.builder.handler:
            return await self.builder.handler(messages, options)
        evidence = strict_json(messages[-1][1])
        assert evidence['facts']
        return {'content': compact({'inferences': [{'text': '报告观测不足以证明收敛，需要更多证据。', 'evidence_paths': ['/status']}],
            'recommendations': [{'text': '核对相同评测协议下的完整曲线。', 'evidence_paths': ['/collection_complete'],
                                 'verification': '固定数据划分和参数，以多次验证观测做对照。'}],
            'limitations': ['没有核实论文依据。']})}

    async def aclose(self):
        self.closed = True


class FakeAnalysisBuilder:
    def __init__(self):
        self.created, self.calls, self.handler = [], [], None

    def __call__(self, *, api_key):
        gateway = FakeAnalysisGateway(self, api_key)
        self.created.append(gateway)
        return gateway


async def verify(password, redis_state):
    groups = []
    settings = EventBusSettings(redis_url=redis_state['url'], prefix='analysis_verify_' + uuid4().hex)
    bus = RedisEventBus(settings)
    try:
        with temporary_chat_api(password, analysis=True) as (app, state), TestClient(app) as client:
            name, repo = state['database_name'], state['analysis_repository']
            builder = FakeAnalysisBuilder()
            factory = UserModelGatewayFactory(state['chat'].gateway_factory.credential_service, builder)
            worker = AnalysisService(repo, gateway_factory=factory, settings=AnalysisSettings(enabled=True))
            relay = EventRelayService(state['events'], bus, settings)
            receiver = AnalysisEventReceiver(state['events'], bus, 'analysis_fixture_receiver')

            def sql(statement, values=(), fetch=True):
                with database.get_connection(password) as connection:
                    with connection.cursor(dictionary=True) as cursor:
                        cursor.execute(statement.replace('experiment_assistant', name), values)
                        rows = cursor.fetchall() if fetch else None
                    connection.commit()
                    return rows

            def passed(text):
                groups.append(text)
                print('PASS:', text, flush=True)

            def login(label):
                response = client.post('/auth/register', json={'username': label + uuid4().hex[:8], 'password': 'TestPass123!'})
                assert response.status_code == 201, response.text
                uid, username = response.json()['user_id'], response.json()['username']
                response = client.post('/auth/login', json={'username': username, 'password': 'TestPass123!'})
                assert response.status_code == 200
                return uid, {'Authorization': 'Bearer ' + response.json()['access_token']}

            uid, owner = login('analysis_owner_')
            other_uid, other = login('analysis_other_')
            assert client.put('/me/model-credentials/deepseek', headers=owner, json={'api_key': 'sk-TEST_ONLY_ANALYSIS_OWNER'}).status_code == 204
            assert client.put('/me/model-credentials/deepseek', headers=other, json={'api_key': 'sk-TEST_ONLY_ANALYSIS_OTHER'}).status_code == 204
            experiment = client.post('/experiments', headers=owner, json={'title': 'analysis CPU fixture', 'kind': 'training'}).json()['experiment_id']

            async def new_task():
                sql("UPDATE experiment_assistant.agent_tasks SET status='attention',lease_token=NULL,lease_until=NULL,last_error_code='INTERNAL_ERROR' WHERE status IN ('queued','running')", fetch=False)
                response = client.post(f'/experiments/{experiment}/runs', headers=owner,
                    json={'request_id': uuid4().hex, 'parameters': {'epochs': 1, 'learning_rate': 0.00005}})
                assert response.status_code == 201, response.text
                run = response.json()['run']['run_id']
                sql("UPDATE experiment_assistant.experiment_runs SET status='succeeded',started_at=created_at,finished_at=UTC_TIMESTAMP(6),exit_code=0,updated_at=UTC_TIMESTAMP(6) WHERE id=%s", (run,), False)
                raw = b'{"type":"metric","step":1,"epoch":1,"split":"validation","dataset":"cod10k","mae":0.03}\n'
                stream, events = parse_page(initial_stream(), raw,
                    {'log_size': len(raw), 'truncated': False, 'stale': False, 'status': 'succeeded'})
                assert state['metric_repository'].commit_page(uid, run, 0, stream, events)
                response = client.post(f'/runs/{run}/report', headers=owner)
                assert response.status_code == 200, response.text
                assert await relay.tick() and await receiver.tick(block=False)
                tasks = client.get(f'/runs/{run}/agent-tasks', headers=owner).json()
                assert len(tasks) == 1 and tasks[0]['status'] == 'queued'
                return run, uuid4().hex if not tasks else tasks[0]['task_id'].replace('-', ''), tasks[0]['event_id'].replace('-', '')

            def task(run, tid, headers=owner):
                response = client.get(f'/runs/{run}/analysis-tasks/{tid}', headers=headers)
                assert response.status_code == 200, response.text
                return response.json()

            def results(run):
                response = client.get(f'/runs/{run}/analyses', headers=owner)
                assert response.status_code == 200, response.text
                assert response.headers['Cache-Control'] == 'no-store'
                return response.json()

            def enable(value=True):
                response = client.put('/me/analysis-settings', headers=owner, json={'enabled': value})
                assert response.status_code == 200 and response.json()['enabled'] is value

            run, tid, event_id = await new_task()
            assert client.get('/me/analysis-settings', headers=owner).json() == {'enabled': False}
            assert await worker.tick() is False and not builder.created
            assert task(run, tid)['status'] == 'queued'
            assert client.get('/me/analysis-settings').status_code == 401
            assert client.put('/me/analysis-settings', headers=owner, json={'enabled': 'true'}).status_code == 422
            assert client.get('/me/analysis-settings', headers=other).json()['enabled'] is False
            passed('real login, strict opt-in, default-disabled preference and owner-specific settings')

            # 在临时库模拟旧版 27 表，保留真实排队任务后执行增量迁移。
            sql('DROP TABLE experiment_assistant.analysis_results', fetch=False)
            sql('ALTER TABLE experiment_assistant.agent_tasks DROP CHECK ck_analysis_lease, DROP INDEX idx_analysis_due, DROP INDEX idx_analysis_lease, '
                'DROP COLUMN attempts,DROP COLUMN attempts_in_cycle,DROP COLUMN lease_token,DROP COLUMN lease_until,'
                'DROP COLUMN next_attempt_at,DROP COLUMN last_error_code,DROP COLUMN started_at,DROP COLUMN finished_at', fetch=False)
            read_sql = database._read_sql
            with patch.object(database, '_read_sql', side_effect=lambda file: read_sql(file).replace('experiment_assistant', name)):
                with database.get_connection(password) as connection:
                    database._ensure_analysis(connection)
                    database._ensure_analysis(connection)
            assert task(run, tid)['attempts'] == 0 and task(run, tid)['status'] == 'queued'
            assert len(sql('SELECT id FROM experiment_assistant.agent_tasks')) == 1
            assert len(sql('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s', (name,))) == 39
            passed('legacy queued task survives additive migration and repeat migration; 39 current business tables')

            assert client.get(f'/runs/{run}/analyses', headers=other).status_code == 404
            assert client.get(f'/runs/{run}/analysis-tasks/{tid}', headers=other).status_code == 404
            assert client.post(f'/runs/{run}/analysis-tasks/{tid}/retry', headers=other).status_code == 404
            assert client.get('/runs/bad/analyses', headers=owner).status_code == 422
            assert client.get(f'/runs/{run}/analyses?limit=21', headers=owner).status_code == 422
            assert client.get(f'/runs/{run}/analyses?offset=-1', headers=owner).status_code == 422
            passed('run and task ownership, UUID and pagination validation do not expose other users')

            enable()
            disabled = AnalysisService(repo, gateway_factory=factory)
            assert await disabled.tick() is False and not builder.created
            assert await worker.tick()
            record = task(run, tid)
            assert record['status'] == 'succeeded' and record['attempts'] == 1
            assert builder.created[-1].api_key == 'sk-TEST_ONLY_ANALYSIS_OWNER' and builder.created[-1].closed
            result = results(run)[0]
            assert result['report_version'] == 1 and result['is_stale'] is False
            assert any(fact['value'] == 0.03 for fact in result['content']['facts'])
            assert 'lease_token' not in compact(record) and 'sk-TEST_ONLY' not in compact(result)
            assert client.post(f'/runs/{run}/analysis-tasks/{tid}/retry', headers=owner).status_code == 409
            passed('admin switch plus user opt-in executes one owned gateway call and saves verifiable facts/inferences')

            calls = len(builder.calls)
            assert await worker.tick() is False
            await bus.publish(event_id)
            assert await receiver.tick(block=False)
            current_tasks = client.get(f'/runs/{run}/agent-tasks', headers=owner).json()
            assert len(current_tasks) == 2 and {row['agent_name'] for row in current_tasks} == {'analysis', 'suggestion'}
            assert len(results(run)) == 1 and len(builder.calls) == calls
            passed('duplicate Redis event and completed task do not duplicate model analysis or saved result')

            # 新聊天读取已存结果；没有调用后台分析网关。
            sid = state['sessions'].create_session(uid, 'new session reading saved analysis')
            async def chat_handler(messages, options):
                if messages[-1].get('role') == 'tool':
                    data = strict_json(messages[-1]['content'])
                    assert data['ok'] and data['data']['report_version'] == 1
                    return {'content': '已读取你这次训练的已保存分析，推断仍需验证。', 'tool_calls': []}
                return {'content': '', 'tool_calls': [{'id': 'saved_analysis', 'name': 'get_run_analysis',
                    'args': {'run_id': run, 'section': 'facts'}}]}
            state['builder'].handler = chat_handler
            response = client.post(f'/sessions/{sid}/messages', headers=owner,
                json={'content': '读取这次训练的后台分析', 'request_id': uuid4().hex})
            assert response.status_code == 200, response.text
            assert len(builder.calls) == calls
            audit = client.get(f'/sessions/{sid}/tool-calls', headers=owner)
            assert audit.status_code == 200 and audit.json()['items'][0]['tool_name'] == 'get_run_analysis', audit.text
            passed('new chat reads saved background analysis through audited read-only tool without rerunning analysis')

            # 新报告产生后，旧分析必须显示过期，不能冒充新报告分析。
            sql("UPDATE experiment_assistant.experiment_runs SET status='failed',exit_code=1,updated_at=UTC_TIMESTAMP(6) WHERE id=%s", (run,), False)
            refreshed = client.post(f'/runs/{run}/report', headers=owner)
            assert refreshed.status_code == 200 and refreshed.json()['version'] == 2
            assert results(run)[0]['is_stale'] is True
            # 消费新版事件以免后续夹带未交付旧事件；后续新 fixture 会隔离 queued 状态。
            assert await relay.tick() and await receiver.tick(block=False)
            passed('saved result keeps immutable report version and is marked stale after new report')

            bad_run, bad_tid, _ = await new_task()
            async def invalid(*args):
                return {'content': 'PRIVATE_INVALID_MODEL_BODY'}
            builder.handler = invalid
            assert await worker.tick()
            assert task(bad_run, bad_tid)['last_error_code'] == 'INVALID_MODEL_OUTPUT' and not results(bad_run)
            assert client.get(f'/runs/{bad_run}', headers=owner).json()['status'] == 'succeeded'
            builder.handler = None
            assert client.post(f'/runs/{bad_run}/analysis-tasks/{bad_tid}/retry', headers=owner).status_code == 202
            assert await worker.tick()
            assert task(bad_run, bad_tid)['attempts'] == 2 and len(results(bad_run)) == 1
            passed('invalid output creates no result, preserves training success, and explicit retry saves one result')

            missing_run, missing_tid, _ = await new_task()
            assert client.delete('/me/model-credentials/deepseek', headers=owner).status_code == 204
            calls = len(builder.calls)
            assert await worker.tick()
            assert task(missing_run, missing_tid)['last_error_code'] == 'KEY_UNAVAILABLE' and len(builder.calls) == calls
            assert client.put('/me/model-credentials/deepseek', headers=owner, json={'api_key': 'sk-TEST_ONLY_ANALYSIS_OWNER'}).status_code == 204
            passed('missing personal key marks attention without provider call or training mutation')

            lease_run, lease_tid, _ = await new_task()
            old = repo.claim(30, 3)
            assert old['task_id'] == lease_tid
            await bus.publish(old['event_id'])
            assert await receiver.tick(block=False)
            assert sql('SELECT lease_token FROM experiment_assistant.agent_tasks WHERE id=%s', (lease_tid,))[0]['lease_token'] == old['lease_token']
            old_source = repo.source(uid, lease_tid, old['lease_token'])
            _, evidence = evidence_messages(old_source, worker.settings)
            fake = await FakeAnalysisGateway(builder, 'test').invoke([('system', 'test'), ('human', compact(evidence))])
            content = render_analysis(fake['content'], evidence)
            sql('UPDATE experiment_assistant.agent_tasks SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE id=%s', (lease_tid,), False)
            current = repo.claim(30, 3)
            assert current['task_id'] == lease_tid and current['lease_token'] != old['lease_token']
            assert not repo.renew(uid, lease_tid, old['lease_token'], 30)
            try:
                repo.complete(uid, lease_run, lease_tid, old['lease_token'], content, 'deepseek-flash')
                raise AssertionError('old lease committed')
            except AnalysisLeaseLostError:
                pass
            assert not results(lease_run)
            repo.complete(uid, lease_run, lease_tid, current['lease_token'], content, 'deepseek-flash')
            assert len(results(lease_run)) == 1
            passed('expired lease is reclaimed and fences old renewal/completion before a new owner commits')

            atomic_run, atomic_tid, _ = await new_task()
            claimed = repo.claim(30, 3)
            row = repo.source(uid, atomic_tid, claimed['lease_token'])
            _, evidence = evidence_messages(row, worker.settings)
            content = render_analysis((await FakeAnalysisGateway(builder, 'test').invoke([('system', 'test'), ('human', compact(evidence))]))['content'], evidence)
            with patch.object(repo, 'finish_analysis_task_sql', 'INVALID TEST SQL'):
                try:
                    repo.complete(uid, atomic_run, atomic_tid, claimed['lease_token'], content, 'deepseek-flash')
                    raise AssertionError('invalid finish SQL did not fail')
                except MySQLError:
                    pass
            assert task(atomic_run, atomic_tid)['status'] == 'running' and not results(atomic_run)
            repo.fail(uid, atomic_tid, claimed['lease_token'], status='attention', error_code='STORAGE_ERROR')
            passed('result insert and succeeded status roll back together when final SQL fails')

            lost_run, lost_tid, _ = await new_task()
            complete = repo.complete
            def lost_ack(*args):
                complete(*args)
                raise MySQLError('PRIVATE_COMMIT_ACK_LOST')
            with patch.object(repo, 'complete', side_effect=lost_ack):
                assert await worker.tick()
            assert task(lost_run, lost_tid)['status'] == 'succeeded' and len(results(lost_run)) == 1
            assert await worker.tick() is False
            passed('lost acknowledgement after durable completion does not regress success or invoke provider again')

            exhausted_run, exhausted_tid, _ = await new_task()
            sql("UPDATE experiment_assistant.agent_tasks SET status='running',attempts=3,attempts_in_cycle=3,lease_token=%s,lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE id=%s", (uuid4().hex, exhausted_tid), False)
            assert repo.claim(30, 3) is None
            assert task(exhausted_run, exhausted_tid)['last_error_code'] == 'ATTEMPTS_EXHAUSTED'
            passed('repeated abandoned leases stop at bounded attempts and require explicit recovery')

            cancel_run, cancel_tid, _ = await new_task()
            started = asyncio.Event()
            async def hanging(*args):
                started.set()
                await asyncio.sleep(30)
            builder.handler = hanging
            job = asyncio.create_task(worker.tick())
            await started.wait()
            job.cancel()
            try:
                await job
            except asyncio.CancelledError:
                pass
            builder.handler = None
            assert task(cancel_run, cancel_tid)['last_error_code'] == 'WORKER_CANCELLED' and not results(cancel_run)
            assert builder.created[-1].closed
            passed('graceful cancellation closes provider and records safe recoverable attention state')

            disabled_run, disabled_tid, _ = await new_task()
            class DisableDuringKeyLoad:
                async def create_for_user(self, user_id):
                    gateway = await factory.create_for_user(user_id)
                    repo.preference(user_id, enabled=False)
                    return gateway
            calls = len(builder.calls)
            worker.gateway_factory = DisableDuringKeyLoad()
            assert await worker.tick()
            worker.gateway_factory = factory
            assert len(builder.calls) == calls and task(disabled_run, disabled_tid)['last_error_code'] == 'ANALYSIS_INTERRUPTED'
            assert client.post(f'/runs/{disabled_run}/analysis-tasks/{disabled_tid}/retry', headers=owner).status_code == 409
            enable()
            passed('user disabling during key load prevents request; disabled preference blocks retry')

            heartbeat_run, heartbeat_tid, _ = await new_task()
            started = asyncio.Event()
            async def awaiting(*args):
                started.set()
                await asyncio.sleep(30)
            builder.handler = awaiting
            original_settings = worker.settings
            worker.settings = replace(worker.settings, heartbeat_seconds=1)
            job = asyncio.create_task(worker.tick())
            await started.wait()
            before = sql('SELECT lease_until FROM experiment_assistant.agent_tasks WHERE id=%s', (heartbeat_tid,))[0]['lease_until']
            await asyncio.sleep(1.2)
            after = sql('SELECT lease_until FROM experiment_assistant.agent_tasks WHERE id=%s', (heartbeat_tid,))[0]['lease_until']
            assert after > before
            enable(False)
            await asyncio.wait_for(job, timeout=5)
            worker.settings, builder.handler = original_settings, None
            assert task(heartbeat_run, heartbeat_tid)['last_error_code'] == 'ANALYSIS_INTERRUPTED'
            assert builder.created[-1].closed and not results(heartbeat_run)
            enable()
            passed('real SQL heartbeat extends lease and disabled user interrupts in-flight work without publishing result')

            concurrent_run, concurrent_tid, _ = await new_task()
            with ThreadPoolExecutor(max_workers=8) as executor:
                claims = list(executor.map(lambda _: repo.claim(30, 3), range(8)))
            valid = [claim for claim in claims if claim is not None]
            assert len(valid) == 1 and valid[0]['task_id'] == concurrent_tid
            assert client.post(f'/runs/{concurrent_run}/analysis-tasks/{concurrent_tid}/retry', headers=owner).status_code == 409
            repo.fail(uid, concurrent_tid, valid[0]['lease_token'], status='attention', error_code='ANALYSIS_INTERRUPTED')
            passed('eight concurrent claimers obtain one task lease and active task retry is rejected')

            # 读取校验不能接受与报告不一致的缓存式“事实”。只修改当前随机临时库。
            stored = sql('SELECT content_json FROM experiment_assistant.analysis_results WHERE task_id=%s', (lost_tid,))[0]['content_json']
            sql('UPDATE experiment_assistant.analysis_results SET content_digest=%s WHERE task_id=%s', ('0'*64, lost_tid), False)
            response = client.get(f'/runs/{lost_run}/analyses', headers=owner)
            assert response.status_code == 503 and response.json()['detail'] == '请稍后重试。'
            modified = strict_json(stored)
            modified['facts'][0]['value'] = 'FABRICATED_FACT'
            changed = compact(modified)
            sql('UPDATE experiment_assistant.analysis_results SET content_json=%s,content_digest=%s WHERE task_id=%s',
                (changed, hashlib.sha256(changed.encode()).hexdigest(), lost_tid), False)
            assert client.get(f'/runs/{lost_run}/analyses', headers=owner).status_code == 503
            passed('tampered persisted analysis is rejected with safe 503 rather than returned as verified facts')

            logs = '\n'.join(path.read_text(encoding='utf-8') for path in Path(state['log_dir']).rglob('*.txt'))
            for private in ('sk-TEST_ONLY_ANALYSIS_OWNER', 'sk-TEST_ONLY_ANALYSIS_OTHER',
                            'PRIVATE_INVALID_MODEL_BODY', 'PRIVATE_COMMIT_ACK_LOST'):
                assert private not in logs, 'private material found in log'
            assert 'task_id=' + lost_tid in logs and 'run_id=' + lost_run in logs and 'analysis_code=' in logs
            assert all(gateway.closed for gateway in builder.created)
            passed('logs identify task and error category without credentials, provider body or analysis text')
    finally:
        await bus.aclose()
    print(f'ALL {len(groups)} analysis integration groups passed; fake model only; temporary DB/Redis removed.', flush=True)


def main():
    password = get_mysql_password()
    with temporary_redis() as redis_state:
        asyncio.run(verify(password, redis_state))


if __name__ == '__main__':
    main()
