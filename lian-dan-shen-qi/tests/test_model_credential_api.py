"""隔离挂载真实个人Key路由；模拟身份和异步业务，不连接数据库或模型。"""

import unittest
from unittest.mock import create_autospec

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app.api.dependencies import get_current_user_service, get_model_credential_service
from app.api.error_handlers import request_validation_exception_handler
from app.api.model_credential_routes import router
from app.api.request_logging import RequestLoggingMiddleware
from app.errors import InvalidTokenError
from app.security.tokens import TokenService
from app.services.current_user_service import CurrentUserService
from app.services.model_credential_service import ModelCredentialService


PATH = "/me/model-credentials/deepseek"
KEY = "sk-TEST_ONLY_FAKE_KEY"


class ModelCredentialApiTests(unittest.TestCase):
    def setUp(self):
        self.token = TokenService().create_token()
        self.second_token = TokenService().create_token()
        self.identity = create_autospec(CurrentUserService, instance=True, spec_set=True)

        async def identify(token):
            if token == self.token:
                return 7, "alice"
            if token == self.second_token:
                return 8, "bob"
            raise InvalidTokenError("TEST_ONLY_INVALID_TOKEN")

        self.identity.get_current_user.side_effect = identify
        self.service = create_autospec(ModelCredentialService, instance=True, spec_set=True)
        self.service.save_api_key.return_value = None
        self.service.is_configured.return_value = True
        self.service.delete_api_key.return_value = True
        self.factory_calls = []

        def factory():
            self.factory_calls.append(True)
            return self.service

        self.application = FastAPI()
        self.application.add_middleware(RequestLoggingMiddleware)
        self.application.add_exception_handler(RequestValidationError, request_validation_exception_handler)
        self.application.include_router(router)
        self.application.dependency_overrides[get_current_user_service] = lambda: self.identity
        self.application.dependency_overrides[get_model_credential_service] = factory
        self.client = TestClient(self.application)

    def tearDown(self):
        self.client.close()
        self.application.dependency_overrides.clear()

    def request(self, method="GET", headers=None, **kwargs):
        if headers is None:
            headers = {"Authorization": "Bearer " + self.token}
        return self.client.request(method, PATH, headers=headers, **kwargs)

    def assert_private_response(self, response):
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        self.assertTrue(response.headers.get("X-Request-ID"))

    def test_save_extracts_secret_and_awaits_authenticated_business(self):
        response = self.request("PUT", json={"api_key": KEY})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assert_private_response(response)
        self.service.save_api_key.assert_awaited_once_with(user_id=7, api_key=KEY)

    def test_status_returns_only_public_fields(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"provider": "deepseek", "configured": True})
        self.assert_private_response(response)
        self.service.is_configured.assert_awaited_once_with(user_id=7)
        self.service.get_api_key.assert_not_called()

    def test_unconfigured_status_is_successful_false(self):
        self.service.is_configured.return_value = False
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"provider": "deepseek", "configured": False})

    def test_delete_returns_empty_204(self):
        response = self.request("DELETE")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assert_private_response(response)
        self.service.delete_api_key.assert_awaited_once_with(7)

    def test_repeat_delete_is_still_empty_204(self):
        self.service.delete_api_key.side_effect = [True, False]
        for _ in range(2):
            response = self.request("DELETE")
            self.assertEqual(response.status_code, 204)
            self.assertEqual(response.content, b"")

    def test_anonymous_requests_stop_before_credential_factory(self):
        for method, options in (("PUT", {"json": {"api_key": KEY}}), ("GET", {}), ("DELETE", {})):
            with self.subTest(method=method):
                response = self.request(method, headers={}, **options)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        self.assertFalse(self.factory_calls)
        self.identity.get_current_user.assert_not_awaited()

    def test_invalid_token_stops_before_credential_factory(self):
        response = self.request(headers={"Authorization": "Bearer " + TokenService().create_token()})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.factory_calls)
        self.service.is_configured.assert_not_awaited()

    def test_request_owner_and_server_config_fields_are_rejected(self):
        for field in ("user_id", "provider", "master_key", "mysql_password"):
            with self.subTest(field=field):
                response = self.request("PUT", json={"api_key": KEY, field: "TEST_ONLY_PRIVATE"})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(KEY, response.text)
                self.assertNotIn("TEST_ONLY_PRIVATE", response.text)
        self.service.save_api_key.assert_not_awaited()

    def test_missing_or_wrong_type_key_is_safe_422(self):
        for payload in ({}, {"api_key": None}, {"api_key": 123}, {"api_key": {"secret": KEY}}):
            with self.subTest(kind=type(payload.get("api_key")).__name__):
                response = self.request("PUT", json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(KEY, response.text)
                self.assertTrue(all(set(item) == {"loc", "type", "msg"} for item in response.json()["detail"]))
        self.service.save_api_key.assert_not_awaited()

    def test_query_owner_does_not_override_authenticated_user(self):
        response = self.request(params={"user_id": 999})
        self.assertEqual(response.status_code, 200)
        self.service.is_configured.assert_awaited_once_with(7)

    def test_two_tokens_pass_their_own_identity(self):
        self.service.is_configured.side_effect = lambda user_id: user_id == 7
        first = self.request()
        second = self.request(headers={"Authorization": "Bearer " + self.second_token})
        self.assertEqual(first.json()["configured"], True)
        self.assertEqual(second.json()["configured"], False)
        self.assertEqual(self.service.is_configured.await_count, 2)

    def test_business_value_errors_are_safe_400(self):
        cases = (("PUT", {"json": {"api_key": KEY}}, self.service.save_api_key),
                 ("GET", {}, self.service.is_configured), ("DELETE", {}, self.service.delete_api_key))
        for method, options, operation in cases:
            with self.subTest(method=method):
                operation.side_effect = ValueError("TEST_ONLY_PRIVATE_BUSINESS_ERROR")
                response = self.request(method, **options)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"detail": "请求参数不符合要求。"})

    def test_business_database_errors_are_generic_503(self):
        cases = (("PUT", {"json": {"api_key": KEY}}, self.service.save_api_key),
                 ("GET", {}, self.service.is_configured), ("DELETE", {}, self.service.delete_api_key))
        for method, options, operation in cases:
            with self.subTest(method=method):
                operation.side_effect = MySQLError("TEST_ONLY_PRIVATE_SQL_ERROR", errno=1146)
                response = self.request(method, **options)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"detail": "请稍后重试。"})
                self.assertTrue(response.headers.get("X-Request-ID"))

    def test_factory_configuration_failure_is_generic_503(self):
        def unavailable():
            raise HTTPException(status_code=503, detail="请稍后重试。")
        self.application.dependency_overrides[get_model_credential_service] = unavailable
        response = self.request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.service.is_configured.assert_not_awaited()

    def test_unexpected_business_error_is_generic_500(self):
        self.service.is_configured.side_effect = RuntimeError(KEY)
        response = self.request()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertNotIn(KEY, response.text)

    def test_openapi_declares_bearer_and_empty_204_responses(self):
        operation = self.client.get("/openapi.json").json()["paths"][PATH]
        self.assertEqual(set(operation), {"put", "get", "delete"})
        for method in operation:
            self.assertEqual(operation[method]["security"], [{"BearerAuth": []}])
        for method in ("put", "delete"):
            self.assertNotIn("content", operation[method]["responses"]["204"])
