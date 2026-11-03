"""用真实临时 MySQL 库验证可读日志、故障定位和接口响应，不调用模型。"""

from pathlib import Path
import re
from tempfile import TemporaryDirectory
from uuid import uuid4

from dotenv import load_dotenv
from fastapi.testclient import TestClient

from app import database
from app.api.dependencies import get_login_service
from app.config import get_mysql_password
from app.logging_config import DEFAULT_LOG_DIR, configure_logging
from app.main import app
from app.repositories.login_session_repository import LoginSessionRepository
from app.repositories.user_repository import UserRepository
from app.security.passwords import PasswordService
from app.security.tokens import TokenService
from app.services.auth_service import AuthService
from app.services.login_service import LoginService


ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / "config" / ".env", override=False, interpolate=False)
    password = get_mysql_password()
    name = "_codex_readable_log_check_" + uuid4().hex
    assert re.fullmatch(r"_codex_readable_log_check_[0-9a-f]{32}", name)
    created = False
    original_overrides = dict(app.dependency_overrides)

    def sql(filename):
        return (ROOT / "sql" / filename).read_text(encoding="utf-8").replace("experiment_assistant", name)

    try:
        with database.get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                created = True
                cursor.execute(sql("init.sql"))
                while cursor.nextset():
                    pass
            connection.commit()

        users = UserRepository(password)
        for attribute, value in vars(users).copy().items():
            if attribute.endswith("_sql") and isinstance(value, str):
                setattr(users, attribute, value.replace("experiment_assistant", name))
        passwords = PasswordService()
        user_id = users.create_registered_user("readable_log_test", passwords.hash_password("Test1234"))
        sessions = LoginSessionRepository(password)
        sessions.save_session_sql = sql("save_login_session.sql")
        sessions.get_active_user_sql = sql("get_active_login_user.sql")
        tokens = TokenService()
        service = LoginService(AuthService(users, passwords), sessions, tokens)
        app.dependency_overrides[get_login_service] = lambda: service

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            configure_logging(directory, console=False, force=True, log_format="text")
            try:
                with TestClient(app) as client:
                    # 此临时库尚未创建登录记录表，触发真实的 MySQL 1146。
                    failed = client.post("/auth/login", json={"username": "readable_log_test", "password": "Test1234"})
                    assert failed.status_code == 503
                    assert failed.json() == {"detail": "请稍后重试。"}
                    request_id = failed.headers["X-Request-ID"]
                    text = "\n".join(path.read_text(encoding="utf-8") for path in directory.rglob("*.txt"))
                    assert "request_id=" + request_id in text
                    assert "数据库表不存在" in text and "errno=1146" in text
                    assert "[app/repositories/login_session_repository.py]" in text
                    assert re.search(r"login_session_repository:save_session:[1-9][0-9]*", text)
                    assert "Test1234" not in text and password not in text
                    assert "status_code=503" in text

                    with database.get_connection(password) as connection:
                        with connection.cursor() as cursor:
                            cursor.execute(sql("create_login_sessions.sql"))
                        connection.commit()
                    successful = client.post("/auth/login", json={"username": "readable_log_test", "password": "Test1234"})
                    assert successful.status_code == 200
                    result = successful.json()
                    assert result["user"]["user_id"] == user_id
                    text = "\n".join(path.read_text(encoding="utf-8") for path in directory.rglob("*.txt"))
                    assert result["access_token"] not in text
                    assert tokens.hash_token(result["access_token"]) not in text
                    assert "status_code=200" in text
                    assert "执行成功" in text
            finally:
                configure_logging(DEFAULT_LOG_DIR, console=False, force=True)

        print("PASS: real HTTP login and MySQL failure 1146, readable file/function/line, safe reason and trace, request ID correlation, generic 503, successful retry, no password/token disclosure.")
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
        if created:
            assert re.fullmatch(r"_codex_readable_log_check_[0-9a-f]{32}", name)
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{name}`")
            print("Temporary readable-log verification database removed.")


if __name__ == "__main__":
    main()
