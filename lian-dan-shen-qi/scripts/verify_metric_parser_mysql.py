"""通用普通日志解析：隔离 HTTP/MySQL，日志为替身，不连接真实服务器或训练。"""
import base64
import hashlib
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.dependencies import get_metric_service
from app.config import get_mysql_password
from app.models.training_metrics import initial_stream
from app.services.metric_service import MetricService
from scripts._chat_api_test_app import temporary_chat_api

RAW=b'ordinary log\nstep=1 loss=0.5\nstep=2 loss=0.25\n'
RULE={'pattern':r'step=(?P<step>\d+)\s+loss=(?P<loss>[-+0-9.eE]+)',
      'dataset':'my_dataset','split':'train','metrics':['loss']}

class Logs:
    async def operate(self,user_id,run_id,action,*,offset,limit):
        assert action=='logs'
        chunk=RAW[offset:offset+limit]
        return {'observation':{'status':'running','log_size':len(RAW),'truncated':False,'stale':False,
            'source_changed':False,'raw_base64':base64.b64encode(chunk).decode(),
            'anchor_sha256':hashlib.sha256(RAW[max(0,offset-128):offset]).hexdigest()}}

def main():
    with temporary_chat_api(get_mysql_password()) as (app,state),TestClient(app) as client:
        def user(label):
            body={'username':label+uuid4().hex[:8],'password':'TestPass123!'}
            response=client.post('/auth/register',json=body);assert response.status_code==201,response.text
            uid=response.json()['user_id']
            response=client.post('/auth/login',json=body);assert response.status_code==200,response.text
            return uid,{'Authorization':'Bearer '+response.json()['access_token']}
        uid,owner=user('metric_owner_');_,other=user('metric_other_')
        response=client.post('/experiments',headers=owner,json={'title':'通用采集测试','kind':'training'})
        assert response.status_code==201,response.text
        experiment=response.json()['experiment_id']
        response=client.post(f'/experiments/{experiment}/runs',headers=owner,json={'request_id':uuid4().hex,'parameters':{}})
        assert response.status_code in {200,201},response.text
        run=response.json()['run']['run_id'];repo=state['metric_repository']
        app.dependency_overrides[get_metric_service]=lambda:MetricService(repo,Logs())
        path=f'/runs/{run}/metrics'
        first=client.post(path+'/sync',headers=owner)
        assert first.status_code==200 and first.json()['new_events']==0,first.text
        assert first.json()['stream']['ignored']==3
        before=repo.snapshot(uid,run)
        assert client.get(path+'/parser',headers=other).status_code==404
        assert client.put(path+'/parser',headers=other,json={'rules':[RULE]}).status_code==404
        assert client.put(path+'/parser',json={'rules':[RULE]}).status_code==401
        assert client.put(path+'/parser',headers=owner,json={'rules':[{**RULE,'pattern':'('}]}).status_code==422
        changed=client.put(path+'/parser',headers=owner,json={'rules':[RULE]})
        assert changed.status_code==200,changed.text
        assert repo.snapshot(uid,run)['state']['offset']==0
        assert repo.commit_page(uid,run,before['revision'],before['state'],[]) is False
        result=client.post(path+'/sync',headers=owner)
        assert result.status_code==200 and result.json()['new_events']==2,result.text
        rows=client.get(path,headers=owner).json()['items']
        assert [row['values']['loss'] for row in rows]==[0.5,0.25]
        assert [row['source_offset'] for row in rows]==[len(b'ordinary log\n'),len(b'ordinary log\nstep=1 loss=0.5\n')]
        assert client.post(path+'/sync',headers=owner).json()['new_events']==0
        assert client.put(path+'/parser',headers=owner,json={'rules':[]}).status_code==409
        assert client.put(path+'/parser',headers=owner,json={'rules':[RULE]}).status_code==200
        assert len(client.get(path,headers=owner).json()['items'])==2
        print('PASS: 通用规则、零指标诊断、真实HTTP鉴权和参数校验、历史补采、字节来源、CAS失效、去重和已有数据保护')
        print('PASS: 临时数据库自动清理，未修改真实运行、未启动训练或调用模型')

if __name__=='__main__':main()
