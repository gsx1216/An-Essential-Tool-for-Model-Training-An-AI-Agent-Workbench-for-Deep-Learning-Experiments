"""评测声明真实 HTTP/MySQL 验收；随机临时库，禁止真实模型和训练调用。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from mysql.connector import IntegrityError

from app import database
from app.config import get_mysql_password
from app.models.evaluation import canonical_evaluation, evaluation_digest
from app.models.training_metrics import initial_stream, parse_page
from app.repositories.metric_repository import encode
from scripts._chat_api_test_app import temporary_chat_api


def verify():
    password=get_mysql_password()
    root=Path(__file__).resolve().parents[1]
    example=json.loads((root/'examples/evaluation_examples.json').read_text(encoding='utf-8'))['camouflaged_object_detection']
    with temporary_chat_api(password) as (app,state),TestClient(app) as client:
        def user(label):
            result=client.post('/auth/register',json={'username':label+uuid4().hex[:8],'password':'TestPass123!'})
            assert result.status_code==201,result.text
            account=result.json()
            result=client.post('/auth/login',json={'username':account['username'],'password':'TestPass123!'})
            assert result.status_code==200,result.text
            return account['user_id'],{'Authorization':'Bearer '+result.json()['access_token']}
        first,headers=user('eval_a_');second,other=user('eval_b_')
        response=client.post('/experiments',headers=headers,json=example)
        assert response.status_code==201,response.text
        experiment=response.json()['experiment_id'];original=response.json()['evaluation']
        assert original['task_type']=='camouflaged_object_detection'
        assert client.get(f'/experiments/{experiment}',headers=other).status_code==404
        bad={**example,'evaluation':{**original,'primary_metric':'undefined'}}
        assert client.post('/experiments',headers=headers,json=bad).status_code==422
        print('PASS: validated profile create/detail, typed OpenAPI and foreign-user isolation')

        def new_run(exp=experiment,request=None):
            response=client.post(f'/experiments/{exp}/runs',headers=headers,json={'request_id':request or uuid4().hex,'parameters':{}})
            assert response.status_code in {200,201},response.text
            return response.json()
        request=uuid4().hex;run=new_run(request=request)['run']['run_id']
        assert new_run(request=request)['run']['evaluation']==original
        assert client.get(f'/runs/{run}',headers=headers).json()['evaluation']==original
        assert client.get('/runs/latest-training',headers=headers).json()['run']['evaluation']==original
        assert client.get(f'/runs/{run}',headers=other).status_code==404
        name=state['database_name'];assert re.fullmatch(r'_codex_chat_http_[0-9a-f]{32}',name)

        def sql(statement,params=(),fetch=False):
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement.replace('experiment_assistant',name),params)
                    rows=cursor.fetchall() if fetch else None
                connection.commit()
                return rows

        # Only this isolated fixture simulates future experiment edits.
        changed={**original,'protocol_version':'new-evaluator-v2'}
        text=canonical_evaluation(changed)
        sql('UPDATE experiment_assistant.experiment_evaluation_specs SET spec_json=%s,spec_digest=%s WHERE experiment_id=%s',
            (text,evaluation_digest(text),experiment))
        assert client.get(f'/runs/{run}',headers=headers).json()['evaluation']==original
        replay=new_run(request=request);assert replay['replayed'] and replay['run']['evaluation']==original
        assert new_run()['run']['evaluation']['protocol_version']=='new-evaluator-v2'
        with ThreadPoolExecutor(max_workers=2) as pool:
            same=uuid4().hex;results=list(pool.map(lambda _:new_run(request=same),range(2)))
        assert results[0]['run']['run_id']==results[1]['run']['run_id']
        print('PASS: atomic immutable run snapshot, retry and simultaneous creation use the original request identity')

        for table,key,value in [('experiment_evaluation_specs','experiment_id',experiment),('run_evaluation_specs','run_id',run)]:
            try:sql(f'UPDATE experiment_assistant.{table} SET user_id=%s WHERE {key}=%s',(second,value))
            except IntegrityError:pass
            else:raise AssertionError('owner FK accepted foreign user')
        print('PASS: composite foreign keys reject cross-user profile and run snapshot writes')

        lines=[{'dataset':'cod10k','split':'test','step':10,'mae':.3,'sm':.6},
               {'dataset':'camo','split':'test','step':1,'mae':.5,'sm':.4},
               {'dataset':'cod10k','split':'test','step':11,'mae':.1,'sm':.8},
               {'dataset':'camo','split':'test','step':2,'mae':.4,'sm':.5},
               {'dataset':'undeclared','split':'validation','step':1,'custom_metric':4},
               {'dataset':'cod10k','split':'validation','step':1,'mae':3}]
        raw=b'\n'.join(json.dumps({'type':'metric',**row}).encode() for row in lines)+b'\n'
        parsed,events=parse_page(initial_stream(),raw,{'log_size':len(raw),'status':'succeeded','truncated':False,'stale':False})
        repo=state['metric_repository'];assert repo.commit_page(first,run,0,parsed,events)
        assert not repo.commit_page(first,run,0,parsed,events)
        response=client.get(f'/runs/{run}/metrics?dataset=camo',headers=headers)
        assert response.status_code==200,response.text
        assert len(response.json()['items'])==2 and all(x['dataset']=='camo' for x in response.json()['items'])
        assert client.get(f'/runs/{run}/metrics?dataset=../other',headers=headers).status_code==422
        latest=client.get(f'/runs/{run}/metrics/latest',headers=headers).json()['items']
        cod=next(x for x in latest if (x['dataset'],x['split'],x['metric_name'])==('cod10k','test','mae'))
        cam=next(x for x in latest if (x['dataset'],x['split'],x['metric_name'])==('camo','test','mae'))
        assert cod['points']==cam['points']==2 and cod['last_value']==.1 and cam['last_value']==.4
        assert parsed['regressions']==0
        print('PASS: dataset pagination, independent step sequences, CAS and SQL dataset/split/metric aggregation')

        response=client.post(f'/runs/{run}/report',headers=headers);assert response.status_code==200,response.text
        report=response.json();data=report['data']
        assert data['schema_version']==2 and data['evaluation']==original
        for row in data['series']:
            if row['dataset']=='undeclared':assert row['quality_flags']==['DATASET_UNDECLARED','METRIC_UNDECLARED']
            if row['split']=='validation' and row['metric_name']=='mae':
                assert row['max_value']==3 and row['quality_flags']==['VALUE_OUT_OF_RANGE'] and row['observed_best_value'] is None
            if row['dataset']=='cod10k' and row['split']=='test' and row['metric_name']=='mae':
                assert row['observed_best_value']==.1
        assert client.post(f'/runs/{run}/report',headers=headers).json()['version']==report['version']
        assert client.get(f'/runs/{run}/report',headers=other).status_code==404
        old={**data};old.pop('schema_version');old.pop('evaluation');text,digest=encode(old)
        sql('UPDATE experiment_assistant.run_reports SET report_json=%s,report_digest=%s WHERE run_id=%s',(text,digest,run))
        assert client.get(f'/runs/{run}/report',headers=headers).json()['is_stale']
        rebuilt=client.post(f'/runs/{run}/report',headers=headers).json()
        assert rebuilt['version']==report['version']+1 and not rebuilt['is_stale'] and rebuilt['data']['schema_version']==2
        print('PASS: report uses run snapshot, retains outliers, selects observed extrema only by declared direction; old cached report marked stale')

        # Simulate exactly the previous schema in this temporary DB, retaining a genuine old event and cursor.
        sql('DELETE FROM experiment_assistant.run_metric_events')
        legacy_exp=client.post('/experiments',headers=headers,json={'title':'legacy','kind':'training'}).json()['experiment_id']
        legacy_run=new_run(exp=legacy_exp)['run']['run_id']
        raw=b'{"type":"metric","step":3,"loss":0.2}\n'
        state2,events=parse_page(initial_stream(),raw,{'log_size':len(raw),'status':'running','truncated':False,'stale':False})
        assert repo.commit_page(first,legacy_run,0,state2,events)
        state2['series']=['train/loss'];state2['last_steps']={'train':3}
        text,digest=encode(state2)
        sql('UPDATE experiment_assistant.run_metric_streams SET state_json=%s,state_digest=%s WHERE run_id=%s',(text,digest,legacy_run))
        before=sql('SELECT state_json,state_digest,source_offset,revision FROM experiment_assistant.run_metric_streams WHERE run_id=%s',(legacy_run,),True)
        sql('ALTER TABLE experiment_assistant.run_metric_events DROP INDEX idx_metrics_run_dataset_split')
        sql('ALTER TABLE experiment_assistant.run_metric_events DROP COLUMN dataset')
        def read(file):return (root/'sql'/file).read_text(encoding='utf-8').replace('experiment_assistant',name)
        with patch.object(database,'_read_sql',side_effect=read),database.get_connection(password) as connection:
            database._ensure_evaluation(connection);database._ensure_evaluation(connection)
        after=sql('SELECT state_json,state_digest,source_offset,revision FROM experiment_assistant.run_metric_streams WHERE run_id=%s',(legacy_run,),True)
        assert before==after
        result=client.get(f'/runs/{legacy_run}/metrics',headers=headers);assert result.status_code==200,result.text
        assert result.json()['items'][0]['dataset']=='unspecified'
        snapshot=repo.snapshot(first,legacy_run)
        next_raw=b'{"type":"metric","step":4,"loss":0.1}\n'
        next_state,next_events=parse_page(snapshot['state'],next_raw,{'log_size':len(raw)+len(next_raw),'status':'succeeded','truncated':False,'stale':False})
        assert repo.commit_page(first,legacy_run,snapshot['revision'],next_state,next_events)
        latest=repo.latest(first,legacy_run)['items'];assert len(latest)==1 and latest[0]['points']==2
        result=client.post(f'/runs/{legacy_run}/report',headers=headers)
        assert result.status_code==200,result.text
        assert result.json()['data']['evaluation'] is None
        assert 'DATASET_UNSPECIFIED' in result.json()['data']['series'][0]['quality_flags']
        print('PASS: repeated old-schema migration preserves old cursor bytes and events; collection resumes without duplicate or lost metrics')

        # Snapshot INSERT failure must roll back the parent run, not leave partial state.
        request=uuid4().hex
        saved=state['runs'].save_run_evaluation_sql
        state['runs'].save_run_evaluation_sql='INSERT INTO missing_snapshot_table VALUES (%s,%s,%s,%s)'
        try:
            result=client.post(f'/experiments/{experiment}/runs',headers=headers,json={'request_id':request,'parameters':{}})
            assert result.status_code==503,result.text
            assert sql('SELECT COUNT(*) FROM experiment_assistant.experiment_runs WHERE request_id=%s',(request,),True)[0][0]==0
        finally:state['runs'].save_run_evaluation_sql=saved
        # Profile insertion failure also rolls back the experiment parent.
        title='rollback-'+uuid4().hex
        saved=state['experiments'].save_experiment_evaluation_sql
        state['experiments'].save_experiment_evaluation_sql='INSERT INTO missing_profile_table VALUES (%s,%s,%s,%s)'
        try:
            result=client.post('/experiments',headers=headers,json={**example,'title':title})
            assert result.status_code==503,result.text
            assert sql('SELECT COUNT(*) FROM experiment_assistant.experiments WHERE title=%s',(title,),True)[0][0]==0
        finally:state['experiments'].save_experiment_evaluation_sql=saved
        print('PASS: profile insert failure rolls back experiment creation without orphan metadata')
        assert not state['builder'].created
        assert app.openapi()['components']['schemas']['EvaluationSpec']
        print('PASS: snapshot insert failure rolls back run creation; all checks use fake models and no SSH or real training')
    print('PASS: 8 evaluation HTTP/MySQL groups; temporary database removed.')


if __name__=='__main__':
    verify()
