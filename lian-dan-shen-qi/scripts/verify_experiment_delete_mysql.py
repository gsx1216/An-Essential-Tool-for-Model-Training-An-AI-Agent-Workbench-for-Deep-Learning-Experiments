"""真实临时 MySQL/HTTP 验证硬删除；模型、远端连接均为替身，不删除生产数据。"""
import asyncio
import base64
from dataclasses import replace
from contextlib import ExitStack
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from uuid import uuid4

from fastapi.testclient import TestClient
from redis import Redis

from app.config import get_mysql_password,get_event_bus_settings
from app.database import get_connection
from app.api.experiment_delete_routes import router,get_purge_repository
from app.models.analysis import AnalysisSettings,compact
from app.models.runner import RunnerSettings
from app.models.suggestion import SuggestionSettings
from app.repositories.experiment_purge_repository import ExperimentPurgeRepository
from app.services.analysis_service import AnalysisService
from app.services.suggestion_service import SuggestionService
from scripts._chat_api_test_app import temporary_chat_api
from tests.test_analysis import response as analysis_response
from tests.test_suggestion import code,paper,answer

RAW=(json.dumps({'type':'metric','step':1,'epoch':1,'dataset':'fixture','split':'validation','mae':0.03,'loss':1})+'\n').encode()


class Transport:
    async def request(self,profile,credentials,payload):
        data={'status':'succeeded','started':True,'exit_code':0,'error_code':None,'stale':False,'log_bytes':len(RAW)}
        if payload['action']=='logs':
            offset=payload['offset'];raw=RAW[offset:offset+payload['limit']]
            data.update(content=raw.decode(),raw_base64=base64.b64encode(raw).decode(),next_offset=offset+len(raw),
                has_more=offset+len(raw)<len(RAW),truncated=False,log_size=len(RAW),source_changed=False,
                anchor_sha256=hashlib.sha256(RAW[max(0,offset-256):offset]).hexdigest())
        return data


def verify():
    password=get_mysql_password()
    settings=RunnerSettings(json.dumps({'fixture_train':{'path':'train.py','sha256':'a'*64,
        'parameters':{'epochs':{'type':'int','min':1,'max':3}},'max_runtime_seconds':60,'max_log_bytes':100000}}))
    with ExitStack() as cleanup,temporary_chat_api(password,runner_settings=settings,analysis=True,suggestions=True) as (app,state),TestClient(app) as client:
        schema=state['database_name']
        repo=ExperimentPurgeRepository(password)
        for attr,value in vars(repo).copy().items():
            if attr!='password':setattr(repo,attr,value.replace('experiment_assistant',schema))
        app.include_router(router)
        app.dependency_overrides[get_purge_repository]=lambda:repo
        state['runner'].transport=Transport()

        def sql(text,values=(),fetch=False):
            with get_connection(password) as connection:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(text.replace('experiment_assistant',schema),values)
                    result=cursor.fetchall() if fetch else None
                connection.commit();return result

        def account(prefix):
            credentials={'username':prefix+uuid4().hex[:8],'password':'TestPass123!'}
            assert client.post('/auth/register',json=credentials).status_code==201
            value=client.post('/auth/login',json=credentials).json()
            return value['user']['user_id'],{'Authorization':'Bearer '+value['access_token']}

        uid,owner=account('purge_owner_');_,other=account('purge_other_')
        exp=client.post('/experiments',headers=owner,json={'title':'删除验收','kind':'training','evaluation':{
            'task_type':'custom','protocol_id':'purge-fixture','protocol_version':'1',
            'datasets':[{'id':'fixture','display_name':'Fixture','version':'1'}],
            'metrics':[{'name':'mae','display_name':'MAE','unit':'ratio','direction':'minimize'}],
            'primary_metric':'mae'}}).json()['experiment_id']
        def create_run():
            result=client.post(f'/experiments/{exp}/runs',headers=owner,json={'request_id':uuid4().hex,'parameters':{'epochs':1}})
            assert result.status_code==201,result.text
            return result.json()['run']['run_id']
        run=create_run()
        assert client.delete('/runs/'+run).status_code==401
        assert client.delete('/runs/'+run,headers=other).status_code==404
        assert client.delete('/experiments/'+exp,headers=other).status_code==404
        # 活动任务拒绝且数据保留。
        sql("UPDATE experiment_assistant.experiment_runs SET status='queued',updated_at=UTC_TIMESTAMP(6) WHERE id=%s",(run,))
        assert client.delete('/runs/'+run,headers=owner).status_code==409
        assert client.delete('/experiments/'+exp,headers=owner).status_code==409
        sql("UPDATE experiment_assistant.experiment_runs SET status='created',updated_at=UTC_TIMESTAMP(6) WHERE id=%s",(run,))
        print('PASS: login, owner isolation, queued/running deletion rejected',flush=True)

        server=client.post('/servers',headers=owner,json={'name':'测试替身','host':'127.0.0.1','port':22,'username':'fixture',
            'auth_method':'password','password':'TEST_ONLY','work_dir':'/owned/project',
            'host_fingerprint':'SHA256:'+base64.b64encode(bytes(32)).decode().rstrip('=')}).json()['server_id']
        result=client.post('/runs/'+run+'/start',headers=owner,json={'server_id':server,'script_id':'fixture_train'})
        assert result.status_code==202,result.text
        result=client.post('/runs/'+run+'/metrics/sync',headers=owner)
        assert result.status_code==200,result.text
        result=client.post('/runs/'+run+'/report',headers=owner)
        assert result.status_code==200,result.text
        event=state['events'].list(uid,run)[0]['event_id'];state['events'].receive(event)
        state['analysis_repository'].preference(uid,enabled=True)
        state['suggestion_repository'].preference(uid,enabled=True)
        gateway=SimpleNamespace(invoke=AsyncMock(return_value={'content':compact(analysis_response())}),aclose=AsyncMock())
        factory=SimpleNamespace(create_for_user=AsyncMock(return_value=gateway))
        asyncio.run(AnalysisService(state['analysis_repository'],gateway_factory=factory,settings=AnalysisSettings(enabled=True)).tick())
        assert state['analysis_repository'].results(uid,run)
        p=paper()
        gateway=SimpleNamespace(invoke=AsyncMock(side_effect=[
            {'tool_calls':[{'id':'code','name':'read_project_file','args':{'path':'train.py'}}]},
            {'tool_calls':[{'id':'paper','name':'read_reference','args':{'url':p['url']}}]},
            {'content':compact(answer())}]),aclose=AsyncMock())
        factory.create_for_user=AsyncMock(return_value=gateway)
        service=SuggestionService(state['suggestion_repository'],gateway_factory=factory,settings=SuggestionSettings(enabled=True),
            source_factory=lambda:SimpleNamespace(query=AsyncMock(return_value=code())),
            research_factory=lambda:SimpleNamespace(read=AsyncMock(return_value={**p,'content':p['text']})))
        asyncio.run(service.tick())
        saved_suggestions=state['suggestion_repository'].results(uid,run)
        if not saved_suggestions:
            for log in state['log_dir'].rglob('*.txt'):
                print('\n'.join(log.read_text(encoding='utf-8').splitlines()[-25:]))
            raise AssertionError('fixture suggestions not saved')
        suggested=saved_suggestions[0]['task_id']
        session=client.post('/sessions',headers=owner,json={'title':'独立聊天保留'}).json()['session_id']
        turn=state['messages'].start_turn(uid,session,'这是一条独立的聊天',uuid4().hex)
        task=client.post('/training-tasks',headers=owner,json={'name':'删除验收配置','experiment_id':exp,'server_id':server,
            'script_id':'fixture_train','default_parameters':{'epochs':1}}).json()['task_id']
        plan=uuid4().hex;change=uuid4().hex
        sql('INSERT INTO experiment_assistant.chat_training_plans (id,user_id,session_id,prepared_turn_id,task_id,snapshot_json,snapshot_sha256,run_request_id,run_id,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))',
            (plan,uid,session,turn,task,'{}','a'*64,uuid4().hex,run))
        sql('INSERT INTO experiment_assistant.training_launch_claims VALUES(%s,%s,%s,%s,UTC_TIMESTAMP(6))',(session,turn,uid,plan))
        sql('INSERT INTO experiment_assistant.chat_run_bindings VALUES(%s,%s,%s,UTC_TIMESTAMP(6))',(session,uid,run))
        sql('INSERT INTO experiment_assistant.code_changes (id,user_id,run_id,suggestion_task_id,suggestion_index,request_id,status,source_json,source_digest,created_at,updated_at) VALUES(%s,%s,%s,%s,1,%s,%s,%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))',
            (change,uid,run,suggested,uuid4().hex,'attention','{}','a'*64))
        sql('INSERT INTO experiment_assistant.code_change_claims VALUES(%s,%s,%s,%s,UTC_TIMESTAMP(6))',(session,turn,uid,change))
        sql('INSERT INTO experiment_assistant.run_metric_parsers VALUES(%s,%s,%s,UTC_TIMESTAMP(6))',(run,uid,'[]'))
        sql('INSERT INTO experiment_assistant.chat_tool_calls (session_id,user_id,turn_id,attempt_id,call_id,call_index,tool_name,arguments_json,outcome,result_meta_json,duration_ms,created_at) VALUES(%s,%s,%s,%s,%s,1,%s,%s,%s,%s,1,UTC_TIMESTAMP(6))',
            (session,uid,turn,uuid4().hex,'fixture_call','get_run_report',json.dumps({'run_id':run}),'OK',json.dumps({'run_id':run})))
        sql('INSERT INTO experiment_assistant.research_references VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))',
            (uuid4().hex,session,uid,turn,uuid4().hex,run,'https://arxiv.org/html/2401.12345','Fixture','paper_html','Fixture text','a'*64,'{}'))
        sql('INSERT INTO experiment_assistant.research_advice VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))',
            (uuid4().hex,session,uid,turn,uuid4().hex,run,'{}','Fixture advice','a'*64))
        child=create_run()
        sql('INSERT INTO experiment_assistant.revision_runs (run_id,user_id,original_run_id,change_id,request_id,binding_json,binding_digest,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))',
            (child,uid,run,change,uuid4().hex,'{}','a'*64))
        sql('INSERT INTO experiment_assistant.revision_launch_claims VALUES(%s,%s,%s,%s,UTC_TIMESTAMP(6))',(session,turn,uid,child))
        assert client.delete('/runs/'+run,headers=owner).status_code==409
        deleted=client.delete('/runs/'+child,headers=owner)
        if deleted.status_code!=204:
            for log in state['log_dir'].rglob('*.txt'):
                print('\n'.join(log.read_text(encoding='utf-8').splitlines()[-25:]))
        assert deleted.status_code==204,deleted.text
        print('PASS: derived run order, execution, metrics, analysis/suggestion and change graph seeded',flush=True)

        tables=['experiment_runs','run_executions','run_monitor_tasks','run_metric_parsers','run_metric_events','run_metric_streams',
            'run_reports','agent_tasks','event_outbox','analysis_results','suggestion_results','suggestion_evidence','code_changes',
            'code_change_claims','chat_training_plans','training_launch_claims','chat_run_bindings','chat_tool_calls',
            'research_references','research_advice','revision_runs','revision_launch_claims','run_evaluation_specs']
        event_settings=replace(get_event_bus_settings(),prefix='codex_purge_'+uuid4().hex)
        redis=Redis.from_url(event_settings.redis_url,decode_responses=True)
        cleanup.callback(redis.close)
        cleanup.callback(redis.delete,event_settings.stream)
        cleanup.enter_context(patch('app.api.experiment_delete_routes.get_event_bus_settings',return_value=event_settings))
        event_message=redis.xadd(event_settings.stream,{'schema_version':'1','event_id':event})
        other_message=redis.xadd(event_settings.stream,{'schema_version':'1','event_id':uuid4().hex})
        redis.xgroup_create(event_settings.stream,event_settings.analysis_group,id='0')
        redis.xreadgroup(event_settings.analysis_group,'fixture',{event_settings.stream:'>'},count=2)
        sql('UPDATE experiment_assistant.event_outbox SET redis_message_id=%s WHERE id=%s',(event_message,event))
        def counts():return {table:sql(f'SELECT COUNT(*) AS n FROM experiment_assistant.{table}',fetch=True)[0]['n'] for table in tables}
        before=counts()
        sql("CREATE TRIGGER experiment_assistant.purge_failure BEFORE DELETE ON experiment_assistant.run_reports FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='TEST_ONLY_ROLLBACK'")
        assert client.delete('/runs/'+run,headers=owner).status_code==503
        assert counts()==before
        assert redis.xlen(event_settings.stream)==2
        sql('DROP TRIGGER experiment_assistant.purge_failure')
        assert client.delete('/runs/'+run,headers=owner).status_code==204
        assert all(value==0 for value in counts().values()),counts()
        assert client.get('/experiments/'+exp,headers=owner).status_code==200
        assert client.get('/servers/'+server,headers=owner).status_code==200
        assert client.get('/sessions/'+session+'/messages',headers=owner).status_code==200
        assert client.get('/runs/'+run,headers=owner).status_code==404
        assert [message[0] for message in redis.xrange(event_settings.stream)]==[other_message]
        assert redis.xpending(event_settings.stream,event_settings.analysis_group)['pending']==1
        print('PASS: real midway SQL failure rolls back all deletes; single run hard deletion removes full graph',flush=True)

        create_run();create_run()
        pending_plan=uuid4().hex
        sql('INSERT INTO experiment_assistant.chat_training_plans (id,user_id,session_id,prepared_turn_id,task_id,snapshot_json,snapshot_sha256,run_request_id,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))',
            (pending_plan,uid,session,turn,task,'{}','a'*64,uuid4().hex))
        assert client.delete('/experiments/'+exp,headers=owner).status_code==204
        assert client.get('/experiments/'+exp,headers=owner).status_code==404
        assert sql('SELECT COUNT(*) AS n FROM experiment_assistant.experiment_runs',fetch=True)[0]['n']==0
        assert sql('SELECT COUNT(*) AS n FROM experiment_assistant.training_tasks',fetch=True)[0]['n']==0
        assert sql('SELECT COUNT(*) AS n FROM experiment_assistant.chat_training_plans',fetch=True)[0]['n']==0
        assert client.get('/servers/'+server,headers=owner).status_code==200
        print('PASS: whole experiment hard deletion includes all runs and training configurations',flush=True)
    print('ALL PASS: temporary database removed; no model, SSH, GPU or production deletion',flush=True)


if __name__=='__main__':verify()
