"""冗余修复 HTTP/MySQL 验收：随机临时库，模拟模型，不连接训练服务器。"""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import get_mysql_password
from app.database import get_connection
from scripts._chat_api_test_app import temporary_chat_api


def verify(password):
    groups = []

    def passed(label):
        groups.append(label)
        print('PASS:', label, flush=True)

    with temporary_chat_api(password) as (app, state), TestClient(app) as client:
        name = state['database_name']

        def user(label):
            username = label + uuid4().hex[:8]
            payload = {'username': username, 'password': 'TestPass123!'}
            result = client.post('/auth/register', json=payload)
            assert result.status_code == 201, result.text
            result = client.post('/auth/login', json=payload)
            assert result.status_code == 200, result.text
            data = result.json()
            return data['user']['user_id'], {'Authorization': 'Bearer ' + data['access_token']}

        def count(table):
            assert table in {'chat_messages', 'chat_turns'}
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'SELECT COUNT(*) FROM `{name}`.`{table}`')
                    return cursor.fetchone()[0]

        uid, owner = user('cleanup_owner')
        other_uid, other = user('cleanup_other')
        result = client.post('/sessions', headers=owner, json={'title': 'cleanup check'})
        assert result.status_code == 201, result.text
        sid = result.json()['session_id']
        assert client.put('/me/model-credentials/deepseek', headers=owner,
                          json={'api_key': 'sk-TEST_ONLY_CLEANUP_OWNER'}).status_code == 204
        rid = uuid4().hex
        body = {'content': 'original question', 'request_id': rid}
        result = client.post(f'/sessions/{sid}/messages', headers=owner, json=body)
        assert result.status_code == 200, result.text
        original = result.json()
        assert count('chat_messages') == 2 and count('chat_turns') == 1
        passed('new question persists one human/AI turn through real SQL and fake gateway')

        created = len(state['builder'].created)
        assert client.delete('/me/model-credentials/deepseek', headers=owner).status_code == 204
        for _ in range(2):
            result = client.post(f'/sessions/{sid}/messages', headers=owner, json=body)
            assert result.status_code == 200 and result.json() == original, result.text
        assert len(state['builder'].created) == created
        assert count('chat_messages') == 2 and count('chat_turns') == 1
        passed('completed replay works after key deletion without gateway construction or new messages')

        changed = client.post(f'/sessions/{sid}/messages', headers=owner,
                              json={**body, 'content': 'different question'})
        assert changed.status_code == 409, changed.text
        passed('same request ID with different content stays 409 even without model key')

        denied = client.post(f'/sessions/{sid}/messages', headers=other, json=body)
        missing = client.post(f'/sessions/{uuid4().hex}/messages', headers=owner, json=body)
        assert denied.status_code == missing.status_code == 404, (denied.text, missing.text)
        assert client.post(f'/sessions/{sid}/messages', json=body).status_code == 401
        assert len(state['builder'].created) == created
        passed('foreign/missing sessions remain 404 and absent login remains 401 before gateway loading')

        result = client.post(f'/sessions/{sid}/messages', headers=owner,
                             json={'content': 'new question without key', 'request_id': uuid4().hex})
        assert result.status_code == 400, result.text
        assert count('chat_messages') == 2 and count('chat_turns') == 1
        passed('new question without key still fails without saving a pending question')

        assert state['sessions'].list_sessions(other_uid) == []
        assert state['sessions'].get_session(other_uid, sid) is None
        assert state['sessions'].get_session(uid, sid)[0] == sid
        passed('simplified session reads retain owner isolation and empty-list semantics')

        assert client.put('/me/model-credentials/deepseek', headers=owner,
                          json={'api_key': 'sk-TEST_ONLY_CLEANUP_RECONFIGURED'}).status_code == 204
        result = client.post(f'/sessions/{sid}/messages', headers=owner,
                             json={'content': 'new question after key reconfiguration', 'request_id': uuid4().hex})
        assert result.status_code == 200, result.text
        assert count('chat_messages') == 4 and count('chat_turns') == 2
        assert len(state['builder'].created) == created + 1
        passed('normal new chat continues with fresh personal key and fresh saved context')

        assert client.delete(f'/sessions/{sid}', headers=other).status_code == 404
        assert client.delete(f'/sessions/{sid}', headers=owner).status_code == 204
        assert count('chat_messages') == count('chat_turns') == 0
        assert client.post(f'/sessions/{sid}/messages', headers=owner, json=body).status_code == 404
        passed('session deletion still removes messages/turns and blocks later replay')

    print(f'ALL {len(groups)} cleanup integration groups passed; temporary DB removed; no real model/GPU.', flush=True)
    return len(groups)


def main():
    verify(get_mysql_password())


if __name__ == '__main__':
    main()
