"""本地前端验收：真实 FastAPI/临时 MySQL，模型和 SSH 均替身，不启动训练。

依赖现有后端的验收设施；不修改生产库、生产配置或服务器。
"""
import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

parser = argparse.ArgumentParser()
parser.add_argument('--backend-root', required=True)
parser.add_argument('--port', type=int, default=8001)
args = parser.parse_args()
sys.path.insert(0, str(Path(args.backend_root).resolve()))

import uvicorn
from fastapi.testclient import TestClient
from app.config import get_mysql_password
from app.models.runner import RunnerSettings
from scripts._chat_api_test_app import temporary_chat_api
from scripts.verify_source_code_mysql import SourceFixture
from tests.paper_fixtures import pdf_bytes, PAPER_PAGES

RAW = b''.join(json.dumps({'type': 'metric', 'step': i, 'epoch': i, 'split': 'validation', 'dataset': 'fixture', 'loss': round(1 / i, 6), 'mae': round(.2 / i, 6)}).encode() + b'\n' for i in range(1, 13))

class FakeConnector:
    async def test_connection(self, *_): return 1.2

class FakeTransport:
    async def request(self, profile, credentials, payload):
        action = payload['action']
        state = 'cancelled' if action == 'stop' else 'succeeded'
        result = {'status': state, 'started': True, 'exit_code': 0 if state == 'succeeded' else None,
                  'error_code': None, 'stale': False, 'log_bytes': len(RAW)}
        if action == 'logs':
            offset, limit = payload['offset'], payload['limit']
            raw = RAW[offset:offset+limit]
            result.update(content=raw.decode(errors='replace'), raw_base64=base64.b64encode(raw).decode(),
                          next_offset=offset+len(raw), has_more=offset+len(raw)<len(RAW), truncated=False,
                          log_size=len(RAW), source_changed=False,
                          anchor_sha256=hashlib.sha256(RAW[max(0,offset-256):offset]).hexdigest())
        return result

fixture = SourceFixture()
spec = {'path': 'train.py', 'sha256': hashlib.sha256(fixture.files['train.py']).hexdigest(),
        'parameters': {'epochs': {'type': 'int', 'min': 1, 'max': 100}, 'learning_rate': {'type': 'float', 'min': .000001, 'max': 1}},
        'max_runtime_seconds': 60, 'max_log_bytes': 100000}

with temporary_chat_api(get_mysql_password(), runner_settings=RunnerSettings(json.dumps({'fixture_train': spec})),
                        source_code=True, training=True, analysis=True, research=True, papers=True, skills=True) as (app, state):
    async def fake_answer(messages, options):
        return {'content': '这是**模拟模型回复**，未调用真实模型。\n\n工作台可记录训练、查看指标，并在明确指令下使用训练工具。\n\n- 实验与训练：配置任务、查看运行。\n- 资料库：上传 PDF、读取正文与来源。\n- 修改建议：核对代码和论文后，再确认应用。'}
    state['builder'].handler = fake_answer
    state['runner'].transport = FakeTransport()
    state['sources'].transport = fixture
    state['server_service'].connector = FakeConnector()
    with TestClient(app) as client:
        def request(method, path, *, expected=(200,), **kwargs):
            response = client.request(method, path, **kwargs)
            assert response.status_code in expected, (path, response.status_code, response.text)
            return response.json() if response.content else None
        account = {'username': 'frontend_test', 'password': 'TestPass123!'}
        request('POST', '/auth/register', json=account, expected=(201,))
        login = request('POST', '/auth/login', json=account)
        headers = {'Authorization': 'Bearer '+login['access_token']}
        request('PUT', '/me/model-credentials/deepseek', headers=headers, json={'api_key': 'sk-TEST_ONLY_FRONTEND'}, expected=(204,))
        experiment = request('POST', '/experiments', headers=headers, expected=(201,), json={
            'title': '前端验收实验（模拟数据）', 'kind': 'training', 'description': '独立临时数据库；模型和 SSH 均为替身，不运行真实训练。',
            'evaluation': {'task_type': 'custom', 'protocol_id': 'frontend-fixture', 'protocol_version': '1',
                'datasets': [{'id': 'fixture', 'display_name': '验收数据集', 'version': '1'}],
                'metrics': [{'name': 'mae', 'display_name': 'MAE', 'unit': 'ratio', 'direction': 'minimize'}]}})
        server = request('POST', '/servers', headers=headers, expected=(201,), json={
            'name': '验收替身服务器（不连接外部）', 'host': '127.0.0.1', 'port': 22, 'username': 'trainer',
            'auth_method': 'password', 'password': 'TEST_ONLY_SSH_PASSWORD', 'work_dir': '/owned/project',
            'host_fingerprint': 'SHA256:'+base64.b64encode(bytes(32)).decode().rstrip('=')})
        run = request('POST', f"/experiments/{experiment['experiment_id']}/runs", headers=headers, expected=(201,),
                      json={'request_id': uuid4().hex, 'parameters': {'epochs': 12}})['run']
        request('POST', f"/runs/{run['run_id']}/start", headers=headers, expected=(202,), json={'server_id': server['server_id'], 'script_id': 'fixture_train'})
        request('POST', f"/runs/{run['run_id']}/metrics/sync", headers=headers)
        request('POST', f"/runs/{run['run_id']}/report", headers=headers)
        request('POST', '/training-tasks', headers=headers, expected=(201,), json={'name': '前端验收任务', 'experiment_id': experiment['experiment_id'], 'server_id': server['server_id'], 'script_id': 'fixture_train', 'default_parameters': {'epochs': 12}})
        session = request('POST', '/sessions', headers=headers, expected=(201,), json={'title': '实验讨论（验收）'})
        request('POST', f"/sessions/{session['session_id']}/messages", headers=headers,
                json={'request_id': uuid4().hex, 'content': '你好，请介绍这个实验工作台。'})
        request('POST', '/papers', headers={**headers, 'Content-Type': 'application/pdf'}, expected=(201,), params={'title': '前端验收论文（测试文本）', 'request_id': uuid4().hex}, content=pdf_bytes(PAPER_PAGES))
    print(f'FRONTEND_QA_READY http://127.0.0.1:{args.port} | account frontend_test | password TestPass123! | fake model and SSH', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)
print('FRONTEND_QA_CLEANED 临时库已清理', flush=True)
