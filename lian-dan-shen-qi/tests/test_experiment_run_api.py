"""运行 HTTP 契约：身份、幂等状态码、归属错误、静态最新路由和安全响应。"""

from datetime import datetime, timezone
import unittest
from unittest.mock import create_autospec

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app.api.dependencies import get_current_user_service, get_experiment_run_service
from app.api.error_handlers import request_validation_exception_handler
from app.api.experiment_run_routes import router
from app.api.request_logging import RequestLoggingMiddleware
from app.errors import ExperimentNotFoundError, InvalidTokenError, RunNotFoundError, RunRequestConflictError, RunStorageError
from app.models.experiment_run import RunCreation, RunPage, RunRecord
from app.security.tokens import TokenService
from app.services.current_user_service import CurrentUserService
from app.services.experiment_run_service import ExperimentRunService


RUN, EXP, REQ = 'a' * 32, 'b' * 32, 'c' * 32
PARENT = '/experiments/' + EXP + '/runs'


class RunApiTests(unittest.TestCase):
    def setUp(self):
        self.token = TokenService().create_token()
        self.identity = create_autospec(CurrentUserService, instance=True, spec_set=True)
        async def identify(token):
            if token != self.token:
                raise InvalidTokenError('PRIVATE_TOKEN')
            return 7, 'alice'
        self.identity.get_current_user.side_effect = identify
        self.service = create_autospec(ExperimentRunService, instance=True, spec_set=True)
        stamp = datetime(2026, 10, 25, tzinfo=timezone.utc)
        self.record = RunRecord(run_id=RUN, experiment_id=EXP, experiment_title='training', experiment_kind='training',
                                status='created', created_at=stamp, updated_at=stamp, started_at=None, finished_at=None,
                                exit_code=None, error_code=None, has_logs=False, has_result=False, result_version=0,
                                request_id=REQ, experiment_description='description', parameters_json='{"epochs":2}')
        self.service.create_run.return_value = RunCreation(self.record, True)
        self.service.get_run.return_value = self.record
        self.service.list_runs.return_value = RunPage((self.record,), RUN)
        self.service.latest_training_run.return_value = self.record
        self.application = FastAPI()
        self.application.add_middleware(RequestLoggingMiddleware)
        self.application.add_exception_handler(RequestValidationError, request_validation_exception_handler)
        self.application.include_router(router)
        self.application.dependency_overrides[get_current_user_service] = lambda: self.identity
        self.application.dependency_overrides[get_experiment_run_service] = lambda: self.service
        self.client = TestClient(self.application)
        self.headers = {'Authorization': 'Bearer ' + self.token}
        self.addCleanup(self.client.close)
        self.addCleanup(self.application.dependency_overrides.clear)

    def test_first_create_uses_token_owner_and_returns_201_without_internal_fields(self):
        reply = self.client.post(PARENT + '?user_id=999', headers=self.headers, json={'request_id': REQ, 'parameters': {'epochs': 2}})
        self.assertEqual(reply.status_code, 201)
        self.service.create_run.assert_awaited_once_with(7, EXP, REQ, {'epochs': 2})
        self.assertFalse(reply.json()['replayed'])
        self.assertEqual(reply.headers['Location'], '/runs/' + RUN)
        self.assertEqual(reply.headers['Cache-Control'], 'no-store')
        self.assertNotIn('parameters_json', reply.json()['run'])
        self.assertEqual(reply.json()['run']['parameters'], {'epochs': 2})

    def test_replayed_create_is_200_and_marks_replayed(self):
        self.service.create_run.return_value = RunCreation(self.record, False)
        reply = self.client.post(PARENT, headers=self.headers, json={'request_id': REQ})
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(reply.json()['replayed'])
        self.service.create_run.assert_awaited_once_with(7, EXP, REQ, {})

    def test_list_uses_limit_cursor_and_omits_large_parameter_snapshot(self):
        reply = self.client.get(PARENT, headers=self.headers, params={'limit': 2, 'before_id': RUN})
        self.assertEqual(reply.status_code, 200)
        self.service.list_runs.assert_awaited_once_with(7, EXP, limit=2, before_id=RUN)
        self.assertNotIn('parameters', reply.json()['items'][0])
        self.assertEqual(reply.json()['next_cursor'], RUN)

    def test_empty_list_and_empty_latest_are_successful_results(self):
        self.service.list_runs.return_value = RunPage((), None)
        self.service.latest_training_run.return_value = None
        self.assertEqual(self.client.get(PARENT, headers=self.headers).json(), {'items': [], 'next_cursor': None})
        reply = self.client.get('/runs/latest-training', headers=self.headers)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json(), {'run': None})

    def test_latest_static_route_precedes_uuid_and_default_status_is_not_success_only(self):
        reply = self.client.get('/runs/latest-training', headers=self.headers)
        self.assertEqual(reply.status_code, 200)
        self.service.latest_training_run.assert_awaited_once_with(7, experiment_id=None, status=None)

    def test_latest_accepts_explicit_experiment_and_status_filter(self):
        self.client.get('/runs/latest-training', headers=self.headers, params={'experiment_id': EXP, 'status': 'failed'})
        self.service.latest_training_run.assert_awaited_once_with(7, experiment_id=EXP, status='failed')

    def test_detail_normalizes_uuid_and_preserves_owner(self):
        reply = self.client.get('/runs/AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA', headers=self.headers)
        self.assertEqual(reply.status_code, 200)
        self.service.get_run.assert_awaited_once_with(7, RUN)

    def test_missing_and_invalid_bearer_block_every_operation(self):
        for headers in ({}, {'Authorization': 'Bearer invalid'}):
            for method, path, options in (('post', PARENT, {'json': {'request_id': REQ}}), ('get', PARENT, {}),
                                          ('get', '/runs/' + RUN, {}), ('get', '/runs/latest-training', {})):
                reply = getattr(self.client, method)(path, headers=headers, **options)
                self.assertEqual(reply.status_code, 401)
                self.assertEqual(reply.headers['WWW-Authenticate'], 'Bearer')
        self.service.create_run.assert_not_awaited()
        self.service.latest_training_run.assert_not_awaited()

    def test_bad_paths_filters_and_forbidden_fields_are_422(self):
        for body in ({}, {'request_id': 'bad'}, {'request_id': REQ, 'parameters': []},
                     {'request_id': REQ, 'status': 'succeeded'}, {'request_id': REQ, 'user_id': 8},
                     {'request_id': REQ, 'command': 'python train.py'}):
            self.assertEqual(self.client.post(PARENT, headers=self.headers, json=body).status_code, 422)
        for path in ('/runs/not-uuid', PARENT + '?limit=0', PARENT + '?before_id=bad',
                     '/runs/latest-training?status=unknown', '/runs/latest-training?experiment_id=bad'):
            self.assertEqual(self.client.get(path, headers=self.headers).status_code, 422)
        self.service.create_run.assert_not_awaited()

    def test_conflict_and_invalid_parameter_errors_are_safe(self):
        self.service.create_run.side_effect = RunRequestConflictError('PRIVATE_PARAMETERS')
        reply = self.client.post(PARENT, headers=self.headers, json={'request_id': REQ})
        self.assertEqual(reply.status_code, 409)
        self.assertNotIn('PRIVATE_PARAMETERS', reply.text)
        self.service.create_run.side_effect = ValueError('PRIVATE_PARAMETERS')
        self.assertEqual(self.client.post(PARENT, headers=self.headers, json={'request_id': REQ}).status_code, 400)

    def test_ownership_failures_are_404_without_disclosing_existence(self):
        self.service.get_run.side_effect = RunNotFoundError('PRIVATE_OWNER')
        reply = self.client.get('/runs/' + RUN, headers=self.headers)
        self.assertEqual(reply.status_code, 404)
        self.assertEqual(reply.json(), {'detail': '运行不存在或不属于当前用户。'})
        self.service.list_runs.side_effect = ExperimentNotFoundError('PRIVATE_OWNER')
        self.assertEqual(self.client.get(PARENT, headers=self.headers).status_code, 404)

    def test_database_and_storage_errors_are_generic_503_for_every_operation(self):
        for error in (MySQLError(msg='PRIVATE_SQL', errno=1146), RunStorageError('PRIVATE_JSON')):
            for method in ('create_run', 'get_run', 'list_runs', 'latest_training_run'):
                getattr(self.service, method).side_effect = error
            for method, path, options in (('post', PARENT, {'json': {'request_id': REQ}}), ('get', PARENT, {}),
                                          ('get', '/runs/' + RUN, {}), ('get', '/runs/latest-training', {})):
                reply = getattr(self.client, method)(path, headers=self.headers, **options)
                self.assertEqual(reply.status_code, 503)
                self.assertEqual(reply.json(), {'detail': '请稍后重试。'})

    def test_production_openapi_exposes_bearer_and_creation_replay_status(self):
        from app.main import app
        paths = app.openapi()['paths']
        for path, method in (('/experiments/{experiment_id}/runs', 'post'), ('/experiments/{experiment_id}/runs', 'get'),
                              ('/runs/latest-training', 'get'), ('/runs/{run_id}', 'get')):
            self.assertEqual(paths[path][method]['security'], [{'BearerAuth': []}])
        self.assertIn('200', paths['/experiments/{experiment_id}/runs']['post']['responses'])
        self.assertIn('201', paths['/experiments/{experiment_id}/runs']['post']['responses'])


if __name__ == '__main__':
    unittest.main()
