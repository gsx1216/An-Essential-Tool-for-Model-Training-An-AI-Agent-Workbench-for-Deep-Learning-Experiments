"""用独立临时 MySQL 库验证登录签发业务，不调用模型或修改业务库。"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from unittest.mock import patch
from uuid import uuid4

from dotenv import load_dotenv
from mysql.connector import Error as MySQLError

from app import database
from app.config import get_mysql_password
from app.repositories import login_session_repository
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
    name = "_codex_login_service_check_" + uuid4().hex
    assert re.fullmatch(r"_codex_login_service_check_[0-9a-f]{32}", name)
    created = False

    def sql(filename):
        return (ROOT / "sql" / filename).read_text(encoding="utf-8").replace("experiment_assistant", name)

    def count_sessions():
        with database.get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM `{name}`.login_sessions")
                return cursor.fetchone()[0]

    try:
        with database.get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                created = True
                cursor.execute(sql("init.sql"))
                while cursor.nextset():
                    pass
            connection.commit()
            with patch.object(database, "_read_sql", side_effect=sql):
                database._ensure_login_sessions(connection)

        users = UserRepository(password)
        for attribute, value in vars(users).copy().items():
            if attribute.endswith("_sql") and isinstance(value, str):
                setattr(users, attribute, value.replace("experiment_assistant", name))
        passwords = PasswordService()
        user_id = users.create_registered_user("alice", passwords.hash_password("Test1234"))
        sessions = LoginSessionRepository(password)
        sessions.save_session_sql = sql("save_login_session.sql")
        tokens = TokenService()
        service = LoginService(AuthService(users, passwords), sessions, tokens)

        result = asyncio.run(service.login("  ALICE  ", "Test1234"))
        assert (result.user_id, result.username, result.expires_in, result.token_type) == (user_id, "alice", 259200, "bearer")
        assert result.access_token not in repr(result)
        with database.get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT user_id, token_hash, expires_at, revoked_at FROM `{name}`.login_sessions")
                row = cursor.fetchone()
                assert row[0] == user_id
                assert row[1] == tokens.hash_token(result.access_token)
                assert row[1] != result.access_token
                assert row[3] is None
                expires_at = row[2].replace(tzinfo=timezone.utc)
                assert datetime.now(timezone.utc) < expires_at <= datetime.now(timezone.utc) + timedelta(days=3)

        again = asyncio.run(service.login("alice", "Test1234"))
        assert again.access_token != result.access_token
        assert count_sessions() == 2

        for username, candidate in (("alice", "WrongPassword8"), ("missing", "Test1234")):
            with patch.object(tokens, "create_token", wraps=tokens.create_token) as create_token:
                try:
                    asyncio.run(service.login(username, candidate))
                except ValueError:
                    pass
                else:
                    raise AssertionError("Rejected credentials were accepted")
                create_token.assert_not_called()
            assert count_sessions() == 2

        original = MySQLError("TEST_ONLY_DATABASE_FAILURE", errno=2003)
        with patch.object(login_session_repository, "get_connection", side_effect=original):
            try:
                asyncio.run(service.login("alice", "Test1234"))
            except MySQLError as error:
                assert error is original
            else:
                raise AssertionError("Failed storage returned a successful login")
        assert count_sessions() == 2
        print("PASS: real LoginService password verification, token hashing and durable storage, canonical username, 3-day expiry, distinct repeated logins, rejection without issuance, storage error propagation.")
        print("Incoming bearer validation is tested separately by scripts.verify_current_user_api_mysql.")
    finally:
        if created:
            assert re.fullmatch(r"_codex_login_service_check_[0-9a-f]{32}", name)
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{name}`")
            print("Temporary login service verification database removed.")


if __name__ == "__main__":
    main()
