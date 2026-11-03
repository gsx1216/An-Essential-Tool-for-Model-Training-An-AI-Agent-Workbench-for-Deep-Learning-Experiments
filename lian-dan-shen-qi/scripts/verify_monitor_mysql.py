"""真实 API/MySQL/回环 SSH 的后台 Worker 验收；微型 CPU 作业，无付费模型。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from mysql.connector import IntegrityError

from app import database
from app.config import get_mysql_password
from app.errors import ServerConnectionError
from app.models.monitor import MonitorSettings
from app.models.runner import RunnerSettings
from app.repositories.monitor_repository import MonitorRepository
from app.services.monitor_service import MonitorService
from app.workers.monitor_worker import run_worker
from scripts._chat_api_test_app import temporary_chat_api
from scripts._runner_ssh_test_server import local_runner_ssh

SCRIPT='''import argparse,json,time,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--config');a=p.parse_args()
config=Path(a.config);d=json.loads(config.read_text())
with (config.parent/'launch_count.txt').open('a') as f:f.write('started\\n')
print(json.dumps({'type':'metric','dataset':'demo','step':0,'loss':1.0}),flush=True)
if d.get('filler'):print('x'*3000,flush=True)
time.sleep(d.get('delay',0))
print(json.dumps({'type':'metric','dataset':'demo','step':1,'loss':0.25}),flush=True)
sys.exit(d.get('exit_code',0))
'''


def verify(password,ssh,work,loop):
    path=work/'monitor_micro.py';path.write_text(SCRIPT,encoding='utf-8')
    spec={'path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'parameters':{'delay':{'type':'int','min':0,'max':8},'exit_code':{'type':'int','min':0,'max':3},'filler':{'type':'bool'}},
        'max_runtime_seconds':12,'max_log_bytes':10000}
    settings=RunnerSettings(json.dumps({'micro':spec,'truncated':{**spec,'max_log_bytes':512}}))
    with temporary_chat_api(password,runner_settings=settings,training=True) as (app,state),TestClient(app) as client,ExitStack() as cleanup:
        worker=[]
        state['monitor'].settings=MonitorSettings(poll_seconds=1,idle_seconds=1,lease_seconds=40,step_timeout_seconds=20,
            max_pages=1,max_failures=2,backoff_seconds=1,max_backoff_seconds=4)
        def user(label):
            body={'username':label+uuid4().hex[:8],'password':'TestPass123!'}
            assert client.post('/auth/register',json=body).status_code==201
            response=client.post('/auth/login',json=body);assert response.status_code==200,response.text
            return {'Authorization':'Bearer '+response.json()['access_token']}
        owner=user('monitor_a_');foreign=user('monitor_b_')
        def request(method,path,**kwargs):
            response=client.request(method,path,headers=owner,**kwargs)
            assert response.status_code<400,(path,response.status_code,response.text)
            return response
        def sql(statement,args=(),fetch=True):
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement.replace('experiment_assistant',state['database_name']),args)
                    result=cursor.fetchall() if fetch else None
                connection.commit();return result
        def stop_worker():
            if not worker:return
            future,finished=worker.pop();future.cancel()
            assert finished.wait(15),'worker did not release/cancel current task'
        def start_worker(service=None):
            assert not worker
            started,finished=Event(),Event()
            async def run():
                started.set()
                try:await run_worker(service or state['monitor'])
                finally:finished.set()
            future=asyncio.run_coroutine_threadsafe(run(),loop);worker.append((future,finished))
            assert started.wait(5)
        def cleanup_jobs():
            stop_worker()
            for (run_id,) in sql('SELECT run_id FROM experiment_assistant.run_executions'):
                response=client.post(f'/runs/{run_id}/stop',headers=owner)
                end=time.monotonic()+15
                while time.monotonic()<end:
                    value=client.post(f'/runs/{run_id}/refresh',headers=owner)
                    if value.status_code<400 and value.json()['execution']['status'] in {'succeeded','failed','cancelled','queued'}:break
                    time.sleep(.2)
            time.sleep(.5)
        cleanup.callback(cleanup_jobs)
        exp=request('POST','/experiments',json={'title':'monitor acceptance','kind':'training','description':'CPU only'}).json()['experiment_id']
        work_dir='/'+work.as_posix() if sys.platform=='win32' else work.as_posix()
        server=request('POST','/servers',json={'name':'local CPU','host':'127.0.0.1','port':ssh.port,'username':'trainer',
            'auth_method':'private_key','private_key':ssh.private_key,'host_fingerprint':ssh.fingerprint,'work_dir':work_dir}).json()['server_id']
        def run(parameters=None,start=True,script='micro'):
            run_id=request('POST',f'/experiments/{exp}/runs',json={'request_id':uuid4().hex,'parameters':parameters or {}}).json()['run']['run_id']
            if start:request('POST',f'/runs/{run_id}/start',json={'server_id':server,'script_id':script})
            return run_id
        def monitor(run_id):return request('GET',f'/runs/{run_id}/monitor').json()
        def wait(run_id,wanted='completed'):
            end=time.monotonic()+25
            while time.monotonic()<end:
                value=monitor(run_id)
                if value['status']==wanted:return value
                if worker and worker[0][0].done():worker[0][0].result()
                time.sleep(.15)
            raise AssertionError(('monitor timeout',run_id,value))
        def job(run_id):return work/'.experiment-assistant'/state['executions'].get(1,run_id)['job_id']

        # 明确通过聊天 Tool 启动，之后只轮询数据库读取接口。
        assert request('PUT','/me/model-credentials/deepseek',json={'api_key':'sk-TEST_ONLY_MONITOR_GATEWAY'}).status_code==204
        task=request('POST','/training-tasks',json={'name':'监控测试','experiment_id':exp,'server_id':server,'script_id':'micro','default_parameters':{'delay':2}}).json()['task_id']
        sid=request('POST','/sessions',json={'title':'auto monitor'}).json()['session_id']
        responses=[{'content':'','tool_calls':[{'id':'prep','name':'prepare_training_run','args':{'task_id':task}}]},
            {'content':'','tool_calls':[{'id':'start','name':'start_training_run','args':{}}]},{'content':'已提交训练，请查看后台结果。'}]
        async def model(messages,options):
            if isinstance(messages[0],tuple):return {'content':'本人的训练计划。'}
            return responses.pop(0)
        state['builder'].handler=model
        request('POST',f'/sessions/{sid}/messages',json={'content':'启动监控测试','request_id':uuid4().hex})
        first=request('GET',f'/sessions/{sid}/training-plan').json()['run_id']
        initial=monitor(first)
        assert initial['status']=='scheduled' and initial['attempts']==0
        assert 'lease_token' not in initial and 'user_id' not in initial
        try:sql('UPDATE experiment_assistant.run_monitor_tasks SET user_id=2 WHERE run_id=%s',(first,),False)
        except IntegrityError:pass
        else:raise AssertionError('monitor owner FK must reject a foreign owner')
        assert client.get(f'/runs/{first}/monitor').status_code==401
        assert client.get(f'/runs/{first}/monitor',headers=foreign).status_code==404
        assert client.post(f'/runs/{first}/monitor/retry',headers=foreign).status_code==404
        assert client.get('/runs/not-a-uuid/monitor',headers=owner).status_code==422
        print('PASS: chat Tool atomically enrolls monitor before SSH; actual token, owner and UUID errors are enforced')

        start_worker();done=wait(first)
        report=request('GET',f'/runs/{first}/report').json()
        assert done['report_version']==1 and report['data']['collection_complete'] and report['data']['counts']['valid']==2
        assert report['data']['series'][0]['last_value']==.25
        assert (job(first)/'launch_count.txt').read_text().splitlines()==['started']
        request('POST',f'/runs/{first}/monitor/retry');wait(first)
        assert request('GET',f'/runs/{first}/report').json()['version']==1
        assert (job(first)/'launch_count.txt').read_text().splitlines()==['started']
        print('PASS: independent Worker updates state/metrics/report automatically; no manual refresh/sync/report-generation calls; monitoring replay never retrains or increments unchanged report')

        failed=run({'exit_code':3});wait(failed)
        report=request('GET',f'/runs/{failed}/report').json()
        assert report['data']['status']=='failed' and report['data']['exit_code']==3 and report['data']['collection_complete']
        print('PASS: failed training gets a completed monitor and a truthful failure report')

        stop_worker();legacy=run()
        sql('DELETE FROM experiment_assistant.run_monitor_tasks WHERE run_id=%s',(legacy,),False)
        start_worker();wait(legacy)
        assert (job(legacy)/'launch_count.txt').read_text().splitlines()==['started']
        print('PASS: Worker startup reconciles a missing active monitor from a legacy/deployment gap without starting another job')

        cancelled=run({'delay':8});request('POST',f'/runs/{cancelled}/stop');wait(cancelled)
        assert request('GET',f'/runs/{cancelled}/report').json()['data']['status']=='cancelled'
        print('PASS: explicit user stop is collected as cancelled; Worker itself never submits stop/start')

        stop_worker()
        flaky=run({'delay':1})
        with patch.object(state['runner'].transport,'request',new=__import__('unittest.mock',fromlist=['AsyncMock']).AsyncMock(side_effect=ServerConnectionError('PRIVATE_TEST_ERROR'))):
            assert asyncio.run(state['monitor'].tick())
        assert monitor(flaky)['failure_count']==1 and monitor(flaky)['last_error_code']=='CONNECTION_ERROR'
        assert request('GET',f'/runs/{flaky}/execution').json()['status'] in {'queued','running'}
        start_worker();wait(flaky)
        assert monitor(flaky)['failure_count']==0 and (job(flaky)/'launch_count.txt').read_text().splitlines()==['started']
        print('PASS: temporary network failure backs off and recovers without false training failure or duplicate job')

        stop_worker()
        unknown=run(start=False)
        with patch.object(state['runner'].transport,'request',side_effect=ServerConnectionError('lost start response')):
            assert client.post(f'/runs/{unknown}/start',headers=owner,json={'server_id':server,'script_id':'micro'}).status_code==502
        start_worker();value=wait(unknown,'attention')
        assert value['last_error_code']=='SOURCE_UNAVAILABLE' and value['failure_count']==2
        assert request('GET',f'/runs/{unknown}/execution').json()['status']=='queued'
        assert not (job(unknown)/'launch_count.txt').exists()
        print('PASS: unconfirmed start remains queued and needs attention after bounded retries; monitoring cannot launch a missing job')

        stop_worker()
        recover=run()
        with ThreadPoolExecutor(max_workers=8) as pool:claims=list(pool.map(lambda _:state['monitor_repository'].claim(40),range(8)))
        active=[value for value in claims if value]
        assert len(active)==1 and active[0]['run_id']==recover
        assert client.post(f'/runs/{recover}/monitor/retry',headers=owner).status_code==409
        old=active[0]
        sql('UPDATE experiment_assistant.run_monitor_tasks SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE run_id=%s',(recover,),False)
        rebuilt=MonitorRepository(password)
        for key,value in vars(rebuilt).copy().items():
            if key.endswith('_sql'):setattr(rebuilt,key,value.replace('experiment_assistant',state['database_name']))
        replacement=rebuilt.claim(40)
        assert replacement['run_id']==recover and replacement['lease_token']!=old['lease_token']
        assert not rebuilt.renew(1,recover,old['lease_token'],40)
        assert not rebuilt.settle(1,recover,old['lease_token'],status='attention',delay=0,error_code='INTERNAL_ERROR')
        assert rebuilt.settle(1,recover,replacement['lease_token'],status='scheduled',delay=0)
        recovered_service=MonitorService(rebuilt,state['executions'],state['metric_repository'],runner_factory=lambda:state['runner'],settings=state['monitor'].settings)
        start_worker(recovered_service);wait(recover)
        print('PASS: eight concurrent claimers yield one lease; rebuilt Worker recovers expiration, fences old token and rejects retry during live lease')

        # 不自动重置已变化的日志游标或抹掉既有事实。
        stop_worker();before_metrics=request('GET',f'/runs/{first}/metrics/latest').json()
        (job(first)/'output.log').write_bytes(b'shortened\n')
        request('POST',f'/runs/{first}/monitor/retry');start_worker();value=wait(first,'attention')
        assert value['last_error_code']=='SOURCE_CHANGED'
        assert request('GET',f'/runs/{first}/metrics/latest').json()==before_metrics
        assert request('GET',f'/runs/{first}/report').json()['is_stale']
        print('PASS: source replacement needs attention, preserves metrics and marks saved report stale')

        truncated=run({'filler':True},script='truncated');value=wait(truncated,'attention')
        report=request('GET',f'/runs/{truncated}/report').json()
        assert value['last_error_code']=='LOG_TRUNCATED' and not report['data']['collection_complete']
        assert 'LOG_TRUNCATED' in report['data']['warnings']
        print('PASS: truncated terminal log generates explicitly incomplete report instead of retrying forever or claiming completeness')

        stop_worker();not_started=run(start=False);before_commands=ssh.commands
        original=state['executions'].enqueue_run_monitor_sql
        try:
            state['executions'].enqueue_run_monitor_sql='INSERT INTO '+state['database_name']+'.missing_table VALUES (1)'
            response=client.post(f'/runs/{not_started}/start',headers=owner,json={'server_id':server,'script_id':'micro'})
            assert response.status_code==503,response.text
        finally:state['executions'].enqueue_run_monitor_sql=original
        assert state['executions'].get(1,not_started,required=False) is None
        assert request('GET',f'/runs/{not_started}').json()['status']=='created'
        assert ssh.commands==before_commands
        print('PASS: monitor enrollment failure rolls back binding/queue transition before any remote start')

        before=sql('SELECT COUNT(*) FROM experiment_assistant.run_monitor_tasks')[0][0]
        for filename in ('create_run_monitor_tasks.sql','backfill_active_run_monitors.sql'):
            sql((Path(__file__).resolve().parents[1]/'sql'/filename).read_text(encoding='utf-8'),fetch=False)
        assert sql('SELECT COUNT(*) FROM experiment_assistant.run_monitor_tasks')[0][0]==before
        paths=app.openapi()['paths']
        assert paths['/runs/{run_id}/monitor']['get']['security']
        assert paths['/runs/{run_id}/monitor']['get']['responses']['200']['content']['application/json']['schema']['$ref'].endswith('MonitorResponse')
        text=''.join(p.read_text(encoding='utf-8') for p in state['log_dir'].rglob('*.txt'))
        for secret in (ssh.private_key,'sk-TEST_ONLY_MONITOR_GATEWAY','PRIVATE_TEST_ERROR'):assert secret not in text
        print('PASS: repeat migration preserves attention/completed rows; typed OpenAPI and credential-safe logs verified')
    print('PASS: 12 automatic-monitor HTTP/MySQL/real-loopback-SSH/Worker groups; temporary database removed; CPU micro jobs only, no paid model/GPU calls.')


async def main():
    with TemporaryDirectory() as directory:
        async with local_runner_ssh() as ssh:
            await asyncio.to_thread(verify,get_mysql_password(),ssh,Path(directory),asyncio.get_running_loop())


if __name__=='__main__':asyncio.run(main())
