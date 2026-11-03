"""建议 Agent HTTP/MySQL 验收：随机临时库，模拟模型/SSH/公开论文，无训练或付费调用。"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app import database
from app.config import get_mysql_password
from app.models.analysis import AnalysisSettings
from app.models.suggestion import SuggestionSettings
from app.models.training_metrics import initial_stream, parse_page
from app.models.analysis import compact
from app.models.runner import RunnerSettings
from app.services.analysis_service import AnalysisService
from app.services.suggestion_service import SuggestionService
from app.services.user_model_gateway_factory import UserModelGatewayFactory
from app.models.research import reference_row
from scripts._chat_api_test_app import temporary_chat_api, FakeGatewayBuilder
from scripts.verify_source_code_mysql import SourceFixture
from scripts.verify_analysis_mysql_redis import FakeAnalysisBuilder


async def verify(password):
    groups = []
    fixture = SourceFixture()
    fixture.files['train.py'] = b'loss = criterion(prediction, target)\nloss.backward()\n'
    spec = {'path': 'train.py', 'sha256': hashlib.sha256(fixture.files['train.py']).hexdigest(),
        'parameters': {}, 'max_runtime_seconds': 10, 'max_log_bytes': 1000}
    settings = RunnerSettings(compact({'suggestion_fixture': spec}))
    with temporary_chat_api(password, runner_settings=settings, suggestions=True, analysis=True) as (app, state), TestClient(app) as client:
        name, repo = state['database_name'], state['suggestion_repository']
        state['sources'].transport = fixture
        def sql(text, values=(), fetch=True):
            with database.get_connection(password) as connection:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(text.replace('experiment_assistant', name), values)
                    rows = cursor.fetchall() if fetch else None
                connection.commit(); return rows
        def passed(text):
            groups.append(text); print('PASS:', text, flush=True)
        def login(prefix):
            payload = {'username': prefix + uuid4().hex[:8], 'password': 'TestPass123!'}
            assert client.post('/auth/register', json=payload).status_code == 201
            response = client.post('/auth/login', json=payload)
            assert response.status_code == 200, response.text
            return response.json()['user']['user_id'], {'Authorization': 'Bearer ' + response.json()['access_token']}
        uid, owner = login('suggest_owner_')
        _, other = login('suggest_other_')
        assert client.get('/me/suggestion-settings', headers=owner).json() == {'enabled': False}
        assert client.get('/me/suggestion-settings').status_code == 401
        assert client.put('/me/suggestion-settings', headers=owner, json={'enabled': 'true'}).status_code == 422
        assert client.put('/me/model-credentials/deepseek', headers=owner, json={'api_key': 'sk-TEST_ONLY_SUGGESTION'}).status_code == 204
        assert client.put('/me/analysis-settings', headers=owner, json={'enabled': True}).status_code == 200
        exp = client.post('/experiments', headers=owner, json={'title': 'suggestion fixture', 'kind': 'training'}).json()['experiment_id']
        server = client.post('/servers', headers=owner, json={'name': 'fixture', 'host': '127.0.0.1', 'port': 22,
            'username': 'trainer', 'auth_method': 'password', 'password': 'TEST_ONLY_SSH_PASSWORD',
            'host_fingerprint': 'SHA256:' + base64.b64encode(bytes(32)).decode().rstrip('='), 'work_dir': '/owned/project'})
        assert server.status_code == 201, server.text
        sid = server.json()['server_id']
        profile = await state['server_service'].get(uid, sid)
        upstream_builder = FakeAnalysisBuilder()
        upstream = AnalysisService(state['analysis_repository'], settings=AnalysisSettings(enabled=True),
            gateway_factory=UserModelGatewayFactory(state['chat'].gateway_factory.credential_service, upstream_builder))
        async def new_task(*, fail_handoff=False):
            response = client.post(f'/experiments/{exp}/runs', headers=owner, json={'request_id': uuid4().hex, 'parameters': {}})
            assert response.status_code == 201, response.text
            run = response.json()['run']['run_id']
            launch = {'version': 1, 'run_id': run, 'user_id': uid, 'server_id': sid, 'server_binding': profile.binding(),
                'script_id': 'suggestion_fixture', 'work_dir': profile.work_dir, 'spec': spec, 'parameters': {}, 'output_directory': 'training_outputs'}
            state['executions'].claim(uid, run, profile, 'suggestion_fixture', launch)
            sql("UPDATE experiment_assistant.experiment_runs SET status='succeeded',exit_code=0,started_at=created_at,finished_at=UTC_TIMESTAMP(6) WHERE id=%s", (run,), False)
            raw = b'{"type":"metric","step":1,"epoch":1,"split":"validation","dataset":"cod10k","mae":0.03}\n'
            stream, events = parse_page(initial_stream(), raw, {'log_size': len(raw), 'truncated': False, 'stale': False, 'status': 'succeeded'})
            assert state['metric_repository'].commit_page(uid, run, 0, stream, events)
            response = client.post(f'/runs/{run}/report', headers=owner)
            assert response.status_code == 200, response.text
            event = client.get(f'/runs/{run}/events', headers=owner).json()[0]['event_id']
            state['events'].receive(event)  # 既有 Redis 接收器的持久入口；此验收不另连 Redis。
            if fail_handoff:
                with patch.object(state['analysis_repository'], 'create_suggestion_task_sql',
                        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='TEST_HANDOFF_FAILURE'"):
                    assert await upstream.tick()
                tasks = client.get(f'/runs/{run}/agent-tasks', headers=owner).json()
                assert len(tasks) == 1 and tasks[0]['status'] == 'attention'
                assert client.get(f'/runs/{run}/analyses', headers=owner).json() == []
                assert client.get(f'/runs/{run}/suggestions', headers=owner).json() == []
                return run, tasks[0]['task_id'], event
            assert await upstream.tick()
            tasks = client.get(f'/runs/{run}/agent-tasks', headers=owner).json()
            assert {v['agent_name'] for v in tasks} == {'analysis', 'suggestion'}
            return run, next(v['task_id'] for v in tasks if v['agent_name'] == 'suggestion'), event

        # 临时库尚无任务时模拟旧版类型约束，增量升级不得创建结果或开启用户开关。
        sql("ALTER TABLE experiment_assistant.agent_tasks DROP CHECK ck_agent_task_name, ADD CONSTRAINT ck_agent_task_name CHECK(agent_name='analysis')", fetch=False)
        old_read = database._read_sql
        with patch.object(database, '_read_sql', side_effect=lambda file: old_read(file).replace('experiment_assistant', name)):
            with database.get_connection(password) as connection:
                database._ensure_suggestions(connection)
        run, task, event = await new_task()
        prefix = f'/runs/{run}/suggestion-tasks/{task}'
        assert client.get(prefix, headers=owner).json()['status'] == 'queued'
        assert client.get(f'/runs/{run}/suggestions', headers=owner).json() == []
        assert client.get(prefix, headers=other).status_code == 404
        assert client.get(prefix + '/evidence', headers=other).status_code == 404
        assert client.get(f'/runs/{run}/analysis-tasks/{task}', headers=owner).status_code == 404
        passed('analysis success atomically hands off exactly one queued suggestion; identity/stage isolation and default off')

        # 重复交付/重复迁移不会覆盖已登记任务。
        state['events'].receive(event)
        with patch.object(database, '_read_sql', side_effect=lambda file: old_read(file).replace('experiment_assistant', name)):
            with database.get_connection(password) as connection:
                database._ensure_suggestions(connection)
                database._ensure_suggestions(connection)
        assert len([v for v in client.get(f'/runs/{run}/agent-tasks', headers=owner).json() if v['agent_name'] == 'suggestion']) == 1
        passed('repeat event receipt and additive migration preserve task identity and existing rows')

        builder = FakeGatewayBuilder()
        client_calls = []
        quote = 'A controlled ablation keeps the data split fixed'
        async def read(url, run_id):
            client_calls.append((url, run_id))
            return reference_row(url=url, title='Fixture public paper', text=quote + ' and compares the validation metric.', kind='paper_html', run_id=run_id)
        public = SimpleNamespace(read=read, search=None)
        async def model(messages, options):
            assert not {'start_training_run', 'prepare_training_run'} & {v['function']['name'] for v in options['tools']}
            delivered = [v for v in messages if isinstance(v, dict) and v.get('role') == 'tool']
            if len(delivered) == 0:
                return {'tool_calls': [{'id': 'readcode', 'name': 'read_project_file', 'args': {'path': 'train.py'}}]}
            if len(delivered) == 1:
                return {'tool_calls': [{'id': 'readpaper', 'name': 'read_reference', 'args': {'url': 'https://arxiv.org/html/2401.12345'}}]}
            return {'content': compact({'suggestions': [{'problem': '当前仅有一次验证观测。', 'hypothesis': '损失项可能需要消融，尚未确认原因。',
                'change': '保持其他条件固定，对损失权重做对照。', 'conditions': '保持数据划分一致。', 'validation': '多次重复并比较完整验证曲线。',
                'evidence_paths': ['/series/0/points'], 'code': [{'evidence_id': 'code_1', 'start_line': 1, 'end_line': 2}],
                'papers': [{'evidence_id': 'paper_2', 'quote': quote}]}], 'limitations': ['测试来源，不代表真实论文建议有效。']})}
        builder.handler = model
        worker = SuggestionService(repo, settings=SuggestionSettings(enabled=True),
            gateway_factory=UserModelGatewayFactory(state['chat'].gateway_factory.credential_service, builder),
            source_factory=lambda: state['sources'], research_factory=lambda: public)
        assert await worker.tick() is False and not builder.created and not fixture.calls
        assert client.put('/me/suggestion-settings', headers=owner, json={'enabled': True}).status_code == 200
        assert client.get('/me/suggestion-settings', headers=other).json() == {'enabled': False}
        assert await worker.tick()
        status = client.get(prefix, headers=owner).json()
        assert status['status'] == 'succeeded', status
        result = client.get(f'/runs/{run}/suggestions', headers=owner)
        assert result.status_code == 200, result.text
        assert result.headers['Cache-Control'] == 'no-store'
        content = result.json()[0]['content']
        assert content['evidence_status'] == 'supported' and len(content['suggestions']) == 1
        assert content['code_evidence'][0]['path'] == 'train.py' and content['paper_evidence'][0]['kind'] == 'paper_html'
        assert state['runs'].get_run(uid, run).status == 'succeeded'
        passed('opted-in bounded worker reads owned source and actual delivered paper fixture, saves three-way evidence, leaves training untouched')
        audit = client.get(prefix + '/evidence', headers=owner)
        assert audit.status_code == 200, audit.text
        assert [v['tool_name'] for v in audit.json()] == ['read_project_file', 'read_reference']
        assert 'attempt_token' not in compact(audit.json()) and 'TEST_ONLY_' not in compact(audit.json())
        assert client.post(prefix + '/retry', headers=owner).status_code == 409
        assert await worker.tick() is False
        passed('persisted tool evidence is queryable, redacts lease/credentials and prevents rerun of completed task')

        # 已完成建议查询无需模型 Key，聊天通过新会话读取已存结果。
        assert client.delete('/me/model-credentials/deepseek', headers=owner).status_code == 204
        assert client.get(f'/runs/{run}/suggestions', headers=owner).status_code == 200
        assert client.put('/me/model-credentials/deepseek', headers=owner, json={'api_key': 'sk-TEST_ONLY_SUGGESTION'}).status_code == 204
        session = client.post('/sessions', headers=owner, json={'title': 'new chat reads saved advice'}).json()['session_id']
        async def chat_model(messages, options):
            delivered = [m for m in messages if isinstance(m, dict) and m.get('role') == 'tool']
            if not delivered:
                return {'tool_calls': [{'id': 'saved', 'name': 'get_run_suggestions', 'args': {'run_id': run, 'section': 'suggestions'}}]}
            assert 'train.py' in delivered[-1]['content'] and quote in delivered[-1]['content']
            return {'content': '已读取保存的建议；建议尚未应用，需要对照实验验证。'}
        state['builder'].handler = chat_model
        response = client.post(f'/sessions/{session}/messages', headers=owner, json={'content': '查看这次运行已保存的修改建议', 'request_id': uuid4().hex})
        assert response.status_code == 200, response.text
        assert client.get(f'/sessions/{session}/tool-calls', headers=owner).json()['items'][0]['tool_name'] == 'get_run_suggestions'
        passed('saved results remain readable without model Key; a new chat can retrieve the persisted advice with audit')

        sql('UPDATE experiment_assistant.experiment_runs SET result_version=result_version+1 WHERE id=%s', (run,), False)
        assert client.get(f'/runs/{run}/suggestions', headers=owner).json()[0]['is_stale'] is True
        passed('later report version marks stored suggestions stale without replacing their source or code evidence')

        # 第二个任务明确模拟远端失败，仍生成可查询的证据不足结果。
        run2, task2, _ = await new_task()
        state['sources'].transport.files.pop('train.py')
        assert await worker.tick()
        missing = client.get(f'/runs/{run2}/suggestions', headers=owner).json()[0]['content']
        assert missing['evidence_status'] == 'insufficient' and missing['suggestions'] == []
        evidence = client.get(f'/runs/{run2}/suggestion-tasks/{task2}/evidence', headers=owner).json()
        assert evidence[0]['result']['code'] == 'SOURCE_NOT_FOUND'
        passed('missing source is explicit and recorded; a model fabricated success cannot create supported advice')

        state['sources'].transport.files['train.py'] = b'loss = criterion(prediction, target)\nloss.backward()\n'
        assert client.post(f'/runs/{run2}/suggestion-tasks/{task2}/retry', headers=owner).status_code == 202
        assert await worker.tick()
        recovered = client.get(f'/runs/{run2}/suggestions', headers=owner).json()
        assert len(recovered) == 1 and recovered[0]['content']['evidence_status'] == 'supported'
        evidence = client.get(f'/runs/{run2}/suggestion-tasks/{task2}/evidence', headers=owner).json()
        assert len(evidence) == 4 and {v['attempt_number'] for v in evidence} == {1, 2}
        assert evidence[0]['result']['code'] == 'SOURCE_NOT_FOUND'
        passed('explicit retry can recover insufficient evidence without losing prior attempt tool records')

        # 第三个任务中断后旧租约不能写回，重试仍检查本人开关。
        run3, task3, _ = await new_task()
        with ThreadPoolExecutor(max_workers=2) as pool:
            leases = list(pool.map(lambda _: repo.claim(90, 3), range(2)))
        assert len([value for value in leases if value is not None]) == 1
        lease = next(value for value in leases if value is not None)
        assert lease and lease['task_id'] == task3.replace('-', '')
        sql('UPDATE experiment_assistant.agent_tasks SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE id=%s', (lease['task_id'],), False)
        try:
            repo.complete(uid, run3, lease['task_id'], lease['lease_token'], content, 'deepseek-flash')
        except Exception as error:
            assert type(error).__name__ == 'AnalysisLeaseLostError'
        else:
            raise AssertionError('old lease completed')
        assert client.put('/me/suggestion-settings', headers=owner, json={'enabled': False}).status_code == 200
        retry = f'/runs/{run3}/suggestion-tasks/{task3}/retry'
        assert client.post(retry, headers=owner).status_code == 409
        assert client.put('/me/suggestion-settings', headers=owner, json={'enabled': True}).status_code == 200
        assert client.post(retry, headers=owner).status_code == 202
        passed('two claimers receive one active lease; expired attempt cannot commit and retry respects opt-in')

        # 清理本次待重试项，随后故意使交接 SQL 失败，确认上游结果也回滚。
        sql("UPDATE experiment_assistant.agent_tasks SET status='attention',lease_token=NULL,lease_until=NULL WHERE agent_name='suggestion' AND status='queued'", fetch=False)
        await new_task(fail_handoff=True)
        passed('handoff insertion failure rolls back analysis result and success state together; task remains recoverable')

        logs = '\n'.join(p.read_text(encoding='utf-8') for p in state['log_dir'].rglob('*.txt'))
        assert all(value not in logs for value in ('TEST_ONLY_SSH_PASSWORD', 'sk-TEST_ONLY_SUGGESTION', lease['lease_token']))
        passed('operation/request logs contain no fixture SSH password, model Key or lease credential')
    print(f'ALL {len(groups)} suggestion HTTP/MySQL groups passed; temporary database removed; fake model/SSH/public source only.', flush=True)
    return len(groups)


if __name__ == '__main__':
    asyncio.run(verify(get_mysql_password()))
