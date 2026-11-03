"""在独立临时库验证真实的登录记录仓储，包括提交和失败回滚。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from unittest.mock import patch
from uuid import uuid4

from dotenv import load_dotenv
from mysql.connector import Error as MySQLError, IntegrityError, errorcode

from app import database
from app.config import get_mysql_password
from app.repositories import login_session_repository
from app.repositories.login_session_repository import LoginSessionRepository, _validate_login_session_parameters
from app.repositories.user_repository import UserRepository
from app.security.passwords import PasswordService
from app.security.tokens import TokenService


ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / "config" / ".env", override=False, interpolate=False)
    password = get_mysql_password()
    name = "_codex_login_check_" + uuid4().hex
    assert re.fullmatch(r"_codex_login_check_[0-9a-f]{32}", name)
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
            connection.commit()
            # 重复创建必须成功，且不会清空已有记录。
            with patch.object(database, "_read_sql", side_effect=sql):
                database._ensure_login_sessions(connection)
                database._ensure_login_sessions(connection)

        users = UserRepository(password)
        for attribute, value in vars(users).copy().items():
            if attribute.endswith("_sql") and isinstance(value, str):
                setattr(users, attribute, value.replace("experiment_assistant", name))
        user_id = users.create_registered_user("login_storage_test", PasswordService().hash_password("Test1234"))
        tokens = TokenService()
        raw_token = tokens.create_token()
        token_hash = tokens.hash_token(raw_token)
        expires_at = tokens.get_expires_at()
        parameters = _validate_login_session_parameters(user_id, token_hash, expires_at)

        repository = LoginSessionRepository(password)
        repository.save_session_sql = sql("save_login_session.sql")
        row_id = repository.save_session(user_id, token_hash, expires_at)
        assert row_id is not None

        # 直接调用真实仓储，确保约束错误继续抛出，而不是被吞掉。
        for values, expected_errno in (
            ((user_id, token_hash, expires_at), errorcode.ER_DUP_ENTRY),
            ((user_id + 10000, tokens.hash_token(tokens.create_token()), expires_at), errorcode.ER_NO_REFERENCED_ROW_2),
        ):
            try:
                repository.save_session(*values)
            except IntegrityError as error:
                assert error.errno == expected_errno
            else:
                raise AssertionError("Repository did not propagate a constraint failure")

        # 真实插入，但模拟 commit 报错；调用者必须收到异常，新记录不能保留。
        rollback_hash = tokens.hash_token(tokens.create_token())
        transaction = database.get_connection(password)
        simulated_failure = MySQLError("TEST_ONLY_COMMIT_FAILURE", errno=2013)
        try:
            with patch.object(transaction, "commit", side_effect=simulated_failure):
                with patch.object(login_session_repository, "get_connection", return_value=transaction):
                    try:
                        repository.save_session(user_id, rollback_hash, expires_at)
                    except MySQLError as error:
                        assert error is simulated_failure
                    else:
                        raise AssertionError("Commit failure was reported as a successful save")
        finally:
            transaction.close()

        # 新连接能够查到已提交记录；切换为北京时间会话也不能改变 UTC DATETIME 的值。
        with database.get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET time_zone = '+08:00'")
                cursor.execute(f"SELECT user_id, token_hash, expires_at, revoked_at FROM `{name}`.login_sessions WHERE id = %s", (row_id,))
                row = cursor.fetchone()
                assert row == (user_id, token_hash, expires_at.replace(tzinfo=None), None)
                assert raw_token not in repr(row)
                cursor.execute(f"SELECT COUNT(*) FROM `{name}`.login_sessions WHERE token_hash = %s", (rollback_hash,))
                assert cursor.fetchone()[0] == 0

                # 相同哈希不允许重复；不存在的用户不允许保存登录记录。
                cases = (
                    (parameters, errorcode.ER_DUP_ENTRY),
                    ((user_id + 10000, tokens.hash_token(tokens.create_token()), parameters[2]), errorcode.ER_NO_REFERENCED_ROW_2),
                )
                for values, expected_errno in cases:
                    try:
                        cursor.execute(sql("save_login_session.sql"), values)
                    except IntegrityError as error:
                        assert error.errno == expected_errno
                        connection.rollback()
                    else:
                        raise AssertionError("A database constraint was not enforced")

                # 验证回滚后新记录没有保存。
                rollback_hash = tokens.hash_token(tokens.create_token())
                cursor.execute(sql("save_login_session.sql"), (user_id, rollback_hash, parameters[2]))
                connection.rollback()
                cursor.execute(f"SELECT COUNT(*) FROM `{name}`.login_sessions")
                assert cursor.fetchone()[0] == 1
                connection.commit()

            with patch.object(database, "_read_sql", side_effect=sql):
                database._ensure_login_sessions(connection)
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM `{name}`.login_sessions")
                assert cursor.fetchone()[0] == 1

        print("PASS: real repository save and durable commit, UTC round-trip, duplicate hash and missing-user errors propagate, simulated commit failure rolls back, repeat initialization preserves rows.")
        print("Repository storage is tested separately from HTTP token issuance.")
    finally:
        if created:
            assert re.fullmatch(r"_codex_login_check_[0-9a-f]{32}", name)
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{name}`")
            print("Temporary login verification database removed.")


if __name__ == "__main__":
    main()
