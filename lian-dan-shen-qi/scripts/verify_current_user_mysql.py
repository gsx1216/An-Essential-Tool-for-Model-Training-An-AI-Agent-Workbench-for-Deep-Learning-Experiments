"""独立临时库验证有效凭证查询及用户手写的 CurrentUserService。"""

import asyncio
from pathlib import Path
import re
from uuid import uuid4

from dotenv import load_dotenv
from mysql.connector import Error as MySQLError

from app import database
from app.config import get_mysql_password
from app.errors import InvalidTokenError
from app.repositories.login_session_repository import LoginSessionRepository
from app.repositories.user_repository import UserRepository
from app.security.tokens import TokenService
from app.services.current_user_service import CurrentUserService


ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / "config" / ".env", override=False, interpolate=False)
    password = get_mysql_password()
    name = "_codex_identity_check_" + uuid4().hex
    assert re.fullmatch(r"_codex_identity_check_[0-9a-f]{32}", name)
    created = False

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
                cursor.execute(sql("create_login_sessions.sql"))
            connection.commit()

        users = UserRepository(password)
        for attribute, value in vars(users).copy().items():
            if attribute.endswith("_sql") and isinstance(value, str):
                setattr(users, attribute, value.replace("experiment_assistant", name))
        alice_id = users.create_user("identity_alice")
        bob_id = users.create_user("identity_bob")
        repository = LoginSessionRepository(password)
        repository.save_session_sql = sql("save_login_session.sql")
        repository.get_active_user_sql = sql("get_active_login_user.sql")
        tokens = TokenService()

        raw_tokens = {label: tokens.create_token() for label in ("alice", "bob", "revoked", "expired", "boundary")}
        alice_hash = tokens.hash_token(raw_tokens["alice"])
        bob_hash = tokens.hash_token(raw_tokens["bob"])
        revoked_hash = tokens.hash_token(raw_tokens["revoked"])
        expired_hash = tokens.hash_token(raw_tokens["expired"])
        boundary_hash = tokens.hash_token(raw_tokens["boundary"])
        alice_session = repository.save_session(alice_id, alice_hash, tokens.get_expires_at())
        repository.save_session(bob_id, bob_hash, tokens.get_expires_at())
        revoked_session = repository.save_session(alice_id, revoked_hash, tokens.get_expires_at())

        with database.get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE `{name}`.login_sessions SET revoked_at = UTC_TIMESTAMP(6) WHERE id = %s",
                    (revoked_session,),
                )
                # 到期记录通过测试 SQL 构造，不绕过正式保存方法的未来时间校验。
                cursor.execute(
                    f"INSERT INTO `{name}`.login_sessions (user_id, token_hash, created_at, expires_at) "
                    "VALUES (%s, %s, UTC_TIMESTAMP(6) - INTERVAL 2 DAY, UTC_TIMESTAMP(6) - INTERVAL 1 DAY)",
                    (alice_id, expired_hash),
                )
                cursor.execute(
                    f"INSERT INTO `{name}`.login_sessions (user_id, token_hash, created_at, expires_at) "
                    "VALUES (%s, %s, UTC_TIMESTAMP(6) - INTERVAL 1 HOUR, UTC_TIMESTAMP(6))",
                    (alice_id, boundary_hash),
                )
                cursor.execute(f"SELECT expires_at FROM `{name}`.login_sessions WHERE id = %s", (alice_session,))
                original_expiry = cursor.fetchone()[0]
            connection.commit()

        # 每次调用打开新的连接，确认提交后的数据确实可读。
        assert repository.get_active_user(alice_hash) == (alice_id, "identity_alice")
        assert repository.get_active_user(bob_hash) == (bob_id, "identity_bob")
        assert repository.get_active_user(tokens.hash_token(tokens.create_token())) is None
        assert repository.get_active_user(expired_hash) is None
        assert repository.get_active_user(boundary_hash) is None
        assert repository.get_active_user(revoked_hash) is None

        service = CurrentUserService(repository, tokens)
        assert asyncio.run(service.get_current_user(raw_tokens["alice"])) == (alice_id, "identity_alice")
        assert asyncio.run(service.get_current_user(raw_tokens["bob"])) == (bob_id, "identity_bob")
        for token in (tokens.create_token(), raw_tokens["expired"], raw_tokens["boundary"],
                      raw_tokens["revoked"], "", "A" * 42, "A" * 42 + "!"):
            try:
                asyncio.run(service.get_current_user(token))
            except InvalidTokenError:
                pass
            else:
                raise AssertionError("CurrentUserService accepted an invalid token")

        # 用临时库中不存在的表触发真实 1146，必须透传数据库异常，不能冒充凭证失效。
        original_query = repository.get_active_user_sql
        try:
            repository.get_active_user_sql = original_query.replace(".login_sessions", "._missing_login_sessions")
            try:
                asyncio.run(service.get_current_user(raw_tokens["alice"]))
            except MySQLError as error:
                assert error.errno == 1146
            else:
                raise AssertionError("A real database failure did not propagate")
        finally:
            repository.get_active_user_sql = original_query

        with database.get_connection(password) as connection:
            with connection.cursor() as cursor:
                # 北京时间的 MySQL 连接也必须正确比较按 UTC 保存的 DATETIME。
                cursor.execute("SET time_zone = '+08:00'")
                cursor.execute(repository.get_active_user_sql, (alice_hash,))
                assert cursor.fetchone() == (alice_id, "identity_alice")
                cursor.execute(repository.get_active_user_sql, (expired_hash,))
                assert cursor.fetchone() is None
                cursor.execute(f"SELECT COUNT(*) FROM `{name}`.login_sessions")
                assert cursor.fetchone()[0] == 5
                cursor.execute(f"SELECT expires_at FROM `{name}`.login_sessions WHERE id = %s", (alice_session,))
                assert cursor.fetchone()[0] == original_expiry

        print("PASS: real CurrentUserService and repository, user separation, unknown/expired/revoked/malformed rejection, expiry boundary, real database error propagation, UTC comparison, no extra sessions or expiry extension.")
        print("HTTP bearer extraction and /auth/me are tested by scripts.verify_current_user_api_mysql.")
    finally:
        if created:
            assert re.fullmatch(r"_codex_identity_check_[0-9a-f]{32}", name)
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{name}`")
            print("Temporary identity verification database removed.")


if __name__ == "__main__":
    main()
