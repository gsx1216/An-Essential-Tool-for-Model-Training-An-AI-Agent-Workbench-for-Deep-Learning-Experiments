"""普通文本建议：源码完整分页、原始日志、单次模型调用与持久化来源。"""
import base64
import hashlib
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from app.models.analysis import ModelReplyError, AnalysisProtocolError, AnalysisStorageError, AnalysisSettings, verify_saved_content
from app.models.context_state import ContextTooLargeError
from app.models.suggestion import SuggestionSettings, verify_suggestions
from app.models.suggestion_tools import SuggestionArguments, suggestion_projection
from app.services.direct_code_advice import DirectCodeAdviceService, ReportSnapshotService
from tests.test_suggestion import source
from tests.test_analysis import source as report_source, RUN, TASK, TOKEN


def service(raw=b'Epoch: 1 MAE: 0.05\n', lines=None):
    lines = lines or ['import torch', 'def forward(x):', '    return x']
    sha = hashlib.sha256('\n'.join(lines).encode()).hexdigest()
    async def query(uid, run, action, **args):
        start=args['start_line']; end=min(start+1,len(lines))
        return {'path':'models/net.py','file_sha256':sha,'lines':[
            {'number':i,'text':lines[i-1]} for i in range(start,end+1)],
            'total_lines':len(lines),'next_line':end+1 if end<len(lines) else None}
    async def logs(uid, run, action, *, offset, limit):
        chunk=raw[offset:offset+limit]
        return {'observation':{'status':'ok','raw_base64':base64.b64encode(chunk).decode(),
            'log_size':len(raw),'next_offset':offset+len(chunk)}}
    src=SimpleNamespace(main_network=AsyncMock(return_value={'path':'models/net.py','file_sha256':sha}),query=AsyncMock(side_effect=query))
    runner=SimpleNamespace(operate=AsyncMock(side_effect=logs))
    repo=Mock();repo.source.return_value=source();repo.renew.return_value=True
    gateway=SimpleNamespace(invoke=AsyncMock(return_value={'content':'## 建议\n检查 `forward` 的维度。\n```python\nreturn x\n```'}),aclose=AsyncMock())
    factory=SimpleNamespace(create_for_user=AsyncMock(return_value=gateway))
    svc=DirectCodeAdviceService(repo,source=src,runner_factory=lambda:runner,gateway_factory=factory,settings=SuggestionSettings(enabled=True))
    return svc,src,runner,gateway


class DirectCodeAdviceTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_selected_network_and_exact_short_text_in_prompt(self):
        raw='Epoch [001/010], LR:0.001||sal_loss:2.7\nEpoch: 1 MAE: 0.05\n'.encode()
        svc,src,runner,_=service(raw)
        messages,snapshot=await svc.prepare_input(2,RUN)
        self.assertIn('以下是日志内容：\n'+raw.decode(),messages[1][1])
        self.assertIn('下面是网络代码：',messages[1][1])
        self.assertIn('0003 |     return x',messages[1][1])
        self.assertNotIn('metric_definition',messages[1][1])
        self.assertEqual(snapshot['network_lines'],3)
        self.assertEqual(snapshot['log_bytes_sent'],len(raw))
        self.assertFalse(snapshot['log_clipped'])
        self.assertEqual(src.query.await_count,2)
        self.assertEqual(src.query.call_args.kwargs['expected_sha256'],snapshot['network_sha256'])
        self.assertEqual(runner.operate.call_args.args[:3],(2,RUN,'logs'))

    async def test_long_raw_log_uses_annotated_head_tail(self):
        raw=b'BEGIN\n'+b'x'*50000+b'\nEND'
        svc,_,runner,_=service(raw)
        text,total,sent,clipped,available=await svc.read_logs(2,RUN,20000)
        self.assertTrue(text.startswith('BEGIN'))
        self.assertTrue(text.endswith('END'))
        self.assertIn('中间部分未提供',text)
        self.assertEqual((total,sent,clipped,available),(len(raw),20000,True,True))
        self.assertLessEqual(max(c.kwargs['limit'] for c in runner.operate.call_args_list),16384)

    async def test_missing_logs_do_not_prevent_code_review(self):
        svc,_,runner,gateway=service()
        runner.operate.return_value={'observation':{'status':'missing'}}
        runner.operate.side_effect=None
        messages,snapshot=await svc.prepare_input(2,RUN)
        self.assertFalse(snapshot['log_available'])
        self.assertIn('仅检查主网络代码',messages[1][1])
        gateway.invoke.assert_not_awaited()

    async def test_one_plain_response_saved_without_model_json_or_tools(self):
        svc,_,_,gateway=service()
        lease={'user_id':2,'run_id':RUN,'task_id':TASK,'lease_token':TOKEN}
        await svc._perform(lease)
        gateway.invoke.assert_awaited_once()
        self.assertIs(gateway.invoke.call_args.kwargs['thinking'],False)
        self.assertEqual(set(gateway.invoke.call_args.kwargs),{'max_tokens','thinking'})
        svc.gateway_factory.create_for_user.assert_awaited_once_with(2)
        gateway.aclose.assert_awaited_once()
        content=svc.repository.complete.call_args.args[4]
        self.assertIn('## 建议',content['direct_advice'])
        self.assertEqual(content['direct_input']['network_path'],'models/net.py')
        self.assertEqual(verify_suggestions(content,source())['direct_advice'],content['direct_advice'])
        content['source']['analysis_digest']='0'*64
        with self.assertRaises(AnalysisStorageError):verify_suggestions(content,source())

    async def test_snapshot_stage_never_constructs_or_calls_model(self):
        repo=Mock();repo.source.return_value=report_source()
        factory=SimpleNamespace(create_for_user=AsyncMock())
        svc=ReportSnapshotService(repo,gateway_factory=factory,settings=AnalysisSettings())
        await svc._perform({'user_id':2,'run_id':RUN,'task_id':TASK,'lease_token':TOKEN})
        factory.create_for_user.assert_not_awaited()
        args=repo.complete.call_args.args
        self.assertEqual(args[-1],'report-snapshot')
        self.assertEqual(verify_saved_content(args[4],report_source())['inferences'],[])

    async def test_large_network_fails_before_model_instead_of_silent_code_truncation(self):
        svc,_,_,gateway=service(lines=['x'*5000])
        svc.settings=replace(svc.settings,max_input_tokens=4096)
        with self.assertRaises(ContextTooLargeError):await svc.prepare_input(2,RUN)
        gateway.invoke.assert_not_awaited()
        self.assertEqual(svc.error_code(ContextTooLargeError('large')),('INPUT_TOO_LARGE','attention'))

    async def test_changed_or_nonprogressing_source_cannot_mix_pages(self):
        svc,src,_,gateway=service()
        src.query.return_value={'file_sha256':'a'*64,'lines':[],'next_line':1}
        src.query.side_effect=None
        with self.assertRaises(AnalysisProtocolError):await svc.prepare_input(2,RUN)
        gateway.invoke.assert_not_awaited()

    async def test_empty_model_response_rejected_and_gateway_closed(self):
        svc,_,_,gateway=service()
        gateway.invoke.return_value={'content':'  '}
        with self.assertRaises(ModelReplyError) as caught:
            await svc._perform({'user_id':2,'run_id':RUN,'task_id':TASK,'lease_token':TOKEN})
        self.assertEqual(caught.exception.code,'EMPTY_MODEL_OUTPUT')
        self.assertEqual(svc.error_code(caught.exception),('EMPTY_MODEL_OUTPUT','attention'))
        svc.repository.complete.assert_not_called()
        gateway.aclose.assert_awaited_once()

    async def test_saved_text_can_be_read_in_chat_pages(self):
        svc,_,_,_=service()
        await svc._perform({'user_id':2,'run_id':RUN,'task_id':TASK,'lease_token':TOKEN})
        content=svc.repository.complete.call_args.args[4];content['direct_advice']='建'*1100
        row={'content':content,'task_id':TASK,'report_version':1,'is_stale':True}
        overview=suggestion_projection(row,SuggestionArguments())
        self.assertTrue(overview['has_direct_advice'])
        page=suggestion_projection(row,SuggestionArguments(section='advice',offset=2))
        self.assertEqual(page['text'],'建'*100)
        self.assertIsNone(page['next_offset']);self.assertTrue(page['is_stale'])
