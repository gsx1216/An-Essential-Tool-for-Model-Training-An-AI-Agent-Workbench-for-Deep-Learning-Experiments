from app.logging_config import logged_operation

from getpass import getpass
from pathlib import Path
from uuid import uuid4

import mysql.connector
from mysql.connector import IntegrityError, errorcode

from app.database import get_connection
from app.validators import normalize_required_text, normalize_session_id, validate_positive_integer


class SessionRepository:
    @logged_operation("session_repository.__init__", log_success=False, expected_errors=())
    def __init__(self, password: str):
        self.password = password
        sql_root = Path(__file__).resolve().parents[2] / "sql"
        self.sql_session_sql = (sql_root / "create_session.sql").read_text(encoding="utf-8")
        self.list_sessions_sql = (sql_root / "list_sessions.sql").read_text(encoding="utf-8")
        self.get_session_sql = (sql_root / "get_session.sql").read_text(encoding="utf-8")
        self.get_user_sql = (sql_root / "get_user.sql").read_text(encoding="utf-8")
        self.lock_session_sql = (sql_root / "lock_session.sql").read_text(encoding="utf-8")
        self.delete_session_turns_sql = (sql_root / "delete_session_turns.sql").read_text(encoding="utf-8")
        self.delete_session_messages_sql = (sql_root / "delete_session_messages.sql").read_text(encoding="utf-8")
        self.delete_session_sql = (sql_root / "delete_session.sql").read_text(encoding="utf-8")

    def _require_user(self, cursor, user_id: int) -> None:
        cursor.execute(self.get_user_sql, (user_id,))
        if cursor.fetchone() is None:
            raise ValueError(f"用户 ID {user_id} 不存在，请先创建用户或输入已有用户 ID。")

    @logged_operation("session_repository.create_session")
    def create_session(self, user_id: int, title: str = "新聊天") -> str:
        user_id = validate_positive_integer(user_id, "用户 ID")
        title = normalize_required_text(title, "会话标题", 200)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    # 输入一个数字不代表该用户存在，必须实际查询用户表。
                    self._require_user(cursor, user_id)
                    session_id = uuid4().hex
                    cursor.execute(self.sql_session_sql, (session_id, user_id, title))
                connection.commit()
            except IntegrityError as error:
                connection.rollback()
                if error.errno == errorcode.ER_NO_REFERENCED_ROW_2:
                    raise ValueError("关联用户不存在，无法创建会话。") from error
                raise
            except Exception:
                connection.rollback()
                raise
        return session_id

    @logged_operation("session_repository.list_sessions")
    def list_sessions(self, user_id: int) -> list[tuple[str, str]]:
        user_id = validate_positive_integer(user_id, "用户 ID")
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.list_sessions_sql, (user_id,))
                return cursor.fetchall()

    @logged_operation("session_repository.get_session")
    def get_session(self, user_id: int, session_id: str) -> tuple[str, str] | None:
        user_id = validate_positive_integer(user_id, "用户 ID")
        session_id = normalize_session_id(session_id)
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.get_session_sql, (user_id, session_id))
                # 查询本身限制归属；会话不存在或属于别人时统一返回 None。
                return cursor.fetchone()

    @logged_operation("session_repository.delete_session")
    def delete_session(self, user_id: int, session_id: str) -> bool:
        """事务删除当前用户的整段聊天；不存在或不属于该用户时统一返回 False。"""
        user_id = validate_positive_integer(user_id, "用户 ID")
        session_id = normalize_session_id(session_id)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    # 与消息写入使用相同的会话行锁，避免检查归属后数据发生变化。
                    cursor.execute(self.lock_session_sql, (user_id, session_id))
                    if cursor.fetchone() is None:
                        connection.rollback()
                        return False

                    # 外键方向：轮次引用消息，消息引用会话，必须按此顺序删除。
                    # 每句 SQL 仍同时限制用户和会话，不能只凭一个会话 UUID 删除。
                    cursor.execute(self.delete_session_turns_sql, (user_id, session_id))
                    cursor.execute(self.delete_session_messages_sql, (user_id, session_id))
                    cursor.execute(self.delete_session_sql, (user_id, session_id))
                    if cursor.rowcount != 1:
                        raise ValueError("会话状态已变化，本次删除已撤销，请重试。")
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise


if __name__ == "__main__":
    password = getpass("请输入 MySQL 密码：")
    try:
        repository = SessionRepository(password)
        user_id = int(input("请输入已有用户 ID：").strip())
        session_id = repository.create_session(user_id=user_id, title="用户会话测试")
        print("当前用户的会话列表：", repository.list_sessions(user_id))
        print("会话 ID：", session_id)
        print("当前用户查询：", repository.get_session(user_id, session_id))
        other_user_id = int(input("输入另一个不同的已有用户 ID：").strip())
        if other_user_id == user_id:
            raise ValueError("比较归属时，请输入另一个不同的用户 ID。")
        print("其他用户查询：", repository.get_session(other_user_id, session_id))
    except (ValueError, mysql.connector.Error) as error:
        print("操作失败：", error)
