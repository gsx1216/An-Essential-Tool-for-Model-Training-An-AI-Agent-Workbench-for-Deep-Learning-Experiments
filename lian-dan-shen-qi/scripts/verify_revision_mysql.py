"""修改后训练 HTTP/MySQL 验收：临时库、模拟模型和 SSH，不运行 GPU。"""
import asyncio
import hashlib
import json
from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import database
from app.config import get_mysql_password
from app.errors import ServerConnectionError
from app.models.analysis import AnalysisSettings,compact
from app.models.runner import RunnerSettings
from app.models.revision_run import RevisionSettings
from app.models.source_code import SourceFile
from app.models.suggestion import render_suggestions
from app.models.training_metrics import initial_stream,parse_page
from app.services.analysis_service import AnalysisService
from app.services.user_model_gateway_factory import UserModelGatewayFactory
from scripts._chat_api_test_app import temporary_chat_api
from scripts.verify_source_code_mysql import SourceFixture
from scripts.verify_code_change_mysql import EditorFixture
from scripts.verify_analysis_mysql_redis import FakeAnalysisBuilder
from tests.test_suggestion import answer as fixture_advice,paper as fixture_paper
import base64


class InspectionFixture:
    def __init__(self,editor,source,spec):self.editor,self.source,self.spec=editor,source,spec;self.calls=[]
    async def request(self,profile,credentials,payload):
        self.calls.append(deepcopy(payload))
        assert credentials['password']=='TEST_ONLY_SSH_PASSWORD'
        from app.runner.revision_source import canonical,digest
        from app.services.revision_transport import validate_inspection
        files={name:digest(raw) for name,raw in self.editor.candidates[payload['change_id']].items()}
        return validate_inspection({'ok':True,'data':{'files':files,'source_digest':digest(canonical(files).encode()),'mode':'cwd'}},payload)


class RunnerFixture:
    def __init__(self):self.calls=[];self.fail_next=False
    async def request(self,profile,credentials,payload):
        self.calls.append(deepcopy(payload))
        if self.fail_next:self.fail_next=False;raise ServerConnectionError('TEST_ONLY_LOST_START_RESPONSE')
        return {'status':'queued','started':False,'exit_code':None,'error_code':None,'stale':False,'log_bytes':0}


async def verify(password):
    groups=[]; fixture=SourceFixture(); editor=EditorFixture(fixture)
    spec={'path':'train.py','sha256':hashlib.sha256(fixture.files['train.py']).hexdigest(),
        'parameters':{},'max_runtime_seconds':10,'max_log_bytes':1000}
    settings=RunnerSettings(compact({'source_fixture':spec}))
    with temporary_chat_api(password,runner_settings=settings,code_changes=True,suggestions=True,analysis=True,revisions=True) as (app,state),TestClient(app) as client:
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
        from tests.test_evaluation import example
        evaluation=example()
        exp=client.post('/experiments',headers=owner,json={'title':'revision fixture','kind':'training','evaluation':evaluation}).json()['experiment_id']
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

        row=prepare();change=row['change_id']
        assert client.post('/code-changes/'+change+'/apply',headers=owner).status_code==200
        inspector=InspectionFixture(editor,fixture,spec);runner_remote=RunnerFixture()
        service=state['revision_service'];service.settings=RevisionSettings(compact({'source_fixture':'cwd'}))
        service.transport_factory=lambda:inspector
        state['runner'].transport=runner_remote
        endpoint='/code-changes/'+change+'/training-runs'
        request=uuid4().hex
        def new_plan(req=None):
            response=client.post(endpoint,headers=owner,json={'request_id':req or uuid4().hex,'parameters':{}})
            assert response.status_code==200,response.text
            return response.json()
        assert client.post(endpoint,json={'request_id':request}).status_code==401
        assert client.post(endpoint,headers=other,json={'request_id':request}).status_code==404
        assert client.post(endpoint,headers=owner,json={'request_id':request,'command':'anything'}).status_code==422
        assert not inspector.calls
        passed('login, ownership and strict body reject access before source inspection')

        plan=new_plan(request);rid=plan['run_id']
        assert plan['status']=='created' and plan['original_run_id']==run.replace('-','') and plan['change_id']==change.replace('-','')
        assert plan['check_status']=='incomplete' and not runner_remote.calls
        original=state['runs'].get_run(uid,run)
        assert state['runs'].get_run(uid,rid).evaluation==original.evaluation
        assert state['executions'].get(uid,rid,required=False) is None
        assert client.get(endpoint,headers=owner).json()[0]['run_id']==rid
        assert client.get('/runs/'+rid+'/revision',headers=other).status_code==404
        assert client.post('/runs/'+rid+'/revision/start',headers=other).status_code==404
        passed('prepare atomically links candidate run to original and change, preserves evaluation, no execution or training')

        count=len(inspector.calls)
        assert new_plan(request)['run_id']==rid and len(inspector.calls)==count
        assert client.post(endpoint,headers=owner,json={'request_id':request,'parameters':{'different':1}}).status_code==409
        assert client.post(endpoint,headers=owner,json={'request_id':uuid4().hex,'parameters':{'unknown':1}}).status_code==400
        ordinary_request=original.request_id
        before=len(sql('SELECT id FROM experiment_assistant.experiment_runs'))
        assert client.post(endpoint,headers=owner,json={'request_id':ordinary_request}).status_code==409
        assert len(sql('SELECT id FROM experiment_assistant.experiment_runs'))==before
        passed('same request reuses saved plan without SSH; changed parameters and reused ordinary request conflict without orphan runs')

        # The existing /start route also has to preserve candidate binding.
        response=client.post('/runs/'+rid+'/start',headers=owner,json={'server_id':server,'script_id':'source_fixture'})
        assert response.status_code==202,response.text
        execution=state['executions'].get(uid,rid)
        assert execution['launch']['revision']['change_id']==change.replace('-','')
        assert execution['launch']['revision']['files']['train.py']!=spec['sha256']
        assert sql('SELECT run_id FROM experiment_assistant.run_monitor_tasks WHERE run_id=%s',(rid,))
        response=client.post('/runs/'+rid+'/revision/start',headers=owner)
        assert response.status_code==202,response.text
        assert len({item['job_id'] for item in runner_remote.calls})==1
        passed('ordinary and dedicated start APIs share one frozen candidate launch/job and enqueue existing monitor')

        saved_settings=service.settings;service.settings=RevisionSettings()
        assert client.post(endpoint,headers=owner,json={'request_id':uuid4().hex}).status_code==409
        service.settings=saved_settings
        editor.fail_checks=True;failed_change=prepare()['change_id']
        assert client.post('/code-changes/'+failed_change+'/apply',headers=owner).json()['status']=='checks_failed'
        editor.fail_checks=False
        assert client.post('/code-changes/'+failed_change+'/training-runs',headers=owner,json={'request_id':uuid4().hex}).status_code==409
        passed('unregistered revision mode and failed code checks block candidate preparation')

        # No report is a normal not-ready comparison, not a made-up score.
        comparison=client.get('/runs/'+rid+'/comparison',headers=owner)
        assert comparison.status_code==200,comparison.text
        assert comparison.json()['comparable'] is False and comparison.json()['series']==[]
        assert client.get('/runs/'+rid+'/comparison',headers=other).status_code==404
        sql("UPDATE experiment_assistant.experiment_runs SET status='succeeded',exit_code=0,started_at=created_at,finished_at=UTC_TIMESTAMP(6) WHERE id=%s",(rid,),False)
        raw=b'{"type":"metric","step":1,"epoch":1,"split":"validation","dataset":"cod10k","mae":0.02}\n'
        stream,events=parse_page(initial_stream(),raw,{'log_size':len(raw),'truncated':False,'stale':False,'status':'succeeded'})
        assert state['metric_repository'].commit_page(uid,rid,0,stream,events)
        assert client.post('/runs/'+rid+'/report',headers=owner).status_code==200
        comparison=client.get('/runs/'+rid+'/comparison',headers=owner).json()
        assert comparison['comparable'] is True,comparison
        assert abs(comparison['series'][0]['delta']+0.01)<1e-10
        assert comparison['series'][0]['outcome']=='improved'
        assert comparison['original_report_version']==1 and comparison['candidate_report_version']==1
        passed('saved complete matching reports compare dataset/split/metric/step and respect minimize direction; missing report is explicit')

        calls=len(runner_remote.calls)
        assert client.post('/runs/'+rid+'/revision/start',headers=owner).status_code==202
        assert len(runner_remote.calls)==calls
        passed('terminal candidate start replays local result without another remote start')

        # Launch response uncertainty retains same run and immutable binding.
        uncertain=new_plan();uncertain_id=uncertain['run_id'];runner_remote.fail_next=True
        assert client.post('/runs/'+uncertain_id+'/revision/start',headers=owner).status_code==502
        job=state['executions'].get(uid,uncertain_id)['job_id']
        assert client.post('/runs/'+uncertain_id+'/revision/start',headers=owner).status_code==202
        assert state['executions'].get(uid,uncertain_id)['job_id']==job
        passed('lost start response retries same run/job instead of allocating a second training')

        # Chat current-human instruction -> prepare -> start, no model-provided user/request/command.
        session=client.post('/sessions',headers=owner,json={'title':'revision training'}).json()['session_id']
        async def chat(messages,options):
            names={row['function']['name'] for row in options['tools']}
            assert 'start_revision_run' in names and 'start_training_run' not in names and 'apply_code_change' not in names
            delivered=[m for m in messages if isinstance(m,dict) and m.get('role')=='tool']
            if not delivered:return {'tool_calls':[{'id':'prep_rev','name':'prepare_revision_run','args':{'change_id':change}}]}
            if len(delivered)==1:
                data=json.loads(delivered[0]['content']);assert data['ok'],data
                return {'tool_calls':[{'id':'start_rev','name':'start_revision_run','args':{'run_id':data['run_id']}}]}
            return {'content':'training finished'}  # final result must not repeat this fake completion claim
        state['builder'].handler=chat
        response=client.post('/sessions/'+session+'/messages',headers=owner,json={'content':'启动修改副本 '+change+' 的训练','request_id':uuid4().hex})
        assert response.status_code==200,response.text
        assert '启动请求不代表训练已完成' in response.json()['content'] and 'training finished' not in response.json()['content']
        audit=client.get('/sessions/'+session+'/tool-calls',headers=owner).json()['items']
        assert [row['tool_name'] for row in audit]==['prepare_revision_run','start_revision_run']
        chatrun=audit[-1]['result_metadata']['run_id']
        assert sql('SELECT run_id FROM experiment_assistant.revision_launch_claims WHERE run_id=%s',(chatrun,))
        passed('chat reads persisted human authorization, prepares/starts candidate, audits tools and replaces false model completion claim')

        first,second=new_plan()['run_id'],new_plan()['run_id']
        count=len(runner_remote.calls)
        async def two_runs(messages,options):
            if any(isinstance(m,dict) and m.get('role')=='tool' for m in messages):return {'content':'done'}
            return {'tool_calls':[{'id':'first_run','name':'start_revision_run','args':{'run_id':first}},
                {'id':'second_run','name':'start_revision_run','args':{'run_id':second}}]}
        state['builder'].handler=two_runs
        response=client.post('/sessions/'+session+'/messages',headers=owner,json={
            'content':'启动修改副本 '+first+' 和 '+second+' 的训练','request_id':uuid4().hex})
        assert response.status_code==200,response.text
        assert len(runner_remote.calls)==count+1
        assert state['executions'].get(uid,second,required=False) is None
        passed('one persisted human turn cannot launch two different candidate runs')

        count=len(runner_remote.calls)
        async def discussion(messages,options):
            assert 'start_revision_run' not in {row['function']['name'] for row in options['tools']}
            if any(isinstance(m,dict) and m.get('role')=='tool' for m in messages):return {'content':'Only a discussion.'}
            return {'tool_calls':[{'id':'forged_rev','name':'start_revision_run','args':{'run_id':uncertain_id}}]}
        state['builder'].handler=discussion
        response=client.post('/sessions/'+session+'/messages',headers=owner,json={'content':'如何启动修改副本 '+change+'？','request_id':uuid4().hex})
        assert response.status_code==200,response.text
        assert len(runner_remote.calls)==count
        passed('discussion exposes read-only revision tools and rejects forged model start calls')

        assert client.delete('/sessions/'+session,headers=owner).status_code==204
        assert client.get('/runs/'+chatrun+'/revision',headers=owner).status_code==200
        assert client.get('/runs/'+rid+'/comparison',headers=owner).status_code==200
        logs='\n'.join(p.read_text(encoding='utf-8') for p in state['log_dir'].rglob('*.txt'))
        assert all(secret not in logs for secret in ('sk-TEST_ONLY_EDITOR_KEY','TEST_ONLY_SSH_PASSWORD'))
        passed('chat deletion retains revision provenance and reports; operational logs do not contain credentials')
    print(f'ALL {len(groups)} revision HTTP/MySQL groups passed; temp DB removed, no real model/SSH/GPU.',flush=True)
    return len(groups)


if __name__=='__main__':asyncio.run(verify(get_mysql_password()))
