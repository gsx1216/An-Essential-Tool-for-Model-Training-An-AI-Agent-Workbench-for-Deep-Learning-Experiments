"""网关客户端资源回归；SDK构造和模型调用全部模拟。"""

import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from langchain_deepseek import ChatDeepSeek as RealModel

from app.services.model_gateway import ModelGateway


class ModelGatewayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.model = SimpleNamespace(
            root_client=SimpleNamespace(close=Mock()),
            root_async_client=SimpleNamespace(close=AsyncMock()),
            ainvoke=AsyncMock(),
        )
        with patch('app.services.model_gateway.ChatDeepSeek', return_value=self.model) as constructor:
            self.gateway = ModelGateway(api_key='sk-TEST_ONLY_LIFECYCLE')
        self.constructor = constructor

    async def asyncTearDown(self):
        await self.constructor.call_args.kwargs['http_async_client'].aclose()
        self.constructor.call_args.kwargs['http_client'].close()

    async def test_closes_both_clients(self):
        await self.gateway.aclose()
        self.model.root_async_client.close.assert_awaited_once_with()
        self.model.root_client.close.assert_called_once_with()

    async def test_sync_close_runs_in_worker_thread(self):
        loop_thread = threading.get_ident()
        threads = []
        self.model.root_client.close.side_effect = lambda: threads.append(threading.get_ident())
        await self.gateway.aclose()
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], loop_thread)

    async def test_async_failure_still_closes_sync_client(self):
        error = RuntimeError('TEST_ONLY_ASYNC_CLOSE')
        self.model.root_async_client.close.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            await self.gateway.aclose()
        self.assertIs(caught.exception, error)
        self.model.root_client.close.assert_called_once()

    async def test_both_failures_preserve_async_error_and_sync_cause(self):
        first, second = RuntimeError('TEST_ONLY_ASYNC'), RuntimeError('TEST_ONLY_SYNC')
        self.model.root_async_client.close.side_effect = first
        self.model.root_client.close.side_effect = second
        with self.assertRaises(RuntimeError) as caught:
            await self.gateway.aclose()
        self.assertIs(caught.exception, first)
        self.assertIs(caught.exception.__cause__, second)

    async def test_sync_failure_propagates_after_async_close(self):
        error = RuntimeError('TEST_ONLY_SYNC_CLOSE')
        self.model.root_client.close.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            await self.gateway.aclose()
        self.assertIs(caught.exception, error)
        self.model.root_async_client.close.assert_awaited_once()

    async def test_async_cancellation_still_closes_sync_client(self):
        self.model.root_async_client.close.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.gateway.aclose()
        self.model.root_client.close.assert_called_once()

    async def test_existing_invoke_returns_content_and_usage(self):
        self.model.ainvoke.return_value = SimpleNamespace(content='假回复', usage_metadata={'total_tokens': 9})
        messages = [('human', '假问题')]
        result = await self.gateway.invoke(messages)
        self.assertEqual(result, {'content': '假回复', 'usage': {'total_tokens': 9}})
        self.model.ainvoke.assert_awaited_once_with(messages)

    async def test_constructor_uses_explicit_key_without_calling_model(self):
        self.assertEqual(self.constructor.call_args.kwargs['api_key'], 'sk-TEST_ONLY_LIFECYCLE')
        self.model.ainvoke.assert_not_awaited()

    async def test_invoke_forwards_per_request_output_limit(self):
        self.model.ainvoke.return_value = SimpleNamespace(content='精简稿', usage_metadata=None)
        messages = [('human', '假材料')]
        result = await self.gateway.invoke(messages, max_tokens=100)
        self.assertEqual(result['content'], '精简稿')
        self.model.ainvoke.assert_awaited_once_with(messages, max_tokens=100)

    async def test_invalid_output_limit_never_calls_model(self):
        for limit in (True, 0, -1, 4097, 1.5, '100'):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                await self.gateway.invoke([('human', '假材料')], max_tokens=limit)
        self.model.ainvoke.assert_not_awaited()

    async def test_real_sdk_serializes_override_without_network(self):
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={
                'id': 'test-offline', 'object': 'chat.completion', 'created': 0,
                'model': 'deepseek-flash',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '假精简稿'},
                             'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
            })
        transport = httpx.MockTransport(handler)
        sync_client = httpx.Client(transport=transport, trust_env=False)
        async_client = httpx.AsyncClient(transport=transport, trust_env=False)
        with patch('app.services.model_gateway.ChatDeepSeek', side_effect=lambda **kwargs: RealModel(
            **{**kwargs,'http_client':sync_client,'http_async_client':async_client},
        )):
            gateway = ModelGateway(api_key='sk-TEST_ONLY_OFFLINE')
        try:
            result = await gateway.invoke([('human', '假问题')], max_tokens=80,thinking=False)
            self.assertEqual(result['content'], '假精简稿')
            self.assertEqual(len(payloads), 1)
            self.assertEqual(payloads[0]['max_tokens'], 80)
            self.assertEqual(payloads[0]['thinking'],{'type':'disabled'})
            self.assertNotIn('response_format',payloads[0])
            self.assertNotIn('tools',payloads[0])
        finally:
            await gateway.aclose()
            await async_client.aclose()
            sync_client.close()

    async def test_plain_code_advice_disables_thinking_and_preserves_response_diagnostics(self):
        from langchain_core.messages import AIMessage
        self.model.ainvoke.return_value=AIMessage(content='',additional_kwargs={'reasoning_content':'PRIVATE_REASONING'},
            response_metadata={'finish_reason':'length'},usage_metadata={'input_tokens':10,'output_tokens':4096,'total_tokens':4106,'output_token_details':{'reasoning':4096}})
        with self.assertLogs('app.services.model_gateway',level='INFO') as captured:
            result=await self.gateway.invoke([('human','fixture')],thinking=False)
        self.assertEqual(result['content'],'')
        record=next(r for r in captured.records if r.msg=='model_response_received')
        self.assertEqual(record.finish_reason,'length')
        self.assertEqual(record.reasoning_chars,len('PRIVATE_REASONING'))
        self.assertEqual(self.model.ainvoke.call_args.kwargs['extra_body'],{'thinking':{'type':'disabled'}})

    async def test_text_blocks_are_combined_without_exposing_reasoning(self):
        from langchain_core.messages import AIMessage
        self.model.ainvoke.return_value=AIMessage(content=[{'type':'text','text':'第一条'},{'type':'text','text':'建议'}])
        result=await self.gateway.invoke([('human','fixture')],thinking=False)
        self.assertEqual(result['content'],'第一条建议')
