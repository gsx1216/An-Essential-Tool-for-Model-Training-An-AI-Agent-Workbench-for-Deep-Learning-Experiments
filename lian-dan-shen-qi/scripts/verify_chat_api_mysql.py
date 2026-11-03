"""真实聊天HTTP、登录与临时MySQL联调；网关为假对象，无供应商调用。"""

from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from openai import APITimeoutError

from app.config import get_mysql_password
from scripts._chat_api_test_app import temporary_chat_api


def main():
    password = get_mysql_password()
    keys = ['sk-TEST_ONLY_HTTP_ALICE', 'sk-TEST_ONLY_HTTP_ALICE_NEW', 'sk-TEST_ONLY_HTTP_BOB']
    secrets = ['Test1234'] + keys
    with temporary_chat_api(password) as (app, state):
        builder = state['builder']
        secrets.append(state['master'])
        with TestClient(app) as client:
            headers = []
            sessions = []
            users = []
            for username in ('http_chat_alice', 'http_chat_bob'):
                response = client.post('/auth/register', json={'username': username, 'password': 'Test1234'})
                assert response.status_code == 201
                users.append(response.json()['user_id'])
                response = client.post('/auth/login', json={'username': username, 'password': 'Test1234'})
                assert response.status_code == 200
                token = response.json()['access_token']
                secrets.append(token)
                owner = {'Authorization': 'Bearer ' + token}
                headers.append(owner)
                response = client.post('/sessions', headers=owner, json={'title': username})
                assert response.status_code == 201
                sessions.append(response.json()['session_id'])
            alice, bob = headers
            alice_session, bob_session = sessions
            send = f'/sessions/{alice_session}/messages'
            retry = f'/sessions/{alice_session}/retry'
            body = {'content': 'HTTP_FIRST_QUESTION', 'request_id': str(uuid4())}

            def history(owner=alice, session=alice_session):
                response = client.get(f'/sessions/{session}/messages', headers=owner)
                assert response.status_code == 200
                return response.json()

            assert client.post(send, json=body).status_code == 401
            assert client.post(retry).status_code == 401
            assert client.post(send, headers=alice, json=body).status_code == 400
            assert not builder.created and history() == []
            for owner, key in ((alice, keys[0]), (bob, keys[2])):
                assert client.put('/me/model-credentials/deepseek', headers=owner, json={'api_key': key}).status_code == 204
            assert history() == [] and not builder.created

            response = client.post(send, headers=alice, params={'user_id': users[1]}, json=body)
            assert response.status_code == 200
            assert set(response.json()) == {'content'}
            assert response.headers['Cache-Control'] == 'no-store'
            assert response.headers['Pragma'] == 'no-cache'
            assert response.headers['X-Request-ID']
            reply = response.json()['content']
            assert builder.created[-1].api_key == keys[0]
            assert builder.created[-1].closed
            first_history = history()
            assert [(row['seq'], row['role']) for row in first_history] == [(1, 'human'), (2, 'ai')]
            assert first_history[-1]['content'] == reply
            completed_gateway_count=len(builder.created)
            assert client.post(send, headers=alice, json=body).json() == {'content': reply}
            assert len(builder.created)==completed_gateway_count and builder.created[-1].closed
            assert history() == first_history

            response = client.post(send, headers=alice, json={**body, 'content': 'HTTP_DIFFERENT_QUESTION'})
            assert response.status_code == 409
            assert response.json() == {'detail': '该请求编号已用于其他问题，请为新问题生成新的 request_id。'}
            assert response.headers['X-Request-ID']
            assert len(builder.created)==completed_gateway_count and builder.created[-1].closed
            assert history() == first_history

            assert client.post(send, headers=bob, json={**body, 'request_id': str(uuid4())}).status_code == 404
            assert len(builder.created)==completed_gateway_count and builder.created[-1].closed
            response = client.post(f'/sessions/{bob_session}/messages', headers=bob,
                                   json={'content': 'HTTP_BOB_QUESTION', 'request_id': str(uuid4())})
            assert response.status_code == 200 and builder.created[-1].api_key == keys[2]

            assert client.put('/me/model-credentials/deepseek', headers=alice, json={'api_key': keys[1]}).status_code == 204
            builder.next_errors.append(APITimeoutError(httpx.Request('POST', 'https://example.invalid/chat')))
            response = client.post(send, headers=alice, json={'content': 'HTTP_RECOVERABLE_QUESTION', 'request_id': str(uuid4())})
            assert response.status_code == 504
            assert response.json() == {'detail': '模型响应超时，请稍后重试。'}
            assert builder.created[-1].api_key == keys[1] and builder.created[-1].closed
            failed_history = history()
            assert len(failed_history) == 3 and failed_history[-1]['content'] == 'HTTP_RECOVERABLE_QUESTION'
            response = client.post(send, headers=alice,
                                   json={'content': 'HTTP_BLOCKED_NEW_QUESTION', 'request_id': str(uuid4())})
            assert response.status_code == 409
            assert response.json() == {'detail': '当前会话还有未完成的问题，请先重试或新建聊天。'}
            assert builder.created[-1].calls == [] and builder.created[-1].closed
            assert history() == failed_history
            assert client.post(retry, headers=alice).status_code == 200
            recovered = history()
            assert len(recovered) == 4
            assert sum(row['content'] == 'HTTP_RECOVERABLE_QUESTION' for row in recovered) == 1
            assert builder.created[-1].calls[0][1:] == [(row['role'], row['content']) for row in failed_history]
            response = client.post(retry, headers=alice)
            assert response.status_code == 400
            assert response.json() == {'detail': '当前会话没有等待回复的问题。'}
            assert builder.created[-1].calls == [] and builder.created[-1].closed
            assert history() == recovered

            for payload in ({**body, 'user_id': users[1]}, {**body, 'api_key': keys[0]}, {**body, 'request_id': 'bad'}):
                response = client.post(send, headers=alice, json=payload)
                assert response.status_code == 422 and keys[0] not in response.text

            response = client.post('/sessions', headers=alice, json={'title': 'SQL failure'})
            error_session = response.json()['session_id']
            messages = state['messages']
            broken = messages.create_turn_sql.replace('.chat_turns', '._missing_chat_turns')
            assert broken != messages.create_turn_sql
            with patch.object(messages, 'create_turn_sql', broken):
                response = client.post(f'/sessions/{error_session}/messages', headers=alice,
                                       json={'content': 'HTTP_SQL_ROLLBACK', 'request_id': str(uuid4())})
            assert response.status_code == 503 and response.json() == {'detail': '请稍后重试。'}
            assert history(session=error_session) == []
            assert builder.created[-1].calls == [] and builder.created[-1].closed

            assert client.delete('/me/model-credentials/deepseek', headers=alice).status_code == 204
            count = len(builder.created)
            response = client.post(send, headers=alice, json={'content': 'MISSING_KEY', 'request_id': str(uuid4())})
            assert response.status_code == 400 and len(builder.created) == count
            assert history() == recovered
            assert client.post('/auth/logout', headers=alice).status_code == 204
            assert client.post(send, headers=alice, json=body).status_code == 401
            assert all(gateway.closed for gateway in builder.created)
        contents = '\n'.join(path.read_text(encoding='utf-8') for path in state['log_dir'].rglob('*.txt'))
        assert all(secret not in contents for secret in secrets)
        for error_type, reason in (
            ('RequestIdConflictError', '同一请求编号被用于不同问题'),
            ('UnfinishedTurnError', '会话存在尚未完成的提问'),
            ('NoPendingReplyError', '会话没有等待回复的提问'),
        ):
            assert 'error_type=' + error_type in contents
            assert '原因：' + reason in contents
    print('PASS: actual HTTP routes, token authentication, service factories, personal-key encryption, messages and turns with temporary MySQL; fake gateways only.')
    print('Verified: send/retry, ownership, deduplication, updated/deleted keys, timeout retention/recovery, rollback, revoked tokens, cleanup and secret-free logs.')
    print('Verified typed business rejections: request-ID conflict 409, unfinished-turn conflict 409, no-pending-reply 400; no extra messages/model calls and safe log reasons.')
    print('Temporary chat HTTP database removed. Production main/configuration unchanged. No model was called.')


if __name__ == '__main__':
    main()
