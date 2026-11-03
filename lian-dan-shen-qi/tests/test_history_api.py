"""隔离挂载真实历史路由，模拟身份和异步业务；不连接 MySQL 或模型。"""

import unittest
from unittest.mock import create_autospec

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app.api.dependencies import get_chat_history_service, get_current_user_service
from app.api.error_handlers import request_validation_exception_handler
from app.api.history_routes import router
from app.api.request_logging import RequestLoggingMiddleware
from app.errors import InvalidTokenError, SessionNotFoundError
from app.security.tokens import TokenService
from app.services.chat_history_service import ChatHistoryService
from app.services.current_user_service import CurrentUserService


SESSION_ID = "b4c4e67554ee4c1981740a1e901ec614"
HISTORY_PATH = "/sessions/" + SESSION_ID + "/messages"


class HistoryRouteMountingTests(unittest.TestCase):
    def test_production_entrypoint_registers_history_route_with_bearer(self):
        from app.main import app
        operation = app.openapi()["paths"]["/sessions/{session_id}/messages"]["get"]
        self.assertEqual(operation["security"], [{"BearerAuth": []}])
        self.assertEqual(operation["responses"]["200"]["description"], "Successful Response")


class HistoryApiTests(unittest.TestCase):
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
        self.service = create_autospec(ChatHistoryService, instance=True, spec_set=True)
        self.service.load_history_page.return_value = [
            (1, "human", "实验怎样了？"),
            (2, "ai", "训练仍在运行。"),
        ]
        self.factory_calls = []

        def history_factory():
            self.factory_calls.append(True)
            return self.service

        # 路由行为用隔离应用验证；正式入口是否挂载由独立测试检查。
        self.application = FastAPI()
        self.application.add_middleware(RequestLoggingMiddleware)
        self.application.add_exception_handler(
            RequestValidationError, request_validation_exception_handler,
        )
        self.application.include_router(router)
        self.application.dependency_overrides[get_current_user_service] = lambda: self.identity
        self.application.dependency_overrides[get_chat_history_service] = history_factory
        self.client = TestClient(self.application)

    def tearDown(self):
        self.client.close()
        self.application.dependency_overrides.clear()

    def request(self, path=HISTORY_PATH, headers=None, **kwargs):
        if headers is None:
            headers = {"Authorization": "Bearer " + self.token}
        return self.client.get(path, headers=headers, **kwargs)

    def assert_unauthenticated(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "登录已失效，请重新登录。"})
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        self.assertTrue(response.headers.get("X-Request-ID"))

    def test_defaults_await_business_and_return_all_named_rows(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [
            {"seq": 1, "role": "human", "content": "实验怎样了？"},
            {"seq": 2, "role": "ai", "content": "训练仍在运行。"},
        ])
        self.service.load_history_page.assert_awaited_once_with(
            user_id=7, session_id=SESSION_ID, after_seq=0, limit=50,
        )

    def test_custom_cursor_and_limit_are_forwarded(self):
        response = self.request(params={"after_seq": 17, "limit": 3})
        self.assertEqual(response.status_code, 200)
        self.service.load_history_page.assert_awaited_once_with(
            user_id=7, session_id=SESSION_ID, after_seq=17, limit=3,
        )

    def test_valid_paging_bounds_reach_business(self):
        for cursor, limit in ((0, 1), (2**64 - 1, 100)):
            with self.subTest(after_seq=cursor, limit=limit):
                self.service.load_history_page.reset_mock()
                response = self.request(params={"after_seq": cursor, "limit": limit})
                self.assertEqual(response.status_code, 200)
                self.service.load_history_page.assert_awaited_once_with(
                    user_id=7, session_id=SESSION_ID, after_seq=cursor, limit=limit,
                )

    def test_empty_history_returns_successful_empty_array(self):
        self.service.load_history_page.return_value = []
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_response_preserves_database_sequence_and_code_whitespace(self):
        code = "\n    def train():\n        return 1\n  "
        self.service.load_history_page.return_value = [(41, "human", code), (45, "ai", "  完成\n")]
        response = self.request(params={"after_seq": 40})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [
            {"seq": 41, "role": "human", "content": code},
            {"seq": 45, "role": "ai", "content": "  完成\n"},
        ])

    def test_success_disables_cache_and_has_request_id(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        self.assertTrue(response.headers.get("X-Request-ID"))

    def test_query_cannot_override_authenticated_owner(self):
        response = self.request(params={"user_id": 999, "username": "someone_else"})
        self.assertEqual(response.status_code, 200)
        self.service.load_history_page.assert_awaited_once_with(
            user_id=7, session_id=SESSION_ID, after_seq=0, limit=50,
        )

    def test_two_tokens_pass_their_own_identities_to_business(self):
        async def history(user_id, session_id, after_seq, limit):
            return [(9, "human", "alice消息" if user_id == 7 else "bob消息")]

        self.service.load_history_page.side_effect = history
        first = self.request()
        second = self.request(headers={"Authorization": "Bearer " + self.second_token})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()[0]["content"], "alice消息")
        self.assertEqual(second.json()[0]["content"], "bob消息")
        self.assertEqual(self.service.load_history_page.await_count, 2)

    def test_missing_empty_or_wrong_scheme_token_stops_before_business_factory(self):
        for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": "Basic abc"}):
            with self.subTest(headers=headers):
                self.assert_unauthenticated(self.request(headers=headers))
        self.assertFalse(self.factory_calls)
        self.identity.get_current_user.assert_not_awaited()
        self.service.load_history_page.assert_not_awaited()

    def test_unknown_token_stops_before_business_factory(self):
        self.assert_unauthenticated(self.request(headers={
            "Authorization": "Bearer " + TokenService().create_token(),
        }))
        self.assertFalse(self.factory_calls)
        self.service.load_history_page.assert_not_awaited()

    def test_expired_or_revoked_identity_result_is_401(self):
        self.identity.get_current_user.side_effect = InvalidTokenError("TEST_ONLY_EXPIRED_OR_REVOKED")
        response = self.request()
        self.assert_unauthenticated(response)
        self.assertNotIn("TEST_ONLY_EXPIRED_OR_REVOKED", response.text)
        self.assertFalse(self.factory_calls)

    def test_authentication_database_error_is_generic_503(self):
        self.identity.get_current_user.side_effect = MySQLError("TEST_ONLY_IDENTITY_FAILURE", errno=2003)
        response = self.request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertNotIn("TEST_ONLY_IDENTITY_FAILURE", response.text)
        self.assertFalse(self.factory_calls)

    def test_invalid_paging_is_422_before_business_and_does_not_echo_input(self):
        for params in ({"after_seq": -1}, {"after_seq": 2**64},
                       {"limit": 0}, {"limit": 101},
                       {"after_seq": "TEST_ONLY_PRIVATE_CURSOR"},
                       {"limit": "TEST_ONLY_PRIVATE_LIMIT"}):
            with self.subTest(params=params):
                response = self.request(params=params)
                self.assertEqual(response.status_code, 422)
                for item in response.json()["detail"]:
                    self.assertEqual(set(item), {"loc", "type", "msg"})
                self.assertNotIn("TEST_ONLY_PRIVATE", response.text)
                self.assertTrue(response.headers.get("X-Request-ID"))
        self.service.load_history_page.assert_not_awaited()

    def test_missing_or_unowned_business_error_is_404_before_value_error(self):
        self.service.load_history_page.side_effect = SessionNotFoundError("TEST_ONLY_PRIVATE_OWNER")
        response = self.request()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "会话不存在或不属于当前用户。"})
        self.assertNotIn("TEST_ONLY_PRIVATE_OWNER", response.text)

    def test_malformed_uuid_business_error_becomes_generic_400(self):
        # UUID 格式由实际业务层校验；本测试仅验证业务异常的 HTTP 转换。
        self.service.load_history_page.side_effect = ValueError("TEST_ONLY_BAD_UUID")
        response = self.request(path="/sessions/bad-id/messages")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "请求参数不符合要求。"})
        self.assertNotIn("TEST_ONLY_BAD_UUID", response.text)

    def test_history_database_failure_is_generic_503(self):
        self.service.load_history_page.side_effect = MySQLError("TEST_ONLY_PRIVATE_SQL", errno=1146)
        response = self.request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertNotIn("TEST_ONLY_PRIVATE_SQL", response.text)
        self.assertTrue(response.headers.get("X-Request-ID"))

    def test_unexpected_failure_is_generic_500(self):
        self.service.load_history_page.side_effect = RuntimeError("TEST_ONLY_INTERNAL_DETAIL")
        response = self.request()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertNotIn("TEST_ONLY_INTERNAL_DETAIL", response.text)
        self.service.load_history_page.assert_awaited_once()

    def test_openapi_declares_bearer_paging_and_response_fields(self):
        schema = self.client.get("/openapi.json").json()
        operation = schema["paths"]["/sessions/{session_id}/messages"]["get"]
        self.assertEqual(operation["security"], [{"BearerAuth": []}])
        parameters = {item["name"]: item for item in operation["parameters"]}
        self.assertEqual(parameters["after_seq"]["schema"]["default"], 0)
        self.assertEqual(parameters["after_seq"]["schema"]["minimum"], 0)
        self.assertEqual(parameters["after_seq"]["schema"]["maximum"], 2**64 - 1)
        self.assertEqual(parameters["limit"]["schema"]["default"], 50)
        self.assertEqual(parameters["limit"]["schema"]["minimum"], 1)
        self.assertEqual(parameters["limit"]["schema"]["maximum"], 100)
        response_schema = schema["components"]["schemas"]["HistoryMessageResponse"]
        self.assertEqual(set(response_schema["required"]), {"seq", "role", "content"})
