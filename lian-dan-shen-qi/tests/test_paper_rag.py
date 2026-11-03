"""论文索引、PDF 进程、权限参数与真实引文校验；不使用真实模型。"""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,MagicMock,patch
from uuid import uuid4

from app.errors import ChatToolProtocolError
from app.models.paper import (SearchPapers,ReadPaperChunk,MAX_PDF_BYTES,PaperTooLarge,PaperUnsupported,PaperBusy,
    PaperParseError,PAPER_ARGUMENTS,render_answer,sha256,evidence)
from app.models.chat_tools import bounded_result,tool_definitions
from app.services.pdf_parser import PdfParser,validate_result
from app.services.pdf_parser_process import extract,ParseFailure
from app.services.paper_chunking import normalize_page,page_chunks,MAX_CHARACTERS,OVERLAP_CHARACTERS
from app.services.paper_service import PaperService
from app.models.suggestion import PdfPaperEvidence
from tests.paper_fixtures import pdf_bytes,PAPER_PAGES


class ExtractionTests(unittest.TestCase):
    def test_real_pdf_pages_and_chunk_positions(self):
        result=extract(pdf_bytes(PAPER_PAGES))
        self.assertEqual(result['page_count'],2)
        self.assertEqual([r['page_number'] for r in result['chunks']],[1,2])
        self.assertIn('boundary supervision',result['chunks'][0]['content'])
        self.assertEqual(validate_result({'ok':True,'data':result}),result)

    def test_overlap_and_page_boundaries(self):
        result=extract(pdf_bytes(['Boundary loss and attention. '*350,'Second page ablation study.']))
        first,second=result['chunks'][:2]
        overlap=first['char_start']+len(first['content'])-second['char_start']
        self.assertTrue(0<overlap<=OVERLAP_CHARACTERS)
        self.assertEqual(first['content'][-overlap:],second['content'][:overlap])
        self.assertGreater(len(first['content']),3000)
        self.assertEqual(result['chunks'][-1]['char_start'],0)
        self.assertEqual(result['chunks'][-1]['page_number'],2)
        validate_result({'ok':True,'data':result})

    def test_blank_pages_recorded_not_invented(self):
        data=extract(pdf_bytes(['',PAPER_PAGES[0]]))
        self.assertEqual(data['empty_pages'],[1]);self.assertEqual(data['chunks'][0]['page_number'],2)
        with self.assertRaisesRegex(ParseFailure,'NO_EXTRACTABLE_TEXT'):extract(pdf_bytes(['']))

    def test_encrypted_and_malformed_pdf(self):
        with self.assertRaisesRegex(ParseFailure,'ENCRYPTED_PDF'):extract(pdf_bytes(['private'],encrypted=True))
        with self.assertRaises(ParseFailure):extract(b'not a PDF')

    def test_excess_page_count(self):
        with self.assertRaisesRegex(ParseFailure,'PDF_LIMIT'):extract(pdf_bytes(['']*201))

    def test_protocol_rejects_hash_page_and_overlap_corruption(self):
        source=extract(pdf_bytes(['Boundary loss and attention. '*350]))
        for field,value in [('content_sha256','0'*64),('page_number',9),('char_start',20)]:
            data=deepcopy(source);data['chunks'][0][field]=value
            with self.subTest(field=field),self.assertRaises(PaperParseError):validate_result({'ok':True,'data':data})
        data=deepcopy(source);data['chunks'][1]['char_start']=1
        with self.assertRaises(PaperParseError):validate_result({'ok':True,'data':data})

    def test_unicode_normalization_preserves_math_symbols(self):
        page=SimpleNamespace(extract_text=lambda:'伪装目标检测  边界损失\n x² + α')
        reader=SimpleNamespace(pages=[page],is_encrypted=False)
        with patch('pypdf.PdfReader',return_value=reader):
            data=extract(b'%PDF-fixture')
        self.assertEqual(data['chunks'][0]['content'],'伪装目标检测 边界损失 x² + α')

    def test_complete_method_page_is_not_split_into_tiny_fragments(self):
        text='Problem: ambiguous boundaries. '+('Motivation and design. '*180)+'Method: gated boundary supervision.'
        data=extract(pdf_bytes([text]))
        self.assertEqual(len(data['chunks']),1)
        self.assertTrue(data['chunks'][0]['content'].startswith('Problem:'))
        self.assertIn('Method: gated boundary supervision.',data['chunks'][0]['content'])
        self.assertIn('paragraph-v2',data['parser_version'])

    def test_long_page_prefers_complete_paragraph_and_bounded_overlap(self):
        paragraphs=['A '+('a '*1350),'B '+('b '*1000),'C '+('c '*1350),'D '+('d '*1200)]
        text=normalize_page('\n\n'.join(paragraphs))
        spans=list(page_chunks(text))
        self.assertGreater(len(spans),1)
        self.assertTrue(text[:spans[0][1]].endswith('\n\n'))
        self.assertIn(paragraphs[1].rstrip(),text[:spans[0][1]])
        restored='';end=0
        for start,stop in spans:
            self.assertLessEqual(stop-start,MAX_CHARACTERS)
            self.assertTrue(0<=end-start<=OVERLAP_CHARACTERS)
            restored+=text[max(end,start):stop];end=stop
        self.assertEqual(restored,text)

    def test_absent_paragraphs_use_sentence_boundary_then_hard_limit(self):
        text=('This sentence keeps the method together. '*220).strip()
        spans=list(page_chunks(text))
        self.assertTrue(text[:spans[0][1]].rstrip().endswith('.'))
        no_breaks='x'*14000
        spans=list(page_chunks(no_breaks))
        self.assertTrue(all(stop-start<=6000 for start,stop in spans))
        self.assertEqual(spans[-1][1],len(no_breaks))

    def test_new_normalized_offsets_preserve_paragraph_breaks(self):
        self.assertEqual(normalize_page('First line\nwrap.\n\nSecond paragraph x².'),'First line wrap.\n\nSecond paragraph x².')


class ParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_bounded_process_reads_pdf(self):
        result=await PdfParser().parse(pdf_bytes(PAPER_PAGES))
        self.assertEqual(result['page_count'],2);self.assertEqual(len(result['chunks']),2)

    async def test_actual_blank_and_damaged_process_errors(self):
        for raw,code in [(pdf_bytes(['']),'NO_EXTRACTABLE_TEXT'),(b'%PDF-invalid','INVALID_PDF')]:
            with self.subTest(code=code),self.assertRaises(PaperParseError) as cm:await PdfParser().parse(raw)
            self.assertEqual(cm.exception.code,code)

    async def test_timeout_kills_and_waits_for_process(self):
        process=MagicMock();process.pid=123;process.returncode=None
        async def communicate(_):await asyncio.Event().wait()
        process.communicate=communicate;process.wait=AsyncMock()
        with patch('asyncio.create_subprocess_exec',AsyncMock(return_value=process)),patch('app.services.pdf_parser.WindowsMemoryJob'):
            with self.assertRaises(PaperParseError) as cm:await PdfParser(timeout=.01).parse(b'%PDF-test')
        self.assertEqual(cm.exception.code,'PDF_TIMEOUT');process.kill.assert_called_once();process.wait.assert_awaited_once()


class ToolContractTests(unittest.TestCase):
    def test_tool_has_no_user_identity_or_file_path(self):
        for key in ('user_id','path','url','command'):
            with self.subTest(key=key),self.assertRaises(ValueError):SearchPapers.model_validate({'query':'boundary loss',key:'x'})
        for args in ({},{'chunk_id':uuid4(),'paper_id':uuid4()},{'chunk_id':uuid4(),'chunk_index':2}):
            with self.subTest(args=args),self.assertRaises(ValueError):ReadPaperChunk.model_validate(args)

    def test_optional_paper_tools_do_not_change_old_default_catalog(self):
        names=lambda **kw:{row['function']['name'] for row in tool_definitions(**kw)}
        self.assertNotIn('read_paper_chunk',names())
        self.assertTrue(set(PAPER_ARGUMENTS)<=names(include_papers=True))

    def test_paper_analysis_of_training_does_not_select_execution_tools(self):
        from app.models.training_task import training_mode
        for text in ('根据论文分析训练结果并提出建议。','论文中的训练损失是什么？','根据上传 PDF 提出训练建议，不要训练'):
            with self.subTest(text=text):self.assertFalse(training_mode(text))
        self.assertTrue(training_mode('启动 PRNet 复现这篇论文的训练'))
        self.assertTrue(training_mode('列出我的论文复现训练任务'))

    def test_effective_search_keywords(self):
        self.assertEqual(SearchPapers(query='  boundary   loss ').query,'boundary loss')
        for text in ('x','    ','!!'):
            with self.subTest(text=text),self.assertRaises(ValueError):SearchPapers(query=text)

    def test_paper_read_budget_supports_full_method_but_other_tools_stay_bounded(self):
        from app.models.chat_tools import compact_json,tool_result_budget
        args=ReadPaperChunk(chunk_id=uuid4())
        self.assertEqual(args.max_bytes,12000)
        ReadPaperChunk(chunk_id=uuid4(),offset=5500,max_bytes=18000)
        self.assertEqual(tool_result_budget(49152,include_papers=True),32768)
        self.assertEqual(tool_result_budget(49152),16384)
        data={'kind':'uploaded_pdf','reference_id':uuid4().hex,'offset':0,'text':'中'*6000,'content_length':6000}
        result=bounded_result({'ok':True,'code':'OK','data':data},20000)
        self.assertEqual(len(result['data']['text']),6000)
        ordinary=bounded_result({'ok':True,'code':'OK','data':{'text':'x'*18000}},20000)
        self.assertLessEqual(len(compact_json(ordinary).encode()),8192)


class CitationTests(unittest.TestCase):
    def setUp(self):
        self.cid=uuid4().hex;self.pid=uuid4().hex
        self.source={'chunk_id':self.cid,'paper_id':self.pid,'title':'Boundary paper','page_number':2,'chunk_index':1,
            'page_char_start':880,'offset':10,'text':'Boundary supervision improves the alignment of predictions.',
            'content_sha256':'a'*64,'file_sha256':'b'*64}
        self.answer={'answer':'可考虑边界监督，需要做对照实验。','citations':[{'chunk_id':self.cid,'quote':'Boundary supervision'}],
            'limitations':['仅读取了一个片段。']}

    def render(self,answer=None):return render_answer(json.dumps(answer or self.answer),{self.cid:[self.source]}, {})

    def test_program_generates_physical_page_and_exact_quote_position(self):
        result=self.render()
        self.assertIn('PDF 第 2 页',result);self.assertIn('890–910',result)
        self.assertIn(self.cid,result)

    def test_unread_chunk_or_fabricated_quote_rejected(self):
        for update in ({'chunk_id':uuid4().hex},{'quote':'This text was never actually delivered.'}):
            data=deepcopy(self.answer);data['citations'][0].update(update)
            with self.subTest(update=update),self.assertRaises(ChatToolProtocolError):self.render(data)

    def test_model_cannot_supply_fabricated_page_or_url(self):
        data=deepcopy(self.answer);data['citations'][0]['page_number']=99
        with self.assertRaises(ChatToolProtocolError):self.render(data)
        data=deepcopy(self.answer);data['answer']='https://unverified.invalid/'
        with self.assertRaises(ChatToolProtocolError):self.render(data)

    def test_mixed_public_and_private_quotes(self):
        rid=uuid4().hex;data=deepcopy(self.answer)
        data['citations'].append({'reference_id':rid,'quote':'Actual public paper body'})
        public={rid:[{'kind':'paper_html','title':'Public paper','url':'https://arxiv.org/html/2401.12345',
            'text':'Actual public paper body from a read result.'}]}
        result=render_answer(json.dumps(data),{self.cid:[self.source]},public)
        self.assertIn('arxiv.org',result)
        public[rid][0]['kind']='search_abstract'
        with self.assertRaises(ChatToolProtocolError):render_answer(json.dumps(data),{self.cid:[self.source]},public)

    def test_bounded_result_audits_only_delivered_excerpt(self):
        data={**self.source,'kind':'uploaded_pdf','reference_id':self.cid,'content_length':len(self.source['text']),
            'next_offset':None,'title':'Long title','text':'Boundary supervision. '*50}
        data['content_length']=len(data['text'])
        result=bounded_result({'ok':True,'code':'OK','data':data},800)
        checked=evidence(result)
        self.assertLess(len(checked['text']),len(data['text']))
        self.assertEqual(checked['text'],result['data']['text'])

    def test_pdf_suggestion_evidence_requires_consistent_chunk(self):
        data={**self.source,'evidence_id':'paper_2','reference_id':self.cid,'kind':'uploaded_pdf','url':None,
            'retrieved_at':'2026-10-28T00:00:00Z','abstract_only':False}
        data.pop('chunk_index')
        PdfPaperEvidence.model_validate(data)
        data['reference_id']=uuid4().hex
        with self.assertRaises(ValueError):PdfPaperEvidence.model_validate(data)

    def test_pdf_suggestion_accepts_actual_long_method_excerpt(self):
        data={**self.source,'evidence_id':'paper_2','reference_id':self.cid,'kind':'uploaded_pdf','url':None,
            'retrieved_at':'2026-10-28T00:00:00Z','abstract_only':False,'offset':1500,'text':'Method and motivation. '*200}
        data.pop('chunk_index')
        self.assertGreater(len(PdfPaperEvidence.model_validate(data).text),4000)


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_size_and_type_fail_before_sql(self):
        repo=MagicMock();service=PaperService(repo)
        async def stream(raw):yield raw
        for raw,error in [(b'x'*(MAX_PDF_BYTES+1),PaperTooLarge),(b'not pdf',PaperUnsupported)]:
            with self.subTest(error=error),self.assertRaises(error):await service.upload(1,uuid4().hex,'Paper',stream(raw))
        repo.reserve.assert_not_called()
        self.assertFalse(service.slots.locked())

    async def test_busy_upload_does_not_read_request_body(self):
        service=PaperService(MagicMock());await service.slots.acquire();await service.slots.acquire()
        async def body():raise AssertionError('must not read');yield b''
        with self.assertRaises(PaperBusy):await service.upload(1,uuid4().hex,'Paper',body())
        service.slots.release();service.slots.release()

    async def test_cancelled_parse_records_interruption_and_releases_capacity(self):
        repo=MagicMock();repo.reserve.return_value=({'paper_id':'a'*32},'b'*32)
        parser=SimpleNamespace(parse=AsyncMock(side_effect=asyncio.CancelledError()))
        service=PaperService(repo,parser=parser)
        async def body():yield b'%PDF-test'
        with self.assertRaises(asyncio.CancelledError):await service.upload(1,uuid4().hex,'Paper',body())
        self.assertEqual(repo.fail.call_args.args[-1],'PROCESS_INTERRUPTED')
        self.assertFalse(service.slots.locked());repo.finish.assert_not_called()


if __name__=='__main__':unittest.main()
