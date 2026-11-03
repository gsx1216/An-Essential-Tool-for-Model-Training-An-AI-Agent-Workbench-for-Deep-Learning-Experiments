"""历史回查与流式回复联调；复用隔离 MySQL/Redis，模型替身不产生费用。"""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.chat_stream import stream_reply
from app.api.chat_routes import _stream_error
from app.config import get_mysql_password
from app.models.context_cache import ContextCacheSettings
from app.models.context_state import ContextSummary
from app.services.redis_context_cache import RedisContextCache
from scripts._chat_api_test_app import temporary_chat_api
from scripts._temporary_redis import temporary_redis


async def verify(fixture, user_id, session_id, cache):
    messages, contexts, chat = fixture['messages'], fixture['contexts'], fixture['chat']
    for question, answer in [('最初学习率为0.00005。', '已记录训练配置。'), ('使用哪个优化器？', '使用Adam。')]:
        turn = messages.start_turn(user_id, session_id, question, uuid4().hex)
        attempt, _ = messages.claim_turn(user_id, session_id, turn)
        messages.complete_turn(user_id, session_id, turn, attempt, answer)
    contexts.save_summary(user_id, session_id, ContextSummary('讨论过训练配置，具体学习率已省略。', 2, 1),
                          expected_version=0, expected_tail_seq=4)
    gate = asyncio.Event()
    observed = []

    async def handler(history, options):
        observed.append(deepcopy(history))
        index = len(observed)
        if index <= 2:
            name, args = ('search_chat_history', {'query': '学习率'}) if index == 1 else ('read_chat_history', {'seq': 1})
            return {'content': '', 'tool_calls': [{'id': f'history_call_{index}', 'name': name, 'args': args}]}
        original = json.loads(history[-1]['content'])
        assert original['data']['seq'] == 1 and '0.00005' in original['data']['text']
        await options['on_delta']('最初学习率是')
        await gate.wait()
        await options['on_delta']('0.00005。')
        return {'content': '最初学习率是0.00005。', 'tool_calls': []}

    fixture['builder'].handler = handler
    request_id = uuid4().hex
    response = stream_reply(lambda callback: chat.send_message(user_id, session_id,
        '请查回最初的学习率。', request_id, on_delta=callback), lambda error: (503, '联调失败'))
    events = response.body_iterator
    frames = []
    async with asyncio.timeout(45):
        async for frame in events:
            frames.append(frame)
            if 'event: delta' in frame:
                # 用户原问题先保存，半截回复尚未保存。
                rows = await asyncio.to_thread(messages.load_messages, user_id, session_id)
                assert len(rows) == 5 and rows[-1][0] == 'human'
                gate.set()
    assert any('event: done' in frame for frame in frames), frames
    assert not any('event: error' in frame for frame in frames), frames
    rows = messages.load_messages(user_id, session_id)
    assert len(rows) == 6 and rows[-1] == ('ai', '最初学习率是0.00005。')
    assert '0.00005' not in str(observed[0]), '被摘要覆盖的原文不应预先塞入上下文'
    cached = cache.load(user_id, session_id)
    assert cached.tail_seq == 6 and cached.summary.summarized_until_seq == 2
    assert [message.seq for message in cached.messages] == [3, 4, 5, 6]
    calls = chat.tool_service.repository.list_calls(user_id, session_id)['items']
    assert [item['tool_name'] for item in calls] == ['search_chat_history', 'read_chat_history']
    before = len(fixture['builder'].created)
    assert await chat.send_message(user_id, session_id, '请查回最初的学习率。', request_id) == rows[-1][1]
    assert len(fixture['builder'].created) == before
    print('PASS: MySQL原文回查、工具审计、真实Redis摘要/近期消息回填、流式提交顺序、重复请求去重')


def main():
    with temporary_redis() as state:
        key = Fernet.generate_key()
        cache = RedisContextCache(ContextCacheSettings(redis_url=state['url'], prefix='stream_verify_' + uuid4().hex),
                                  lambda: key)
        try:
            with temporary_chat_api(get_mysql_password(), context_cache=cache, chat_tools=True) as (app, fixture):
                with TestClient(app) as client:
                    username = 'stream_' + uuid4().hex[:10]
                    registered = client.post('/auth/register', json={'username': username, 'password': 'TestPass123!'})
                    assert registered.status_code == 201, registered.text
                    user_id = registered.json()['user_id']
                    logged_in = client.post('/auth/login', json={'username': username, 'password': 'TestPass123!'})
                    assert logged_in.status_code == 200, logged_in.text
                    headers = {'Authorization': 'Bearer ' + logged_in.json()['access_token']}
                    assert client.put('/me/model-credentials/deepseek', headers=headers,
                                      json={'api_key': 'sk-TEST_ONLY_STREAM'}).status_code == 204
                    session_id = fixture['sessions'].create_session(user_id, 'stream history fixture')
                    asyncio.run(verify(fixture, user_id, session_id, cache))
                    # 默认旧替身只识别元组历史；工具循环使用 dict，明确提供本次流式替身。
                    async def http_handler(history, options):
                        await options['on_delta']('路由')
                        await options['on_delta']('联调成功。')
                        return {'content': '路由联调成功。', 'tool_calls': []}
                    fixture['builder'].handler = http_handler
                    errors = []
                    def capture(error):
                        errors.append(error)
                        return _stream_error(error)
                    with patch('app.api.chat_routes._stream_error', side_effect=capture):
                        response = client.post(f'/sessions/{session_id}/messages/stream', headers=headers,
                            json={'content': '仅验证路由响应', 'request_id': uuid4().hex})
                    if errors:
                        raise AssertionError('HTTP流式链路失败') from errors[0]
                    assert response.status_code == 200 and 'text/event-stream' in response.headers['content-type']
                    assert 'event: done' in response.text and 'event: error' not in response.text, response.text
                    print('PASS: 带登录凭证的真实HTTP路由输出SSE完成事件')
                    ttl = state['client'].ttl(cache._key(user_id, session_id))
                    assert 604700 <= ttl <= 604800
                    print('PASS: Redis缓存有效期7天；验证临时库和临时Redis退出后自动清理')
        finally:
            cache.close()


if __name__ == '__main__':
    main()
