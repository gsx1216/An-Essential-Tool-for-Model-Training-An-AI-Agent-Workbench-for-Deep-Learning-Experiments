"""真实 HTTP/MySQL/回环 SSH/微型进程的指标与报告验收；不调用模型。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from mysql.connector import IntegrityError

from app.config import get_mysql_password
from app.database import get_connection
from app.models.runner import RunnerSettings
from scripts._chat_api_test_app import temporary_chat_api
from scripts._runner_ssh_test_server import local_runner_ssh

SCRIPT = '''import argparse,json,time,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--config');a=p.parse_args();d=json.loads(Path(a.config).read_text())
print('普通日志，不是指标',flush=True)
for i in range(d.get('filler',0)):print('normal log line',flush=True)
mode=d.get('mode','normal')
if mode=='empty':sys.exit(0)
if mode=='datasets':
 for dataset,step,mae in [('cod10k',10,0.3),('camo',1,0.5),('cod10k',11,0.1),('camo',2,0.4)]:
  print(json.dumps({'type':'metric','dataset':dataset,'split':'test','step':step,'mae':mae}),flush=True)
elif mode=='tail':
 sys.stdout.write('{"type":"metric","step":1,');sys.stdout.flush();time.sleep(4)
 print('"loss":0.3}',flush=True)
else:
 print(json.dumps({'type':'metric','step':0,'epoch':0,'loss':0.8,'accuracy':0.5}),flush=True)
 if mode=='invalid':
  print('{"type":"metric","step":1,"loss":null}',flush=True)
  print('{"type":"metric","step":1,"loss":NaN}',flush=True)
  print('{broken}',flush=True)
  print('x'*10000,flush=True)
 print(json.dumps({'type':'metric','split':'validation','step':1,'loss':0.6,'accuracy':0.7}),flush=True)
 print(json.dumps({'type':'metric','step':2,'epoch':1,'loss':0.2,'accuracy':0.8}),flush=True)
time.sleep(d.get('delay',0));sys.exit(d.get('exit_code',0))
'''


def verify(password, ssh, work):
    script=work/'metrics.py';script.write_text(SCRIPT,encoding='utf-8')
    spec={'path':'metrics.py','sha256':hashlib.sha256(script.read_bytes()).hexdigest(),
          'parameters':{'mode':{'type':'str','max_length':16},'filler':{'type':'int','min':0,'max':3000},
                        'delay':{'type':'int','min':0,'max':20},'exit_code':{'type':'int','min':0,'max':3}},
          'max_runtime_seconds':10,'max_log_bytes':200000}
    settings=RunnerSettings(json.dumps({'metrics':spec,'truncated':{**spec,'max_log_bytes':512}}))
    pending=[]
    with temporary_chat_api(password,runner_settings=settings) as (app,state),TestClient(app) as client:
        def user(name):
            assert client.post('/auth/register',json={'username':name,'password':'TestPass123!'}).status_code==201
            result=client.post('/auth/login',json={'username':name,'password':'TestPass123!'})
            assert result.status_code==200,result.text
            return {'Authorization':'Bearer '+result.json()['access_token']}
        first=user('metrics_a_'+uuid4().hex[:8]);second=user('metrics_b_'+uuid4().hex[:8])
        exp=client.post('/experiments',headers=first,json={'title':'metric demo','kind':'training','description':'micro-process only'}).json()['experiment_id']
        profile={'name':'local','host':'127.0.0.1','port':ssh.port,'username':'trainer','auth_method':'private_key','private_key':ssh.private_key,
                 'host_fingerprint':ssh.fingerprint,'work_dir':'/'+work.as_posix() if sys.platform=='win32' else work.as_posix()}
        result=client.post('/servers',headers=first,json=profile);assert result.status_code==201,result.text
        server=result.json()['server_id']

        def new_run(params=None,script_id='metrics'):
            result=client.post(f'/experiments/{exp}/runs',headers=first,json={'request_id':uuid4().hex,'parameters':params or {}})
            assert result.status_code==201,result.text
            run=result.json()['run']['run_id'];pending.append(run)
            result=client.post(f'/runs/{run}/start',headers=first,json={'server_id':server,'script_id':script_id})
            assert result.status_code==202,result.text
            return run
        def terminal(run):
            end=time.monotonic()+15
            while time.monotonic()<end:
                result=client.post(f'/runs/{run}/refresh',headers=first);assert result.status_code==200,result.text
                if result.json()['execution']['status'] in {'succeeded','failed','cancelled'}:
                    return result.json()['execution']
                time.sleep(.08)
            raise AssertionError('micro job did not finish')
        def sync(run,max_pages=8):
            result=client.post(f'/runs/{run}/metrics/sync?max_pages={max_pages}',headers=first)
            assert result.status_code==200,result.text
            return result.json()
        def report(run):
            result=client.post(f'/runs/{run}/report',headers=first);assert result.status_code==200,result.text
            return result.json()
        def count(run):
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT COUNT(*) FROM `{state['database_name']}`.run_metric_events WHERE run_id=%s",(run,))
                    return cursor.fetchone()[0]
        try:
            run=new_run({'filler':2000});terminal(run)
            routes=[('post',f'/runs/{run}/metrics/sync'),('get',f'/runs/{run}/metrics'),('get',f'/runs/{run}/metrics/latest'),
                    ('post',f'/runs/{run}/report'),('get',f'/runs/{run}/report')]
            for method,path in routes:
                assert client.request(method,path).status_code==401
                assert client.request(method,path,headers=second).status_code==404
            assert client.get(f'/runs/{run}/report',headers=first).status_code==404
            before=report(run);assert before['version']==1 and 'NOT_SYNCED' in before['data']['warnings']
            assert before['data']['series']==[] and not before['data']['collection_complete']
            print('PASS: five operations enforce actual login/ownership; absent and unsynced report semantics are explicit')
            part=sync(run,1);assert not part['stream']['caught_up'] and not part['stream']['complete']
            final=sync(run);assert final['stream']['valid']==3 and final['stream']['complete']
            replay=sync(run);assert replay['revision']==final['revision'] and count(run)==3
            assert client.get(f'/runs/{run}/report',headers=first).json()['is_stale']
            print('PASS: bounded multi-page raw-byte ingestion, durable cursor, Unicode and repeated sync without duplicate rows')
            metrics=client.get(f'/runs/{run}/metrics?limit=1',headers=first).json()
            assert len(metrics['items'])==1 and metrics['next_offset'] is not None
            later=client.get(f"/runs/{run}/metrics?after={metrics['next_offset']}",headers=first).json();assert len(later['items'])==2
            filtered=client.get(f'/runs/{run}/metrics?split=validation&metric_name=accuracy',headers=first).json();assert len(filtered['items'])==1
            result=report(run);assert result['version']==2 and result['data']['collection_complete']
            loss=next(item for item in result['data']['series'] if item['split']=='train' and item['metric_name']=='loss')
            assert loss['points']==2 and abs(loss['first_value']-.8)<1e-9 and abs(loss['last_value']-.2)<1e-9 and abs(loss['delta']+.6)<1e-9
            assert report(run)['version']==2
            assert client.get(f'/runs/{run}',headers=first).json()['result_version']==2
            print('PASS: real MySQL JSON/window aggregation, filters, pagination, report idempotency and result linkage')
            datasets=new_run({'mode':'datasets'});terminal(datasets);collected=sync(datasets)
            assert collected['stream']['valid']==4 and collected['stream']['regressions']==0
            filtered=client.get(f'/runs/{datasets}/metrics?dataset=camo',headers=first).json()
            assert len(filtered['items'])==2 and all(x['dataset']=='camo' for x in filtered['items'])
            grouped=report(datasets)['data']['series']
            assert len(grouped)==2 and {x['dataset'] for x in grouped}=={'camo','cod10k'}
            assert all(x['points']==2 for x in grouped)
            print('PASS: real micro-process JSONL preserves dataset metadata through SSH, durable collection, filters and independent report aggregation')
            concurrent=new_run();terminal(concurrent)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:sync(concurrent),range(2)))
            assert count(concurrent)==3 and all(r['stream']['valid']==3 for r in results)
            with ThreadPoolExecutor(max_workers=2) as pool:
                versions=list(pool.map(lambda _:report(concurrent)['version'],range(2)))
            assert versions==[1,1]
            print('PASS: concurrent collectors serialize version commits and persist each physical metric line once')
            invalid=new_run({'mode':'invalid'});terminal(invalid);quality=sync(invalid)
            assert quality['stream']['valid']==3 and quality['stream']['invalid']==3 and quality['stream']['oversized']==1
            quality_report=report(invalid)['data'];assert 'INVALID_LINES' in quality_report['warnings'] and 'OVERSIZED_LINES' in quality_report['warnings']
            empty=new_run({'mode':'empty'});terminal(empty);sync(empty)
            assert report(empty)['data']['series']==[] and 'NO_METRICS' in report(empty)['data']['warnings']
            cut=new_run({'filler':2000},'truncated');terminal(cut);sync(cut)
            cut_report=report(cut)['data'];assert not cut_report['collection_complete'] and 'LOG_TRUNCATED' in cut_report['warnings']
            print('PASS: malformed/non-finite/oversized records, no metrics and log truncation preserve truthful quality flags')
            failed=new_run({'exit_code':3});assert terminal(failed)['exit_code']==3;sync(failed)
            assert report(failed)['data']['status']=='failed' and count(failed)==3
            cancelled=new_run({'delay':20});time.sleep(.4)
            client.post(f'/runs/{cancelled}/stop',headers=first);terminal(cancelled);sync(cancelled)
            assert report(cancelled)['data']['status']=='cancelled'
            tail=new_run({'mode':'tail'});time.sleep(.2)
            early=sync(tail);assert early['stream']['valid']==0 and not early['stream']['complete']
            assert state['metric_repository'].snapshot(1,tail)['state']['pending']
            terminal(tail);sync(tail);assert count(tail)==1
            print('PASS: failed/cancelled runs retain reported facts; incomplete JSON survives durable buffering and later completion')
            rollback=new_run();terminal(rollback)
            original=state['metric_repository'].save_metric_stream_sql
            state['metric_repository'].save_metric_stream_sql='SELECT PRIVATE_MISSING_FIELD'
            response=client.post(f'/runs/{rollback}/metrics/sync',headers=first)
            state['metric_repository'].save_metric_stream_sql=original
            assert response.status_code==503 and response.json()=={'detail':'请稍后重试。'}
            assert count(rollback)==0 and state['metric_repository'].snapshot(1,rollback)['revision']==0
            sync(rollback);assert count(rollback)==3
            original=state['metric_repository'].link_run_report_sql
            state['metric_repository'].link_run_report_sql='SELECT PRIVATE_MISSING_FIELD'
            response=client.post(f'/runs/{rollback}/report',headers=first)
            state['metric_repository'].link_run_report_sql=original
            assert response.status_code==503
            assert client.get(f'/runs/{rollback}/report',headers=first).status_code==404
            assert client.get(f'/runs/{rollback}',headers=first).json()['result_version']==0
            assert report(rollback)['version']==1
            print('PASS: stream-write failure rolls back both metric events and cursor; retry recovers without data loss')
            # 重建仓储/服务，离线读报告和指标不依赖 SSH 或模型。
            from app.repositories.metric_repository import MetricRepository
            from app.services.metric_service import MetricService
            from app.api.dependencies import get_metric_service
            rebuilt=MetricRepository(password)
            for key,value in vars(rebuilt).copy().items():
                if key.endswith('_sql'):setattr(rebuilt,key,value.replace('experiment_assistant',state['database_name']))
            app.dependency_overrides[get_metric_service]=lambda:MetricService(rebuilt,runner_factory=lambda:(_ for _ in ()).throw(AssertionError('must not connect')))
            assert client.get(f'/runs/{run}/metrics/latest',headers=first).status_code==200
            assert client.get(f'/runs/{run}/report',headers=first).json()['version']==2
            assert client.post(f'/runs/{run}/report',headers=first).json()['version']==2
            app.dependency_overrides[get_metric_service]=lambda:state['metrics']
            print('PASS: reconstructed service restores persisted metrics/report; read and generation need no SSH or model')
            job=state['executions'].get(1,run)['job_id'];log=work/'.experiment-assistant'/job/'output.log'
            log.write_bytes(b'changed')
            response=client.post(f'/runs/{run}/metrics/sync',headers=first)
            assert response.status_code==409 and count(run)==3
            assert client.get(f'/runs/{run}/report',headers=first).json()['is_stale']
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    for table in ('run_metric_streams','run_metric_events','run_reports'):
                        try:cursor.execute(f"UPDATE `{state['database_name']}`.`{table}` SET user_id=2 WHERE run_id=%s",(run,))
                        except IntegrityError:connection.rollback()
                        else:connection.rollback();raise AssertionError('cross-owner FK accepted')
                    for file in ('create_run_metric_streams.sql','create_run_metric_events.sql','create_run_reports.sql'):
                        cursor.execute((Path(__file__).resolve().parents[1]/'sql'/file).read_text().replace('experiment_assistant',state['database_name']))
                connection.commit()
            assert count(run)==3
            assert client.get(f'/runs/{run}/metrics?limit=501',headers=first).status_code==422
            logs=''.join(p.read_text(encoding='utf-8') for p in state['log_dir'].rglob('*.txt'))
            assert ssh.private_key not in logs and 'PRIVATE_MISSING_FIELD' not in logs and not state['builder'].created
            assert app.openapi()['paths']['/runs/{run_id}/metrics/sync']['post']['security']
            print('PASS: changed source preserves events and invalidates report freshness; composite owner FKs, repeat DDL and safe logs verified')
        finally:
            for identifier in pending:
                client.post(f'/runs/{identifier}/stop',headers=first);terminal(identifier)
            time.sleep(.4)
    print('PASS: 10 metric/report HTTP+MySQL+real-loopback-SSH+micro-process groups; temporary database cleaned; no paid calls.')


async def main():
    with TemporaryDirectory() as directory:
        async with local_runner_ssh() as ssh:
            await asyncio.to_thread(verify,get_mysql_password(),ssh,Path(directory))


if __name__=='__main__':
    asyncio.run(main())
