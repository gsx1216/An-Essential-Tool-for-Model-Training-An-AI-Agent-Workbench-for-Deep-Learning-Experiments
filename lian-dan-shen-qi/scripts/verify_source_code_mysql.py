"""真实 HTTP/MySQL 的源码工具验收；远端文件为替身，模型为模拟，不启动训练。"""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import get_mysql_password
from app.database import get_connection
from app.models.runner import RunnerSettings
from app.runner.source_reader import SourceReadError, excerpt
from app.services.source_transport import validate_response
from scripts._chat_api_test_app import temporary_chat_api


class SourceFixture:
    def __init__(self):
        self.files = {'train.py': b'import os\nvalue = 1\nprint(value)\n',
                      'models/net.py': b'class Model:\n    pass\n',
                      'config.json': b'{"password":"TEST_ONLY_NOT_FOR_MODEL"}'}
        self.calls = []

    async def request(self, profile, credentials, payload):
        self.calls.append(dict(payload))
        assert payload['work_dir'] == '/owned/project' and credentials['password'] == 'TEST_ONLY_SSH_PASSWORD'
        if payload['action'] == 'list':
            data = {'path': '.', 'items': [{'path': 'models', 'kind': 'directory'}, {'path': 'train.py', 'kind': 'file'}],
                    'offset': payload['offset'], 'next_offset': None}
        else:
            if payload['path'] not in self.files: raise SourceReadError('NOT_FOUND')
            raw = self.files[payload['path']]
            digest = hashlib.sha256(raw).hexdigest()
            if payload.get('expected_sha256') not in (None, digest): raise SourceReadError('SOURCE_CHANGED')
            data = {'path': payload['path'], 'file_sha256': digest, 'file_bytes': len(raw), 'modified_ns': 1,
                    **excerpt(raw, payload['start_line'], payload['line_count'])}
        return validate_response({'ok': True, 'data': data}, payload)


def verify(password):
    groups = []
    fixture = SourceFixture()
    spec = {'path': 'train.py', 'sha256': hashlib.sha256(fixture.files['train.py']).hexdigest(),
            'parameters': {}, 'max_runtime_seconds': 10, 'max_log_bytes': 1000}
    settings = RunnerSettings(json.dumps({'source_fixture': spec}))
    with temporary_chat_api(password, runner_settings=settings, source_code=True, training=True) as (app, state), TestClient(app) as client:
        state['sources'].transport = fixture

        def passed(label):
            groups.append(label); print('PASS:', label, flush=True)

        def user(label):
            payload = {'username': label + uuid4().hex[:8], 'password': 'TestPass123!'}
            assert client.post('/auth/register', json=payload).status_code == 201
            response = client.post('/auth/login', json=payload)
            assert response.status_code == 200, response.text
            data = response.json()
            return data['user']['user_id'], {'Authorization': 'Bearer ' + data['access_token']}

        uid, owner = user('source_owner_')
        other_uid, other = user('source_other_')
        response = client.post('/servers', headers=owner, json={'name': 'source fixture', 'host': '127.0.0.1',
            'port': 22, 'username': 'trainer', 'auth_method': 'password', 'password': 'TEST_ONLY_SSH_PASSWORD',
            'host_fingerprint': 'SHA256:' + base64.b64encode(bytes(32)).decode().rstrip('='), 'work_dir': '/owned/project'})
        assert response.status_code == 201, response.text
        server_id = response.json()['server_id']
        response = client.post('/experiments', headers=owner, json={'title': 'source test', 'kind': 'training'})
        assert response.status_code == 201, response.text
        experiment_id = response.json()['experiment_id']
        response = client.post(f'/experiments/{experiment_id}/runs', headers=owner,
                               json={'request_id': uuid4().hex, 'parameters': {}})
        assert response.status_code == 201, response.text
        run_id = response.json()['run']['run_id']
        profile = asyncio.run(state['server_service'].get(uid, server_id))
        launch = {'version': 1, 'run_id': run_id, 'user_id': uid, 'server_id': server_id,
                  'server_binding': profile.binding(), 'script_id': 'source_fixture', 'work_dir': profile.work_dir,
                  'spec': spec, 'parameters': {}, 'output_directory': 'training_outputs'}
        # 仅创建所需执行绑定，不调用 runner.start，也不执行注册脚本。
        state['executions'].claim(uid, run_id, profile, 'source_fixture', launch)
        initial_status = state['runs'].get_run(uid, run_id).status

        prefix = f'/runs/{run_id}/code'
        response = client.get(prefix + '/files', headers=owner)
        assert response.status_code == 200 and response.json()['entry_path'] == 'train.py', response.text
        assert response.headers['Cache-Control'] == 'no-store'
        response = client.get(prefix + '/file?line_count=1', headers=owner)
        assert response.status_code == 200, response.text
        first = response.json()
        assert first['lines'] == [{'number': 1, 'text': 'import os'}]
        assert first['training_entry_match'] is True and first['version_scope'] == 'current_file'
        assert not state['builder'].created
        passed('authenticated source APIs work without a model key; return current numbered entry and no-store')

        response = client.get(prefix + '/file', headers=owner, params={'path': 'models/net.py'})
        assert response.status_code == 200 and response.json()['training_entry_match'] is None, response.text
        passed('dependent model file is not claimed to match historical training code')

        before = len(fixture.calls)
        assert client.get(prefix + '/file', headers=other).status_code == 404
        assert client.get(prefix + '/file').status_code == 401
        assert len(fixture.calls) == before
        for params in ({'path': '../private.py'}, {'path': '.env'}, {'line_count': 61}, {'start_line': 0}, {'expected_sha256': 'bad'}):
            assert client.get(prefix + '/file', headers=owner, params=params).status_code == 422
        assert len(fixture.calls) == before
        passed('foreign identity, missing token and invalid path/window/version stop before remote access')

        assert client.get(prefix + '/file?path=missing.py', headers=owner).status_code == 404
        response = client.get(prefix + '/file?path=config.json', headers=owner)
        assert response.status_code == 403 and 'TEST_ONLY_NOT_FOR_MODEL' not in response.text
        response = client.get(prefix + '/file', headers=owner, params={'expected_sha256': '0'*64})
        assert response.status_code == 409, response.text
        passed('missing file, credential-containing file and stale file version have explicit safe errors')

        assert client.put('/me/model-credentials/deepseek', headers=owner,
                          json={'api_key': 'sk-TEST_ONLY_SOURCE_OWNER'}).status_code == 204
        response = client.post('/sessions', headers=owner, json={'title': 'inspect code'})
        assert response.status_code == 201, response.text
        sid = response.json()['session_id']

        def plan(replies):
            queue = list(replies)
            async def handler(messages, options):
                assert queue, 'fake model script exhausted'
                return queue.pop(0)
            state['builder'].handler = handler

        def call(name, args, identifier):
            return {'content': '', 'tool_calls': [{'id': identifier, 'name': name, 'args': args}]}

        plan([call('get_latest_training_run', {}, 'latest'), call('list_project_files', {}, 'files'),
              call('read_project_file', {'path': 'train.py', 'line_count': 1}, 'first'),
              call('read_project_file', {'path': 'train.py', 'start_line': 2, 'line_count': 1}, 'second'),
              {'content': '入口导入 os [code_1]，随后初始化 value [code_2]。'}])
        response = client.post(f'/sessions/{sid}/messages', headers=owner,
                               json={'content': '检查最近训练的入口代码', 'request_id': uuid4().hex})
        assert response.status_code == 200, response.text
        assert '第 1～1 行' in response.json()['content'] and '第 2～2 行' in response.json()['content']
        assert fixture.calls[-1]['expected_sha256'] == first['file_sha256']
        response = client.get(f'/sessions/{sid}/tool-calls', headers=owner)
        assert response.status_code == 200, response.text
        records = response.json()['items']
        evidence = [row['result_metadata']['code_evidence'] for row in records if 'code_evidence' in row['result_metadata']]
        assert len(evidence) == 2 and evidence[0]['lines'] == first['lines']
        assert evidence[0]['file_sha256'] == first['file_sha256']
        passed('chat binds the owned run, lists/reads real SQL-audited excerpts and auto-checks same-file paging version')

        assert client.get(f'/sessions/{sid}/tool-calls', headers=other).status_code == 404
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f'SELECT MAX(OCTET_LENGTH(result_meta_json)) FROM `{state["database_name"]}`.chat_tool_calls')
                assert cursor.fetchone()[0] <= 4096
        passed('persisted code evidence obeys existing metadata size and session ownership')

        before = len(fixture.calls)
        plan([{'content': '你好。'}])
        response = client.post(f'/sessions/{sid}/messages', headers=owner,
                               json={'content': '你好', 'request_id': uuid4().hex})
        assert response.status_code == 200 and len(fixture.calls) == before, response.text
        passed('ordinary chat with source tools enabled makes no remote source request')

        plan([call('read_project_file', {'path': 'config.json'}, 'secret'), {'content': '源码未取得，不推测内容。'}])
        response = client.post(f'/sessions/{sid}/messages', headers=owner,
                               json={'content': '读取配置代码', 'request_id': uuid4().hex})
        assert response.status_code == 200 and '代码读取依据' not in response.json()['content'], response.text
        records = client.get(f'/sessions/{sid}/tool-calls', headers=owner).json()['items']
        assert records[-1]['outcome'] == 'SOURCE_SENSITIVE_CONTENT'
        assert 'code_evidence' not in records[-1]['result_metadata']
        assert 'TEST_ONLY_NOT_FOR_MODEL' not in json.dumps(records)
        passed('failed/filtered source read is recorded as failure without fabricated evidence or secret content')

        fixture.files['train.py'] += b'value = 2\n'
        assert client.get(prefix + '/file', headers=owner, params={'expected_sha256': first['file_sha256']}).status_code == 409
        response = client.get(prefix + '/file', headers=owner)
        assert response.status_code == 200 and response.json()['training_entry_match'] is False
        passed('changed live entry is explicit and does not overwrite earlier stored code excerpts')

        assert client.delete(f'/sessions/{sid}', headers=owner).status_code == 204
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f'SELECT COUNT(*) FROM `{state["database_name"]}`.chat_tool_calls')
                assert cursor.fetchone()[0] == 0
        assert not client.get(prefix + '/file', headers=owner).json()['training_entry_match']
        assert state['runs'].get_run(uid, run_id).status == initial_status
        passed('deleting chat cascades its evidence while the owned run remains independently readable')

        logs = '\n'.join(p.read_text(encoding='utf-8') for p in state['log_dir'].rglob('*.txt'))
        assert all(secret not in logs for secret in ('TEST_ONLY_SSH_PASSWORD', 'sk-TEST_ONLY_SOURCE_OWNER', 'TEST_ONLY_NOT_FOR_MODEL'))
        passed('request and operation logs exclude SSH credentials, model key and filtered source values')
    print(f'ALL {len(groups)} source HTTP/MySQL groups passed; temporary DB removed; fake model/remote only.', flush=True)
    return len(groups)


if __name__ == '__main__':
    verify(get_mysql_password())
