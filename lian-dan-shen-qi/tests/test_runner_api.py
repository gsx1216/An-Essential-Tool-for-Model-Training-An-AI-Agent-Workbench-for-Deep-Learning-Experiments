"""Runner 接口的身份、输入、安全响应及错误状态。"""

from datetime import datetime, timezone
import unittest
from unittest.mock import create_autospec, patch

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app.api import dependencies
from app.api.auth_dependencies import require_current_user
from app.api.error_handlers import request_validation_exception_handler
from app.api.runner_routes import router
from app.api.schemas import UserResponse
from app.errors import RunnerConflictError, RunnerNotStartedError, RunnerProtocolError, RunnerScriptNotAllowedError, ServerConnectionError
from app.models.runner import RunnerSettings
from app.services.runner_service import RunnerService


class RunnerApiTests(unittest.TestCase):
    def setUp(self):
        self.service = create_autospec(RunnerService, instance=True)
        self.service.settings = RunnerSettings()
        self.run = 'a'*32; self.server='b'*32
        self.record = {'run_id':self.run,'server_id':self.server,'script_id':'train','status':'queued','exit_code':None,'error_code':None,
                       'created_at':datetime.now(timezone.utc),'checked_at':None,'launch_json':'PRIVATE','job_id':'PRIVATE'}
        self.service.start.return_value={'execution':self.record,'observation':None}
        self.service.get.return_value=self.record
        self.service.operate.return_value={'execution':self.record,'observation':None}
        self.app=FastAPI();self.app.include_router(router)
        self.app.add_exception_handler(RequestValidationError,request_validation_exception_handler)
        self.app.dependency_overrides[dependencies.get_runner_service]=lambda:self.service
        self.app.dependency_overrides[require_current_user]=lambda:UserResponse(user_id=7,username='alice')
        self.client=TestClient(self.app);self.addCleanup(self.client.close)

    def test_start_uses_current_identity_and_response_excludes_internal_snapshot(self):
        result=self.client.post(f'/runs/{self.run}/start?user_id=99',json={'server_id':self.server,'script_id':'train'})
        self.assertEqual(result.status_code,202)
        self.service.start.assert_awaited_once_with(7,self.run,self.server,'train')
        self.assertNotIn('PRIVATE',result.text);self.assertEqual(result.headers['Cache-Control'],'no-store')

    def test_unknown_body_fields_bad_uuid_and_log_budgets_are_rejected(self):
        result=self.client.post(f'/runs/{self.run}/start',json={'server_id':self.server,'script_id':'train','command':'PRIVATE'})
        self.assertEqual(result.status_code,422);self.assertNotIn('PRIVATE',result.text)
        self.assertEqual(self.client.get('/runs/bad/execution').status_code,422)
        self.assertEqual(self.client.get(f'/runs/{self.run}/logs?offset=-1').status_code,422)
        self.service.start.assert_not_awaited();self.service.operate.assert_not_awaited()

    def test_details_refresh_stop_and_log_parameters_forwarded(self):
        self.assertEqual(self.client.get(f'/runs/{self.run}/execution').status_code,200)
        self.assertEqual(self.client.post(f'/runs/{self.run}/refresh').status_code,200)
        self.assertEqual(self.client.post(f'/runs/{self.run}/stop').status_code,202)
        self.assertEqual(self.client.get(f'/runs/{self.run}/logs?offset=4&limit=8').status_code,200)
        self.service.operate.assert_awaited_with(7,self.run,'logs',offset=4,limit=8)

    def test_execution_exposes_output_location_without_internal_snapshot(self):
        self.record['output_directory']='/project/training_outputs/'+self.run
        response=self.client.get(f'/runs/{self.run}/execution')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['output_directory'],self.record['output_directory'])
        self.assertNotIn('PRIVATE',response.text)

    def test_typed_domain_and_database_errors_are_safe(self):
        for error, code in [(RunnerConflictError('PRIVATE'),409),(RunnerNotStartedError('PRIVATE'),404),
                            (RunnerScriptNotAllowedError('PRIVATE'),403),(RunnerProtocolError('PRIVATE'),503),
                            (MySQLError('PRIVATE'),503),(ServerConnectionError('PRIVATE'),502)]:
            self.service.start.side_effect=error
            result=self.client.post(f'/runs/{self.run}/start',json={'server_id':self.server,'script_id':'train'})
            self.assertEqual(result.status_code,code);self.assertNotIn('PRIVATE',result.text)

    def test_formal_openapi_lists_six_authenticated_operations(self):
        from app.main import app
        paths=app.openapi()['paths']
        for path,method in [('/runner/scripts','get'),('/runs/{run_id}/start','post'),('/runs/{run_id}/execution','get'),
                            ('/runs/{run_id}/refresh','post'),('/runs/{run_id}/stop','post'),('/runs/{run_id}/logs','get')]:
            self.assertTrue(paths[path][method]['security'])

    def test_factory_missing_registry_fails_with_generic_response(self):
        dependencies.get_runner_service.cache_clear()
        try:
            with patch.object(dependencies,'get_mysql_password',return_value='PRIVATE'), \
                 patch.object(dependencies,'get_server_connection_service'), \
                 patch.object(dependencies,'get_runner_settings',side_effect=ValueError('PRIVATE')):
                with self.assertRaises(Exception) as raised:
                    dependencies.get_runner_service()
            self.assertEqual(raised.exception.status_code,503)
            self.assertEqual(raised.exception.detail,'请稍后重试。')
        finally:
            dependencies.get_runner_service.cache_clear()
