"""检查真正挂载的全局处理器；所有业务服务都被模拟，不写数据库。"""

import unittest
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from app.api.dependencies import get_auth_service
from app.main import app
from app.services.auth_service import AuthService


PASSWORD = "TEST_PASSWORD_MUST_NOT_APPEAR_123"


class RequestValidationTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock(spec=AuthService)
        self.service.register = AsyncMock(return_value=(7, "alice"))
        self.original_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_auth_service] = lambda: self.service
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.original_overrides)

    def assert_sanitized_validation_error(self, response):
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(PASSWORD, response.text)
        body = response.json()
        self.assertEqual(set(body), {"detail"})
        details = body["detail"]
        self.assertIsInstance(details, list)
        self.assertTrue(details)
        for item in details:
            self.assertEqual(set(item), {"loc", "type", "msg"})
            self.assertIsInstance(item["loc"], list)
            self.assertEqual(item["msg"], "请求字段缺失或格式不正确。")
        self.service.register.assert_not_awaited()
        return details

    def test_missing_username_does_not_echo_password(self):
        response = self.client.post("/auth/register", json={"password": PASSWORD})
        details = self.assert_sanitized_validation_error(response)
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["loc"], ["body", "username"])
        self.assertEqual(details[0]["type"], "missing")

    def test_all_missing_fields_are_reported(self):
        response = self.client.post("/auth/register", json={})
        details = self.assert_sanitized_validation_error(response)
        self.assertEqual(len(details), 2)
        self.assertEqual(
            {tuple(item["loc"]) for item in details},
            {("body", "username"), ("body", "password")},
        )
        self.assertTrue(all(item["type"] == "missing" for item in details))

    def test_invalid_password_type_does_not_echo_nested_input(self):
        response = self.client.post(
            "/auth/register", json={"username": "alice", "password": {"secret": PASSWORD}}
        )
        details = self.assert_sanitized_validation_error(response)
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["loc"], ["body", "password"])
        self.assertEqual(details[0]["type"], "string_type")

    def test_malformed_json_does_not_echo_original_body(self):
        response = self.client.post(
            "/auth/register",
            content='{"username":"alice","password":"' + PASSWORD,
            headers={"Content-Type": "application/json"},
        )
        details = self.assert_sanitized_validation_error(response)
        self.assertEqual(details[0]["loc"][0], "body")
        self.assertEqual(details[0]["type"], "json_invalid")

    def test_non_object_body_does_not_echo_input(self):
        response = self.client.post("/auth/register", json=PASSWORD)
        self.assert_sanitized_validation_error(response)

    def test_valid_request_still_reaches_registration(self):
        response = self.client.post(
            "/auth/register", json={"username": "alice", "password": "Test1234"}
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"user_id": 7, "username": "alice"})
        self.service.register.assert_awaited_once_with("alice", "Test1234")

    def test_business_error_remains_400(self):
        self.service.register.side_effect = ValueError(PASSWORD)
        response = self.client.post(
            "/auth/register", json={"username": "alice", "password": "short"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "用户名或密码不符合要求。"})
        self.assertNotIn(PASSWORD, response.text)
