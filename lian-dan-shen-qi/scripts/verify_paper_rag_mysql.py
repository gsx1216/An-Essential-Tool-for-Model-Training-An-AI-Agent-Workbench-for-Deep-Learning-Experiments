"""论文 RAG 的真实 MySQL/HTTP/PDF 进程验收；模型/SSH 均模拟，不产生付费调用。"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app import database
from app.config import get_mysql_password
from app.models.analysis import AnalysisSettings,compact
from app.models.paper import PaperConflict,PaperParseError,sha256
from app.models.runner import RunnerSettings
from app.models.suggestion import SuggestionSettings
from app.models.training_metrics import initial_stream,parse_page
from app.services.analysis_service import AnalysisService
from app.services.suggestion_service import SuggestionService
from app.services.user_model_gateway_factory import UserModelGatewayFactory
from app.services.pdf_parser_process import extract
from scripts._chat_api_test_app import temporary_chat_api,FakeGatewayBuilder
from scripts.verify_source_code_mysql import SourceFixture
from scripts.verify_analysis_mysql_redis import FakeAnalysisBuilder
from tests.paper_fixtures import pdf_bytes,PAPER_PAGES


async def verify(password):
    groups=[];fixture=SourceFixture();fixture.files['train.py']=b'loss = criterion(prediction, target)\nloss.backward()\n'
    spec={'path':'train.py','sha256':sha256(fixture.files['train.py']),'parameters':{},'max_runtime_seconds':10,'max_log_bytes':1000}
    with temporary_chat_api(password,papers=True,suggestions=True,analysis=True,training=True,
            runner_settings=RunnerSettings(compact({'paper_fixture':spec}))) as (app,state),TestClient(app) as client:
        repo,service=state['paper_repository'],state['papers'];name=state['database_name'];raw=pdf_bytes(PAPER_PAGES)
        state['sources'].transport=fixture
        def sql(text,values=(),fetch=True):
            with database.get_connection(password) as connection:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(text.replace('experiment_assistant',name),values)
                    result=cursor.fetchall() if fetch else None
                connection.commit();return result
        def passed(label):groups.append(label);print('PASS:',label,flush=True)
        def user(prefix):
            request={'username':prefix+uuid4().hex[:8],'password':'TestPass123!'}
            assert client.post('/auth/register',json=request).status_code==201
            result=client.post('/auth/login',json=request).json()
            return result['user']['user_id'],{'Authorization':'Bearer '+result['access_token']}
        uid,owner=user('paper_owner_');_,other=user('paper_other_')
        def upload(content=raw,*,request_id=None,title='Boundary supervision paper',headers=owner):
            return client.post('/papers',params={'request_id':request_id or uuid4().hex,'title':title},
                headers={**headers,'Content-Type':'application/pdf'},content=content)
        assert client.get('/papers').status_code==401
        assert upload(headers={}).status_code==401
        assert client.post('/papers/search',headers=owner,json={'query':'loss','user_id':uid}).status_code==422
        assert client.post('/papers',headers=owner,params={'request_id':uuid4().hex,'title':'x'},json={'url':'anywhere'}).status_code==415
        assert upload(b'not PDF').status_code==415
        assert client.get('/papers',headers=owner).json()['items']==[]
        passed('authentication, strict fields and raw PDF media validation reject requests before indexing')

        request=uuid4().hex;response=upload(request_id=request)
        assert response.status_code==201,response.text
        document=response.json();pid=document['paper_id']
        assert document['status']=='ready' and document['page_count']==2 and document['chunk_count']==2,document
        assert document['file_sha256']==sha256(raw) and 'pypdf-' in document['parser_version']
        assert client.get('/papers/'+pid+'/file',headers=owner).content==raw
        chunks=client.get('/papers/'+pid+'/chunks',headers=owner).json()['items']
        assert [row['page_number'] for row in chunks]==[1,2]
        cid=chunks[0]['chunk_id']
        response=client.get('/papers/chunks/'+cid,headers=owner)
        assert response.status_code==200,response.text
        assert 'boundary supervision' in response.json()['text'] and response.json()['page_number']==1
        assert response.headers['Cache-Control']=='no-store'
        passed('actual bounded PDF subprocess indexes physical pages and hashes; original download exactly matches uploaded bytes')

        with patch.object(service.parser,'parse',side_effect=AssertionError('replay parsed again')):
            assert upload(request_id=request).json()['paper_id']==pid
            assert client.post('/papers/'+pid+'/retry',headers=owner).json()['status']=='ready'
        assert upload(request_id=request,title='another title').status_code==409
        assert upload(pdf_bytes(['different paper']),request_id=request).status_code==409
        assert len(client.get('/papers',headers=owner).json()['items'])==1
        passed('same upload request replays one document without parsing; changed bytes/title conflict; ready retry is read-only')

        for path in ('/papers/'+pid,'/papers/'+pid+'/file','/papers/'+pid+'/chunks','/papers/chunks/'+cid):
            assert client.get(path,headers=other).status_code==404,path
        assert client.delete('/papers/'+pid,headers=other).status_code==404
        assert client.post('/papers/'+pid+'/retry',headers=other).status_code==404
        assert client.post('/papers/search',headers=other,json={'query':'boundary','paper_id':pid}).status_code==404
        assert client.post('/papers/search',headers=other,json={'query':'boundary'}).json()['items']==[]
        other_doc=upload(pdf_bytes(['TOP_SECRET_OTHER_PAPER boundary supervision private result.']),title='Other private paper',headers=other).json()
        result=client.post('/papers/search',headers=owner,json={'query':'boundary'}).json()
        assert all(row['paper_id']==pid for row in result['items'])
        assert 'TOP_SECRET_OTHER_PAPER' not in json.dumps(result)
        passed('all metadata/file/chunk/search/delete/retry paths enforce owner before returning any other tenant content')

        # 中文和短指标词验证检索索引本身；合成 Unicode 页不冒充真实 PDF OCR。
        parser=service.parser
        data={'page_count':1,'empty_pages':[],'parser_version':'unicode-fixture','chunks':[{'chunk_index':0,'page_number':1,'char_start':0,
            'content':'伪装目标检测使用边界监督，MAE 指标用于固定测试集评价。','content_sha256':sha256('伪装目标检测使用边界监督，MAE 指标用于固定测试集评价。'.encode())}]}
        service.parser=SimpleNamespace(parse=lambda _:async_result(data))
        unicode_doc=upload(title='Unicode fulltext fixture').json();service.parser=parser
        for query in ('伪装目标检测','边界监督','MAE'):
            response=client.post('/papers/search',headers=owner,json={'query':query,'paper_id':unicode_doc['paper_id']})
            assert response.status_code==200 and response.json()['items'],(query,response.text)
        assert client.post('/papers/search',headers=owner,json={'query':'boundary','paper_id':pid}).json()['items'][0]['page_number']==1
        passed('real MySQL ngram index retrieves English, Chinese and MAE; stopwords are disabled only for paper connections/index creation')

        blank=upload(pdf_bytes(['']),title='Scanned-like blank PDF').json()
        assert blank['status']=='failed' and blank['error_code']=='NO_EXTRACTABLE_TEXT',blank
        encrypted=upload(pdf_bytes(['private'],encrypted=True),title='Encrypted PDF').json()
        assert encrypted['status']=='failed' and encrypted['error_code']=='ENCRYPTED_PDF',encrypted
        assert client.post('/papers/search',headers=owner,json={'query':'private','paper_id':encrypted['paper_id']}).status_code==409
        service.parser=SimpleNamespace(parse=lambda _:async_raise(PaperParseError('PDF_TIMEOUT')))
        timed=upload(title='Retryable parser timeout').json();service.parser=parser
        assert timed['status']=='failed' and timed['error_code']=='PDF_TIMEOUT'
        assert client.post('/papers/'+timed['paper_id']+'/retry',headers=owner).json()['status']=='ready'
        passed('empty/encrypted/timeout PDFs retain explicit failed state; explicit retry recovers using the stored original')

        # Same request races allocate one row/blob, then an expired lease is fenced.
        rid=uuid4().hex
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:repo.reserve(uid,rid,'Concurrent paper',raw),range(2)))
        assert len({row['paper_id'] for row,token in results})==1
        claimed=[(row,token) for row,token in results if token]
        assert len(claimed)==1
        row,old_token=claimed[0];race=row['paper_id']
        assert client.delete('/papers/'+race,headers=owner).status_code==409
        sql('UPDATE experiment_assistant.papers SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE id=%s',(race,),False)
        row,new_token=repo.claim_retry(uid,race)
        try:repo.finish(uid,race,old_token,extract(raw))
        except PaperConflict:pass
        else:raise AssertionError('stale parse overwrote current lease')
        repo.finish(uid,race,new_token,extract(raw))
        assert client.get('/papers/'+race,headers=owner).json()['chunk_count']==2
        passed('concurrent upload shares one blob/metadata row; active delete and stale parse commits cannot corrupt a reclaimed document')

        row,token=repo.reserve(uid,uuid4().hex,'Atomic index failure',raw);atomic=row['paper_id']
        with patch.object(repo,'finish_paper_sql',"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='TEST_ONLY_INDEX_FAILURE'"):
            try:repo.finish(uid,atomic,token,extract(raw))
            except MySQLError:pass
            else:raise AssertionError('injected commit failure succeeded')
        assert not sql('SELECT id FROM experiment_assistant.paper_chunks WHERE paper_id=%s',(atomic,))
        repo.finish(uid,atomic,token,extract(raw))
        passed('chunk inserts and ready status commit in one transaction; failed save leaves no partial searchable index')

        # Chat reuses normal message lifecycle; source snapshots are recorded before delivery.
        assert client.put('/me/model-credentials/deepseek',headers=owner,json={'api_key':'sk-TEST_ONLY_PAPER_KEY'}).status_code==204
        session=client.post('/sessions',headers=owner,json={'title':'PDF Q and A'}).json()['session_id']
        async def chat(messages,options):
            names={tool['function']['name'] for tool in options['tools']}
            assert 'read_paper_chunk' in names and 'start_training_run' not in names
            delivered=[m for m in messages if isinstance(m,dict) and m.get('role')=='tool']
            if not delivered:return {'tool_calls':[{'id':'findpaper','name':'search_papers','args':{'query':'boundary supervision','paper_id':pid}}]}
            if len(delivered)==1:
                selected=json.loads(delivered[-1]['content'])['data']['items'][0]['chunk_id']
                return {'tool_calls':[{'id':'readpaper','name':'read_paper_chunk','args':{'chunk_id':selected}}]}
            data=json.loads(delivered[-1]['content'])['data']
            return {'content':compact({'answer':'可以对边界监督开展消融，不能据此确认改进。','citations':[{'chunk_id':data['chunk_id'],'quote':'boundary supervision'}],
                'limitations':['本次仅读取一页的片段，尚未阅读完整论文。']})}
        state['builder'].handler=chat;request_id=uuid4().hex
        response=client.post('/sessions/'+session+'/messages',headers=owner,json={'content':'根据上传论文分析训练结果并解释边界监督。','request_id':request_id})
        assert response.status_code==200,response.text
        answer=response.json()['content'];assert 'PDF 第 1 页' in answer and pid in answer and 'boundary supervision' in answer
        audit=client.get('/sessions/'+session+'/tool-calls',headers=owner).json()['items']
        assert [row['tool_name'] for row in audit]==['search_papers','read_paper_chunk']
        assert audit[-1]['result_metadata']['paper_evidence']['text']
        assert audit[-1]['result_metadata']['paper_evidence']['page_number']==1
        calls=len(state['builder'].created)
        assert client.post('/sessions/'+session+'/messages',headers=owner,json={'content':'根据上传论文分析训练结果并解释边界监督。','request_id':request_id}).json()['content']==answer
        assert len(state['builder'].created)==calls
        passed('chat retrieves actual PDF text, saves bounded evidence in tool audit and generates verified page citations; completed request replays without model')

        from unittest.mock import AsyncMock
        from app.services.research_service import ResearchService
        network=SimpleNamespace(hints=AsyncMock(return_value=[]),execute=AsyncMock(side_effect=AssertionError('private text must not leave')),
            remember=ResearchService.remember,finish=ResearchService.finish)
        state['chat'].tool_service.research_factory=lambda:network
        safe_session=client.post('/sessions',headers=owner,json={'title':'PDF privacy boundary'}).json()['session_id']
        async def injected(messages,options):
            delivered=[m for m in messages if isinstance(m,dict) and m.get('role')=='tool']
            names={tool['function']['name'] for tool in options['tools']}
            if not delivered:
                assert 'search_references' in names
                return {'tool_calls':[{'id':'read_private','name':'read_paper_chunk','args':{'chunk_id':cid}},
                    {'id':'exfiltrate','name':'search_references','args':{'query':'DO_NOT_SEND_PRIVATE_PDF_TEXT'}}]}
            assert 'search_references' not in names and 'read_reference' not in names
            assert json.loads(delivered[-1]['content'])['code']=='UNSUPPORTED_TOOL'
            return {'content':compact({'answer':'引用本轮实际读取的论文。','citations':[{'chunk_id':cid,'quote':'boundary supervision'}],
                'limitations':['未执行任何训练或外发私人论文。']})}
        state['builder'].handler=injected
        response=client.post('/sessions/'+safe_session+'/messages',headers=owner,json={'content':'分析上传论文的训练建议。','request_id':uuid4().hex})
        assert response.status_code==200,response.text
        assert network.execute.await_count==0
        audit_private=client.get('/sessions/'+safe_session+'/tool-calls',headers=owner).json()['items']
        assert [row['tool_name'] for row in audit_private]==['read_paper_chunk','unsupported']
        state['chat'].tool_service.research_factory=None
        passed('after private PDF text is read, public network tools are removed and same-batch forged calls cannot exfiltrate it')

        async def forged(messages,options):
            delivered=[m for m in messages if isinstance(m,dict) and m.get('role')=='tool']
            if not delivered:return {'tool_calls':[{'id':'readreal','name':'read_paper_chunk','args':{'chunk_id':cid}}]}
            return {'content':compact({'answer':'An invented conclusion','citations':[{'chunk_id':cid,'quote':'THIS QUOTATION WAS NEVER READ'}],
                'limitations':['fixture']})}
        state['builder'].handler=forged
        response=client.post('/sessions/'+session+'/messages',headers=owner,json={'content':'查看论文片段并回答。','request_id':uuid4().hex})
        assert response.status_code!=200
        assert 'THIS QUOTATION WAS NEVER READ' not in client.get('/sessions/'+session+'/messages',headers=owner).text
        passed('invented quotation fails the chat turn instead of being saved as a sourced answer')

        # Genuine analysis -> independent suggestion worker -> PDF + code + report evidence.
        assert client.put('/me/analysis-settings',headers=owner,json={'enabled':True}).status_code==200
        assert client.put('/me/suggestion-settings',headers=owner,json={'enabled':True}).status_code==200
        exp=client.post('/experiments',headers=owner,json={'title':'PDF backed suggestions','kind':'training'}).json()['experiment_id']
        server=client.post('/servers',headers=owner,json={'name':'fixture','host':'127.0.0.1','port':22,'username':'trainer',
            'auth_method':'password','password':'TEST_ONLY_SSH_PASSWORD','host_fingerprint':'SHA256:'+base64.b64encode(bytes(32)).decode().rstrip('='),
            'work_dir':'/owned/project'}).json()['server_id']
        profile=await state['server_service'].get(uid,server)
        run=client.post('/experiments/'+exp+'/runs',headers=owner,json={'request_id':uuid4().hex,'parameters':{}}).json()['run']['run_id']
        state['executions'].claim(uid,run,profile,'paper_fixture',{'version':1,'run_id':run,'user_id':uid,'server_id':server,
            'server_binding':profile.binding(),'script_id':'paper_fixture','work_dir':profile.work_dir,'spec':spec,'parameters':{},'output_directory':'training_outputs'})
        sql("UPDATE experiment_assistant.experiment_runs SET status='succeeded',exit_code=0,started_at=created_at,finished_at=UTC_TIMESTAMP(6) WHERE id=%s",(run,),False)
        metric=b'{"type":"metric","step":1,"epoch":1,"split":"validation","dataset":"cod10k","mae":0.03}\n'
        stream,events=parse_page(initial_stream(),metric,{'log_size':len(metric),'truncated':False,'stale':False,'status':'succeeded'})
        state['metric_repository'].commit_page(uid,run,0,stream,events)
        assert client.post('/runs/'+run+'/report',headers=owner).status_code==200
        event=client.get('/runs/'+run+'/events',headers=owner).json()[0]['event_id'];state['events'].receive(event)
        upstream=AnalysisService(state['analysis_repository'],settings=AnalysisSettings(enabled=True),
            gateway_factory=UserModelGatewayFactory(state['chat'].gateway_factory.credential_service,FakeAnalysisBuilder()))
        assert await upstream.tick()
        builder=FakeGatewayBuilder()
        async def suggest(messages,options):
            names={tool['function']['name'] for tool in options['tools']}
            assert 'read_paper_chunk' in names and not {'start_training_run','apply_code_change'}&names
            delivered=[m for m in messages if isinstance(m,dict) and m.get('role')=='tool']
            if not delivered:return {'tool_calls':[{'id':'code','name':'read_project_file','args':{'path':'train.py'}}]}
            if len(delivered)==1:return {'tool_calls':[{'id':'paper','name':'read_paper_chunk','args':{'chunk_id':cid}},
                {'id':'blocked_web','name':'search_references','args':{'query':'DO_NOT_SEND_PRIVATE_PDF_TEXT'}}]}
            assert 'search_references' not in names and 'read_reference' not in names
            return {'content':compact({'suggestions':[{'problem':'一次验证观测不足。','hypothesis':'边界监督的效果需要消融验证。',
                'change':'固定其他配置，比较是否加入边界监督。','conditions':'保持数据和评测一致。','validation':'多次重复并比较完整指标曲线。',
                'evidence_paths':['/series/0/points'],'code':[{'evidence_id':'code_1','start_line':1,'end_line':2}],
                'papers':[{'evidence_id':'paper_2','quote':'boundary supervision'}]}],
                'limitations':['仅依赖已读取片段，不能确认因果。']})}
        builder.handler=suggest
        worker=SuggestionService(state['suggestion_repository'],settings=SuggestionSettings(enabled=True),
            gateway_factory=UserModelGatewayFactory(state['chat'].gateway_factory.credential_service,builder),
            source_factory=lambda:state['sources'],paper_factory=lambda:service,
            research_factory=lambda:SimpleNamespace(search=AsyncMock(side_effect=AssertionError('private PDF must not be sent to public search'))))
        assert await worker.tick()
        saved=client.get('/runs/'+run+'/suggestions',headers=owner)
        assert saved.status_code==200 and saved.json(),saved.text
        item=saved.json()[0];source=item['content']['paper_evidence'][0]
        assert item['content']['evidence_status']=='supported' and source['kind']=='uploaded_pdf'
        assert source['page_number']==1 and source['paper_id'].replace('-','')==pid
        history=client.get('/runs/'+run+'/suggestion-tasks/'+item['task_id']+'/evidence',headers=owner).json()
        assert history[-2]['tool_name']=='read_paper_chunk' and history[-2]['result']['data']['kind']=='uploaded_pdf'
        assert history[-1]['tool_name']=='unsupported'
        passed('independent suggestion worker persists actual report facts, source code and owned PDF page/quote evidence without web or mutation tools')

        # 从已部署的旧约束增量升级，保留此前的论文/工具证据。
        sql('ALTER TABLE experiment_assistant.paper_chunks DROP CHECK ck_paper_chunk, ADD CONSTRAINT ck_paper_chunk CHECK(chunk_index<3000 AND page_number BETWEEN 1 AND 200 AND char_start<=100000 AND CHAR_LENGTH(content_text) BETWEEN 1 AND 1000)',fetch=False)
        sql('ALTER TABLE experiment_assistant.chat_tool_calls DROP CHECK ck_chat_tool_meta, ADD CONSTRAINT ck_chat_tool_meta CHECK(JSON_VALID(result_meta_json) AND OCTET_LENGTH(result_meta_json)<=4096)',fetch=False)
        old=database._read_sql
        with patch.object(database,'_read_sql',side_effect=lambda filename:old(filename).replace('experiment_assistant',name)):
            with database.get_connection(password) as connection:database._ensure_papers(connection);database._ensure_papers(connection)
        assert client.get('/papers/'+pid,headers=owner).json()['status']=='ready'
        assert client.get('/sessions/'+session+'/tool-calls',headers=owner).json()['items'][-1]['result_metadata']['paper_evidence']['text']
        passed('old 1000-character and 4096-byte constraints upgrade additively and repeat without losing papers or audit snapshots')

        problem='Problem: boundaries are ambiguous in camouflage. '
        full_page=problem+('Motivation is preserving local details and global context. '*85)+'MethodMarker uses gated boundary supervision.'
        assert 4000<len(full_page)<6000
        method_doc=upload(pdf_bytes([full_page,'ImplementationMarker combines two branches. An ablation study tests each component.']),title='Complete method fixture').json()
        assert method_doc['status']=='ready' and method_doc['chunk_count']==2,method_doc
        mid=method_doc['paper_id']
        method_rows=client.get('/papers/'+mid+'/chunks',headers=owner).json()['items'];mcid=method_rows[0]['chunk_id']
        text=client.get('/papers/chunks/'+mcid,headers=owner).json()
        assert text['text'].startswith(problem) and 'MethodMarker' in text['text']
        assert len(text['text'])>4000 and text['next_offset'] is None and text['next_chunk_index']==1
        clipped=client.get('/papers/chunks/'+mcid,headers=owner,params={'max_bytes':1000}).json()
        tail=client.get('/papers/chunks/'+mcid,headers=owner,params={'offset':clipped['next_offset']}).json()
        assert clipped['text']+tail['text']==text['text']
        method_session=client.post('/sessions',headers=owner,json={'title':'Complete method explanation'}).json()['session_id']
        async def complete_method(messages,options):
            delivered=[json.loads(m['content'])['data'] for m in messages if isinstance(m,dict) and m.get('role')=='tool']
            if not delivered:return {'tool_calls':[{'id':'whole_problem_method','name':'read_paper_chunk','args':{'chunk_id':mcid}}]}
            if len(delivered)==1:
                assert delivered[0]['text'].startswith(problem) and 'MethodMarker' in delivered[0]['text']
                return {'tool_calls':[{'id':'following_implementation','name':'read_paper_chunk','args':{'paper_id':mid,'chunk_index':delivered[0]['next_chunk_index']}}]}
            assert delivered[1]['previous_chunk_index']==0 and 'ImplementationMarker' in delivered[1]['text']
            return {'content':compact({'answer':'读取了问题、动机、方法及下一页实现说明；具体效果仍需验证。',
                'citations':[{'chunk_id':mcid,'quote':'MethodMarker uses gated boundary supervision.'},
                             {'chunk_id':delivered[1]['chunk_id'],'quote':'ImplementationMarker combines two branches.'}],
                'limitations':['只阅读本方法的两页测试资料，不代表完整论文。']})}
        state['builder'].handler=complete_method
        response=client.post('/sessions/'+method_session+'/messages',headers=owner,json={'content':'请完整解释上传论文的方法模块解决什么问题、设计动机和实现。','request_id':uuid4().hex})
        assert response.status_code==200,response.text
        assert 'PDF 第 1 页' in response.json()['content'] and 'PDF 第 2 页' in response.json()['content']
        method_audit=client.get('/sessions/'+method_session+'/tool-calls',headers=owner).json()['items']
        assert len(method_audit[0]['result_metadata']['paper_evidence']['text'])>4000
        passed('a full 4000+ character problem/motivation/method page reaches chat and audit; next-page implementation is read and cited with real pages')

        # 重建旧索引需要用户显式选择；写入失败不删除旧分块。
        before_ids=[row['chunk_id'] for row in method_rows]
        row,rebuild_token=repo.claim_retry(uid,mid,rebuild=True)
        with patch.object(repo,'insert_paper_chunk_sql','INSERT INTO nonexistent_paper_fixture VALUES (%s,%s,%s,%s,%s,%s,%s,%s)'):
            try:repo.finish(uid,mid,rebuild_token,extract(pdf_bytes([full_page,'ImplementationMarker combines two branches. An ablation study tests each component.'])))
            except MySQLError:pass
            else:raise AssertionError('rebuild must fail atomically')
        assert {row['id'] for row in sql('SELECT id FROM experiment_assistant.paper_chunks WHERE user_id=%s AND paper_id=%s',(uid,mid))}==set(before_ids)
        assert client.post('/papers/'+mid+'/retry',headers=owner,params={'rebuild':True}).status_code==409
        sql('UPDATE experiment_assistant.papers SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE id=%s',(mid,),False)
        rebuilt=client.post('/papers/'+mid+'/retry',headers=owner,params={'rebuild':True})
        assert rebuilt.status_code==200 and rebuilt.json()['status']=='ready',rebuilt.text
        assert rebuilt.json()['file_sha256']==method_doc['file_sha256'] and 'paragraph-v2' in rebuilt.json()['parser_version']
        assert client.get('/papers/chunks/'+mcid,headers=owner).status_code==404
        assert client.get('/sessions/'+method_session+'/tool-calls',headers=owner).json()['items'][0]['result_metadata']['paper_evidence']['chunk_id']==mcid
        passed('explicit reindex preserves original bytes, replaces blocks atomically, fences active attempts and retains old citation snapshots')

        assert client.delete('/papers/'+pid,headers=owner).status_code==204
        assert client.get('/papers/'+pid+'/file',headers=owner).status_code==404
        assert client.get('/papers/chunks/'+cid,headers=owner).status_code==404
        assert not sql('SELECT paper_id FROM experiment_assistant.paper_files WHERE paper_id=%s',(pid,))
        assert not sql('SELECT id FROM experiment_assistant.paper_chunks WHERE paper_id=%s',(pid,))
        assert client.post('/papers/search',headers=owner,json={'query':'boundary','paper_id':pid}).status_code==404
        assert client.get('/runs/'+run+'/suggestions',headers=owner).json()[0]['content']['paper_evidence'][0]['kind']=='uploaded_pdf'
        assert client.delete('/sessions/'+session,headers=owner).status_code==204
        assert client.get('/papers/'+unicode_doc['paper_id'],headers=owner).status_code==200
        logs='\n'.join(path.read_text(encoding='utf-8') for path in state['log_dir'].rglob('*.txt'))
        assert all(secret not in logs for secret in ('sk-TEST_ONLY_PAPER_KEY','TEST_ONLY_SSH_PASSWORD','TOP_SECRET_OTHER_PAPER',PAPER_PAGES[0]))
        passed('delete atomically removes original/chunks/index while historical evidence remains a snapshot; deleting chat preserves independent papers and logs exclude content/secrets')

        old=database._read_sql
        with patch.object(database,'_read_sql',side_effect=lambda filename:old(filename).replace('experiment_assistant',name)):
            with database.get_connection(password) as connection:database._ensure_papers(connection);database._ensure_papers(connection)
        assert client.get('/papers/'+unicode_doc['paper_id'],headers=owner).status_code==200
        schema=client.get('/openapi.json').json()
        assert schema['paths']['/papers']['post']['requestBody']['content']['application/pdf']['schema']['format']=='binary'
        assert len(sql('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s',(name,)))==39
        passed('repeat additive migration retains data; Swagger declares raw PDF upload and current schema contains 39 business tables')
    print(f'ALL {len(groups)} paper RAG HTTP/MySQL groups passed; temporary DB removed; real PDF parsing, fake model/SSH, no paid API or GPU.',flush=True)
    return len(groups)


async def async_result(data):return data
async def async_raise(error):raise error


if __name__=='__main__':asyncio.run(verify(get_mysql_password()))
