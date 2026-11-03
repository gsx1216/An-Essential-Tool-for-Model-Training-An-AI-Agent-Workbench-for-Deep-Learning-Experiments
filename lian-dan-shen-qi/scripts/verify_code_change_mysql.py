"""代码修改 HTTP/MySQL 验收；随机临时库、模拟模型和远端，真实编辑器另做 Linux 验证。"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import difflib
import hashlib
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database
from app.config import get_mysql_password
from app.errors import ServerConnectionError
from app.models.analysis import AnalysisSettings, compact
from app.models.code_change import ChangeConflict
from app.models.runner import RunnerSettings
from app.models.source_code import SourceFile
from app.models.suggestion import render_suggestions
from app.models.training_metrics import initial_stream, parse_page
from app.services.analysis_service import AnalysisService
from app.services.code_change_transport import validate_result
from app.services.user_model_gateway_factory import UserModelGatewayFactory
from scripts._chat_api_test_app import temporary_chat_api
from scripts.verify_source_code_mysql import SourceFixture
from scripts.verify_analysis_mysql_redis import FakeAnalysisBuilder
from tests.test_suggestion import answer as fixture_advice, paper as fixture_paper


class EditorFixture:
    def __init__(self, source):
        self.source, self.calls, self.saved, self.candidates = source, [], {}, {}
        self.lose_response = False
        self.fail_checks = False

    async def request(self, profile, credentials, payload):
        self.calls.append(deepcopy(payload))
        assert credentials['password']=='TEST_ONLY_SSH_PASSWORD' and payload['work_dir']=='/owned/project'
        key=payload['change_id']
        if key in self.saved: return validate_result({'ok':True,'data':self.saved[key]},payload)
        changes, candidate=[],{}
        for item in payload['plan']['files']:
            path=item['path']; raw=self.source.files[path]
            if hashlib.sha256(raw).hexdigest()!=item['before_sha256']: raise ChangeConflict('fixture changed')
            text=raw.decode()
            for edit in payload['plan']['edits']:
                if edit['path']!=path:continue
                assert text.count(edit['old_text'])==1
                text=text.replace(edit['old_text'],edit['new_text'],1)
            candidate[path]=text.encode()
            changes.append({'path':path,'before_sha256':item['before_sha256'],'after_sha256':hashlib.sha256(text.encode()).hexdigest(),
                'diff':''.join(difflib.unified_diff(raw.decode().splitlines(True),text.splitlines(True),fromfile='a/'+path,tofile='b/'+path))})
        result={'change_id':key,'plan_digest':payload['plan_digest'],'workspace_directory':'/owned/project/code_changes/'+key+'/project',
            'files':changes,'checks':[{'kind':'project','name':'fixture','status':'failed' if self.fail_checks else 'not_configured','detail':'fixture check'}],
            'check_status':'failed' if self.fail_checks else 'incomplete','snapshot_file_count':len(candidate),'skipped_entry_count':1,
            'python_executable':'/fixture/python3','notice':'fixture: independent copy only; no training'}
        self.saved[key],self.candidates[key]=result,candidate
        if self.lose_response:
            self.lose_response=False
            raise ServerConnectionError('TEST_ONLY_LOST_RESPONSE')
        return validate_result({'ok':True,'data':result},payload)


async def verify(password):
    groups=[]; fixture=SourceFixture(); editor=EditorFixture(fixture)
    spec={'path':'train.py','sha256':hashlib.sha256(fixture.files['train.py']).hexdigest(),
        'parameters':{},'max_runtime_seconds':10,'max_log_bytes':1000}
    settings=RunnerSettings(compact({'source_fixture':spec}))
    with temporary_chat_api(password,runner_settings=settings,code_changes=True,suggestions=True,analysis=True) as (app,state),TestClient(app) as client:
        name=state['database_name']; state['sources'].transport=fixture
        state['changes'].transport_factory=lambda:editor
        def sql(text,values=(),fetch=True):
            with database.get_connection(password) as connection:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(text.replace('experiment_assistant',name),values)
                    rows=cursor.fetchall() if fetch else None
                connection.commit();return rows
        def passed(text):groups.append(text);print('PASS:',text,flush=True)
        def user(prefix):
            payload={'username':prefix+uuid4().hex[:8],'password':'TestPass123!'}
            assert client.post('/auth/register',json=payload).status_code==201
            response=client.post('/auth/login',json=payload);assert response.status_code==200
            return response.json()['user']['user_id'],{'Authorization':'Bearer '+response.json()['access_token']}
        uid,owner=user('edit_owner_');_,other=user('edit_other_')
        assert client.put('/me/model-credentials/deepseek',headers=owner,json={'api_key':'sk-TEST_ONLY_EDITOR_KEY'}).status_code==204
        assert client.put('/me/analysis-settings',headers=owner,json={'enabled':True}).status_code==200
        assert client.put('/me/suggestion-settings',headers=owner,json={'enabled':True}).status_code==200
        exp=client.post('/experiments',headers=owner,json={'title':'code edit fixture','kind':'training'}).json()['experiment_id']
        response=client.post('/servers',headers=owner,json={'name':'fixture','host':'127.0.0.1','port':22,'username':'trainer',
            'auth_method':'password','password':'TEST_ONLY_SSH_PASSWORD','host_fingerprint':'SHA256:'+base64.b64encode(bytes(32)).decode().rstrip('='),
            'work_dir':'/owned/project'})
        assert response.status_code==201,response.text
        server=response.json()['server_id']; profile=await state['server_service'].get(uid,server)
        response=client.post(f'/experiments/{exp}/runs',headers=owner,json={'request_id':uuid4().hex,'parameters':{}})
        run=response.json()['run']['run_id']
        launch={'version':1,'run_id':run,'user_id':uid,'server_id':server,'server_binding':profile.binding(),
            'script_id':'source_fixture','work_dir':profile.work_dir,'spec':spec,'parameters':{},'output_directory':'training_outputs'}
        state['executions'].claim(uid,run,profile,'source_fixture',launch)
        sql("UPDATE experiment_assistant.experiment_runs SET status='succeeded',exit_code=0,started_at=created_at,finished_at=UTC_TIMESTAMP(6) WHERE id=%s",(run,),False)
        raw=b'{"type":"metric","step":1,"epoch":1,"split":"validation","dataset":"cod10k","mae":0.03}\n'
        stream,events=parse_page(initial_stream(),raw,{'log_size':len(raw),'truncated':False,'stale':False,'status':'succeeded'})
        assert state['metric_repository'].commit_page(uid,run,0,stream,events)
        assert client.post(f'/runs/{run}/report',headers=owner).status_code==200
        event=client.get(f'/runs/{run}/events',headers=owner).json()[0]['event_id'];state['events'].receive(event)
        analysis=AnalysisService(state['analysis_repository'],settings=AnalysisSettings(enabled=True),
            gateway_factory=UserModelGatewayFactory(state['chat'].gateway_factory.credential_service,FakeAnalysisBuilder()))
        assert await analysis.tick()
        lease=state['suggestion_repository'].claim(90,3)
        source=state['suggestion_repository'].source(uid,lease['task_id'],lease['lease_token'])
        code=await state['sources'].query(uid,run,'read',line_count=30)
        code={**{k:code[k] for k in SourceFile.model_fields},'evidence_id':'code_1','retrieved_at':code['retrieved_at'],
            'version_scope':'current_file','training_entry_match':True}
        paper={**fixture_paper(),'evidence_id':'paper_2'}
        content=render_suggestions(compact(fixture_advice()),source,[code],[paper])
        for index,tool,data in [(1,'read_project_file',code),(2,'read_reference',paper)]:
            state['suggestion_repository'].record(uid,run,lease['task_id'],lease['lease_token'],index,tool,{}, {'ok':True,'code':'OK','data':data})
        state['suggestion_repository'].complete(uid,run,lease['task_id'],lease['lease_token'],content,'fixture')
        task=lease['task_id']; prefix=f'/runs/{run}/code-changes'
        async def planner(messages,options):
            assert options['json_mode'] is True and 'tools' not in options
            return {'content':compact({'edits':[{'path':'train.py','old_text':'value = 1','new_text':'value = 2','reason':'fixture selected suggestion'}],
                'explanation':'fixture: apply selected recommendation in independent copy'})}
        state['change_builder'].handler=planner
        def prepare(request_id=None):
            response=client.post(prefix,headers=owner,json={'suggestion_task_id':task,'suggestion_index':1,'request_id':request_id or uuid4().hex})
            assert response.status_code==200,response.text
            assert response.json()['status']=='ready',response.text
            return response.json()

        assert client.get(prefix).status_code==401
        assert client.get(prefix,headers=other).status_code==404
        assert client.post(prefix,headers=other,json={'suggestion_task_id':task,'suggestion_index':1,'request_id':uuid4().hex}).status_code==404
        assert client.post(prefix,headers=owner,json={'suggestion_task_id':task,'suggestion_index':1,'request_id':uuid4().hex,'user_id':uid}).status_code==422
        assert not state['change_builder'].created and not editor.calls
        passed('API ownership and strict request fields stop unauthorized planning before model or remote access')

        req=uuid4().hex; row=prepare(req); change=row['change_id']; cid=change.replace('-','')
        assert not editor.calls and row['plan']['files'][0]['before_sha256']==spec['sha256']
        assert client.get('/code-changes/'+change,headers=other).status_code==404
        assert client.post('/code-changes/'+change+'/apply',headers=other).status_code==404
        passed('prepare saves an owned exact plan from real SQL suggestion and delivered current source; no remote writes')

        reads,calls=len(fixture.calls),len(state['change_builder'].created)
        assert client.delete('/me/model-credentials/deepseek',headers=owner).status_code==204
        replay=client.post(prefix,headers=owner,json={'suggestion_task_id':task,'suggestion_index':1,'request_id':req})
        assert replay.status_code==200 and replay.json()['change_id']==change
        assert len(fixture.calls)==reads and len(state['change_builder'].created)==calls
        assert client.post(prefix,headers=owner,json={'suggestion_task_id':task,'suggestion_index':2,'request_id':req}).status_code==409
        assert client.put('/me/model-credentials/deepseek',headers=owner,json={'api_key':'sk-TEST_ONLY_EDITOR_KEY'}).status_code==204
        passed('same request reuses saved plan without Key/SSH/model; changed selection cannot reuse request ID')

        originals=deepcopy(fixture.files)
        applied=client.post('/code-changes/'+change+'/apply',headers=owner)
        assert applied.status_code==200,applied.text
        saved=applied.json()
        assert saved['status']=='applied' and saved['result']['check_status']=='incomplete'
        assert saved['result']['files'][0]['diff'] and editor.candidates[cid]['train.py']!=fixture.files['train.py']
        assert originals==fixture.files and state['runs'].get_run(uid,run).status=='succeeded'
        remote_calls=len(editor.calls)
        assert client.post('/code-changes/'+change+'/apply',headers=owner).json()==saved
        assert len(editor.calls)==remote_calls
        passed('apply stores diff/checks in visible independent copy; original and training unchanged; repeat applies no second write')

        conflict=prepare(); fixture.files['train.py']+=b'# changed after planning\n'
        response=client.post('/code-changes/'+conflict['change_id']+'/apply',headers=owner)
        assert response.status_code==409,response.text
        record=client.get('/code-changes/'+conflict['change_id'],headers=owner).json()
        assert record['status']=='conflict' and record['error_code']=='SOURCE_CHANGED'
        rebased=prepare()
        assert rebased['plan']['files'][0]['source_changed_since_suggestion'] is True
        passed('post-plan version change blocks apply; a new plan rereads current source and records rebase from suggestion version')

        uncertain=prepare(); editor.lose_response=True
        response=client.post('/code-changes/'+uncertain['change_id']+'/apply',headers=owner)
        assert response.status_code==502,response.text
        assert client.get('/code-changes/'+uncertain['change_id'],headers=owner).json()['status']=='attention'
        count=len(editor.saved)
        retry=client.post('/code-changes/'+uncertain['change_id']+'/apply',headers=owner)
        assert retry.status_code==200 and retry.json()['status']=='applied',retry.text
        assert len(editor.saved)==count
        passed('remote completion with lost response recovers same ID and stored result without duplicating candidate')

        failed=prepare(); editor.fail_checks=True
        response=client.post('/code-changes/'+failed['change_id']+'/apply',headers=owner)
        assert response.status_code==200 and response.json()['status']=='checks_failed',response.text
        assert response.json()['result']['check_status']=='failed'
        editor.fail_checks=False
        passed('failed checks are retained explicitly and are not reported as a fully tested model')

        race=prepare(); race_id=race['change_id'].replace('-','')
        def claim(_):
            try:return state['change_repository'].claim_apply(uid,race_id)
            except ChangeConflict:return None
        with ThreadPoolExecutor(max_workers=2) as pool:leases=list(pool.map(claim,range(2)))
        active=[value for value in leases if value is not None]
        assert len(active)==1 and active[0][1] is True
        old=active[0][0]['lease_token']
        sql('UPDATE experiment_assistant.code_changes SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE id=%s',(race_id,),False)
        current,_=state['change_repository'].claim_apply(uid,race_id)
        try:state['change_repository'].finish(uid,race_id,old,result=editor.saved[cid])
        except ChangeConflict:pass
        else:raise AssertionError('old lease committed')
        state['change_repository'].finish(uid,race_id,current['lease_token'],error_code='WORKER_CANCELLED')
        passed('concurrent apply has one lease and expired old attempt cannot overwrite a reclaimed modification')

        # 聊天明确应用：真实当前人类消息授权，模型不能自己提供 authorized 或 request_id。
        session=client.post('/sessions',headers=owner,json={'title':'chat applies chosen advice'}).json()['session_id']
        async def chat(messages,options):
            delivered=[m for m in messages if isinstance(m,dict) and m.get('role')=='tool']
            if not delivered:return {'tool_calls':[{'id':'prepare_edit','name':'prepare_code_change','args':{'run_id':run,'suggestion_task_id':task,'suggestion_index':1}}]}
            if len(delivered)==1:
                data=__import__('json').loads(delivered[-1]['content'])
                assert data['ok'],data
                return {'tool_calls':[{'id':'apply_edit','name':'apply_code_change','args':{'change_id':data['data']['change_id']}}]}
            return {'content':'已在独立副本应用修改，请检查完整差异；未启动训练。'}
        state['builder'].handler=chat
        response=client.post(f'/sessions/{session}/messages',headers=owner,json={'content':f'应用运行 {run} 的第1条建议，不要启动训练','request_id':uuid4().hex})
        assert response.status_code==200,response.text
        assert '代码修改记录（独立副本）' in response.json()['content']
        audit=client.get(f'/sessions/{session}/tool-calls',headers=owner).json()['items']
        assert [v['tool_name'] for v in audit]==['prepare_code_change','apply_code_change']
        assert all(v['result_metadata']['change_id'] for v in audit)
        passed('explicit current chat instruction prepares/applies one candidate with SQL authorization, tool audit and program result footer')

        # 一轮想应用两个已有修改，第二个不得执行。
        second=prepare(); count=len(editor.calls)
        async def two(messages,options):
            if any(isinstance(m,dict) and m.get('role')=='tool' for m in messages):return {'content':'处理完成。'}
            return {'tool_calls':[{'id':'one','name':'apply_code_change','args':{'change_id':change}},
                {'id':'two','name':'apply_code_change','args':{'change_id':second['change_id']}}]}
        state['builder'].handler=two
        response=client.post(f'/sessions/{session}/messages',headers=owner,json={'content':f'应用修改 {change} 和 {second["change_id"]}','request_id':uuid4().hex})
        assert response.status_code==200,response.text
        assert client.get('/code-changes/'+second['change_id'],headers=owner).json()['status']=='ready'
        assert len(editor.calls)==count
        passed('one current human turn cannot authorize applying two different change IDs')

        # 查询/讨论不提供写工具；模型伪造调用也不能写入。
        async def discussion(messages,options):
            assert 'apply_code_change' not in {v['function']['name'] for v in options['tools']}
            if any(isinstance(m,dict) and m.get('role')=='tool' for m in messages):return {'content':'这里只讨论，不执行修改。'}
            return {'tool_calls':[{'id':'forged','name':'apply_code_change','args':{'change_id':second['change_id']}}]}
        state['builder'].handler=discussion
        response=client.post(f'/sessions/{session}/messages',headers=owner,json={'content':'如何应用第1条建议？','request_id':uuid4().hex})
        assert response.status_code==200,response.text
        assert len(editor.calls)==count and client.get('/code-changes/'+second['change_id'],headers=owner).json()['status']=='ready'
        passed('discussion/quoted or conditional instructions do not expose mutation tools or grant model self-authorization')

        assert client.delete('/sessions/'+session,headers=owner).status_code==204
        assert client.get('/code-changes/'+change,headers=owner).status_code==200
        assert client.get(prefix,headers=owner).status_code==200
        logs='\n'.join(p.read_text(encoding='utf-8') for p in state['log_dir'].rglob('*.txt'))
        assert all(secret not in logs for secret in ('sk-TEST_ONLY_EDITOR_KEY','TEST_ONLY_SSH_PASSWORD',old))
        passed('deleting chat retains durable modification records and logs exclude credentials/lease tokens')
    print(f'ALL {len(groups)} code-change HTTP/MySQL groups passed; temp DB removed, fake model/SSH only.',flush=True)
    return len(groups)


if __name__=='__main__':asyncio.run(verify(get_mysql_password()))
