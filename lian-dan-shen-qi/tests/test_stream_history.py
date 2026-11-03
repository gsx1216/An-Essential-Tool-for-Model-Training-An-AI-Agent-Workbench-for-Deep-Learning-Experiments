import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,MagicMock,patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk

from app.api.chat_stream import stream_reply
from app.api.chat_routes import router
from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_user_chat_service
from app.api.schemas import UserResponse
from app.errors import SessionNotFoundError
from app.models.history_recall import SearchHistory
from app.models.chat_tools import bounded_result,compact_json
from app.repositories.chat_tool_repository import ChatToolRepository
from app.services.model_gateway import ModelGateway
from app.services.chat_service import ChatService

class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_actual_chunks_are_forwarded_and_aggregated(self):
        async def chunks(*args,**kwargs):
            yield AIMessageChunk(content='你')
            yield AIMessageChunk(content='好')
        gateway=object.__new__(ModelGateway);gateway.model=MagicMock();gateway.model.astream=chunks
        callback=AsyncMock()
        result=await gateway.invoke([('human','Hi')],on_delta=callback)
        self.assertEqual(result['content'],'你好')
        self.assertEqual([call.args[0] for call in callback.await_args_list],['你','好'])
        gateway.model.ainvoke.assert_not_called()

    async def test_sse_done_waits_for_business_commit(self):
        committed=asyncio.Event()
        async def operation(delta):
            await delta('部分回复')
            await committed.wait()
            return '校验并保存后的完整回复'
        response=stream_reply(operation,lambda error:(503,'请重试。'))
        events=response.body_iterator
        self.assertIn('status',await anext(events))
        self.assertIn('部分回复',await anext(events))
        pending=asyncio.create_task(anext(events));await asyncio.sleep(0)
        self.assertFalse(pending.done())
        committed.set();self.assertIn('done',await pending)
        with self.assertRaises(StopAsyncIteration):await anext(events)

    async def test_disconnect_cancels_generation(self):
        cancelled=asyncio.Event()
        async def operation(delta):
            try:
                await delta('开始');await asyncio.Event().wait()
            finally:cancelled.set()
        events=stream_reply(operation,lambda error:(503,'重试')).body_iterator
        await anext(events);await anext(events)
        await events.aclose()
        self.assertTrue(cancelled.is_set())

    async def test_partial_model_failure_never_saves_ai_reply(self):
        repo=MagicMock();repo.start_turn.return_value='b'*32
        repo.claim_turn.return_value=('c'*32,[('human','Hi')])
        async def invoke(messages,*,on_delta):
            await on_delta('半截');raise RuntimeError('fake interruption')
        service=ChatService(SimpleNamespace(invoke=invoke),repo)
        with self.assertRaises(RuntimeError):await service.send_message(2,'a'*32,'Hi','b'*32,on_delta=AsyncMock())
        repo.complete_turn.assert_not_called();repo.fail_turn.assert_called_once()


class HistoryTests(unittest.TestCase):
    def test_trimming_search_results_does_not_skip_older_matches(self):
        result={'ok':True,'code':'OK','data':{'items':[
            {'seq':seq,'role':'human','preview':'a'*100} for seq in (9,7,5)],'next_before_seq':5}}
        trimmed=bounded_result(result,len(compact_json(result).encode())-1)
        self.assertEqual([item['seq'] for item in trimmed['data']['items']],[9,7])
        self.assertEqual(trimmed['data']['next_before_seq'],7)

    def test_search_is_scoped_and_cannot_read_current_question_or_future(self):
        repo=ChatToolRepository('not-real-password')
        repo._require_session=MagicMock();repo._require_attempt=MagicMock();repo._tail_seq=MagicMock(return_value=11)
        connection=MagicMock();connection.__enter__.return_value=connection
        cursor=connection.cursor.return_value.__enter__.return_value;cursor.fetchall.return_value=[(4,'human','lr 0.01')]
        with patch('app.repositories.chat_tool_repository.get_connection',return_value=connection):
            result=repo.recall_history(2,'a'*32,object(),'search_chat_history',SearchHistory(query='lr',before_seq=999))
        self.assertEqual(cursor.execute.call_args.args[1],('lr',2,'a'*32,11,'lr',3))
        self.assertEqual(result['data']['items'][0]['seq'],4)

    def test_stream_route_rejects_other_users_session_before_headers(self):
        service=SimpleNamespace(repository=SimpleNamespace(check_session=MagicMock(side_effect=SessionNotFoundError())),send_message=AsyncMock())
        app=FastAPI();app.include_router(router)
        app.dependency_overrides[require_current_user]=lambda:UserResponse(user_id=2,username='test')
        app.dependency_overrides[get_user_chat_service]=lambda:service
        with TestClient(app) as client:
            result=client.post('/sessions/'+'a'*32+'/messages/stream',json={'content':'Hi','request_id':'b'*32})
            self.assertEqual(result.status_code,404)
        service.send_message.assert_not_awaited()
