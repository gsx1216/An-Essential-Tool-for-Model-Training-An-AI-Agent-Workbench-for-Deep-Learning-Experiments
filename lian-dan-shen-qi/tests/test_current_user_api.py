"""当前用户接口回归：真实路由和依赖，模拟异步业务，不连接数据库。"""

import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app.api.dependencies import get_current_user_service
from app.errors import InvalidTokenError
from app.main import app
from app.repositories.login_session_repository import LoginSessionRepository
from app.security.tokens import TokenService
from app.services.current_user_service import CurrentUserService


class CurrentUserApiTests(unittest.TestCase):
    def setUp(self):
        self.token = TokenService().create_token()
        self.service = Mock(spec=CurrentUserService)
        self.service.get_current_user = AsyncMock(return_value=(7, "alice"))
        self.factory_calls = []

        def factory():
            self.factory_calls.append(True)
            return self.service

        self.original_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_current_user_service] = factory
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.original_overrides)

    def request(self, headers=None, **kwargs):
        if headers is None:
            headers = {"Authorization": "Bearer " + self.token}
        return self.client.get("/auth/me", headers=headers, **kwargs)

    def assert_unauthenticated(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(response.json(), {"detail": "登录已失效，请重新登录。"})
        self.assertNotIn(self.token, response.text)

    def test_valid_token_is_awaited_and_returns_public_user_without_caching(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user_id": 7, "username": "alice"})
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        self.assertNotIn(self.token, response.text)
        self.service.get_current_user.assert_awaited_once_with(self.token)

    def test_missing_token_is_rejected_before_service_factory(self):
        self.assert_unauthenticated(self.request(headers={}))
        self.service.get_current_user.assert_not_called()
        self.assertFalse(self.factory_calls)

    def test_other_authentication_scheme_is_rejected_before_service_factory(self):
        self.assert_unauthenticated(self.request(headers={"Authorization": "Basic dGVzdDp0ZXN0"}))
        self.service.get_current_user.assert_not_called()
        self.assertFalse(self.factory_calls)

    def test_empty_bearer_is_rejected_before_service_factory(self):
        self.assert_unauthenticated(self.request(headers={"Authorization": "Bearer"}))
        self.service.get_current_user.assert_not_called()
        self.assertFalse(self.factory_calls)

    def test_invalid_token_uses_uniform_401(self):
        self.service.get_current_user.side_effect = InvalidTokenError("TEST_ONLY_PRIVATE_REASON")
        response = self.request()
        self.assert_unauthenticated(response)
        self.assertNotIn("TEST_ONLY_PRIVATE_REASON", response.text)
        self.service.get_current_user.assert_awaited_once_with(self.token)

    def test_database_failure_returns_generic_503(self):
        self.service.get_current_user.side_effect = MySQLError("TEST_ONLY_PRIVATE_DATABASE_DETAIL", errno=2003)
        response = self.request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertNotIn(self.token, response.text)
        self.assertNotIn("TEST_ONLY_PRIVATE_DATABASE_DETAIL", response.text)
        self.service.get_current_user.assert_awaited_once_with(self.token)

    def test_unexpected_error_stays_generic_500(self):
        self.service.get_current_user.side_effect = RuntimeError("TEST_ONLY_INTERNAL_FAILURE")
        response = self.request()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertNotIn("TEST_ONLY_INTERNAL_FAILURE", response.text)
        self.service.get_current_user.assert_awaited_once_with(self.token)

    def test_query_parameters_cannot_select_another_user(self):
        response = self.request(params={"user_id": 999, "username": "someone_else"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user_id": 7, "username": "alice"})
        self.service.get_current_user.assert_awaited_once_with(self.token)

    def test_requests_revalidate_identity_and_do_not_mix_users(self):
        second_token = TokenService().create_token()
        self.service.get_current_user.side_effect = [(7, "alice"), (8, "bob")]
        first = self.request()
        second = self.request(headers={"Authorization": "Bearer " + second_token})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), {"user_id": 7, "username": "alice"})
        self.assertEqual(second.json(), {"user_id": 8, "username": "bob"})
        self.assertEqual(self.service.get_current_user.await_count, 2)

    def test_docs_exposes_bearer_security_on_me_and_keeps_login_public(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertEqual(schema["components"]["securitySchemes"]["BearerAuth"]["scheme"], "bearer")
        operation = schema["paths"]["/auth/me"]["get"]
        self.assertEqual(operation["security"], [{"BearerAuth": []}])
        self.assertNotIn("security", schema["paths"]["/auth/login"]["post"])
        self.assertEqual(operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
                         "#/components/schemas/UserResponse")


class CurrentUserDependencyTests(unittest.TestCase):
    def test_factory_reuses_service_with_correct_collaborators(self):
        get_current_user_service.cache_clear()
        try:
            with patch("app.api.dependencies.get_mysql_password", return_value="TEST_ONLY_UNUSED"):
                first = get_current_user_service()
                second = get_current_user_service()
            self.assertIs(first, second)
            self.assertIsInstance(first, CurrentUserService)
            self.assertIsInstance(first.session_repository, LoginSessionRepository)
            self.assertIsInstance(first.token_service, TokenService)
        finally:
            get_current_user_service.cache_clear()
