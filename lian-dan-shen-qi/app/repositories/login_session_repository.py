"""登录记录存储：保存哈希、查询有效身份、原子撤销单个凭证；不接受客户端用户 ID 指定身份。"""
from app.logging_config import logged_operation


import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.database import get_connection
from app.validators import validate_positive_integer


def _validate_token_hash(token_hash: str) -> str:
    """仓储只接受哈希，不接收、打印或保存原始 Token。"""
    if not isinstance(token_hash, str) or re.fullmatch(r"[0-9a-f]{64}", token_hash) is None:
        raise ValueError("必须传入 64 位小写十六进制 Token 哈希，不能传入原始 Token。")
    return token_hash


def _validate_login_session_parameters(
    user_id: int, token_hash: str, expires_at: datetime,
) -> tuple[int, str, datetime]:
    """只接受哈希与带时区的时间，入库前统一转换为 UTC。"""
    user_id = validate_positive_integer(user_id, "用户 ID")
    token_hash = _validate_token_hash(token_hash)
    if not isinstance(expires_at, datetime) or expires_at.utcoffset() is None:
        raise ValueError("过期时间必须是包含时区的 datetime。")
    expires_at = expires_at.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if not now < expires_at <= now + timedelta(days=3):
        raise ValueError("过期时间必须在未来，且不能超过当前时间之后的 3 天。")
    # MySQL DATETIME 不保存时区标记，先转 UTC 再移除标记；读取时也要按 UTC 理解。
    return user_id, token_hash, expires_at.replace(tzinfo=None)


class LoginSessionRepository:
    @logged_operation("login_session_repository.__init__", log_success=False, expected_errors=())
    def __init__(self, password: str):
        self.password = password
        project_root = Path(__file__).resolve().parents[2]
        self.save_session_sql = (
            project_root / "sql" / "save_login_session.sql"
        ).read_text(encoding="utf-8")
        self.get_active_user_sql = (
            project_root / "sql" / "get_active_login_user.sql"
        ).read_text(encoding="utf-8")
        self.revoke_session_sql = (
            project_root / "sql" / "revoke_login_session.sql"
        ).read_text(encoding="utf-8")

    @logged_operation("login_session_repository.revoke_session")
    def revoke_session(self, token_hash: str) -> bool:
        """原子撤销一个仍有效的凭证；提交成功返回 True，未匹配有效记录返回 False。"""
        token_hash = _validate_token_hash(token_hash)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(self.revoke_session_sql, (token_hash,))
                    revoked = cursor.rowcount == 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return revoked

    @logged_operation("login_session_repository.get_active_user")
    def get_active_user(self, token_hash: str) -> tuple[int, str] | None:
        """返回 (用户 ID, 用户名)；未知、到期、已撤销的凭证统一返回 None。"""
        token_hash = _validate_token_hash(token_hash)
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                # SQL 同时核对哈希、有效期、撤销状态以及用户关联。
                cursor.execute(self.get_active_user_sql, (token_hash,))
                return cursor.fetchone()

    @logged_operation("login_session_repository.save_session")
    def save_session(self, user_id: int, token_hash: str, expires_at: datetime) -> int:
        """保存一条登录记录，返回数据库生成的内部记录 ID。"""
        # 后续 user_id 必须来自账号密码验证结果，不能相信客户端传入的用户 ID。
        parameters = _validate_login_session_parameters(user_id, token_hash, expires_at)

        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    # TODO 1：用 cursor.execute 执行 self.save_session_sql。
                    # 参数已准备为 parameters，直接作为第二个参数传入，不拼接 SQL。
                    cursor.execute(
                        self.save_session_sql,
                        parameters,
                    )
                    # TODO 2：读取 cursor.lastrowid，保存到 login_session_id。
                    login_session_id = cursor.lastrowid
                    # TODO 3：如果 login_session_id 是 None，抛出 RuntimeError。
                    # 提示可写“数据库没有返回登录记录 ID。”
                    if login_session_id is None:
                        raise RuntimeError("无记录")

                # TODO 4：离开 cursor 的 with 后，提交事务。
                connection.commit()
            except Exception:
                # TODO 5：回滚事务；下面的 raise 会把异常继续交给调用者。
                connection.rollback()
                raise

        # TODO 6：返回 login_session_id，不返回原始 Token 或密码。
        return login_session_id
