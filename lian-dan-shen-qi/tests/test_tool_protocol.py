import unittest
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock
from langchain_core.messages import AIMessageChunk
from app.services.tool_protocol import ToolTextStream,ToolTextProtocolError
from app.services.model_gateway import ModelGateway
from app.services.read_only_chat_service import ReadOnlyChatService
from app.models.chat_tools import bounded_result
from app.models.context_snapshot import ContextAttempt

BAD='<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="get_run_logs">参数</｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>'

class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_marker_split_at_every_character_never_leaks(self):
        display=ToolTextStream()
        visible=''.join(display.push(char) for char in '准备查询。'+BAD)+display.finish()
        self.assertEqual(visible,'准备查询。')
        self.assertTrue(display.blocked)

    def test_normal_text_and_comparison_symbols_are_preserved(self):
        display=ToolTextStream();source='损失 < 3，普通文字。'
        self.assertEqual(''.join(display.push(char) for char in source)+display.finish(),source)

    async def test_sdk_stream_does_not_publish_malformed_tool_text(self):
        async def chunks(*args,**kwargs):
            for char in BAD:yield AIMessageChunk(content=char)
        gateway=object.__new__(ModelGateway)
        gateway.model=MagicMock();gateway.model.bind_tools.return_value=SimpleNamespace(astream=chunks)
        callback=AsyncMock()
        with self.assertRaises(ToolTextProtocolError):
            await gateway.invoke([('human','查日志')],tools=[{'type':'function'}],on_delta=callback)
        callback.assert_not_awaited()

    async def test_only_corrected_native_call_is_executed(self):
        repo=MagicMock();repo.binding.return_value=None
        runs=MagicMock();runs.latest_training_run.return_value=None
        service=ReadOnlyChatService(repo,runs,metrics_factory=MagicMock(),runner_factory=MagicMock())
        gateway=SimpleNamespace(invoke=AsyncMock(side_effect=[{'content':BAD},
            {'content':'','tool_calls':[{'id':'real_call','name':'get_latest_training_run','args':{}}]},
            {'content':'没有训练记录。'}]))
        result=await service.answer([('system','助手'),('human','查最近训练')],user_id=2,session_id='a'*32,
            attempt=ContextAttempt('b'*32,'c'*32),gateway=gateway,max_input_tokens=24000)
        self.assertEqual(result,'没有训练记录。')
        runs.latest_training_run.assert_called_once()
        repo.record.assert_called_once()

    async def test_repair_happens_at_most_once_and_never_executes_plain_text(self):
        repo=MagicMock();repo.binding.return_value=None
        service=ReadOnlyChatService(repo,MagicMock(),metrics_factory=MagicMock(),runner_factory=MagicMock())
        gateway=SimpleNamespace(invoke=AsyncMock(return_value={'content':BAD}))
        with self.assertRaises(ToolTextProtocolError):
            await service.answer([('system','助手'),('human','查日志')],user_id=2,session_id='a'*32,
                attempt=ContextAttempt('b'*32,'c'*32),gateway=gateway,max_input_tokens=24000)
        self.assertEqual(gateway.invoke.await_count,2)
        repo.record.assert_not_called()

    async def test_budget_exhaustion_stays_none_during_protocol_correction(self):
        repo=MagicMock();repo.binding.return_value=None
        runs=MagicMock();runs.latest_training_run.return_value=None
        service=ReadOnlyChatService(repo,runs,metrics_factory=MagicMock(),runner_factory=MagicMock())
        replies=[{'content':'','tool_calls':[{'id':f'call_{i}','name':'get_latest_training_run','args':{}}]} for i in range(4)]
        gateway=SimpleNamespace(invoke=AsyncMock(side_effect=replies+[{'content':BAD},{'content':'已有证据不足，未读完整日志。'}]))
        await service.answer([('system','助手'),('human','查日志')],user_id=2,session_id='a'*32,
            attempt=ContextAttempt('b'*32,'c'*32),gateway=gateway,max_input_tokens=24000)
        self.assertEqual(repo.record.call_count,4)
        self.assertEqual([call.kwargs['tool_choice'] for call in gateway.invoke.await_args_list[-2:]],['none','none'])

    def test_complete_14kb_log_is_not_forced_into_8kb_excerpt(self):
        value={'ok':True,'code':'OK','data':{'kind':'run_log','text':'a'*14526,'offset':0,
            'next_offset':14526,'is_complete_log':True,'has_more':False,'display_truncated':False}}
        result=bounded_result(value,16384)
        self.assertEqual(len(result['data']['text']),14526)
        self.assertTrue(result['data']['is_complete_log'])

    def test_budget_trim_does_not_skip_unseen_bytes_or_claim_complete(self):
        value={'ok':True,'code':'OK','data':{'kind':'run_log','text':'a'*10000,'offset':0,
            'next_offset':10000,'is_complete_log':True,'has_more':False,'display_truncated':False}}
        result=bounded_result(value,4000)
        self.assertTrue(result['data']['display_truncated'])
        self.assertFalse(result['data']['is_complete_log'])
        self.assertEqual(result['data']['next_offset'],0)

    async def test_full_log_reaches_answer_model_in_one_native_tool_result(self):
        run_id='d'*32
        repo=MagicMock();repo.binding.return_value=run_id
        runner=SimpleNamespace(operate=AsyncMock(return_value={'observation':{
            'content':'x'*14526,'next_offset':14526,'log_size':14526,'has_more':False,
            'status':'complete','stale':False,'truncated':False,'source_changed':False}}))
        service=ReadOnlyChatService(repo,MagicMock(),metrics_factory=MagicMock(),runner_factory=lambda:runner)
        received=[]
        async def reply(messages,**options):
            results=[message for message in messages if message['role']=='tool']
            if results:
                received.append(json.loads(results[-1]['content']))
                return {'content':'已读取本次可用日志。'}
            return {'content':'','tool_calls':[{'id':'log_read','name':'get_run_logs',
                                               'args':{'offset':0,'limit':16384}}]}
        await service.answer([('system','助手'),('human','查看完整日志')],user_id=2,session_id='a'*32,
            attempt=ContextAttempt('b'*32,'c'*32),gateway=SimpleNamespace(invoke=reply),max_input_tokens=49152)
        runner.operate.assert_awaited_once_with(2,run_id,'logs',offset=0,limit=16384)
        self.assertEqual(len(received[0]['data']['text']),14526)
        self.assertTrue(received[0]['data']['is_complete_log'])
