"""注册密码策略回归测试：模拟仓储，不访问真实用户表。"""

import asyncio
import unittest
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app.api.auth_routes import router
from app.api.dependencies import get_auth_service
from app.errors import UsernameAlreadyExistsError
from app.repositories.user_repository import UserRepository
from app.security.passwords import PasswordService
from app.services.auth_service import AuthService


class RegistrationPasswordPolicyTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock(spec=UserRepository)
        self.repository.create_registered_user.return_value = 7
        self.passwords = PasswordService()
        self.service = AuthService(self.repository, self.passwords)

    def post_registration(self, username="alice", password="Test1234"):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_auth_service] = lambda: self.service
        with TestClient(app) as client:
            return client.post(
                "/auth/register", json={"username": username, "password": password}
            )

    def test_eight_character_password_registers_through_http(self):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_auth_service] = lambda: self.service
        password = "Test1234"
        with TestClient(app) as client:
            response = client.post(
                "/auth/register", json={"username": "  alice  ", "password": password}
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"user_id": 7, "username": "alice"})
        username, encoded = self.repository.create_registered_user.call_args.args
        self.assertEqual(username, "alice")
        self.assertTrue(self.passwords.verify_password(password, encoded))
        self.assertNotEqual(encoded, password)

    def test_outside_registration_length_limits_never_reaches_repository(self):
        for password in ("x" * 7, "x" * 33):
            with self.subTest(length=len(password)), self.assertRaisesRegex(ValueError, "8～32"):
                asyncio.run(self.service.register("alice", password))
        self.repository.create_registered_user.assert_not_called()

    def test_invalid_password_returns_400_without_echoing_password(self):
        for password in ("x" * 7, "x" * 33):
            with self.subTest(length=len(password)):
                response = self.post_registration(password=password)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"detail": "用户名或密码不符合要求。"})
                self.assertNotIn(password, response.text)
        self.repository.create_registered_user.assert_not_called()

    def test_invalid_username_returns_400_without_database_write(self):
        for username in ("", "   ", "a" * 65):
            with self.subTest(username=username):
                response = self.post_registration(username=username)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"detail": "用户名或密码不符合要求。"})
        self.repository.create_registered_user.assert_not_called()

    def test_duplicate_username_returns_409_not_generic_input_error(self):
        internal_detail = "internal duplicate detail must stay private"
        self.repository.create_registered_user.side_effect = UsernameAlreadyExistsError(internal_detail)
        response = self.post_registration()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "用户名已存在，请使用其他用户名。"})
        self.assertNotIn(internal_detail, response.text)

    def test_database_failure_returns_503_without_connection_details(self):
        internal_detail = "internal host and connection credentials must stay private"
        self.repository.create_registered_user.side_effect = MySQLError(internal_detail, errno=2003)
        response = self.post_registration()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertNotIn(internal_detail, response.text)
