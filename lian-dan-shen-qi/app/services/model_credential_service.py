"""个人模型凭据业务：按用户串联加密与存储，原始Key仅供后端内部使用。"""

import asyncio

from app.errors import ModelCredentialDecryptionError, ModelCredentialNotConfiguredError
from app.logging_config import logged_operation
from app.repositories.model_credential_repository import ModelCredentialRepository
from app.security.api_key_cipher import ApiKeyCipher, validate_api_key
from app.validators import validate_positive_integer


class ModelCredentialService:
    @logged_operation("model_credential_service.__init__", log_success=False, expected_errors=())
    def __init__(self, repository: ModelCredentialRepository, cipher: ApiKeyCipher):
        # 只保存协作者；用户ID、原始Key和密文都留在各次方法的局部变量中。
        self.repository = repository
        self.cipher = cipher

    @logged_operation("model_credential_service.save_api_key")
    async def save_api_key(self, user_id: int, api_key: str) -> None:
        """校验并加密用户提交的Key，等待仓储提交后正常结束。"""
        user_id = validate_positive_integer(user_id, "用户 ID")
        api_key = validate_api_key(api_key)
        encrypted_key = await asyncio.to_thread(self.cipher.encrypt, user_id, api_key)
        # 必须先完成加密再保存；数据库事务由仓储负责。
        await asyncio.to_thread(self.repository.save_key, user_id, encrypted_key)

    @logged_operation("model_credential_service.is_configured")
    async def is_configured(self, user_id: int) -> bool:
        user_id = validate_positive_integer(user_id, "用户 ID")
        encrypted_key = await asyncio.to_thread(self.repository.get_key, user_id)
        # 这里只判断是否配置；不解密、不返回密文、不调用供应商验证Key。
        return encrypted_key is not None

    @logged_operation("model_credential_service.delete_api_key")
    async def delete_api_key(self, user_id: int) -> bool:
        user_id = validate_positive_integer(user_id, "用户 ID")
        deleted = await asyncio.to_thread(self.repository.delete_key, user_id)
        # False表示没有配置可删，是正常结果；数据库错误仍会抛出。
        return deleted

    @logged_operation("model_credential_service.get_api_key")
    async def get_api_key(self, user_id: int) -> str:
        """仅供后端调用模型时使用；不能直接作为公开API返回值。"""
        user_id = validate_positive_integer(user_id, "用户 ID")
        encrypted_key = await asyncio.to_thread(self.repository.get_key, user_id)
        if encrypted_key is None:
            raise ModelCredentialNotConfiguredError("尚未配置模型 API Key，请先配置。")

        try:
            return await asyncio.to_thread(self.cipher.decrypt, user_id, encrypted_key)
        except ValueError as error:
            # 已存密文无法解密不同于用户未配置，也不同于本次输入格式错误。
            raise ModelCredentialDecryptionError("无法读取模型凭据。") from error
