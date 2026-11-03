"""个人模型 Key 的加解密工具；不负责登录鉴权、接口响应或数据库读写。

使用库提供的 Fernet，不自行设计加密算法。
把用户 ID、供应商和 API Key 一起加密，解密时核对所属用户。
"""

import json

from cryptography.fernet import Fernet, InvalidToken

from app.logging_config import logged_operation
from app.validators import validate_positive_integer


MAX_API_KEY_LENGTH = 4096
MAX_ENCRYPTED_KEY_LENGTH = 8192


def validate_api_key(value: str) -> str:
    """只做本地输入检查；不证明 Key 在供应商处有效，也不发起模型请求。"""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_API_KEY_LENGTH:
        raise ValueError("模型 API Key 必须是 1～4096 个字符的字符串。")
    if any(not 33 <= ord(character) <= 126 for character in value):
        raise ValueError("模型 API Key 不能包含空白或不可见字符。")
    # 不截断、不 strip、不记录原文；这是本系统输入限制，不是供应商格式规则。
    return value


class ApiKeyCipher:
    @logged_operation("api_key_cipher.__init__", log_success=False, expected_errors=())
    def __init__(self, master_key: str):
        """master_key 是后端持有的 Fernet 主密钥，与用户的 API Key 不同。"""
        if not isinstance(master_key, str) or not master_key:
            raise ValueError("模型凭据加密主密钥未配置或格式不正确。")
        try:
            # 复用部署者传入的主密钥；构造或重建对象时都不生成新密钥。
            self._fernet = Fernet(master_key.encode("ascii"))
        except (TypeError, ValueError):
            raise ValueError("模型凭据加密主密钥未配置或格式不正确。") from None

    @logged_operation("api_key_cipher.encrypt")
    def encrypt(self, user_id: int, api_key: str) -> str:
        """返回可以保存到数据库的密文字符串，不返回或打印原始 Key。"""
        user_id = validate_positive_integer(user_id, "用户 ID")
        api_key = validate_api_key(api_key)
        payload = json.dumps(
            {"version": 1, "provider": "deepseek", "user_id": user_id, "api_key": api_key},
            ensure_ascii=True,
            separators=(",", ":"),
        )

        # Fernet.encrypt 接收字节；密文用 ASCII 字符串交给后续仓储保存。
        payload_bytes = payload.encode("utf-8")
        encrypted_bytes = self._fernet.encrypt(payload_bytes)
        return encrypted_bytes.decode("ascii")

    @logged_operation("api_key_cipher.decrypt")
    def decrypt(self, user_id: int, encrypted_key: str) -> str:
        """只供后端调用模型时解密；未来公开接口不返回本方法的结果。"""
        user_id = validate_positive_integer(user_id, "用户 ID")
        if not isinstance(encrypted_key, str) or not 1 <= len(encrypted_key) <= MAX_ENCRYPTED_KEY_LENGTH:
            raise ValueError("模型凭据密文格式不正确。")

        try:
            # 先由 Fernet 验证密文，再解析已经解密的原文。
            plaintext_bytes = self._fernet.decrypt(encrypted_key.encode("ascii"))
            payload = json.loads(plaintext_bytes.decode("utf-8"))
        except (InvalidToken, TypeError, ValueError):
            # InvalidToken 是加密库的密文验证错误，与登录 Token 异常不同。
            raise ValueError("模型凭据无法解密，请检查配置。") from None

        # 校验加密内容绑定的身份，拒绝把其他用户的整段密文挪到当前用户名下。
        if (
            not isinstance(payload, dict)
            or type(payload.get("version")) is not int
            or payload["version"] != 1
            or payload.get("provider") != "deepseek"
            or type(payload.get("user_id")) is not int
            or payload["user_id"] != user_id
        ):
            raise ValueError("模型凭据不属于当前用户或格式不受支持。")
        return validate_api_key(payload.get("api_key"))
