"""登录接口回归：真实路由、业务和密码校验，模拟仓储，不写数据库。"""

import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app.api.dependencies import get_auth_service, get_login_service
from app.main import app
from app.repositories.user_repository import UserRepository
from app.repositories.login_session_repository import LoginSessionRepository
from app.security.passwords import PasswordService
from app.security.tokens import TokenService
from app.services.auth_service import AuthService
from app.services.login_service import LoginService


class LoginApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.passwords = PasswordService()
        cls.password = "LoginTest8"
        cls.encoded = cls.passwords.hash_password(cls.password)

    def setUp(self):
        self.repository = Mock(spec=UserRepository)
        self.repository.get_user_auth.return_value = (7, "alice", self.encoded)
        self.sessions = Mock(spec=LoginSessionRepository)
        self.sessions.save_session.return_value = 17
        self.tokens = TokenService()
        self.service = LoginService(AuthService(self.repository, self.passwords), self.sessions, self.tokens)
        self.original_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_login_service] = lambda: self.service
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.original_overrides)
        # 登录只读取账号，不能顺便注册账号。
        self.repository.create_user.assert_not_called()
        self.repository.create_registered_user.assert_not_called()

    def login(self, password=None, username="alice"):
        return self.client.post(
            "/auth/login",
            json={"username": username, "password": self.password if password is None else password},
        )

    def assert_login_rejected(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "用户名或密码错误。"})
        self.assertNotIn(self.password, response.text)
        self.assertNotIn(self.encoded, response.text)
        self.sessions.save_session.assert_not_called()

    def assert_request_rejected(self, response, field, error_type):
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(self.password, response.text)
        details = response.json()["detail"]
        self.assertEqual(len(details), 1)
        self.assertEqual(set(details[0]), {"loc", "type", "msg"})
        self.assertEqual(details[0]["loc"], ["body", field])
        self.assertEqual(details[0]["type"], error_type)
        self.repository.get_user_auth.assert_not_called()

    def test_correct_password_returns_persisted_token_and_public_user_information(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"access_token", "token_type", "expires_in", "user"})
        self.assertEqual(body["user"], {"user_id": 7, "username": "alice"})
        self.assertEqual(body["token_type"], "bearer")
        self.assertEqual(body["expires_in"], 259200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")
        user_id, token_hash, expires_at = self.sessions.save_session.call_args.args
        self.assertEqual(user_id, 7)
        self.assertEqual(token_hash, self.tokens.hash_token(body["access_token"]))
        self.assertNotIn(token_hash, response.text)
        self.assertNotIn(self.password, response.text)
        self.assertNotIn(self.encoded, response.text)
        self.repository.get_user_auth.assert_called_once_with("alice")

    def test_login_storage_failure_returns_503_without_token(self):
        self.sessions.save_session.side_effect = MySQLError("TEST_ONLY_STORAGE_FAILURE", errno=2003)
        response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})

    def test_internal_token_validation_error_is_not_misreported_as_bad_password(self):
        self.sessions.save_session.side_effect = ValueError("TEST_ONLY_INTERNAL_FAILURE")
        response = self.login()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})

    def test_reused_service_does_not_reuse_tokens_or_mix_user_identity(self):
        self.repository.get_user_auth.side_effect = lambda username: (
            7 if username == "alice" else 8, username, self.encoded,
        )
        first = self.login(username="alice").json()
        second = self.login(username="bob").json()
        self.assertNotEqual(first["access_token"], second["access_token"])
        self.assertEqual(first["user"], {"user_id": 7, "username": "alice"})
        self.assertEqual(second["user"], {"user_id": 8, "username": "bob"})
        self.assertEqual([call.args[0] for call in self.sessions.save_session.call_args_list], [7, 8])

    def test_docs_and_openapi_describe_the_new_login_response(self):
        self.assertEqual(self.client.get("/docs").status_code, 200)
        schema = self.client.get("/openapi.json").json()
        response_schema = schema["paths"]["/auth/login"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(response_schema["$ref"], "#/components/schemas/LoginResponse")

    def test_wrong_password_returns_401(self):
        self.assert_login_rejected(self.login(password="WrongPassword8"))

    def test_unknown_user_has_same_401_response(self):
        self.repository.get_user_auth.return_value = None
        self.assert_login_rejected(self.login(username="missing_user"))

    def test_dummy_verification_password_cannot_log_in_missing_user(self):
        self.repository.get_user_auth.return_value = None
        self.assert_login_rejected(self.login(password="dummy-account-placeholder"))

    def test_legacy_user_without_password_cannot_log_in(self):
        self.repository.get_user_auth.return_value = (9, "legacy", None)
        self.assert_login_rejected(self.login(username="legacy"))

    def test_invalid_stored_hash_cannot_log_in(self):
        self.repository.get_user_auth.return_value = (7, "alice", "not-a-password-hash")
        self.assert_login_rejected(self.login())

    def test_empty_or_oversized_password_is_rejected(self):
        for password in ("", "x" * 129):
            with self.subTest(length=len(password)):
                self.assert_login_rejected(self.login(password=password))

    def test_password_spaces_are_not_removed(self):
        password = " Login8 "
        encoded = self.passwords.hash_password(password)
        self.repository.get_user_auth.return_value = (7, "alice", encoded)
        self.assertEqual(self.login(password=password).status_code, 200)
        self.sessions.save_session.reset_mock()
        self.assert_login_rejected(self.login(password=password.strip()))

    def test_existing_long_password_remains_usable_for_login(self):
        # 模拟旧策略下已经保存的哈希，不放宽新注册的 32 字符上限。
        from argon2 import PasswordHasher

        password = "x" * 40
        encoded = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1).hash(password)
        self.repository.get_user_auth.return_value = (7, "alice", encoded)
        self.assertEqual(self.login(password=password).status_code, 200)

    def test_database_failure_returns_sanitized_503(self):
        private_detail = "TEST_ONLY_PRIVATE_CONNECTION_DETAIL"
        self.repository.get_user_auth.side_effect = MySQLError(private_detail, errno=2003)
        response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertNotIn(private_detail, response.text)
        self.assertNotIn(self.password, response.text)

    def test_invalid_username_from_repository_is_rejected(self):
        self.repository.get_user_auth.side_effect = ValueError("private validation details")
        self.assert_login_rejected(self.login(username="   "))

    def test_missing_username_returns_safe_422(self):
        response = self.client.post("/auth/login", json={"password": self.password})
        self.assert_request_rejected(response, "username", "missing")

    def test_missing_password_returns_safe_422(self):
        response = self.client.post("/auth/login", json={"username": "alice"})
        self.assert_request_rejected(response, "password", "missing")

    def test_wrong_password_type_returns_safe_422(self):
        response = self.client.post(
            "/auth/login", json={"username": "alice", "password": {"secret": self.password}}
        )
        self.assert_request_rejected(response, "password", "string_type")

    def test_malformed_json_returns_safe_422(self):
        response = self.client.post(
            "/auth/login",
            content='{"username":"alice","password":"' + self.password,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(self.password, response.text)
        self.assertEqual(response.json()["detail"][0]["type"], "json_invalid")
        self.repository.get_user_auth.assert_not_called()


class LoginDependencyTests(unittest.TestCase):
    def test_cached_factory_reuses_services_not_per_user_results(self):
        get_login_service.cache_clear()
        get_auth_service.cache_clear()
        try:
            with patch("app.api.dependencies.get_mysql_password", return_value="TEST_ONLY_BACKEND_PASSWORD"):
                first = get_login_service()
                second = get_login_service()
                self.assertIs(first, second)
                self.assertIs(first.auth_service, get_auth_service())
                self.assertEqual(first.token_service.ttl_seconds, 259200)
        finally:
            get_login_service.cache_clear()
            get_auth_service.cache_clear()
