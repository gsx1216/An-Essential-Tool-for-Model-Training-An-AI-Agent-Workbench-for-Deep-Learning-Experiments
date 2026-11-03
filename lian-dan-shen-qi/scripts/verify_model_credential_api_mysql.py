"""隔离挂载真实个人Key路由并联调临时MySQL；不修改主应用或真实配置。"""

import asyncio
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app import config, database
from app.api import dependencies
from app.api.auth_routes import router as auth_router
from app.api.error_handlers import request_validation_exception_handler
from app.api.model_credential_routes import router as credential_router
from app.api.request_logging import RequestLoggingMiddleware
from app.errors import ModelCredentialConfigurationError, ModelCredentialNotConfiguredError
from app.logging_config import DEFAULT_LOG_DIR, configure_logging
from app.repositories.login_session_repository import LoginSessionRepository
from app.repositories.user_repository import UserRepository
from app.security.passwords import PasswordService
from app.security.tokens import TokenService
from app.services.auth_service import AuthService
from app.services.current_user_service import CurrentUserService
from app.services.login_service import LoginService
from app.services.user_model_gateway_factory import UserModelGatewayFactory


ROOT = Path(__file__).resolve().parents[1]
PATH = "/me/model-credentials/deepseek"


def main():
    password = config.get_mysql_password()
    name = "_codex_credential_api_" + uuid4().hex
    assert re.fullmatch(r"_codex_credential_api_[0-9a-f]{32}", name)
    created = False
    factory = dependencies.get_model_credential_service
    factory.cache_clear()

    def sql(filename):
        return (ROOT / "sql" / filename).read_text(encoding="utf-8").replace("experiment_assistant", name)

    def target_repository(repository):
        for attribute, value in vars(repository).copy().items():
            if attribute.endswith("_sql") and isinstance(value, str):
                setattr(repository, attribute, value.replace("experiment_assistant", name))
        return repository

    try:
        with database.get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                created = True
                cursor.execute(sql("init.sql"))
                while cursor.nextset():
                    pass
                cursor.execute(sql("create_login_sessions.sql"))
                cursor.execute(sql("create_model_credentials.sql"))
            connection.commit()

        users = target_repository(UserRepository(password))
        logins = target_repository(LoginSessionRepository(password))
        auth = AuthService(users, PasswordService())
        token_service = TokenService()
        login = LoginService(auth, logins, token_service)
        current = CurrentUserService(logins, token_service)
        master_key = Fernet.generate_key().decode("ascii")
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            configure_logging(directory / "logs", console=False, force=True)
            try:
                with patch.object(config, "ENV_FILE", directory / ".env"):
                    with patch.dict(os.environ, {"MODEL_CREDENTIAL_ENCRYPTION_KEY": master_key}):
                        with patch.object(dependencies, "get_mysql_password", return_value=password):
                            credentials = factory()
                            assert factory() is credentials
                target_repository(credentials.repository)

                # 路由尚未由用户挂载到main，明确使用独立验证应用。
                app = FastAPI()
                app.add_middleware(RequestLoggingMiddleware)
                app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
                app.include_router(auth_router)
                app.include_router(credential_router)
                app.dependency_overrides[dependencies.get_auth_service] = lambda: auth
                app.dependency_overrides[dependencies.get_login_service] = lambda: login
                app.dependency_overrides[dependencies.get_current_user_service] = lambda: current
                app.dependency_overrides[factory] = lambda: credentials
                keys = ["sk-TEST_ONLY_API_ALICE", "sk-TEST_ONLY_API_ALICE_NEW", "sk-TEST_ONLY_API_BOB"]
                # 实际个人网关工厂 + 实际凭据业务/数据库；模型构造器是纯本地假对象。
                gateway_builder = Mock(side_effect=lambda *, api_key: SimpleNamespace(api_key=api_key))
                user_gateways = UserModelGatewayFactory(credentials, gateway_factory=gateway_builder)
                secrets_to_check = [master_key, "Test1234"] + keys
                with TestClient(app) as client:
                    identities = []
                    headers = []
                    for username in ("credential_api_alice", "credential_api_bob"):
                        response = client.post("/auth/register", json={"username": username, "password": "Test1234"})
                        assert response.status_code == 201
                        identities.append(response.json()["user_id"])
                        response = client.post("/auth/login", json={"username": username, "password": "Test1234"})
                        assert response.status_code == 200
                        token = response.json()["access_token"]
                        secrets_to_check.append(token)
                        headers.append({"Authorization": "Bearer " + token})
                    alice, bob = identities
                    alice_headers, bob_headers = headers

                    def request(method, owner_headers=alice_headers, **options):
                        return client.request(method, PATH, headers=owner_headers, **options)

                    def assert_empty_success(response):
                        assert response.status_code == 204 and response.content == b""
                        assert response.headers["Cache-Control"] == "no-store"
                        assert response.headers["Pragma"] == "no-cache"
                        assert response.headers["X-Request-ID"]

                    for method, options in (("PUT", {"json": {"api_key": keys[0]}}), ("GET", {}), ("DELETE", {})):
                        assert request(method, {}, **options).status_code == 401
                    response = request("GET")
                    assert response.status_code == 200
                    assert response.json() == {"provider": "deepseek", "configured": False}
                    assert response.headers["Cache-Control"] == "no-store"
                    try:
                        asyncio.run(user_gateways.create_for_user(alice))
                    except ModelCredentialNotConfiguredError:
                        pass
                    else:
                        raise AssertionError("Unconfigured user must not receive a gateway")
                    gateway_builder.assert_not_called()
                    assert_empty_success(request("PUT", json={"api_key": keys[0]}))
                    encrypted = credentials.repository.get_key(alice)
                    secrets_to_check.append(encrypted)
                    assert isinstance(encrypted, str) and keys[0] not in encrypted
                    assert asyncio.run(credentials.get_api_key(alice)) == keys[0]
                    first_alice_gateway = asyncio.run(user_gateways.create_for_user(alice))
                    assert first_alice_gateway.api_key == keys[0]
                    assert request("GET").json()["configured"] is True
                    assert request("GET", bob_headers).json()["configured"] is False
                    assert_empty_success(request("PUT", json={"api_key": keys[1]}))
                    assert asyncio.run(credentials.get_api_key(alice)) == keys[1]
                    updated_alice_gateway = asyncio.run(user_gateways.create_for_user(alice))
                    assert updated_alice_gateway is not first_alice_gateway
                    assert updated_alice_gateway.api_key == keys[1]
                    assert_empty_success(request("PUT", bob_headers, json={"api_key": keys[2]}))
                    assert asyncio.run(credentials.get_api_key(bob)) == keys[2]
                    bob_gateway = asyncio.run(user_gateways.create_for_user(bob))
                    assert bob_gateway.api_key == keys[2]
                    assert bob_gateway is not updated_alice_gateway

                    for bad_key in ("", " bad ", "a" * 4097):
                        response = request("PUT", json={"api_key": bad_key})
                        assert response.status_code == 400
                    for payload in ({}, {"api_key": None}, {"api_key": {"secret": keys[0]}},
                                    {"api_key": keys[0], "user_id": bob}, {"api_key": keys[0], "master_key": master_key}):
                        response = request("PUT", json=payload)
                        assert response.status_code == 422
                        assert all(secret not in response.text for secret in keys + [master_key])
                    assert request("GET", bob_headers, params={"user_id": alice}).json()["configured"] is True
                    assert asyncio.run(credentials.get_api_key(alice)) == keys[1]
                    assert asyncio.run(credentials.get_api_key(bob)) == keys[2]

                    cases = (("save_key_sql", "PUT", {"json": {"api_key": keys[0]}}),
                             ("get_key_sql", "GET", {}), ("delete_key_sql", "DELETE", {}))
                    for attribute, method, options in cases:
                        broken = getattr(credentials.repository, attribute).replace(".model_credentials", "._missing_model_credentials")
                        with patch.object(credentials.repository, attribute, broken):
                            response = request(method, **options)
                        assert response.status_code == 503
                        assert response.json() == {"detail": "请稍后重试。"}
                        assert response.headers["X-Request-ID"]
                    assert asyncio.run(credentials.get_api_key(alice)) == keys[1]

                    # 真正走用户工厂的配置失败分支，确认依赖阶段返回通用503。
                    del app.dependency_overrides[factory]
                    factory.cache_clear()
                    try:
                        with patch.object(dependencies, "get_mysql_password", return_value=password):
                            with patch.object(dependencies, "get_model_encryption_key", side_effect=ModelCredentialConfigurationError("TEST_ONLY_PRIVATE_CONFIG")):
                                response = request("GET")
                        assert response.status_code == 503
                        assert response.json() == {"detail": "请稍后重试。"}
                    finally:
                        app.dependency_overrides[factory] = lambda: credentials
                        factory.cache_clear()

                    with patch.object(credentials, "is_configured", new=AsyncMock(side_effect=RuntimeError(keys[0]))):
                        response = request("GET")
                    assert response.status_code == 500
                    assert response.json() == {"detail": "请稍后重试。"}
                    assert_empty_success(request("DELETE"))
                    assert_empty_success(request("DELETE"))
                    assert request("GET").json()["configured"] is False
                    created_before_missing = gateway_builder.call_count
                    try:
                        asyncio.run(user_gateways.create_for_user(alice))
                    except ModelCredentialNotConfiguredError:
                        pass
                    else:
                        raise AssertionError("Deleted credentials must not reuse an old gateway")
                    assert gateway_builder.call_count == created_before_missing
                    assert request("GET", bob_headers).json()["configured"] is True
                    assert asyncio.run(credentials.get_api_key(bob)) == keys[2]

                contents = "\n".join(path.read_text(encoding="utf-8") for path in (directory / "logs").rglob("*.txt"))
                assert all(secret not in contents for secret in secrets_to_check)
            finally:
                configure_logging(DEFAULT_LOG_DIR, console=False, force=True)

        print("PASS: isolated real credential HTTP + actual factory + authenticated users + Fernet + temporary MySQL: save/update/status/delete/repeat-delete, user isolation, safe 400/401/422/503/500, private cache headers and secret-free logs.")
        print("PASS: actual UserModelGatewayFactory reads personal keys from temporary MySQL: unconfigured rejection, user isolation, updated keys and rejection after deletion. Model constructors are fakes.")
        print("Verification uses an isolated HTTP app and temporary master key, not production configuration. No model was called.")
    finally:
        factory.cache_clear()
        if created:
            assert re.fullmatch(r"_codex_credential_api_[0-9a-f]{32}", name)
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{name}`")
            print("Temporary credential API database removed.")


if __name__ == "__main__":
    main()
