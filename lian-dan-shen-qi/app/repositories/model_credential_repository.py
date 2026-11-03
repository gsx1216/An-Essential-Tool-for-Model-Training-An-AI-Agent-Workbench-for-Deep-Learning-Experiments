"""个人模型凭据仓储练习：只接收密文；核心 SQL 调用由你填写后检查。"""

import base64
import binascii
from pathlib import Path

from mysql.connector import IntegrityError, errorcode

from app.database import get_connection
from app.logging_config import logged_operation
from app.security.api_key_cipher import MAX_ENCRYPTED_KEY_LENGTH
from app.validators import validate_positive_integer


def validate_encrypted_key(value: str) -> str:
    """检查 Fernet 密文外形，防止误传明文；完整性和归属仍需由解密工具验证。"""
    if not isinstance(value, str) or not 100 <= len(value) <= MAX_ENCRYPTED_KEY_LENGTH:
        raise ValueError("必须传入加密后的模型凭据。")
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (UnicodeError, ValueError, binascii.Error):
        raise ValueError("必须传入加密后的模型凭据。") from None
    # Fernet：版本1字节 + 时间8字节 + IV16字节 + 16倍数字节密文 + HMAC32字节。
    if (
            len(decoded) < 73
            or decoded[0] != 0x80
            or (len(decoded) - 57) % 16 != 0
            or base64.urlsafe_b64encode(decoded) != encoded
    ):
        raise ValueError("模型凭据密文格式不正确。")
    return value


class ModelCredentialRepository:
    @logged_operation("model_credential_repository.__init__", log_success=False, expected_errors=())
    def __init__(self, password: str):
        self.password = password
        sql_directory = Path(__file__).resolve().parents[2] / "sql"
        self.save_key_sql = (sql_directory / "save_model_credential.sql").read_text(encoding="utf-8")
        self.get_key_sql = (sql_directory / "get_model_credential.sql").read_text(encoding="utf-8")
        self.delete_key_sql = (sql_directory / "delete_model_credential.sql").read_text(encoding="utf-8")
        self.get_user_sql = (sql_directory / "get_user.sql").read_text(encoding="utf-8")

    def _require_user(self, cursor, user_id: int) -> None:
        cursor.execute(self.get_user_sql, (user_id,))
        if cursor.fetchone() is None:
            raise ValueError("用户不存在，无法操作模型凭据。")

    @logged_operation("model_credential_repository.save_key")
    def save_key(self, user_id: int, encrypted_key: str) -> None:
        """保存或替换本人的 DeepSeek 密文；只有提交成功才算保存成功。"""
        user_id = validate_positive_integer(user_id, "用户 ID")
        encrypted_key = validate_encrypted_key(encrypted_key)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._require_user(cursor, user_id)
                    # TODO 1：cursor.execute 执行 self.save_key_sql。
                    # 第二个参数是 (user_id, encrypted_key)，不拼接 SQL。
                    cursor.execute(self.save_key_sql, (user_id, encrypted_key))

                # TODO 2：离开 cursor 的 with 后 connection.commit()。
                # 无需 lastrowid；本表使用 user_id + provider，不生成新 ID。
                connection.commit()
            except IntegrityError as error:
                connection.rollback()
                if error.errno == errorcode.ER_NO_REFERENCED_ROW_2:
                    # 即使用户在检查后被删除，数据库外键仍会拒绝写入。
                    raise ValueError("用户不存在，无法保存模型凭据。") from error
                raise
            except Exception:
                connection.rollback()
                raise

    @logged_operation("model_credential_repository.get_key")
    def get_key(self, user_id: int) -> str | None:
        """内部读取密文；存在用户但尚未配置时返回 None。"""
        user_id = validate_positive_integer(user_id, "用户 ID")
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                self._require_user(cursor, user_id)
                # TODO 3：cursor.execute 执行 self.get_key_sql，参数 (user_id,)。
                cursor.execute(self.get_key_sql, (user_id,))
                # TODO 4：用 row 接收 cursor.fetchone()。
                row = cursor.fetchone()
                # TODO 5：row is None 时返回 None，否则返回 row[0]。
                # row 是一行元组 (encrypted_key,)，不是密文字符串本身。
                if row is None:
                    return None
                return row[0]

    @logged_operation("model_credential_repository.delete_key")
    def delete_key(self, user_id: int) -> bool:
        """删除本人 DeepSeek 配置；删除一条返回 True，未配置返回 False。"""
        user_id = validate_positive_integer(user_id, "用户 ID")
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._require_user(cursor, user_id)
                    # TODO 6：cursor.execute 执行 self.delete_key_sql，参数 (user_id,)。
                    cursor.execute(self.delete_key_sql, (user_id,))
                    # TODO 7：用 deleted 保存 cursor.rowcount == 1 的结果。
                    deleted = cursor.rowcount == 1

                # TODO 8：提交事务。
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        # TODO 9：返回 deleted；不要在提交之前返回成功。
        return deleted
