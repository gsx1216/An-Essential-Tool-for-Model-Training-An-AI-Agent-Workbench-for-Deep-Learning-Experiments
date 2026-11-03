"""退出接口回归：真实路由和业务服务，模拟仓储，不访问 MySQL。"""

import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app.api.dependencies import get_logout_service
from app.main import app
from app.repositories.login_session_repository import LoginSessionRepository
from app.security.tokens import TokenService
from app.services.logout_service import LogoutService


class LogoutApiTests(unittest.TestCase):
    def setUp(self):
        self.tokens = TokenService()
        self.token = self.tokens.create_token()
        self.repository = Mock(spec=LoginSessionRepository)
        self.repository.revoke_session.return_value = True
        self.service = LogoutService(self.repository, self.tokens)
        self.factory_calls = []

        def factory():
            self.factory_calls.append(True)
            return self.service

        self.previous_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_logout_service] = factory
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.previous_overrides)

    def request(self, headers=None):
        if headers is None:
            headers = {"Authorization": "Bearer " + self.token}
        return self.client.post("/auth/logout", headers=headers)

    def assert_unauthenticated(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(response.json(), {"detail": "登录已失效，请重新登录。"})
        self.assertNotIn(self.token, response.text)

    def test_success_revokes_hash_and_returns_empty_204(self):
        response = self.request()
        self.repository.revoke_session.assert_called_once_with(self.tokens.hash_token(self.token))
        self.repository.get_active_user.assert_not_called()
        self.repository.save_session.assert_not_called()
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")

    def test_success_disables_caching(self):
        response = self.request()
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")

    def test_missing_token_rejected_before_factory(self):
        self.assert_unauthenticated(self.request(headers={}))
        self.assertFalse(self.factory_calls)
        self.repository.revoke_session.assert_not_called()

    def test_basic_scheme_rejected_before_factory(self):
        self.assert_unauthenticated(self.request(headers={"Authorization": "Basic dGVzdDp0ZXN0"}))
        self.assertFalse(self.factory_calls)
        self.repository.revoke_session.assert_not_called()

    def test_empty_bearer_rejected_before_factory(self):
        self.assert_unauthenticated(self.request(headers={"Authorization": "Bearer"}))
        self.assertFalse(self.factory_calls)
        self.repository.revoke_session.assert_not_called()

    def test_malformed_token_rejected_before_repository(self):
        self.assert_unauthenticated(self.request(headers={"Authorization": "Bearer bad-token"}))
        self.repository.revoke_session.assert_not_called()

    def test_unknown_expired_or_revoked_token_returns_401(self):
        self.repository.revoke_session.return_value = False
        self.assert_unauthenticated(self.request())
        self.repository.revoke_session.assert_called_once_with(self.tokens.hash_token(self.token))

    def test_database_failure_returns_generic_503(self):
        self.repository.revoke_session.side_effect = MySQLError("TEST_ONLY_PRIVATE_DATABASE_DETAIL", errno=2003)
        response = self.request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertNotIn("TEST_ONLY_PRIVATE_DATABASE_DETAIL", response.text)
        self.assertNotIn(self.token, response.text)

    def test_unexpected_failure_returns_generic_500(self):
        self.repository.revoke_session.side_effect = RuntimeError("TEST_ONLY_PRIVATE_FAILURE")
        response = self.request()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertNotIn("TEST_ONLY_PRIVATE_FAILURE", response.text)

    def test_repeat_logout_has_one_success_then_401(self):
        self.repository.revoke_session.side_effect = [True, False]
        first = self.request()
        second = self.request()
        self.assert_unauthenticated(second)
        self.assertEqual(self.repository.revoke_session.call_count, 2)
        self.assertEqual(first.status_code, 204)

    def test_openapi_requires_bearer_and_documents_no_response_body(self):
        schema = self.client.get("/openapi.json").json()
        operation = schema["paths"]["/auth/logout"]["post"]
        self.assertEqual(operation["security"], [{"BearerAuth": []}])
        self.assertNotIn("content", operation["responses"]["204"])


class LogoutFactoryTests(unittest.TestCase):
    def test_factory_reuses_service_with_correct_collaborators(self):
        get_logout_service.cache_clear()
        try:
            with patch("app.api.dependencies.get_mysql_password", return_value="TEST_ONLY_UNUSED"):
                first = get_logout_service()
                second = get_logout_service()
            self.assertIs(first, second)
            self.assertIsInstance(first, LogoutService)
            self.assertIsInstance(first.session_repository, LoginSessionRepository)
            self.assertIsInstance(first.token_service, TokenService)
        finally:
            get_logout_service.cache_clear()
