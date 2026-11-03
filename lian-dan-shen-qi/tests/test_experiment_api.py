"""挂载真实实验路由和身份依赖，用模拟业务验证 HTTP 契约。"""

from datetime import datetime, timezone
import unittest
from unittest.mock import create_autospec

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app.api.dependencies import get_current_user_service, get_experiment_service
from app.api.error_handlers import request_validation_exception_handler
from app.api.experiment_routes import router
from app.api.request_logging import RequestLoggingMiddleware
from app.errors import ExperimentNotFoundError, ExperimentStorageError, InvalidTokenError
from app.models.experiment import ExperimentOverview, ExperimentPage, ExperimentRecord
from app.security.tokens import TokenService
from app.services.current_user_service import CurrentUserService
from app.services.experiment_service import ExperimentService


ID, OTHER = 'a' * 32, 'b' * 32
STAMP = datetime(2026, 10, 25, 1, 2, 3, tzinfo=timezone.utc)


class ExperimentApiTests(unittest.TestCase):
    def setUp(self):
        self.token = TokenService().create_token()
        self.identity = create_autospec(CurrentUserService, instance=True, spec_set=True)
        async def identify(token):
            if token != self.token:
                raise InvalidTokenError('PRIVATE_TOKEN_REASON')
            return 7, 'alice'
        self.identity.get_current_user.side_effect = identify
        self.service = create_autospec(ExperimentService, instance=True, spec_set=True)
        self.record = ExperimentRecord(ID, '实验', 'training', STAMP, STAMP, '说明')
        self.service.create_experiment.return_value = self.record
        self.service.get_experiment.return_value = self.record
        self.service.list_experiments.return_value = ExperimentPage((ExperimentOverview(ID, '实验', 'training', STAMP, STAMP),), OTHER)
        self.application = FastAPI()
        self.application.add_middleware(RequestLoggingMiddleware)
        self.application.add_exception_handler(RequestValidationError, request_validation_exception_handler)
        self.application.include_router(router)
        self.application.dependency_overrides[get_current_user_service] = lambda: self.identity
        self.application.dependency_overrides[get_experiment_service] = lambda: self.service
        self.client = TestClient(self.application)
        self.headers = {'Authorization': 'Bearer ' + self.token}
        self.addCleanup(self.client.close)
        self.addCleanup(self.application.dependency_overrides.clear)

    def test_create_uses_token_owner_returns_record_location_and_no_cache(self):
        result = self.client.post('/experiments?user_id=999', headers=self.headers,
                                  json={'title': '实验', 'kind': 'training', 'description': '说明'})
        self.assertEqual(result.status_code, 201)
        self.service.create_experiment.assert_awaited_once_with(7, '实验', 'training', '说明')
        self.assertEqual(result.json()['experiment_id'], ID)
        self.assertEqual(result.headers['Location'], '/experiments/' + ID)
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        self.assertEqual(result.json()['created_at'], '2026-10-25T01:02:03Z')
        self.assertNotIn('user_id', result.json())

    def test_defaults_register_script_without_execution_fields(self):
        self.assertEqual(self.client.post('/experiments', headers=self.headers, json={'title': '实验'}).status_code, 201)
        self.service.create_experiment.assert_awaited_once_with(7, '实验', 'script', '')

    def test_detail_accepts_uuid_and_preserves_owner_scope(self):
        result = self.client.get('/experiments/AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA', headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.service.get_experiment.assert_awaited_once_with(7, ID)

    def test_page_uses_bounded_limit_and_normalized_before_id_and_no_description(self):
        result = self.client.get('/experiments', headers=self.headers, params={'limit': 2, 'before_id': OTHER})
        self.assertEqual(result.status_code, 200)
        self.service.list_experiments.assert_awaited_once_with(7, limit=2, before_id=OTHER)
        self.assertEqual(result.json()['next_cursor'], OTHER)
        self.assertNotIn('description', result.json()['items'][0])

    def test_empty_list_returns_success_with_no_next_cursor(self):
        self.service.list_experiments.return_value = ExperimentPage((), None)
        result = self.client.get('/experiments', headers=self.headers)
        self.assertEqual(result.json(), {'items': [], 'next_cursor': None})

    def test_all_three_operations_reject_missing_and_invalid_bearer(self):
        for headers in ({}, {'Authorization': 'Bearer invalid'}):
            for method, path, kwargs in [('post', '/experiments', dict(json={'title': '实验'})),
                                         ('get', '/experiments', {}), ('get', '/experiments/' + ID, {})]:
                with self.subTest(path=path, authenticated=bool(headers)):
                    result = getattr(self.client, method)(path, headers=headers, **kwargs)
                    self.assertEqual(result.status_code, 401)
                    self.assertEqual(result.headers['WWW-Authenticate'], 'Bearer')
        self.service.create_experiment.assert_not_awaited()
        self.service.get_experiment.assert_not_awaited()
        self.service.list_experiments.assert_not_awaited()

    def test_invalid_json_and_forbidden_owner_command_fields_do_not_reach_service(self):
        for body in ({'title': '实验', 'user_id': 8}, {'title': '实验', 'command': 'python train.py'},
                     {'title': '实验', 'kind': 'shell'}, {'title': 123},
                     {'title': '实验', 'description': None}, {'title': '实验', 'description': 'x' * 10001},
                     {'title': ''}, {'title': 'x' * 201}):
            with self.subTest(keys=list(body)):
                result = self.client.post('/experiments', headers=self.headers, json=body)
                self.assertEqual(result.status_code, 422)
                self.assertNotIn('input', str(result.json()))
        self.service.create_experiment.assert_not_awaited()

    def test_invalid_path_cursor_and_limits_are_422(self):
        for path in ('/experiments/not-a-uuid', '/experiments?before_id=wrong',
                     '/experiments?limit=0', '/experiments?limit=101'):
            self.assertEqual(self.client.get(path, headers=self.headers).status_code, 422)
        self.service.get_experiment.assert_not_awaited()
        self.service.list_experiments.assert_not_awaited()

    def test_missing_and_foreign_experiment_or_cursor_share_safe_404(self):
        for service_method, path in ((self.service.get_experiment, '/experiments/' + OTHER),
                                      (self.service.list_experiments, '/experiments?before_id=' + OTHER)):
            service_method.side_effect = ExperimentNotFoundError('PRIVATE_OWNER')
            response = self.client.get(path, headers=self.headers)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {'detail': '实验不存在或不属于当前用户。'})

    def test_database_and_storage_failures_are_generic_503(self):
        for error in (MySQLError(msg='PRIVATE_HOST_AND_SQL', errno=1146), ExperimentStorageError('PRIVATE_STATE')):
            self.service.create_experiment.side_effect = error
            result = self.client.post('/experiments', headers=self.headers, json={'title': '实验'})
            self.assertEqual(result.status_code, 503)
            self.assertEqual(result.json(), {'detail': '请稍后重试。'})

    def test_owner_deleted_after_auth_is_401_while_business_validation_is_400(self):
        self.service.create_experiment.side_effect = InvalidTokenError('PRIVATE_USER')
        result = self.client.post('/experiments', headers=self.headers, json={'title': '实验'})
        self.assertEqual(result.status_code, 401)
        self.service.create_experiment.side_effect = ValueError('PRIVATE_FIELD')
        result = self.client.post('/experiments', headers=self.headers, json={'title': ' '})
        self.assertEqual(result.status_code, 400)
        self.assertNotIn('PRIVATE_FIELD', result.text)

    def test_production_openapi_mounts_all_operations_with_bearer_security(self):
        from app.main import app
        paths = app.openapi()['paths']
        for path, method in (('/experiments', 'get'), ('/experiments', 'post'), ('/experiments/{experiment_id}', 'get')):
            self.assertEqual(paths[path][method]['security'], [{'BearerAuth': []}])
        schema = app.openapi()['components']['schemas']['CreateExperimentRequest']
        self.assertFalse(schema['additionalProperties'])


if __name__ == '__main__':
    unittest.main()
