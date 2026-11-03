"""真实 HTTP/MySQL/回环 SSH 验收对话训练工具；微型 CPU 作业、假模型、无付费调用。"""
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app import database
from app.config import get_mysql_password
from app.models.context_snapshot import ContextAttempt
from app.models.context_state import ContextConflictError
from app.models.runner import RunnerSettings
from scripts._chat_api_test_app import temporary_chat_api
from scripts._runner_ssh_test_server import local_runner_ssh


def verify(password,ssh,work):
    registry={}
    for label in ('detection','point_cloud'):
        path=work/(label+'.py')
        path.write_text("import argparse,json\nfrom pathlib import Path\np=argparse.ArgumentParser();p.add_argument('--config');a=p.parse_args()\nconfig=Path(a.config);params=json.loads(config.read_text())\n(config.parent/'executed.txt').write_text("+repr(label)+")\nprint(json.dumps({'type':'metric','dataset':'demo','split':'train','step':1,'loss':1.0/params['epochs']}),flush=True)\n",encoding='utf-8')
        registry[label]={'path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'parameters':{'epochs':{'type':'int','required':True,'min':1,'max':4}},'max_runtime_seconds':10,'max_log_bytes':10000}
    with temporary_chat_api(password,runner_settings=RunnerSettings(json.dumps(registry)),training=True) as (app,state),TestClient(app) as client,ExitStack() as cleanup:
        def user(label):
            body={'username':label+uuid4().hex[:8],'password':'TestPass123!'}
            assert client.post('/auth/register',json=body).status_code==201
            value=client.post('/auth/login',json=body);assert value.status_code==200,value.text
            headers={'Authorization':'Bearer '+value.json()['access_token']}
            assert client.put('/me/model-credentials/deepseek',headers=headers,json={'api_key':'sk-TEST_ONLY_TRAINING_TOOLS_KEY'}).status_code==204
            return headers
        owner=user('training_a_');foreign=user('training_b_')
        def request(method,path,headers=owner,**kwargs):
            result=client.request(method,path,headers=headers,**kwargs)
            assert result.status_code<400,(path,result.status_code,result.text)
            return result
        def sql(statement,args=(),fetch=True):
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement.replace('experiment_assistant',state['database_name']),args)
                    result=cursor.fetchall() if fetch else None
                connection.commit();return result
        def count():return sql('SELECT COUNT(*) FROM experiment_assistant.run_executions')[0][0]
        def cleanup_jobs():
            for (run_id,) in sql('SELECT run_id FROM experiment_assistant.run_executions'):
                value=client.post(f'/runs/{run_id}/refresh',headers=owner)
                if value.status_code<400 and value.json()['execution']['status'] not in {'succeeded','failed','cancelled'}:
                    client.post(f'/runs/{run_id}/stop',headers=owner)
                    end=time.monotonic()+15
                    while time.monotonic()<end:
                        value=client.post(f'/runs/{run_id}/refresh',headers=owner)
                        if value.status_code<400 and value.json()['execution']['status'] in {'succeeded','failed','cancelled'}:break
                        time.sleep(.2)
            time.sleep(.5)  # Supervisor's final log descriptor closes after terminal state write.
        cleanup.callback(cleanup_jobs)
        def session():return request('POST','/sessions',json={'title':'训练工具验收'}).json()['session_id']
        work_dir='/'+work.as_posix() if sys.platform=='win32' else work.as_posix()
        server=request('POST','/servers',json={'name':'local CPU micro jobs','host':'127.0.0.1','port':ssh.port,'username':'trainer',
            'auth_method':'private_key','private_key':ssh.private_key,'host_fingerprint':ssh.fingerprint,'work_dir':work_dir}).json()['server_id']
        tasks={}
        for label,name in (('detection','检测训练'),('point_cloud','点云训练')):
            exp=request('POST','/experiments',json={'title':name,'kind':'training','description':'CPU acceptance only'}).json()['experiment_id']
            body={'name':name,'experiment_id':exp,'server_id':server,'script_id':label,'default_parameters':{'epochs':1}}
            task=request('POST','/training-tasks',json=body).json();tasks[label]=task
            assert client.post('/training-tasks',headers=owner,json=body).status_code==409
            assert client.get('/training-tasks/'+task['task_id'],headers=foreign).status_code==404
            assert client.post('/training-tasks',headers=foreign,json={**body,'name':'foreign'}).status_code==404
        assert client.get('/training-tasks').status_code==401
        assert count()==0
        print('PASS: two independent projects, defaults, duplicate-name conflict, actual token and owner/FK isolation; catalog creation does not train')

        def tool(name,args=None,cid='call_1'):return {'content':'','tool_calls':[{'id':cid,'name':name,'args':args or {}}]}
        def plan_model(responses):
            responses=list(responses);captured=[]
            async def model(messages,options):
                if isinstance(messages[0],tuple):return {'content':'上下文摘要：仅使用当前用户直接启动指令。'}
                captured.append((messages,options));assert responses,'fake model queue exhausted'
                response=responses.pop(0)
                if isinstance(response,Exception):raise response
                return response
            state['builder'].handler=model;return captured
        def send(sid,text,request_id=None):
            return request('POST',f'/sessions/{sid}/messages',json={'content':text,'request_id':request_id or uuid4().hex}).json()
        def latest(sid):return request('GET',f'/sessions/{sid}/training-plan').json()
        def audit(sid):return request('GET',f'/sessions/{sid}/tool-calls').json()['items']
        def terminal(run):
            end=time.monotonic()+15
            while time.monotonic()<end:
                value=request('POST',f'/runs/{run}/refresh').json()['execution']
                if value['status'] in {'succeeded','failed','cancelled'}:
                    assert value['status']=='succeeded',value
                    return
                time.sleep(.1)
            raise AssertionError('CPU micro job did not finish')

        sid=session();before=ssh.commands
        plan_model([tool('list_training_tasks'),{'content':'你有检测训练和点云训练。'}])
        send(sid,'有哪些训练任务');assert ssh.commands==before and count()==0
        print('PASS: conversational task discovery requires no SSH or launch')

        sid=session();task=tasks['point_cloud'];req=uuid4().hex
        captured=plan_model([tool('list_training_tasks',{'query':'点云'},'list'),tool('get_training_task',{'task_id':task['task_id']},'get'),
            tool('prepare_training_run',{'task_id':task['task_id'],'parameters':{'epochs':2}},'prepare'),tool('start_training_run',cid='start'),{'content':'已提交点云训练，请查询运行状态。'}])
        reply=send(sid,'启动点云训练，epochs=2',req)
        launch=latest(sid);run=launch['run_id'];assert run and launch['parameters']=={'epochs':2}
        terminal(run)
        execution=state['executions'].get(1,run)
        job=work/'.experiment-assistant'/execution['job_id']
        assert (job/'executed.txt').read_text()=='point_cloud'
        assert len(audit(sid))==4 and count()==1
        assert send(sid,'启动点云训练，epochs=2',req)==reply and count()==1
        assert len(audit(sid))==4
        print('PASS: chat list/get/prepare/start selects point-cloud entry with user override; real SSH CPU job succeeds; HTTP replay never repeats tools or training')

        # 换一个项目，启动后故意让最后一次模型回答失败；重试原轮次仍只使用原计划/run。
        sid2=session();task=tasks['detection']
        plan_model([tool('prepare_training_run',{'task_id':task['task_id']},'prepare'),tool('start_training_run',cid='start'),{'content':''}])
        response=client.post(f'/sessions/{sid2}/messages',headers=owner,json={'content':'开始训练检测训练','request_id':uuid4().hex})
        assert response.status_code==502,response.text
        launch2=latest(sid2);run2=launch2['run_id'];assert run2!=run and count()==2
        terminal(run2)
        plan_model([tool('prepare_training_run',{'task_id':task['task_id']},'prepare_retry'),tool('start_training_run',cid='start_retry'),{'content':'已恢复读取原运行，没有重复训练。'}])
        request('POST',f'/sessions/{sid2}/retry')
        assert latest(sid2)['run_id']==run2 and count()==2
        job2=work/'.experiment-assistant'/state['executions'].get(1,run2)['job_id']
        assert (job2/'executed.txt').read_text()=='detection'
        print('PASS: another project selected; failure after real start then chat retry preserves the same durable plan/run/job')

        # 即使模型误调用，当前用户原文的疑问/否定/缺目标仍不授权启动。
        for text in ('如何启动检测训练','不要启动检测训练','开始训练'):
            query=session();before=ssh.commands
            plan_model([tool('prepare_training_run',{'task_id':task['task_id']},'prepare'),tool('start_training_run',cid='start'),{'content':'尚未执行，请明确指令。'}])
            send(query,text)
            assert latest(query)['run_id'] is None and count()==2 and ssh.commands==before
            assert audit(query)[-1]['outcome']=='START_INSTRUCTION_REQUIRED'
        print('PASS: model miscalls cannot turn discussion, negation or an unspecified target into GPU/CPU work')

        # 先讨论计划，再用明确指代启动：不要求用户手动输入 UUID。
        sid3=session();plan_model([tool('prepare_training_run',{'task_id':task['task_id'],'parameters':{'epochs':3}},'prepare'),{'content':'已准备检测训练，epochs=3，尚未启动。'}])
        send(sid3,'准备一个检测训练计划，epochs=3')
        assert latest(sid3)['run_id'] is None
        plan_model([tool('start_training_run',cid='start'),{'content':'已按刚才的计划启动。'}]);send(sid3,'执行这个训练')
        run3=latest(sid3)['run_id'];terminal(run3);assert count()==3
        print('PASS: previous plan survives between messages and explicit referential start resolves within this chat only')

        plan_model([tool('prepare_training_run',{'task_id':task['task_id'],'parameters':{'epochs':4}},'prepare_later'),{'content':'已准备另一份计划，未启动。'}])
        send(sid2,'准备检测训练计划，epochs=4');later=latest(sid2)
        plan_model([{'content':'','tool_calls':[
            {'id':'old','name':'start_training_run','args':{'plan_id':launch2['plan_id']}},
            {'id':'new','name':'start_training_run','args':{'plan_id':later['plan_id']}}]},
            {'content':'本轮复用了原运行，第二份计划未执行。'}])
        send(sid2,'启动检测训练')
        assert [row['outcome'] for row in audit(sid2)[-2:]]==['STARTED','TRAINING_CONFLICT']
        assert count()==3 and latest(sid2)['run_id'] is None
        print('PASS: durable per-turn launch claim rejects a second plan, even after replaying an older plan from another turn')

        sid4=session();before=ssh.commands
        plan_model([tool('start_training_run',{'plan_id':launch2['plan_id']},'foreign_plan'),{'content':'此计划不在本会话。'}])
        send(sid4,'启动检测训练')
        assert audit(sid4)[-1]['outcome']=='NOT_FOUND' and ssh.commands==before and count()==3
        assert client.get(f'/sessions/{sid2}/training-plan',headers=foreign).status_code==404
        print('PASS: cross-chat and cross-user plan access cannot dispatch SSH')

        sid5=session();before=ssh.commands
        plan_model([tool('prepare_training_run',{'task_id':task['task_id'],'parameters':{'epochs':9}},'invalid'),{'content':'轮数超限。'}])
        send(sid5,'启动检测训练，epochs=9')
        assert audit(sid5)[-1]['outcome']=='INVALID_ARGUMENTS' and ssh.commands==before
        records=audit(sid3);old=records[-1]
        try:state['training_repository'].claim(1,sid3,ContextAttempt(old['turn_id'],old['attempt_id']),latest(sid3)['plan_id'])
        except ContextConflictError:pass
        else:raise AssertionError('completed generation must not claim another launch')
        print('PASS: parameter bounds and expired generation fencing are checked before execution')

        assert client.delete(f'/sessions/{sid3}',headers=owner).status_code==204
        assert sql('SELECT COUNT(*) FROM experiment_assistant.chat_training_plans WHERE session_id=%s',(sid3,))[0][0]==0
        assert request('GET',f'/runs/{run3}').json()['run_id']==run3

        # 重复建表不清空目录或计划。数据库复合外键再次阻止绕过服务的跨用户关联。
        before_rows=sql('SELECT COUNT(*) FROM experiment_assistant.chat_training_plans')[0][0]
        for filename in ('create_training_tasks.sql','create_chat_training_plans.sql','create_training_launch_claims.sql'):
            sql((Path(__file__).resolve().parents[1]/'sql'/filename).read_text(encoding='utf-8'),fetch=False)
        assert sql('SELECT COUNT(*) FROM experiment_assistant.chat_training_plans')[0][0]==before_rows
        log_text=''.join(p.read_text(encoding='utf-8') for p in state['log_dir'].rglob('*.txt'))
        assert ssh.private_key not in log_text and 'sk-TEST_ONLY_TRAINING_TOOLS_KEY' not in log_text
        paths=app.openapi()['paths'];assert paths['/training-tasks']['post']['security']
        print('PASS: chat deletion cascades plans without deleting runs; repeated DDL preserves rows, mounted security and credential-safe logs')
    print('PASS: 10 training-tool HTTP/MySQL/real-loopback-SSH groups; 3 CPU micro jobs, temporary database removed; no GPU or paid model calls.')


async def main():
    with TemporaryDirectory() as directory:
        async with local_runner_ssh() as ssh:
            await asyncio.to_thread(verify,get_mysql_password(),ssh,Path(directory))


if __name__=='__main__':asyncio.run(main())
