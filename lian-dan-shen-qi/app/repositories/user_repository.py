"""用户存储：参数化读写账号、校验待存哈希、处理用户名唯一约束；不负责签发 Token。"""

from app.logging_config import logged_operation

from getpass import getpass
from pathlib import Path

import mysql.connector
from mysql.connector import IntegrityError, errorcode

from app.database import get_connection
from app.errors import UsernameAlreadyExistsError
from app.security.passwords import validate_password_hash
from app.validators import normalize_required_text, validate_positive_integer


class UserRepository:
    @logged_operation("user_repository.__init__", log_success=False, expected_errors=())
    def __init__(self, password: str):
        self.password = password
        project_root = Path(__file__).resolve().parents[2]
        self.create_user_sql = (project_root / "sql" / "create_user.sql").read_text(encoding="utf-8")
        self.get_user_sql = (project_root / "sql" / "get_user.sql").read_text(encoding="utf-8")
        self.create_registered_user_sql = (project_root / "sql" / "create_registered_user.sql").read_text(encoding="utf-8")
        self.get_user_auth_sql = (project_root / "sql" / "get_user_auth.sql").read_text(encoding="utf-8")

    @logged_operation("user_repository.create_user")
    def create_user(self, username: str) -> int:
        """兼容旧开发脚本；无密码用户不能登录，正式注册用 create_registered_user。"""
        username = normalize_required_text(username, "用户名", 64)
        return self._insert_user(self.create_user_sql, (username,))

    @logged_operation("user_repository.create_registered_user")
    def create_registered_user(self, username: str, password_hash: str) -> int:
        username = normalize_required_text(username, "用户名", 64)
        password_hash = validate_password_hash(password_hash)
        return self._insert_user(self.create_registered_user_sql, (username, password_hash))

    def _insert_user(self, sql: str, parameters: tuple) -> int:
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(sql, parameters)
                    user_id = cursor.lastrowid
                    if user_id is None:
                        raise RuntimeError("数据库没有返回新用户的 ID。")
                connection.commit()
            except IntegrityError as error:
                connection.rollback()
                if error.errno == errorcode.ER_DUP_ENTRY:
                    raise UsernameAlreadyExistsError("用户名已存在，请使用其他用户名。") from error
                raise
            except Exception:
                connection.rollback()
                raise
        return user_id

    @logged_operation("user_repository.get_user")
    def get_user(self, user_id: int) -> tuple[int, str] | None:
        user_id = validate_positive_integer(user_id, "用户 ID")
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.get_user_sql, (user_id,))
                return cursor.fetchone()

    @logged_operation("user_repository.get_user_auth")
    def get_user_auth(self, username: str) -> tuple[int, str, str | None] | None:
        """内部认证数据，含密码哈希；禁止直接作为接口响应。"""
        username = normalize_required_text(username, "用户名", 64)
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.get_user_auth_sql, (username,))
                return cursor.fetchone()


if __name__ == "__main__":
    password = getpass("请输入 MySQL 密码：")
    try:
        username = input("请输入新用户名：")
        repository = UserRepository(password)
        user_id = repository.create_user(username)
        print("用户创建成功，用户 ID：", user_id)
    except (ValueError, RuntimeError, mysql.connector.Error) as error:
        print("操作失败：", error)
