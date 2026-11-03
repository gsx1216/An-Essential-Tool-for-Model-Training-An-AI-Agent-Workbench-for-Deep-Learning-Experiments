"""已挂载 HTTP 路由 + 个人 Key + 摘要管线的临时 MySQL 联调；网关全部为假对象。"""

import re
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from openai import APITimeoutError

from app.api import dependencies
from app.config import get_mysql_password
from app.database import get_connection
from app.models.context_settings import ContextSettings
from app.models.context_state import ContextSummary
from app.services.context_manager import SUMMARY_PREFIX, COMPRESSED_INPUT_PREFIX
from app.services.history_summarizer import HISTORY_SUMMARY_PROMPT
from app.services.input_compressor import COMPRESSION_PROMPT
from app.services.token_budget import Utf8BudgetCounter
from app.services.user_chat_service import UserChatService
from scripts._chat_api_test_app import temporary_chat_api


def kind(messages):
    text = messages[0][1]
    if text.startswith(HISTORY_SUMMARY_PROMPT):
        return 'summary'
    if text.startswith(COMPRESSION_PROMPT):
        return 'compress'
    return 'answer'


async def normal(messages, options):
    return {'content': {'summary': '历史摘要：调参目标与已确认限制。',
                        'compress': '当前问题：请解释学习率。', 'answer': '模拟回答：请检查学习率。'}[kind(messages)]}


def main():
    password = get_mysql_password()
    settings = ContextSettings(max_input_tokens=2400, compact_at_tokens=1800, max_summary_tokens=600,
                               keep_recent_turns=1, model_request_tokens=6000)
    groups, secrets = [], ['Test1234', 'sk-TEST_ONLY_CONTEXT_ALICE', 'sk-TEST_ONLY_CONTEXT_BOB']
    with temporary_chat_api(password, settings) as (application, state):
        name = state['database_name']
        assert re.fullmatch(r'_codex_chat_http_[0-9a-f]{32}', name)
        builder, messages, contexts, sessions = state['builder'], state['messages'], state['contexts'], state['sessions']
        builder.handler = normal

        def execute(statement, values=(), fetch=False):
            # 下面所有表名为固定文本，库名只来自上面的本次随机临时库。
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement, values)
                    rows = cursor.fetchall() if fetch else None
                connection.commit()
                return rows

        def summary_row(sid):
            rows = execute(f'SELECT content, summarized_until_seq, version FROM `{name}`.chat_context_summaries WHERE session_id=%s', (sid,), True)
            return rows[0] if rows else None

        def seed(owner, turns=3, size=400):
            sid = sessions.create_session(owner, 'temporary seeded context')
            for number in range(turns):
                tid = messages.start_turn(owner, sid, f'SEED_{number}_' + 'a' * size)
                aid, _ = messages.claim_turn(owner, sid, tid)
                messages.complete_turn(owner, sid, tid, aid, 'seeded answer')
            return sid

        with TestClient(application) as client:
            owners, user_ids = [], []
            for index, username in enumerate(('context_http_alice', 'context_http_bob')):
                response = client.post('/auth/register', json={'username': username, 'password': secrets[0]})
                assert response.status_code == 201
                user_ids.append(response.json()['user_id'])
                login = client.post('/auth/login', json={'username': username, 'password': secrets[0]})
                assert login.status_code == 200
                token = login.json()['access_token']
                secrets.append(token)
                owner = {'Authorization': 'Bearer ' + token}
                owners.append(owner)
                assert client.put('/me/model-credentials/deepseek', headers=owner,
                                  json={'api_key': secrets[index + 1]}).status_code == 204
            alice, bob = owners
            alice_id, bob_id = user_ids

            def send(sid, text, owner=alice, rid=None):
                body = {'content': text, 'request_id': str(rid or uuid4())}
                return client.post(f'/sessions/{sid}/messages', headers=owner, json=body), body

            def retry(sid):
                return client.post(f'/sessions/{sid}/retry', headers=alice)

            def history(sid, owner=alice):
                response = client.get(f'/sessions/{sid}/messages', headers=owner)
                assert response.status_code == 200
                return response.json()

            # 多轮请求经过实际认证、个人 Key 解密、任务领取、摘要读取、生成和持久化。
            sid = sessions.create_session(alice_id, 'HTTP context')
            for number in range(7):
                response, _ = send(sid, f'USER_MARKER_{number}_' + 'x' * 400)
                assert response.status_code == 200, (response.status_code, response.json())
                assert builder.created[-1].api_key == secrets[1] and builder.created[-1].closed
            stored = summary_row(sid)
            assert stored is not None and stored[1] >= 2
            raw = history(sid)
            assert len(raw) == 14 and raw[0]['content'].startswith('USER_MARKER_0_')
            response, body = send(sid, '继续解释这个参数')
            assert response.status_code == 200
            last_gateway = builder.created[-1]
            answer = next(call for call in reversed(last_gateway.calls) if kind(call) == 'answer')
            assert any(text.startswith(SUMMARY_PREFIX) for role, text in answer)
            assert not any('USER_MARKER_0_' in text for role, text in answer)
            assert answer.count(('human', '继续解释这个参数')) == 1
            assert Utf8BudgetCounter().count_messages(answer) <= settings.max_input_tokens
            assert len(history(sid)) == 16
            groups.append('real mounted send route compacts history, reuses saved summary, and preserves complete UI history')

            before = summary_row(sid)
            response = client.post(f'/sessions/{sid}/messages', headers=alice, json=body)
            assert response.status_code == 200 and builder.created[-1].calls == []
            assert summary_row(sid) == before and len(history(sid)) == 16
            groups.append('same completed request_id returns saved reply with no extra summary or answer call')

            # 重建业务服务和 ContextManager 后仍从相同仓储恢复，不依赖内存列表。
            original_service = state['chat']
            restored_service = UserChatService(original_service.gateway_factory, messages,
                context_repository=contexts, context_settings=settings)
            application.dependency_overrides[dependencies.get_user_chat_service] = lambda: restored_service
            response, _ = send(sid, '重建服务后的追问')
            assert response.status_code == 200
            assert any(text.startswith(SUMMARY_PREFIX) for role, text in builder.created[-1].calls[-1])
            groups.append('recreated request service restores summary from MySQL')

            denied, _ = send(sid, 'other user request', owner=bob)
            assert denied.status_code == 404 and builder.created[-1].calls == []
            bob_sid = sessions.create_session(bob_id)
            assert send(bob_sid, 'Bob question', owner=bob)[0].status_code == 200
            assert builder.created[-1].api_key == secrets[2]
            groups.append('token ownership and personal gateway isolation remain enforced')

            # 摘要先成功落库，回答失败；重试不能再新增一条用户问题。
            failing = seed(alice_id, turns=4, size=400)
            async def fail_answer(request, options):
                if kind(request) == 'answer':
                    raise APITimeoutError(httpx.Request('POST', 'https://example.invalid/fake'))
                return await normal(request, options)
            builder.handler = fail_answer
            response, _ = send(failing, 'ANSWER_TIMEOUT_PENDING')
            assert response.status_code == 504, (response.status_code, response.json())
            assert summary_row(failing) is not None
            assert history(failing)[-1]['content'] == 'ANSWER_TIMEOUT_PENDING'
            builder.handler = normal
            assert retry(failing).status_code == 200
            raw = history(failing)
            assert len(raw) == 10 and sum(m['content'] == 'ANSWER_TIMEOUT_PENDING' for m in raw) == 1
            groups.append('saved summary survives answer timeout; retry reuses original pending question')

            failing_summary = seed(alice_id, turns=4, size=400)
            async def fail_summary(request, options):
                if kind(request) == 'summary':
                    raise RuntimeError('PRIVATE_PROVIDER_CONTEXT_ERROR')
                return await normal(request, options)
            builder.handler = fail_summary
            response, _ = send(failing_summary, 'SUMMARY_FAILURE_PENDING')
            assert response.status_code == 502 and response.json() == {'detail': '上下文处理暂时不可用，请稍后重试。'}
            assert summary_row(failing_summary) is None
            assert messages.get_unfinished_turn(alice_id, failing_summary).error_code == 'CONTEXT_PREPARATION_FAILED'
            assert all(kind(call) != 'answer' for call in builder.created[-1].calls)
            assert len(history(failing_summary)) == 9
            builder.handler = normal
            assert retry(failing_summary).status_code == 200
            assert len(history(failing_summary)) == 10
            groups.append('summary failure is safe HTTP 502, keeps question, and can be retried')

            long_sid = sessions.create_session(alice_id)
            long_text = 'LONG_CURRENT_ORIGINAL_' + 'x' * 8000
            response, _ = send(long_sid, long_text)
            assert response.status_code == 200, (response.status_code, response.json())
            gateway = builder.created[-1]
            assert any(kind(call) == 'compress' for call in gateway.calls)
            assert gateway.calls[-1][-1][1].startswith(COMPRESSED_INPUT_PREFIX)
            assert history(long_sid)[0]['content'] == long_text and summary_row(long_sid) is None
            groups.append('overlong current input uses compression and preserves full original message')

            impossible = sessions.create_session(alice_id)
            tid = messages.start_turn(alice_id, impossible, 'small historical question')
            aid, _ = messages.claim_turn(alice_id, impossible, tid)
            messages.complete_turn(alice_id, impossible, tid, aid, 'y' * 4000)
            response, _ = send(impossible, '不可丢失的当前问题')
            assert response.status_code == 413 and builder.created[-1].calls == []
            assert len(history(impossible)) == 3
            groups.append('irreducible recent history is HTTP 413 without losing the pending question')

            expired = seed(alice_id, turns=4, size=400)
            async def expire_during_summary(request, options):
                if kind(request) == 'summary':
                    execute(f'UPDATE `{name}`.chat_turns SET lease_until=TIMESTAMPADD(SECOND,-1,CURRENT_TIMESTAMP(6)) WHERE session_id=%s AND status=%s',
                            (expired, 'running'))
                return await normal(request, options)
            builder.handler = expire_during_summary
            response, _ = send(expired, 'LEASE_EXPIRY_PENDING')
            assert response.status_code == 409, (response.status_code, response.json())
            assert summary_row(expired) is None and len(history(expired)) == 9
            builder.handler = normal
            assert retry(expired).status_code == 200
            groups.append('expired attempt cannot save summary; a new retry attempt can continue')

            # 第一批返回后租约过期，第二批不得再访问模型。
            multi = seed(alice_id, turns=2, size=4000)
            third = messages.start_turn(alice_id, multi, 'recent small question')
            third_aid, _ = messages.claim_turn(alice_id, multi, third)
            messages.complete_turn(alice_id, multi, third, third_aid, 'small recent reply')
            async def expire_after_first_batch(request, options):
                if kind(request) == 'summary':
                    execute(f'UPDATE `{name}`.chat_turns SET lease_until=TIMESTAMPADD(SECOND,-1,CURRENT_TIMESTAMP(6)) WHERE session_id=%s AND status=%s',
                            (multi, 'running'))
                return await normal(request, options)
            builder.handler = expire_after_first_batch
            response, _ = send(multi, 'MULTI_BATCH_PENDING')
            assert response.status_code == 409, (response.status_code, response.json())
            assert len(builder.created[-1].calls) == 1 and summary_row(multi) is None
            groups.append('each summary batch rechecks attempt before another provider call')

            replaced = seed(alice_id, turns=4, size=400)
            newer_attempt = uuid4().hex
            async def replace_attempt(request, options):
                if kind(request) == 'summary':
                    execute(f'UPDATE `{name}`.chat_turns SET attempt_id=%s WHERE session_id=%s AND status=%s',
                            (newer_attempt, replaced, 'running'))
                return await normal(request, options)
            builder.handler = replace_attempt
            response, _ = send(replaced, 'REPLACED_ATTEMPT_PENDING')
            assert response.status_code == 409 and summary_row(replaced) is None
            live = execute(f'SELECT status, attempt_id FROM `{name}`.chat_turns WHERE session_id=%s AND open_slot=1', (replaced,), True)[0]
            assert live == ('running', newer_attempt)
            groups.append('old attempt cannot save summary or mark a replacement attempt failed')

            deleted = seed(alice_id, turns=4, size=400)
            async def delete_during_summary(request, options):
                if kind(request) == 'summary':
                    assert sessions.delete_session(alice_id, deleted)
                return await normal(request, options)
            builder.handler = delete_during_summary
            response, _ = send(deleted, 'DELETED_DURING_SUMMARY')
            assert response.status_code == 404 and summary_row(deleted) is None
            assert execute(f'SELECT COUNT(*) FROM `{name}`.chat_messages WHERE session_id=%s', (deleted,), True)[0][0] == 0
            groups.append('deletion while summary is in flight cannot resurrect summary or messages')

            late_reply = sessions.create_session(alice_id)
            async def delete_during_answer(request, options):
                if kind(request) == 'answer':
                    assert sessions.delete_session(alice_id, late_reply)
                return await normal(request, options)
            builder.handler = delete_during_answer
            response, _ = send(late_reply, 'DELETED_DURING_ANSWER')
            assert response.status_code == 404
            assert execute(f'SELECT COUNT(*) FROM `{name}`.chat_messages WHERE session_id=%s', (late_reply,), True)[0][0] == 0
            groups.append('deletion during final answer still rejects late reply writeback')

            corrupt = seed(alice_id)
            contexts.save_summary(alice_id, corrupt, ContextSummary('summary', 2, 1),
                                  expected_version=0, expected_tail_seq=6)
            execute(f'UPDATE `{name}`.chat_context_summaries SET missing_ranges=%s WHERE session_id=%s',
                    ('[{"start_seq":1,"end_seq":999}]', corrupt))
            builder.handler = normal
            response, _ = send(corrupt, 'CORRUPT_CONTEXT_PENDING')
            assert response.status_code == 503 and response.json() == {'detail': '请稍后重试。'}
            assert builder.created[-1].calls == []
            groups.append('invalid stored summary is safe HTTP 503 before any model call')

            assert all(gateway.closed for gateway in builder.created)
            assert '413' in client.get('/openapi.json').json()['paths']['/sessions/{session_id}/messages']['post']['responses']
            groups.append('gateways close on every outcome and OpenAPI documents context errors')

        log_text = '\n'.join(p.read_text(encoding='utf-8') for p in state['log_dir'].rglob('*.txt'))
        assert all(secret not in log_text for secret in secrets + [password, state['master'], 'PRIVATE_PROVIDER_CONTEXT_ERROR', long_text])
        for error_type in ('ContextConflictError', 'ContextCompactionError', 'ContextTooLargeError', 'ContextStorageError'):
            assert error_type in log_text
        groups.append('persistent logs locate context failures without credentials or message bodies')
    for group in groups:
        print('PASS:', group)
    print(f'PASS: {len(groups)} HTTP + real MySQL context verification groups; fake providers only; temporary database removed.')


if __name__ == '__main__':
    main()
