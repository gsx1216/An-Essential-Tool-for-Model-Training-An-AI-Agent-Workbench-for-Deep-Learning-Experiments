"""凭证工具：生成安全随机 Token、计算 SHA-256 哈希和三天 UTC 到期时间。

只处理凭证计算；是否属于有效用户、是否过期或撤销由业务与仓储校验。
原始 Token 交给客户端，数据库只保存哈希。
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone


class TokenService:
    def __init__(self, ttl_seconds: int = 259200):
        # 这版登录凭证默认有效 3 天，允许配置为 1 秒至 3 天。
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 259200:
            raise ValueError("Token 有效期必须是 1～259200 秒的整数。")
        self.ttl_seconds = ttl_seconds

    def create_token(self) -> str:
        """每次登录创建一个新的安全随机凭证。"""
        # TODO 1：使用 secrets.token_urlsafe，随机字节数指定为 32。
        # 返回生成的字符串，不打印凭证，不使用 UUID 或 random 代替。
        token = secrets.token_urlsafe(32)
        return token

    def hash_token(self, token: str) -> str:
        """把原始 Token 转为可存库的 SHA-256 十六进制哈希。"""
        if not isinstance(token, str) or not token:
            raise ValueError("Token 必须是非空字符串。")
        # TODO 2：字符串先 encode("utf-8") 转成字节。
        # 将字节交给 hashlib.sha256，再用 hexdigest() 取得字符串并返回。
        # 不修改 Token 的空格或大小写；此处不进行用户或过期校验。
        hashed_token = hashlib.sha256(token.encode(encoding="utf-8")).hexdigest()
        return hashed_token

    def get_expires_at(self) -> datetime:
        """计算包含 UTC 时区的过期时间。"""
        now = datetime.now(timezone.utc)
        lifetime = timedelta(seconds=self.ttl_seconds)
        return now + lifetime
