"""真实 HTTP/MySQL/Redis 缓存联调；随机临时库和 Redis，全部模型均为替身。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import time
import socket
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
from mysql.connector import Error as MySQLError
from openai import APITimeoutError
from redis.exceptions import ConnectionError as RedisConnectionError

from app.config import get_mysql_password
from app.models.context_cache import ContextCacheSettings
from app.models.context_settings import ContextSettings
from app.models.context_snapshot import ContextSnapshot
from app.models.context_state import ContextMessage, ContextSummary
from app.services.redis_context_cache import RedisContextCache
from app.services.context_manager import SUMMARY_PREFIX
from scripts._chat_api_test_app import temporary_chat_api
from scripts._temporary_redis import temporary_redis


def main():
    password = get_mysql_password()
    groups = []
    clock = [0.0]
    with temporary_redis() as state:
        settings = ContextCacheSettings(redis_url=state['url'], prefix='cache_verify_' + uuid4().hex)
        master = Fernet.generate_key()
        cache = RedisContextCache(settings, lambda: master, clock=lambda: clock[0])
        try:
            with temporary_chat_api(password, context_cache=cache) as (app, fixture), TestClient(app) as client:
                contexts, messages, sessions = fixture['contexts'], fixture['messages'], fixture['sessions']
                builder = fixture['builder']

                def login(username):
                    response = client.post('/auth/register', json={'username': username, 'password': 'TestPass123!'})
                    assert response.status_code == 201, response.text
                    uid = response.json()['user_id']
                    response = client.post('/auth/login', json={'username': username, 'password': 'TestPass123!'})
                    assert response.status_code == 200
                    headers = {'Authorization': 'Bearer ' + response.json()['access_token']}
                    assert client.put('/me/model-credentials/deepseek', headers=headers, json={'api_key': 'sk-TEST_ONLY_CACHE'}).status_code == 204
                    return uid, headers

                owner_id, owner = login('cache_owner_' + uuid4().hex[:8])
                other_id, other = login('cache_other_' + uuid4().hex[:8])

                def send(sid, text='当前问题', *, headers=owner, request_id=None):
                    body = {'content': text, 'request_id': request_id or uuid4().hex}
                    return client.post(f'/sessions/{sid}/messages', headers=headers, json=body), body

                def history(sid):
                    response = client.get(f'/sessions/{sid}/messages', headers=owner)
                    assert response.status_code == 200
                    return response.json()

                def passed(text):
                    groups.append(text)
                    print('PASS:', text)

                sid = sessions.create_session(owner_id, 'Redis cache HTTP fixture')
                with patch.object(contexts, '_read_snapshot', wraps=contexts._read_snapshot) as full:
                    response, _ = send(sid, 'UNIQUE_PRIVATE_QUESTION_MARKER_1')
                    assert response.status_code == 200, response.text
                    assert full.call_count == 1
                snapshot = cache.load(owner_id, sid)
                assert snapshot.tail_seq == 2 and snapshot.messages[-1].role == 'ai'
                raw = state['client'].hgetall(cache._key(owner_id, sid))
                assert 'UNIQUE_PRIVATE_QUESTION_MARKER_1' not in str(raw)
                assert settings.ttl_seconds - 60 <= state['client'].ttl(cache._key(owner_id, sid)) <= settings.ttl_seconds
                assert len(history(sid)) == 2
                passed('cold load persists original messages and encrypted cache with expiring TTL')

                with patch.object(contexts, '_read_snapshot', wraps=contexts._read_snapshot) as full, \
                     patch.object(contexts, '_read_messages', wraps=contexts._read_messages) as reads:
                    response, body = send(sid, '第二个问题')
                    assert response.status_code == 200, response.text
                    full.assert_not_called()
                    assert [call.args[3] for call in reads.call_args_list] == [2]
                assert cache.load(owner_id, sid).tail_seq == 4
                assert len(history(sid)) == 4
                passed('warm route reads only new human message from MySQL and caches committed AI reply')

                response = client.post(f'/sessions/{sid}/messages', headers=owner, json=body)
                assert response.status_code == 200 and builder.created[-1].calls == []
                assert len(history(sid)) == 4 and cache.load(owner_id, sid).tail_seq == 4
                passed('duplicate request_id returns persisted reply without duplicate model call or cache append')

                foreign_key = cache._key(other_id, sid)
                state['client'].hset(foreign_key, mapping=state['client'].hgetall(cache._key(owner_id, sid)))
                denied, _ = send(sid, 'foreign request', headers=other)
                assert denied.status_code == 404 and builder.created[-1].calls == []
                assert len(history(sid)) == 4
                clock[0] += 11
                passed('copied cache payload cannot cross users and MySQL ownership still rejects request')

                state['client'].pexpire(cache._key(owner_id, sid), 1)
                time.sleep(0.02)
                with patch.object(contexts, '_read_snapshot', wraps=contexts._read_snapshot) as full:
                    response, _ = send(sid, '缓存过期后继续')
                    assert response.status_code == 200 and full.call_count == 1
                assert cache.load(owner_id, sid).tail_seq == 6 and len(history(sid)) == 6
                passed('real Redis expiry reloads MySQL and automatically rebuilds context')

                # 模拟另一个进程已经发布了新版摘要；旧缓存不能盖过它。
                contexts.save_summary(owner_id, sid, ContextSummary('已确认前两轮的事实摘要', 4, 1),
                                      expected_version=0, expected_tail_seq=6)
                with patch.object(contexts, '_read_snapshot', wraps=contexts._read_snapshot) as full:
                    response, _ = send(sid, '新版摘要后的追问')
                    assert response.status_code == 200 and full.call_count == 1
                snapshot = cache.load(owner_id, sid)
                assert snapshot.version == 1 and snapshot.messages[0].seq == 5 and snapshot.tail_seq == 8
                assert any(text.startswith(SUMMARY_PREFIX) for role, text in builder.created[-1].calls[-1])
                passed('summary version changed by another process reloads authoritative summary and uncovered text')

                failed_sid = sessions.create_session(owner_id, 'retry fixture')
                builder.next_errors = [APITimeoutError(httpx.Request('POST', 'https://example.invalid/fake'))]
                failed, _ = send(failed_sid, '失败后保留这个问题')
                assert failed.status_code == 504, failed.text
                assert cache.load(owner_id, failed_sid).tail_seq == 1 and len(history(failed_sid)) == 1
                response = client.post(f'/sessions/{failed_sid}/retry', headers=owner)
                assert response.status_code == 200, response.text
                assert len(history(failed_sid)) == 2 and cache.load(owner_id, failed_sid).tail_seq == 2
                passed('model timeout keeps saved pending question; retry uses same turn and appends one reply')

                pool = cache._connection()
                with patch.object(pool, 'hgetall', side_effect=RedisConnectionError('PRIVATE_REDIS_PASSWORD')):
                    response, _ = send(sid, 'Redis 不可用时继续')
                    assert response.status_code == 200, response.text
                assert len(history(sid)) == 10
                clock[0] += 11
                response, _ = send(sid, 'Redis 恢复后继续')
                assert response.status_code == 200 and cache.load(owner_id, sid).tail_seq == 12
                passed('Redis read outage bypasses cache and recovers with MySQL delta after backoff')

                original_eval = pool.eval
                def fail_reply_write(script, n, key, *args):
                    if len(args) == 4 and int(args[1]) % 2 == 0:
                        raise RedisConnectionError('PRIVATE_WRITE_FAILURE')
                    return original_eval(script, n, key, *args)
                with patch.object(pool, 'eval', side_effect=fail_reply_write):
                    response, _ = send(sid, '已提交回复不能因为缓存失败而变成失败')
                    assert response.status_code == 200, response.text
                assert len(history(sid)) == 14
                clock[0] += 11
                passed('post-commit cache write failure does not turn a successful saved reply into HTTP error')

                state['client'].hset(cache._key(owner_id, sid), 'payload', 'invalid ciphertext')
                response, _ = send(sid, '损坏缓存自动降级')
                assert response.status_code == 200 and len(history(sid)) == 16
                clock[0] += 11
                response, _ = send(sid, '重建缓存')
                assert response.status_code == 200 and cache.load(owner_id, sid).tail_seq == 18
                passed('tampered ciphertext is discarded and rebuilt without forwarding it to the model')

                with patch.object(contexts, 'reconcile_context', side_effect=MySQLError('PRIVATE_DB_FAILURE', errno=2013)):
                    response, _ = send(sid, '数据库故障不能当作没有历史')
                    assert response.status_code == 503 and builder.created[-1].calls == [], response.text
                assert 'PRIVATE_DB_FAILURE' not in response.text
                assert client.post(f'/sessions/{sid}/retry', headers=owner).status_code == 200
                passed('warm Redis never hides MySQL ownership/transaction failure or sends unverified context')

                compact_sid = sessions.create_session(owner_id, 'compaction fixture')
                for i in range(4):
                    turn = messages.start_turn(owner_id, compact_sid, f'OLD_{i}_' + 'x' * 400)
                    attempt, _ = messages.claim_turn(owner_id, compact_sid, turn)
                    messages.complete_turn(owner_id, compact_sid, turn, attempt, '历史回复')
                original_settings = fixture['chat'].context_settings
                fixture['chat'].context_settings = ContextSettings(max_input_tokens=2400, compact_at_tokens=1800,
                    max_summary_tokens=600, keep_recent_turns=1, model_request_tokens=6000)
                response, _ = send(compact_sid, '需要保留的当前问题')
                fixture['chat'].context_settings = original_settings
                assert response.status_code == 200, response.text
                compacted = cache.load(owner_id, compact_sid)
                assert compacted.version > 0 and compacted.messages[-1].role == 'ai'
                assert compacted.messages[0].seq > compacted.summary.summarized_until_seq
                assert len(history(compact_sid)) == 10
                passed('real context compaction caches committed new summary and retains all original UI messages')

                fixture_sid = uuid4().hex
                candidates = [ContextSnapshot(None, (ContextMessage(i, 'human', str(i)),), i) for i in range(1, 21)]
                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(lambda snapshot: cache.store(owner_id, fixture_sid, snapshot), reversed(candidates)))
                assert cache.load(owner_id, fixture_sid).tail_seq == 20
                cache.invalidate(owner_id, fixture_sid)
                assert cache.store(owner_id, fixture_sid, candidates[-1]) is False
                assert cache.load(owner_id, fixture_sid) is None
                passed('real Redis Lua prevents concurrent older overwrites and tombstone prevents refilling deleted data')

                # BIGINT 比较不依赖 Lua 浮点精度。
                huge_sid = uuid4().hex
                huge = 2**53 + 10
                newer = ContextSnapshot(None, (ContextMessage(huge, 'human', 'new'),), huge)
                older = ContextSnapshot(None, (ContextMessage(huge - 1, 'human', 'old'),), huge - 1)
                assert cache.store(owner_id, huge_sid, newer)
                assert not cache.store(owner_id, huge_sid, older)
                assert cache.load(owner_id, huge_sid) == newer
                passed('BIGINT cache ordering remains correct above Lua floating-point integer precision')

                # 小上限只跳过缓存；不能限制用户输入或把大输入写回成精简后的内容。
                tiny = RedisContextCache(replace(settings, max_entry_bytes=4096), lambda: master)
                try:
                    fixture['chat'].context_cache = tiny
                    large_sid = sessions.create_session(owner_id, 'large raw input')
                    response, _ = send(large_sid, 'LARGE_ORIGINAL_' + 'x' * 5000)
                    assert response.status_code == 200, response.text
                    assert len(history(large_sid)[0]['content']) > 5000
                    assert tiny._blocked_until == 0
                finally:
                    tiny.close()
                    fixture['chat'].context_cache = cache
                passed('oversized cache entry bypasses storage without rejecting input or tripping process backoff')

                disabled = RedisContextCache(replace(settings, enabled=False), lambda: master)
                fixture['chat'].context_cache = disabled
                response, _ = send(sid, '关闭缓存后继续')
                assert response.status_code == 200 and disabled._client is None
                fixture['chat'].context_cache = cache
                passed('deployment switch disables Redis completely while keeping normal chat persistence')

                # 随机关闭端口和错误认证都是真实连接失败，不只是模拟 Redis 方法。
                with socket.socket() as port_socket:
                    port_socket.bind(('127.0.0.1', 0))
                    closed_port = port_socket.getsockname()[1]
                bad_urls = [f'redis://127.0.0.1:{closed_port}/0',
                            state['url'].replace('redis://', 'redis://:TEST_ONLY_WRONG_AUTH@')]
                for url in bad_urls:
                    broken = RedisContextCache(replace(settings, redis_url=url), lambda: master)
                    try:
                        fixture['chat'].context_cache = broken
                        response, _ = send(sid, '连接故障时仍可聊天')
                        assert response.status_code == 200, response.text
                        assert broken._blocked_until > 0
                    finally:
                        broken.close()
                        fixture['chat'].context_cache = cache
                passed('actual refused TCP connection and Redis authentication error both fall back to MySQL')

                before_delete = cache.load(owner_id, sid)
                assert client.delete(f'/sessions/{sid}', headers=other).status_code == 404
                assert cache.load(owner_id, sid) is not None
                assert client.delete(f'/sessions/{sid}', headers=owner).status_code == 204
                assert cache.load(owner_id, sid) is None
                assert not cache.store(owner_id, sid, before_delete)
                assert send(sid, '删除后不能用缓存继续')[0].status_code == 404
                assert client.get(f'/sessions/{sid}/messages', headers=owner).status_code == 404
                passed('owner deletion removes history and cached body; foreign deletion cannot invalidate it')

                logs = '\n'.join(path.read_text(encoding='utf-8') for path in Path(fixture['log_dir']).rglob('*.txt'))
                for private in ('UNIQUE_PRIVATE_QUESTION_MARKER_1', 'PRIVATE_REDIS_PASSWORD', 'PRIVATE_WRITE_FAILURE',
                                'PRIVATE_DB_FAILURE', 'sk-TEST_ONLY_CACHE'):
                    assert private not in logs, 'private data found in log'
                passed('cache logs contain operational metadata without question text, model key, or secret error body')
        finally:
            cache.close()
        print(f'ALL {len(groups)} integration groups passed; Redis {state["version"]}; no paid model or GPU call.')


if __name__ == '__main__':
    main()
