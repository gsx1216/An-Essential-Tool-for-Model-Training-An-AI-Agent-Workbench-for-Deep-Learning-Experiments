"""真实 HTTP/MySQL、模拟公开网页和模型；不联网、不运行训练、不调用收费 API。"""
import json
from pathlib import Path
from unittest.mock import AsyncMock,patch
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from mysql.connector import IntegrityError,ProgrammingError

from app import database
from app.config import get_mysql_password
from app.models.chat_tools import compact_json
from app.models.context_snapshot import ContextAttempt
from app.models.context_state import ContextConflictError
from app.models.research import ResearchUnavailableError,reference_row
from app.services.public_research_client import PublicResearchClient
from scripts._chat_api_test_app import temporary_chat_api

QUOTE='Compare one change at a time using an unchanged evaluation protocol.'
HTML=('<html><title>Fixture paper, not a real publication</title><main>'+QUOTE+
      ' These findings do not guarantee improvement on another dataset.</main></html>').encode()
ATOM=b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2401.12345v1</id>
<title>Fixture paper, not a real publication</title><summary>Example abstract. Not implementation evidence.</summary>
<author><name>Fixture author</name></author></entry></feed>'''
URL='https://arxiv.org/html/2401.12345v1'


def verify(password):
    with temporary_chat_api(password,research=True) as (app,state),TestClient(app) as client:
        requests=[]
        def transport(request):
            requests.append((request.headers['host'],str(request.url)))
            data=ATOM if request.headers['host']=='export.arxiv.org' else HTML
            return httpx.Response(200,headers={'content-type':'application/atom+xml' if data is ATOM else 'text/html'},stream=httpx.ByteStream(data))
        reader=PublicResearchClient(AsyncMock(),resolver=AsyncMock(return_value=['93.184.216.34']),transport=httpx.MockTransport(transport))
        state['research'].client=reader
        def user(prefix):
            username=prefix+uuid4().hex[:8];body={'username':username,'password':'TestPass123!'}
            result=client.post('/auth/register',json=body);assert result.status_code==201,result.text
            headers={'Authorization':'Bearer '+client.post('/auth/login',json=body).json()['access_token']}
            assert client.put('/me/model-credentials/deepseek',headers=headers,json={'api_key':'sk-TEST_ONLY_RESEARCH_KEY'}).status_code==204
            return result.json()['user_id'],headers
        uid,owner=user('research_a_');other,foreign=user('research_b_')
        def session():
            result=client.post('/sessions',headers=owner,json={'title':'Research fixture'})
            assert result.status_code==201,result.text
            return result.json()['session_id']
        def send(sid,request=None):
            return client.post(f'/sessions/{sid}/messages',headers=owner,json={
                'content':'分析最近一次训练，并查询公开论文给出有出处的修改建议。','request_id':request or uuid4().hex})
        def saved(sid,advice=False,headers=owner):
            return client.get(f'/sessions/{sid}/'+('research-advice' if advice else 'references'),headers=headers)
        def sql(statement,params=(),fetch=False):
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement.replace('experiment_assistant',state['database_name']),params)
                    rows=cursor.fetchall() if fetch else None
                connection.commit();return rows
        def count(table,sid):return sql(f'SELECT COUNT(*) FROM experiment_assistant.{table} WHERE session_id=%s',(sid,),True)[0][0]
        def call(name,args=None,index='call_1'):
            return {'content':'','tool_calls':[{'id':index,'name':name,'args':args or {}}]}
        def structured(ref,quote=QUOTE):
            return compact_json({'facts':['Observed values are not causal evidence.'],
                'hypotheses':['Generalization may require a controlled comparison.'],
                'recommendations':[{'change':'Compare one change against the baseline.',
                    'why':'Use a comparable protocol as motivated by the source.',
                    'conditions':'Keep datasets, split and metric definitions unchanged.',
                    'validation':'Repeat baseline and variant; reject changes that worsen validation.',
                    'citations':[{'reference_id':ref,'quote':quote}]}],
                'limitations':['Fixture source and fake model; not a real scientific recommendation.']})
        def plan(steps):
            queue=list(steps);captured=[]
            async def handler(messages,options):
                if isinstance(messages[0],tuple):return {'content':'历史摘要：训练观察与来源引用均需重新核验。'}
                captured.append((messages,options));assert queue,'fake model script exhausted'
                item=queue.pop(0)
                return item(messages) if callable(item) else item
            state['builder'].handler=handler;return captured
        def last(messages):return json.loads(messages[-1]['content'])['data']
        def read_search(messages):return call('read_reference',{'reference_id':last(messages)['items'][0]['reference_id']},'read_1')
        def final(messages):return {'content':structured(last(messages)['reference_id'])}

        sid=session();captured=plan([{'content':'你好'}])
        response=send(sid);assert response.status_code==200,response.text
        assert len(captured[0][1]['tools'])==5 and requests==[] and count('research_references',sid)==0
        assert saved(sid).headers['cache-control']=='no-store'
        print('PASS: research tools wired into real API; ordinary chat has no network requests')

        example=json.loads((Path(__file__).resolve().parents[1]/'examples/evaluation_examples.json').read_text(encoding='utf-8'))['camouflaged_object_detection']
        exp=client.post('/experiments',headers=owner,json=example).json()['experiment_id']
        rid=client.post(f'/experiments/{exp}/runs',headers=owner,json={'request_id':uuid4().hex,'parameters':{}}).json()['run']['run_id']
        sid=session();request=uuid4().hex
        captured=plan([call('get_latest_training_run',index='latest'),call('get_run_report',index='report'),
            call('search_references',{'query':'camouflaged object detection','limit':1},'search'),read_search,final])
        response=send(sid,request);assert response.status_code==200,response.text
        assert URL in response.json()['content'] and rid in response.json()['content']
        refs=saved(sid).json()['items'];adv=saved(sid,True).json()['items']
        assert len(refs)==2 and len(adv)==1 and adv[0]['run_id']==rid
        assert adv[0]['advice']['sources'][0]['kind']=='paper_html'
        assert adv[0]['advice']['recommendations'][0]['citations'][0]['character_range']==[0,len(QUOTE)]
        assert count('chat_messages',sid)==2
        reads_before=len(requests);assert send(sid,request).json()==response.json()
        assert len(requests)==reads_before and len(saved(sid,True).json()['items'])==1
        print('PASS: latest run + missing report + arXiv search + HTML read + quote-checked advice persisted; request retry idempotent')

        ref=next(row['reference_id'] for row in refs if row['kind']=='paper_html')
        assert saved(sid,headers=foreign).status_code==404 and saved(sid,True,foreign).status_code==404
        assert client.get(f'/sessions/{sid}/references').status_code==401
        isolated=session();captured=plan([call('read_reference',{'reference_id':ref}),{'content':'Cannot access source.'}])
        response=send(isolated);assert response.status_code==200,response.text
        assert json.loads(captured[-1][0][-1]['content'])['code']=='REFERENCE_NOT_FOUND' and len(requests)==reads_before
        print('PASS: real login, session ownership and same-owner cross-session isolation stop forbidden reads before network')

        # Reopen a saved source without HTTP; model still must read this turn to quote it.
        captured=plan([call('read_reference',{'reference_id':ref}),final])
        assert send(sid).status_code==200
        assert len(requests)==reads_before and len(saved(sid,True).json()['items'])==2
        assert ref in captured[0][0][0]['content']
        print('PASS: later question can read immutable source snapshot from MySQL; final citation requires this-turn excerpt')

        invalid=session();captured=plan([call('read_reference',{'url':URL}),
            lambda m:{'content':structured(last(m)['reference_id'],'A quote that was never in the source.')}])
        response=send(invalid);assert response.status_code==502,response.text
        assert count('chat_messages',invalid)==1 and count('research_advice',invalid)==0
        assert sql('SELECT status FROM experiment_assistant.chat_turns WHERE session_id=%s',(invalid,),True)[0][0]=='failed'
        bad_ref=saved(invalid).json()['items'][0]['reference_id']
        plan([call('read_reference',{'reference_id':bad_ref}),final])
        response=client.post(f'/sessions/{invalid}/retry',headers=owner);assert response.status_code==200,response.text
        assert count('chat_messages',invalid)==2 and count('research_advice',invalid)==1
        print('PASS: invented quote rejects final answer, preserves user question and sources; retry recovers without duplicate question')

        atomic=session();plan([call('read_reference',{'url':URL}),final])
        audit=state['chat'].tool_service.repository
        with patch.object(audit,'save_chat_tool_call_sql','INSERT INTO missing_audit_table VALUES (1)'):
            response=send(atomic)
        assert response.status_code==503,response.text
        assert count('research_references',atomic)==count('chat_tool_calls',atomic)==0 and count('chat_messages',atomic)==1
        print('PASS: tool-audit failure rolls back newly read source in the same transaction')

        atomic_reply=session();plan([call('read_reference',{'url':URL}),final])
        with patch.object(state['messages'],'save_research_advice_sql','INSERT INTO missing_advice_table VALUES (1)'):
            response=send(atomic_reply)
        assert response.status_code==503,response.text
        assert count('research_advice',atomic_reply)==0 and count('chat_messages',atomic_reply)==1
        assert count('research_references',atomic_reply)==1 and count('chat_tool_calls',atomic_reply)==1
        # Direct repository proof: the inserted advice also rolls back when the AI-message INSERT fails.
        from app.models.research import render_grounded_reply
        row=saved(atomic_reply).json()['items'][0];refid=row['reference_id']
        stored=state['research_repository'].get_reference(uid,atomic_reply,refid)
        delivered={refid:[dict(reference_id=refid,url=stored['url'],title=stored['title'],kind=stored['kind'],
            text=stored['content'],offset=0,content_sha256=stored['content_sha256'])]}
        reply=render_grounded_reply(structured(refid),delivered,None)
        tid,aid=sql('SELECT id,attempt_id FROM experiment_assistant.chat_turns WHERE session_id=%s',(atomic_reply,),True)[0]
        with patch.object(state['messages'],'save_message_sql','INSERT INTO missing_message_table VALUES (1)'):
            try:state['messages'].complete_turn(uid,atomic_reply,tid,aid,reply.text,research_reply=reply)
            except ProgrammingError:pass
            else:raise AssertionError('expected rollback')
        assert count('research_advice',atomic_reply)==0 and count('chat_messages',atomic_reply)==1
        print('PASS: advice and assistant reply commit atomically; either INSERT failure preserves question and rolls back advice')

        # Live attempts and composite ownership FK guard persistence even after an HTTP fetch.
        newrow=reference_row(url=URL,title='Fixture',text=QUOTE,kind='paper_html')
        try:audit.record(uid,atomic_reply,ContextAttempt(tid,uuid4().hex),call_id='stale',call_index=6,
            name='read_reference',arguments={'url':URL},outcome='OK',metadata={},duration_ms=1,reference_rows=[newrow])
        except ContextConflictError:pass
        else:raise AssertionError('stale attempt wrote source')
        assert count('research_references',atomic_reply)==1
        try:sql('UPDATE experiment_assistant.research_references SET user_id=%s WHERE id=%s',(other,refid))
        except IntegrityError:pass
        else:raise AssertionError('reference accepted wrong owner')
        print('PASS: stale attempts and SQL composite ownership constraints prevent orphan or foreign source writes')

        unavailable=session();plan([call('search_references',{'query':'public research'}),{'content':'Made-up advice with an invented URL.'}])
        with patch.object(reader,'search',side_effect=ResearchUnavailableError('offline')):
            response=send(unavailable)
        assert response.status_code==200,response.text
        assert 'Made-up' not in response.json()['content'] and count('research_advice',unavailable)==0
        print('PASS: search failure produces explicit no-evidence fallback instead of model-authored fake recommendations')

        rate=state['research_repository'];assert rate.reserve_request()==0
        assert 0<rate.reserve_request()<=3
        sql('ALTER TABLE experiment_assistant.chat_tool_calls DROP CHECK ck_chat_tool_index, ADD CONSTRAINT ck_chat_tool_index CHECK(call_index BETWEEN 1 AND 4)')
        with database.get_connection(password) as connection,patch.object(database,'_read_sql',side_effect=lambda filename:
            (database.PROJECT_ROOT/'sql'/filename).read_text(encoding='utf-8').replace('experiment_assistant',state['database_name'])):
            database._ensure_research(connection);database._ensure_research(connection)
        assert len(saved(sid,True).json()['items'])==2
        assert '6' in sql("SELECT CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=%s AND CONSTRAINT_NAME='ck_chat_tool_index'",(state['database_name'],),True)[0][0]
        assert client.delete(f'/sessions/{sid}',headers=owner).status_code==204
        assert count('research_references',sid)==count('research_advice',sid)==0
        assert sql('SELECT COUNT(*) FROM experiment_assistant.experiment_runs WHERE id=%s',(rid,),True)[0][0]==1
        print('PASS: global MySQL rate reservation, repeat migration, session cascade deletion retain actual training run')

        for path in ('/sessions/{session_id}/references','/sessions/{session_id}/research-advice'):
            assert app.openapi()['paths'][path]['get']['security']
        logs=''.join(path.read_text(encoding='utf-8') for path in state['log_dir'].rglob('*.txt'))
        assert 'sk-TEST_ONLY_RESEARCH_KEY' not in logs and QUOTE not in logs
        assert all(gateway.closed for gateway in state['builder'].created)
        print('PASS: authenticated OpenAPI, source-body/key-free operational logs, all fake gateways closed')
    print('PASS: 11 research HTTP/MySQL groups; temporary database removed; public pages and all models mocked.')


if __name__=='__main__':verify(get_mysql_password())
