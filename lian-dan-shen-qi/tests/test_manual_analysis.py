import unittest
from unittest.mock import Mock,AsyncMock,MagicMock,patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.manual_analysis_routes import router,build_manual_service
from app.api.auth_dependencies import require_current_user
from app.api.schemas import UserResponse
from app.models.analysis import AnalysisConflictError
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.suggestion_repository import SuggestionRepository
from app.services.manual_analysis_service import ManualAnalysisService
from app.errors import RunNotFoundError
RUN='a'*32
EVENT='b'*32
STATE={'report_ready':True,'main_network':{'path':'models/net.py'},'processing':False,'can_start':True,'message':'就绪'}

class ManualRoutes(unittest.TestCase):
 def setUp(self):
  self.service=Mock(state=AsyncMock(return_value=STATE),prepare=AsyncMock(return_value={**STATE,'accepted':True,'event_id':EVENT}),execute=AsyncMock())
  app=FastAPI();app.include_router(router)
  app.dependency_overrides[require_current_user]=lambda:UserResponse(user_id=7,username='alice')
  app.dependency_overrides[build_manual_service]=lambda:self.service
  self.client=TestClient(app);self.addCleanup(self.client.close)
 def test_get_is_readonly_and_only_explicit_post_schedules_current_run(self):
  self.assertEqual(self.client.get(f'/runs/{RUN}/analysis-readiness').status_code,200)
  self.service.execute.assert_not_awaited();self.service.prepare.assert_not_awaited()
  self.assertEqual(self.client.post(f'/runs/{RUN}/analysis/start').status_code,202)
  self.service.prepare.assert_awaited_once_with(7,RUN)
  self.service.execute.assert_awaited_once_with(7,RUN,EVENT)
 def test_not_ready_and_duplicate_requests_do_not_execute(self):
  self.service.prepare.side_effect=AnalysisConflictError('先选择主网络文件。')
  self.assertEqual(self.client.post(f'/runs/{RUN}/analysis/start').status_code,409)
  self.service.execute.assert_not_awaited()
  self.service.prepare.side_effect=None;self.service.prepare.return_value={**STATE,'accepted':False}
  self.assertEqual(self.client.post(f'/runs/{RUN}/analysis/start').status_code,202)
  self.service.execute.assert_not_awaited()
 def test_foreign_run_and_invalid_uuid_cannot_trigger(self):
  self.service.prepare.side_effect=RunNotFoundError('PRIVATE')
  response=self.client.post(f'/runs/{RUN}/analysis/start')
  self.assertEqual(response.status_code,404);self.assertNotIn('PRIVATE',response.text)
  self.assertEqual(self.client.post('/runs/bad/analysis/start').status_code,422)
  self.service.execute.assert_not_awaited()

class ManualLifecycle(unittest.IsolatedAsyncioTestCase):
 async def test_explicit_processing_does_not_read_or_write_automatic_preferences(self):
  a,s=Mock(),Mock();a.tick=AsyncMock();s.tick=AsyncMock()
  a.repository=Mock();s.repository=Mock()
  service=ManualAnalysisService(Mock(),Mock(),Mock(),lambda settings:a,lambda settings:s)
  await service.execute(7,RUN,EVENT)
  for stage in (a,s):
   stage.repository.manual_for.assert_called_once_with(7,RUN,EVENT)
   stage.tick.assert_awaited_once_with(user_id=7,run_id=RUN)
   stage.preference.assert_not_called()
 async def test_report_and_network_readiness_never_constructs_gateway(self):
  metrics=Mock();metrics.report.return_value={'version':1,'is_stale':False,'data':{'status':'succeeded','collection_complete':True}}
  events=Mock();events.list.return_value=[]
  source=Mock();source.main_network=AsyncMock(return_value={'path':None})
  service=ManualAnalysisService(metrics,events,source)
  self.assertFalse((await service.state(7,RUN))['can_start'])
  source.main_network.return_value={'path':'models/net.py'}
  self.assertTrue((await service.state(7,RUN))['can_start'])
  events.receive.assert_not_called()

class ManualClaimScope(unittest.TestCase):
 def test_scoped_manual_authorization_retains_owner_lease_and_event_filters(self):
  for cls in (AnalysisRepository,SuggestionRepository):
   repo=cls('TEST_ONLY').manual_for(7,RUN,EVENT)
   self.assertNotIn('_preferences',repo.load_analysis_source_sql)
   self.assertIn('t.user_id=%s',repo.load_analysis_source_sql)
   self.assertIn('lease_token=%s',repo.renew_analysis_task_sql)
   connection=MagicMock();connection.__enter__.return_value=connection
   cursor=connection.cursor.return_value.__enter__.return_value
   cursor.fetchone.side_effect=[{'run_id':RUN},None]
   with patch('app.repositories.analysis_repository.get_connection',return_value=connection):
    self.assertIsNone(repo.claim(120,3,user_id=7,run_id=RUN))
   self.assertEqual(cursor.execute.call_args.args[1],(7,RUN,3,EVENT))
   self.assertIn('t.event_id=%s',cursor.execute.call_args.args[0])
   with self.assertRaises(ValueError):repo.claim(120,3)
   with self.assertRaises(ValueError):repo.claim(120,3,user_id=8,run_id=RUN)
