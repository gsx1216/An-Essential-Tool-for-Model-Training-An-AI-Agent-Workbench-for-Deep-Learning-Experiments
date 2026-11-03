"""Argon2id 密码哈希，不存储、不打印明文密码。"""

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type


MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 32
# 只用于登录核对，兼容此前允许注册的长密码，不改变新注册的 32 字符上限。
MAX_VERIFICATION_PASSWORD_LENGTH = 128


def validate_password_hash(value: str) -> str:
    """只接受编码哈希，防止误把明文密码写入密码哈希字段。"""
    if not isinstance(value, str) or not value.startswith("$argon2id$") or len(value) > 255:
        raise ValueError("必须传入 Argon2id 密码哈希，不能传入明文密码。")
    try:
        parameters = extract_parameters(value)
    except (InvalidHashError, ValueError) as error:
        raise ValueError("密码哈希格式不正确。") from error
    if not (
        parameters.type is Type.ID and parameters.version == 19
        and 19456 <= parameters.memory_cost <= 262144
        and 2 <= parameters.time_cost <= 10
        and 1 <= parameters.parallelism <= 8
        and 16 <= parameters.salt_len <= 64
        and 16 <= parameters.hash_len <= 64
    ):
        raise ValueError("密码哈希参数不受支持。")
    return value


class PasswordService:
    def __init__(self):
        self._hasher = PasswordHasher(
            type=Type.ID, memory_cost=19456, time_cost=2, parallelism=1,
            salt_len=16, hash_len=32,
        )
        # 用户不存在或旧账号没有密码时，仍执行一次验证以减小耗时差异。
        self._dummy_hash = self._hasher.hash("dummy-account-placeholder")

    def hash_password(self, password: str) -> str:
        if not isinstance(password, str):
            raise ValueError("登录密码必须是字符串。")
        if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
            raise ValueError(f"登录密码必须为 {MIN_PASSWORD_LENGTH}～{MAX_PASSWORD_LENGTH} 个字符。")
        try:
            password.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("登录密码包含无法编码的字符。") from error
        # 不 strip 或截断密码，盐值由库自动随机生成。
        return self._hasher.hash(password)

    def verify_password(self, password: str, password_hash: str | None) -> bool:
        if not isinstance(password, str) or not 1 <= len(password) <= MAX_VERIFICATION_PASSWORD_LENGTH:
            return False
        try:
            target = validate_password_hash(password_hash)
            real_account = True
        except ValueError:
            target = self._dummy_hash
            real_account = False
        try:
            matched = self._hasher.verify(target, password)
        except (VerificationError, InvalidHashError, UnicodeEncodeError):
            return False
        return real_account and matched
