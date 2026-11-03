"""真实临时 MySQL 与 HTTP 手动分析验收；模型、SSH 都使用替身。"""
import asyncio,base64,hashlib,json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from fastapi.testclient import TestClient
from app.config import get_mysql_password
from app.models.runner import RunnerSettings
from app.models.analysis import compact
from app.services.direct_code_advice import ReportSnapshotService,DirectCodeAdviceService
from app.services.manual_analysis_service import ManualAnalysisService,build_manual_service
from app.api.manual_analysis_routes import router
from app.repositories.run_network_repository import RunNetworkRepository
from scripts._chat_api_test_app import temporary_chat_api
from scripts.verify_experiment_delete_mysql import Transport
from tests.test_suggestion import code

def verify():
 password=get_mysql_password()
 settings=RunnerSettings(json.dumps({'fixture_train':{'path':'train.py','sha256':'a'*64,'parameters':{'epochs':{'type':'int','min':1,'max':3}},'max_runtime_seconds':60,'max_log_bytes':100000}}))
 with temporary_chat_api(password,runner_settings=settings,analysis=True,suggestions=True) as (app,state),TestClient(app) as client:
  app.include_router(router);state['runner'].transport=Transport()
  def target(repo):
   for key,value in vars(repo).copy().items():
    if key.endswith('_sql'):setattr(repo,key,value.replace('experiment_assistant',state['database_name']))
   return repo
  networks=target(RunNetworkRepository(password));state['sources'].network_repository=networks
  from app.models.source_code import SourceFile
  data={key:value for key,value in code().items() if key in SourceFile.model_fields};data['path']='models/net.py'
  async def read_source(profile,credentials,payload):return data
  state['sources'].transport=SimpleNamespace(request=read_source)
  creds={'username':'manual_'+uuid4().hex[:8],'password':'TestPass123!'}
  assert client.post('/auth/register',json=creds).status_code==201
  login=client.post('/auth/login',json=creds).json();uid=login['user']['user_id'];headers={'Authorization':'Bearer '+login['access_token']}
  exp=client.post('/experiments',headers=headers,json={'title':'手动测试','kind':'training'}).json()['experiment_id']
  run=client.post(f'/experiments/{exp}/runs',headers=headers,json={'request_id':uuid4().hex,'parameters':{'epochs':1}}).json()['run']['run_id']
  server=client.post('/servers',headers=headers,json={'name':'替身','host':'127.0.0.1','port':22,'username':'fixture','auth_method':'password','password':'TEST_ONLY','work_dir':'/owned/project','host_fingerprint':'SHA256:'+base64.b64encode(bytes(32)).decode().rstrip('=')}).json()['server_id']
  assert client.post(f'/runs/{run}/start',headers=headers,json={'server_id':server,'script_id':'fixture_train'}).status_code==202
  assert client.post(f'/runs/{run}/metrics/sync',headers=headers).status_code==200
  assert client.post(f'/runs/{run}/report',headers=headers).status_code==200
  # 自动偏好关闭时，明确手动授权仍能处理，且不修改偏好。
  assert not state['analysis_repository'].preference(uid)['enabled']
  assert not state['suggestion_repository'].preference(uid)['enabled']
  selected=client.put(f'/runs/{run}/code/main-network',headers=headers,json={'path':'models/net.py'})
  if selected.status_code!=200:
   for log in state['log_dir'].rglob('*.txt'):print('\n'.join(log.read_text(encoding='utf-8').splitlines()[-35:]))
  assert selected.status_code==200,selected.text
  factory=SimpleNamespace(create_for_user=AsyncMock())
  sg=SimpleNamespace(invoke=AsyncMock(return_value={'content':'## 主网络建议\n检查 `loss.backward()` 前张量形状，并做对照验证。'}),aclose=AsyncMock())
  sf=SimpleNamespace(create_for_user=AsyncMock(return_value=sg))
  def analysis(settings):return ReportSnapshotService(state['analysis_repository'],settings=settings,gateway_factory=factory)
  def suggestion(settings):return DirectCodeAdviceService(state['suggestion_repository'],settings=settings,gateway_factory=sf,source=state['sources'],runner_factory=lambda:state['runner'])
  service=ManualAnalysisService(state['metric_repository'],state['events'],state['sources'],analysis,suggestion)
  app.dependency_overrides[build_manual_service]=lambda:service
  result=client.get(f'/runs/{run}/analysis-readiness',headers=headers)
  assert result.status_code==200 and result.json()['can_start'],result.text
  factory.create_for_user.assert_not_awaited();sg.invoke.assert_not_awaited()
  result=client.post(f'/runs/{run}/analysis/start',headers=headers)
  assert result.status_code==202,result.text
  tasks=state['events'].list(uid,run,tasks=True)
  assert len(tasks)==2 and all(t['status']=='succeeded' for t in tasks),tasks
  saved=state['suggestion_repository'].results(uid,run)
  assert '主网络建议' in saved[0]['content']['direct_advice']
  assert saved[0]['content']['direct_input']['network_path']=='models/net.py'
  assert saved[0]['content']['direct_input']['network_lines']==2
  assert saved[0]['content']['direct_input']['log_available']
  assert set(sg.invoke.call_args.kwargs)=={'max_tokens','thinking'}
  assert '以下是日志内容：' in sg.invoke.call_args.args[0][1][1]
  assert '下面是网络代码：' in sg.invoke.call_args.args[0][1][1]
  assert not state['analysis_repository'].preference(uid)['enabled']
  assert not state['suggestion_repository'].preference(uid)['enabled']
  assert client.post(f'/runs/{run}/analysis/start',headers=headers).json()['accepted'] is False
  factory.create_for_user.assert_not_awaited()
  assert sg.invoke.await_count==1
  print('PASS: temporary MySQL + HTTP, explicit manual trigger, disabled auto preferences, complete network + raw text logs, one plain model call, persisted Markdown advice, duplicate request does not call model')
if __name__=='__main__':verify()
