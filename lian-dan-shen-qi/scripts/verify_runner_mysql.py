"""HTTP + 临时 MySQL + 真实回环 SSH + 微型监督进程；不运行 GPU 训练或模型。"""

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
from mysql.connector import Error as MySQLError, IntegrityError

from app.config import get_mysql_password
from app.database import get_connection
from app.errors import ServerConnectionError
from app.models.runner import RunnerSettings
from app.services.runner_service import RunnerService
from app.services.runner_transport import RunnerTransport
from app.repositories.runner_repository import RunnerRepository
from app.api import dependencies
from scripts._chat_api_test_app import temporary_chat_api
from scripts._runner_ssh_test_server import local_runner_ssh

SCRIPT = '''import argparse,json,time,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--config');a=p.parse_args()
d=json.loads(Path(a.config).read_text())
with Path('count.txt').open('a') as f:f.write('run\\n')
print('epoch=1 loss=0.5',flush=True)
print('x'*d.get('output',1),flush=True)
time.sleep(d.get('delay',0));sys.exit(d.get('exit_code',0))
'''


def verify(password, ssh, work):
    script=work/'train.py'; script.write_text(SCRIPT,encoding='utf-8')
    spec={'path':'train.py','sha256':hashlib.sha256(script.read_bytes()).hexdigest(),
          'parameters':{'delay':{'type':'int','min':0,'max':20},'exit_code':{'type':'int','min':0,'max':10},'output':{'type':'int','min':0,'max':100000}},
          'max_runtime_seconds':10,'max_log_bytes':1024}
    settings=RunnerSettings(json.dumps({'train':spec}))
    pending=[]
    with temporary_chat_api(password,runner_settings=settings) as (app,state), TestClient(app) as client:
        def user(name):
            assert client.post('/auth/register',json={'username':name,'password':'TestPass123!'}).status_code==201
            response=client.post('/auth/login',json={'username':name,'password':'TestPass123!'})
            assert response.status_code==200,response.text
            return {'Authorization':'Bearer '+response.json()['access_token']}
        first=user('runner_first_'+uuid4().hex[:8]); second=user('runner_second_'+uuid4().hex[:8])
        exp=client.post('/experiments',headers=first,json={'title':'micro','kind':'training','description':'no GPU'}).json()['experiment_id']
        work_dir='/'+work.as_posix() if sys.platform=='win32' else work.as_posix()
        profile={'name':'local only','host':'127.0.0.1','port':ssh.port,'username':'trainer','auth_method':'private_key',
                 'private_key':ssh.private_key,'host_fingerprint':ssh.fingerprint,'work_dir':work_dir}
        response=client.post('/servers',headers=first,json=profile);assert response.status_code==201,response.text
        server=response.json()['server_id']
        other=client.post('/servers',headers=second,json=profile).json()['server_id']
        def new_run(params=None):
            response=client.post(f'/experiments/{exp}/runs',headers=first,json={'request_id':uuid4().hex,'parameters':params or {}})
            assert response.status_code==201,response.text
            identifier=response.json()['run']['run_id'];pending.append(identifier);return identifier
        def start(run, target=server, script_id='train'):
            return client.post(f'/runs/{run}/start',headers=first,json={'server_id':target,'script_id':script_id})
        def terminal(run):
            end=time.monotonic()+15
            while time.monotonic()<end:
                result=client.post(f'/runs/{run}/refresh',headers=first)
                assert result.status_code==200,result.text
                value=result.json()
                if value['execution']['status'] in {'succeeded','failed','cancelled'}:
                    return value
                time.sleep(.1)
            raise AssertionError('job did not finish')
        try:
            run=new_run()
            for method,path,body in [('get','/runner/scripts',None),('post',f'/runs/{run}/start',{'server_id':server,'script_id':'train'}),
                                     ('get',f'/runs/{run}/execution',None),('post',f'/runs/{run}/refresh',None),
                                     ('post',f'/runs/{run}/stop',None),('get',f'/runs/{run}/logs',None)]:
                assert client.request(method,path,json=body).status_code==401
            assert client.get('/runner/scripts',headers=first).json()['items'][0]['script_id']=='train'
            assert client.get(f'/runs/{run}/execution',headers=first).status_code==404
            print('PASS: six operations require actual login; registry and not-started responses verified')
            assert start(run,other).status_code==404
            assert start(run,script_id='missing').status_code==403
            assert client.post(f'/runs/{run}/start',headers=first,json={'server_id':server,'script_id':'train','command':'echo bad'}).status_code==422
            bad=new_run({'unknown':1}); assert start(bad).status_code==400
            print('PASS: foreign servers, unknown scripts, command injection fields and unregistered parameters rejected')
            before=ssh.commands
            with ThreadPoolExecutor(max_workers=3) as pool:
                results=list(pool.map(lambda _:start(run),range(3)))
            assert all(r.status_code==202 for r in results),[(r.status_code,r.text) for r in results]
            value=terminal(run); assert value['execution']['status']=='succeeded',value
            assert (work/'count.txt').read_text().count('run')==1
            assert ssh.commands>before
            saved=state['executions'].get(1,run) # 临时库首个注册用户为 1
            assert not any(k in json.dumps(saved['launch']).lower() for k in ['private_key','passphrase'])
            assert start(run).status_code==202
            assert (work/'count.txt').read_text().count('run')==1
            print('PASS: concurrent HTTP/SSH starts persist one binding and execute one real micro-script; terminal replay does not restart')
            response=client.get(f'/runs/{run}/logs?offset=0&limit=8',headers=first)
            assert response.status_code==200,response.text
            log=response.json()['observation'];assert log['next_offset']==8 and log['has_more']
            assert client.get(f'/runs/{run}/logs?limit=16385',headers=first).status_code==422
            assert client.get(f'/runs/{run}',headers=first).json()['has_logs']
            assert client.get('/runs/latest-training',headers=first).status_code==200
            for method,suffix in [('get','execution'),('post','refresh'),('post','stop'),('get','logs')]:
                assert client.request(method,f'/runs/{run}/{suffix}',headers=second).status_code==404
            assert client.delete(f'/servers/{server}',headers=first).status_code==409
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    try:
                        cursor.execute(f"UPDATE `{state['database_name']}`.run_executions SET server_id=%s WHERE run_id=%s",(other,run))
                    except IntegrityError:
                        connection.rollback()
                    else:
                        connection.rollback();raise AssertionError('database accepted cross-owner server')
            print('PASS: bounded log cursor, run status projection, owner isolation and bound-server deletion guard')
            mismatch=client.post(f'/runs/{run}/start',headers=first,json={'server_id':other,'script_id':'train'})
            assert mismatch.status_code==409
            failure=new_run({'exit_code':3}); assert start(failure).status_code==202
            assert terminal(failure)['execution']['exit_code']==3
            cancelled=new_run({'delay':20}); assert start(cancelled).status_code==202
            # 重建后端协作者；任务标识与状态来自 MySQL/远端，不依赖内存任务表。
            rebuilt=RunnerRepository(password)
            for name,value in vars(rebuilt).copy().items():
                if name.endswith('_sql'):
                    setattr(rebuilt,name,value.replace('experiment_assistant',state['database_name']))
            state['runner']=RunnerService(rebuilt,state['runs'],state['server_service'],
                RunnerTransport(state['server_service'].connector,'python3'),settings)
            app.dependency_overrides[dependencies.get_runner_service]=lambda:state['runner']
            assert client.post(f'/runs/{cancelled}/stop',headers=first).status_code==202
            assert terminal(cancelled)['execution']['status']=='cancelled'
            print('PASS: immutable target conflict, nonzero failure and asynchronous stop confirmed by actual supervisor')
            uncertain=new_run()
            with patch.object(state['runner'].transport,'request',side_effect=ServerConnectionError('MUST_NOT_LEAK')):
                assert start(uncertain).status_code==502
            stored=state['executions'].get(1,uncertain); job=stored['job_id']
            assert stored['status']=='queued'
            assert start(uncertain).status_code==202
            assert terminal(uncertain)['execution']['status']=='succeeded'
            assert state['executions'].get(1,uncertain)['job_id']==job
            # 旧非终态响应不得覆盖已经成功的数据库状态。
            state['executions'].observe(1,uncertain,{'status':'running','started':True,'exit_code':None,'error_code':None,'stale':False,'log_bytes':0})
            assert state['executions'].get(1,uncertain)['status']=='succeeded'
            print('PASS: network uncertainty preserves queued binding; retry reuses job id; late response cannot regress terminal state')
            stopped=new_run()
            with patch.object(state['runner'].transport,'request',side_effect=ServerConnectionError('MUST_NOT_LEAK')):
                assert start(stopped).status_code==502
            assert client.post(f'/runs/{stopped}/stop',headers=first).status_code==202
            assert terminal(stopped)['execution']['status']=='cancelled'
            assert client.get(f'/runs/{stopped}',headers=first).json()['started_at'] is None
            assert start(stopped).json()['execution']['status']=='cancelled'
            print('PASS: stop before any successful SSH launch creates a durable cancelled job, preventing later replay')
            with patch.object(state['runner'].repository,'get',side_effect=MySQLError('MUST_NOT_LEAK')):
                response=client.get(f'/runs/{run}/execution',headers=first)
                assert response.status_code==503 and response.json()=={'detail':'请稍后重试。'}
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    ddl=(Path(__file__).resolve().parents[1]/'sql/create_run_executions.sql').read_text().replace('experiment_assistant',state['database_name'])
                    cursor.execute(ddl)
                connection.commit()
            text=''.join(p.read_text(encoding='utf-8') for p in state['log_dir'].rglob('*.txt'))
            assert ssh.private_key not in text and 'MUST_NOT_LEAK' not in text and not state['builder'].created
            paths=app.openapi()['paths']; assert paths[f'/runs/{{run_id}}/start']['post']['security']
            print('PASS: generic database response, repeated migration, mounted security and credential-safe persistent logs; no model calls')
        finally:
            for identifier in pending:
                if state['executions'].get(1,identifier,required=False) is not None:
                    client.post(f'/runs/{identifier}/stop',headers=first)
                    terminal(identifier)
            time.sleep(.4)
    print('PASS: 8 HTTP/MySQL/real-SSH/micro-process groups; temporary database cleaned; Linux-specific branches not exercised.')


async def main():
    with TemporaryDirectory() as directory:
        async with local_runner_ssh() as ssh:
            await asyncio.to_thread(verify,get_mysql_password(),ssh,Path(directory))


if __name__=='__main__':
    asyncio.run(main())
