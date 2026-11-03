"""隔离挂载真实会话路由；模拟异步业务与身份服务，不连接数据库。"""

import unittest
from unittest.mock import create_autospec

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app.api.dependencies import get_chat_session_service, get_current_user_service
from app.api.error_handlers import request_validation_exception_handler
from app.api.request_logging import RequestLoggingMiddleware
from app.api.session_routes import router
from app.errors import InvalidTokenError, SessionNotFoundError
from app.security.tokens import TokenService
from app.services.chat_session_service import ChatSessionService
from app.services.current_user_service import CurrentUserService


SESSION_ID = "b4c4e67554ee4c1981740a1e901ec614"
SECOND_ID = "430390d71ece4e63ae202043b057e9c2"


class SessionRouteMountingTests(unittest.TestCase):
    def test_production_entrypoint_registers_session_routes(self):
        from app.main import app
        schema = app.openapi()
        self.assertIn("post", schema["paths"]["/sessions"])
        self.assertIn("get", schema["paths"]["/sessions"])
        self.assertIn("delete", schema["paths"]["/sessions/{session_id}"])


class SessionApiTests(unittest.TestCase):
    def setUp(self):
        self.token = TokenService().create_token()
        self.second_token = TokenService().create_token()
        self.identity = create_autospec(CurrentUserService, instance=True, spec_set=True)

        async def identify(access_token):
            if access_token == self.token:
                return 7, "alice"
            if access_token == self.second_token:
                return 8, "bob"
            raise InvalidTokenError("TEST_ONLY_INVALID_CREDENTIAL")

        self.identity.get_current_user.side_effect = identify
        self.service = create_autospec(ChatSessionService, instance=True, spec_set=True)
        self.service.create_session.return_value = SESSION_ID
        self.service.list_sessions.return_value = [(SESSION_ID, "会话一"), (SECOND_ID, "会话二")]
        self.service.delete_session.return_value = None
        self.factory_calls = []

        def service_factory():
            self.factory_calls.append(True)
            return self.service

        # 生产 main 尚未挂载会话路由；这里只构建与其异常/日志配置一致的隔离应用。
        self.application = FastAPI()
        self.application.add_middleware(RequestLoggingMiddleware)
        self.application.add_exception_handler(RequestValidationError, request_validation_exception_handler)
        self.application.include_router(router)
        self.application.dependency_overrides[get_current_user_service] = lambda: self.identity
        self.application.dependency_overrides[get_chat_session_service] = service_factory
        self.client = TestClient(self.application)

    def tearDown(self):
        self.client.close()
        self.application.dependency_overrides.clear()

    def request(self, method="GET", path="/sessions", headers=None, **kwargs):
        if headers is None:
            headers = {"Authorization": "Bearer " + self.token}
        return self.client.request(method, path, headers=headers, **kwargs)

    def assert_no_cache(self, response):
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")

    def assert_unauthenticated(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(response.json(), {"detail": "登录已失效，请重新登录。"})

    def test_create_awaits_business_with_authenticated_owner_and_default_title(self):
        response = self.request("POST", json={})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"session_id": SESSION_ID})
        self.assert_no_cache(response)
        self.service.create_session.assert_awaited_once_with(user_id=7, title="新聊天")

    def test_create_passes_title_to_business_for_normalization(self):
        response = self.request("POST", json={"title": "  实验一  "})
        self.assertEqual(response.status_code, 201)
        self.service.create_session.assert_awaited_once_with(user_id=7, title="  实验一  ")

    def test_create_business_validation_returns_400(self):
        self.service.create_session.side_effect = ValueError("TEST_ONLY_TITLE_REJECTED")
        response = self.request("POST", json={"title": "   "})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "请求参数不符合要求。"})
        self.assertNotIn("TEST_ONLY_TITLE_REJECTED", response.text)

    def test_create_rejects_client_selected_owner(self):
        response = self.request("POST", json={"title": "实验", "user_id": 999})
        self.assertEqual(response.status_code, 422)
        self.service.create_session.assert_not_awaited()

    def test_create_rejects_wrong_title_type_without_echoing_input(self):
        response = self.request("POST", json={"title": {"secret": "TEST_ONLY_PRIVATE_INPUT"}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("TEST_ONLY_PRIVATE_INPUT", response.text)
        self.service.create_session.assert_not_awaited()

    def test_list_returns_all_rows_as_named_fields(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"session_id": SESSION_ID, "title": "会话一"},
                                          {"session_id": SECOND_ID, "title": "会话二"}])
        self.assert_no_cache(response)
        self.service.list_sessions.assert_awaited_once_with(7)

    def test_list_empty_is_successful_empty_array(self):
        self.service.list_sessions.return_value = []
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_list_ignores_query_owner_and_keeps_authenticated_identity(self):
        response = self.request(params={"user_id": 999, "username": "someone_else"})
        self.service.list_sessions.assert_awaited_once_with(7)
        self.assertEqual(response.status_code, 200)

    def test_list_two_tokens_are_scoped_to_their_own_identities(self):
        async def rows(user_id):
            return [(SESSION_ID, "alice会话")] if user_id == 7 else [(SECOND_ID, "bob会话")]

        self.service.list_sessions.side_effect = rows
        first = self.request()
        second = self.request(headers={"Authorization": "Bearer " + self.second_token})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), [{"session_id": SESSION_ID, "title": "alice会话"}])
        self.assertEqual(second.json(), [{"session_id": SECOND_ID, "title": "bob会话"}])
        self.assertEqual(self.service.list_sessions.await_count, 2)

    def test_list_business_validation_returns_400(self):
        self.service.list_sessions.side_effect = ValueError("TEST_ONLY_REJECTED")
        response = self.request()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "请求参数不符合要求。"})

    def test_delete_awaits_current_owner_and_returns_empty_204(self):
        response = self.request("DELETE", "/sessions/" + SESSION_ID)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assert_no_cache(response)
        self.service.delete_session.assert_awaited_once_with(7, session_id=SESSION_ID)

    def test_delete_missing_or_unowned_is_404_before_generic_value_error(self):
        self.service.delete_session.side_effect = SessionNotFoundError("TEST_ONLY_PRIVATE_OWNER")
        response = self.request("DELETE", "/sessions/" + SESSION_ID)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "会话不存在或不属于当前用户。"})
        self.assertNotIn("TEST_ONLY_PRIVATE_OWNER", response.text)

    def test_delete_malformed_uuid_business_error_is_400(self):
        self.service.delete_session.side_effect = ValueError("TEST_ONLY_BAD_UUID")
        response = self.request("DELETE", "/sessions/bad-id")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "请求参数不符合要求。"})

    def test_repeat_delete_is_success_then_missing(self):
        self.service.delete_session.side_effect = [None, SessionNotFoundError("TEST_ONLY_MISSING")]
        first = self.request("DELETE", "/sessions/" + SESSION_ID)
        second = self.request("DELETE", "/sessions/" + SESSION_ID)
        self.assertEqual(first.status_code, 204)
        self.assertEqual(second.status_code, 404)

    def test_all_routes_reject_missing_token_before_chat_service_factory(self):
        for method, path, options in (("POST", "/sessions", {"json": {}}),
                                      ("GET", "/sessions", {}),
                                      ("DELETE", "/sessions/" + SESSION_ID, {})):
            with self.subTest(method=method):
                self.assert_unauthenticated(self.request(method, path, headers={}, **options))
        self.assertFalse(self.factory_calls)
        self.identity.get_current_user.assert_not_awaited()

    def test_invalid_token_never_reaches_chat_service(self):
        response = self.request(headers={"Authorization": "Bearer " + TokenService().create_token()})
        self.assert_unauthenticated(response)
        self.assertFalse(self.factory_calls)
        self.service.list_sessions.assert_not_awaited()

    def test_authentication_database_error_stays_generic_503(self):
        self.identity.get_current_user.side_effect = MySQLError("TEST_ONLY_IDENTITY_FAILURE", errno=2003)
        response = self.request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertFalse(self.factory_calls)

    def test_database_errors_from_all_business_methods_are_generic_503(self):
        cases = (("POST", "/sessions", {"json": {}}, self.service.create_session),
                 ("GET", "/sessions", {}, self.service.list_sessions),
                 ("DELETE", "/sessions/" + SESSION_ID, {}, self.service.delete_session))
        for method, path, options, operation in cases:
            with self.subTest(method=method):
                operation.side_effect = MySQLError("TEST_ONLY_PRIVATE_DATABASE_DETAIL", errno=2013)
                response = self.request(method, path, **options)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"detail": "请稍后重试。"})
                self.assertNotIn("TEST_ONLY_PRIVATE_DATABASE_DETAIL", response.text)

    def test_unexpected_errors_are_generic_500(self):
        cases = (("POST", "/sessions", {"json": {}}, self.service.create_session),
                 ("GET", "/sessions", {}, self.service.list_sessions),
                 ("DELETE", "/sessions/" + SESSION_ID, {}, self.service.delete_session))
        for method, path, options, operation in cases:
            with self.subTest(method=method):
                operation.side_effect = RuntimeError("TEST_ONLY_INTERNAL_FAILURE")
                response = self.request(method, path, **options)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json(), {"detail": "请稍后重试。"})
                self.assertNotIn("TEST_ONLY_INTERNAL_FAILURE", response.text)

    def test_openapi_has_bearer_security_and_empty_delete_response(self):
        schema = self.client.get("/openapi.json").json()
        for path, method in (("/sessions", "post"), ("/sessions", "get"),
                             ("/sessions/{session_id}", "delete")):
            with self.subTest(method=method):
                self.assertEqual(schema["paths"][path][method]["security"], [{"BearerAuth": []}])
        self.assertNotIn("content", schema["paths"]["/sessions/{session_id}"]["delete"]["responses"]["204"])
