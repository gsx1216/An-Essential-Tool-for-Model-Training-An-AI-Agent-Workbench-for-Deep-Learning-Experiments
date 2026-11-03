"""在独立临时数据库验证迁移、注册和登录，不调用模型。"""

import asyncio
import re
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app import database
from app.api.auth_routes import router
from app.api.dependencies import get_auth_service, get_login_service
from app.api.error_handlers import request_validation_exception_handler
from app.api.request_logging import RequestLoggingMiddleware
from app.config import get_mysql_password
from app.repositories.user_repository import UserRepository
from app.repositories import user_repository
from app.repositories import login_session_repository
from app.repositories.login_session_repository import LoginSessionRepository
from app.security.passwords import PasswordService
from app.security.tokens import TokenService
from app.services.auth_service import AuthService
from app.services.login_service import LoginService


ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / "config" / ".env", override=False, interpolate=False)
    password = get_mysql_password()
    name = "_codex_auth_check_" + uuid4().hex
    assert re.fullmatch(r"_codex_auth_check_[0-9a-f]{32}", name)
    created = False

    def sql(filename):
        return (ROOT / "sql" / filename).read_text(encoding="utf-8").replace("experiment_assistant", name)

    try:
        with database.get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                created = True
                # 构造没有 password_hash 的旧 users 表，模拟已有用户迁移。
                old_schema = "\n".join(line for line in sql("init.sql").splitlines() if "`password_hash`" not in line)
                cursor.execute(old_schema)
                while cursor.nextset():
                    pass
            connection.commit()

        repository = UserRepository(password)
        for attribute, value in vars(repository).copy().items():
            if attribute.endswith("_sql") and isinstance(value, str):
                setattr(repository, attribute, value.replace("experiment_assistant", name))
        legacy_id = repository.create_user("legacy")

        with database.get_connection(password) as connection:
            with patch.object(database, "_read_sql", side_effect=sql):
                assert database._ensure_user_password_column(connection) is True
                assert database._ensure_user_password_column(connection) is False
                database._ensure_login_sessions(connection)
        assert repository.get_user(legacy_id) == (legacy_id, "legacy")
        assert repository.get_user_auth("legacy") == (legacy_id, "legacy", None)

        passwords = PasswordService()
        example = "Test1234"
        encoded = passwords.hash_password(example)
        user_id = repository.create_registered_user("  alice  ", encoded)
        row = repository.get_user_auth("alice")
        assert row == (user_id, "alice", encoded)
        assert example not in repr(row)
        assert passwords.verify_password(example, row[2])
        assert not passwords.verify_password("wrong-password-value", row[2])
        assert not passwords.verify_password(example, repository.get_user_auth("legacy")[2])
        assert repository.get_user(user_id) == (user_id, "alice")
        assert repository.get_user_auth("' OR 1=1 --") is None
        try:
            repository.create_registered_user("alice", encoded)
        except ValueError:
            pass
        else:
            raise AssertionError("Duplicate username was not rejected")
        assert repository.get_user_auth("alice") == row

        # 真实 HTTP 处理链路 + 真实临时 MySQL 表，验证最低 8 字符可以注册。
        service = AuthService(repository, passwords)
        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)
        app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
        app.include_router(router)
        app.dependency_overrides[get_auth_service] = lambda: service
        login_repository = LoginSessionRepository(password)
        login_repository.save_session_sql = sql("save_login_session.sql")
        token_service = TokenService()
        login_service = LoginService(service, login_repository, token_service)
        app.dependency_overrides[get_login_service] = lambda: login_service
        api_password = "ApiTest8"
        with TestClient(app) as client:
            response = client.post(
                "/auth/register", json={"username": "api_eight", "password": api_password}
            )
            assert response.status_code == 201
            public_user = response.json()
            login_response = client.post(
                "/auth/login", json={"username": "api_eight", "password": api_password}
            )
            assert login_response.status_code == 200
            login_body = login_response.json()
            assert set(login_body) == {"access_token", "token_type", "expires_in", "user"}
            assert login_body["user"] == public_user
            assert login_body["expires_in"] == 259200
            assert login_body["token_type"] == "bearer"
            assert login_response.headers["cache-control"] == "no-store"
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT user_id, token_hash FROM `{name}`.login_sessions")
                    saved = cursor.fetchall()
                    assert saved == [(public_user["user_id"], token_service.hash_token(login_body["access_token"]))]
            # 密码错误、用户不存在、旧用户没有密码都返回同一结果。
            for username, candidate in (
                ("api_eight", "WrongPassword8"),
                ("missing_user", api_password),
                ("legacy", api_password),
                ("missing_user", "dummy-account-placeholder"),
            ):
                rejected_login = client.post(
                    "/auth/login", json={"username": username, "password": candidate}
                )
                assert rejected_login.status_code == 401
                assert rejected_login.json() == {"detail": "用户名或密码错误。"}
                assert candidate not in rejected_login.text
            for invalid_body, field in (
                ({"password": api_password}, "username"),
                ({"username": "api_eight"}, "password"),
                ({"username": "api_eight", "password": {"secret": api_password}}, "password"),
            ):
                invalid_login = client.post("/auth/login", json=invalid_body)
                assert invalid_login.status_code == 422
                assert api_password not in invalid_login.text
                assert invalid_login.json()["detail"][0]["loc"] == ["body", field]
            duplicate_response = client.post(
                "/auth/register", json={"username": "api_eight", "password": api_password}
            )
            assert duplicate_response.status_code == 409
            assert duplicate_response.json() == {"detail": "用户名已存在，请使用其他用户名。"}
            for invalid_password in ("x" * 7, "x" * 33):
                invalid_response = client.post(
                    "/auth/register", json={"username": "api_invalid", "password": invalid_password}
                )
                assert invalid_response.status_code == 400
                assert invalid_response.json() == {"detail": "用户名或密码不符合要求。"}
                assert invalid_password not in invalid_response.text

            # 仅模拟本次连接失败，不停止 MySQL，不影响任何真实用户。
            private_detail = "TEST_ONLY_PRIVATE_DATABASE_DETAIL"
            with patch.object(user_repository, "get_connection", side_effect=MySQLError(private_detail, errno=2003)):
                unavailable_response = client.post(
                    "/auth/register", json={"username": "api_unavailable", "password": api_password}
                )
            assert unavailable_response.status_code == 503
            assert unavailable_response.json() == {"detail": "请稍后重试。"}
            assert private_detail not in unavailable_response.text
            with patch.object(user_repository, "get_connection", side_effect=MySQLError(private_detail, errno=2003)):
                unavailable_login = client.post(
                    "/auth/login", json={"username": "api_eight", "password": api_password}
                )
            assert unavailable_login.status_code == 503
            assert unavailable_login.json() == {"detail": "请稍后重试。"}
            assert private_detail not in unavailable_login.text
            with patch.object(login_session_repository, "get_connection", side_effect=MySQLError(private_detail, errno=2003)):
                unavailable_storage = client.post(
                    "/auth/login", json={"username": "api_eight", "password": api_password}
                )
            assert unavailable_storage.status_code == 503
            assert unavailable_storage.json() == {"detail": "请稍后重试。"}
            assert private_detail not in unavailable_storage.text
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT COUNT(*) FROM `{name}`.login_sessions")
                    assert cursor.fetchone()[0] == 1
        assert response.status_code == 201
        public_user = response.json()
        assert set(public_user) == {"user_id", "username"}
        assert public_user["username"] == "api_eight"
        api_row = repository.get_user_auth("api_eight")
        assert api_row[0] == public_user["user_id"]
        assert api_row[2] != api_password
        assert passwords.verify_password(api_password, api_row[2])
        assert repository.get_user_auth("api_invalid") is None
        assert repository.get_user_auth("api_unavailable") is None
        assert repository.get_user_auth("missing_user") is None
        assert repository.get_user_auth("legacy") == (legacy_id, "legacy", None)
        assert asyncio.run(service.authenticate("api_eight", api_password)) == (api_row[0], "api_eight")
        print("PASS: real MySQL migration, registration HTTP 201/409/400, login HTTP 200 with persisted hashed token, 3-day expiry and no-store headers, 401/422 rejection, sanitized 503 for authentication/storage failures, no login rows on failure.")
    finally:
        if created:
            assert re.fullmatch(r"_codex_auth_check_[0-9a-f]{32}", name)
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{name}`")
            print("Temporary password verification database removed.")


if __name__ == "__main__":
    main()
