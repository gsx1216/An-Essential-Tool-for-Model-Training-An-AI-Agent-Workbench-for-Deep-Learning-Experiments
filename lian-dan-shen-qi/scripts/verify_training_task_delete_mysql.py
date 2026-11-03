"""训练配置删除联调：隔离 MySQL/HTTP，不连接 SSH、不运行训练或付费模型。"""
import asyncio
import json
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app import database
from app.config import get_mysql_password
from app.models.context_snapshot import ContextAttempt
from app.models.runner import RunnerSettings
from app.models.training_task import TrainingNotFoundError
from scripts._chat_api_test_app import temporary_chat_api


def main():
    password=get_mysql_password()
    settings=RunnerSettings(json.dumps({'delete_test':{'path':'train.py','sha256':'0'*64,
        'parameters':{},'max_runtime_seconds':30,'max_log_bytes':10000}}))
    with temporary_chat_api(password,runner_settings=settings,training=True) as (app,state),TestClient(app) as client:
        def sql(statement,args=(),fetch=True):
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement.replace('experiment_assistant',state['database_name']),args)
                    result=cursor.fetchall() if fetch else None
                connection.commit()
                return result

        # 模拟更新前的真实旧表，再调用同一增量迁移两次，检查新旧环境都可升级。
        sql('ALTER TABLE experiment_assistant.training_tasks DROP INDEX uq_training_task_name, '
            'DROP COLUMN active_name, DROP COLUMN deleted_at, ADD UNIQUE KEY uq_training_task_name(user_id,name)',fetch=False)
        read_sql=database._read_sql
        with patch.object(database,'_read_sql',side_effect=lambda filename:read_sql(filename).replace('experiment_assistant',state['database_name'])):
            for _ in range(2):
                with database.get_connection(password) as connection:
                    database._ensure_training_tools(connection)
        print('PASS: 旧表增量迁移和重复迁移通过')

        def user(label):
            body={'username':label+uuid4().hex[:8],'password':'TestPass123!'}
            response=client.post('/auth/register',json=body)
            assert response.status_code==201,response.text
            uid=response.json()['user_id']
            response=client.post('/auth/login',json=body)
            assert response.status_code==200,response.text
            return uid,{'Authorization':'Bearer '+response.json()['access_token']}

        uid,owner=user('delete_owner_')
        _,other=user('delete_other_')
        response=client.post('/experiments',headers=owner,json={'title':'删除测试实验','kind':'training','description':''})
        assert response.status_code==201,response.text
        experiment=response.json()['experiment_id']
        response=client.post('/servers',headers=owner,json={'name':'仅保存配置','host':'127.0.0.1','port':2222,
            'username':'test','auth_method':'password','password':'TEST_ONLY','host_fingerprint':'SHA256:'+'A'*43,'work_dir':'/tmp/delete_test'})
        assert response.status_code==201,response.text
        server=response.json()['server_id']
        body={'name':'可删除任务','experiment_id':experiment,'server_id':server,'script_id':'delete_test','default_parameters':{}}
        response=client.post('/training-tasks',headers=owner,json=body)
        assert response.status_code==201,response.text
        task_id=response.json()['task_id']
        assert client.post('/training-tasks',headers=owner,json=body).status_code==409
        sid=state['sessions'].create_session(uid,'保留历史计划')
        turn=state['messages'].start_turn(uid,sid,'启动 可删除任务',uuid4().hex)
        attempt_id,_=state['messages'].claim_turn(uid,sid,turn)
        attempt=ContextAttempt(turn,attempt_id)
        plan=asyncio.run(state['training'].prepare(uid,sid,attempt,task_id,{}))
        response=client.post(f'/experiments/{experiment}/runs',headers=owner,json={'request_id':uuid4().hex,'parameters':{}})
        assert response.status_code in {200,201},response.text
        run_id=response.json()['run']['run_id']
        repository=state['training_repository']
        repository.claim(uid,sid,attempt,plan['plan_id'])
        repository.bind_run(uid,sid,plan['plan_id'],run_id)

        path='/training-tasks/'+task_id
        assert client.delete(path).status_code==401
        assert client.delete(path,headers=other).status_code==404
        assert client.get(path,headers=owner).status_code==200
        response=client.delete(path,headers=owner)
        assert response.status_code==204 and response.content==b'',response.text
        assert client.get(path,headers=owner).status_code==404
        assert client.delete(path,headers=owner).status_code==404
        assert task_id not in str(client.get('/training-tasks',headers=owner).json())
        assert client.get(f'/runs/{run_id}',headers=owner).status_code==200
        assert repository.get_plan(uid,sid,plan['plan_id'])['run_id']==run_id
        assert sql('SELECT deleted_at IS NOT NULL FROM experiment_assistant.training_tasks WHERE id=%s',(task_id,))==[(1,)]
        for operation in (
            lambda:repository.get_by_script(uid,'delete_test'),
            lambda:repository.claim(uid,sid,attempt,plan['plan_id']),
            lambda:repository.prepare(uid,sid,attempt,{'task_id':task_id}),
        ):
            try:operation()
            except TrainingNotFoundError:pass
            else:raise AssertionError('已删除配置仍可用于新计划或启动')
        print('PASS: 删除鉴权、历史外键和运行保留、列表移除、旧计划禁止重新领取')
        response=client.post('/training-tasks',headers=owner,json=body)
        assert response.status_code==201 and response.json()['task_id']!=task_id,response.text
        assert sql('SELECT COUNT(*) FROM experiment_assistant.run_executions')==[(0,)]
        print('PASS: 删除后可新建同名配置；整个验证没有启动训练；临时数据库自动清理')


if __name__=='__main__':
    main()
