"""练习：读取指定用户的个人Key，创建供本次调用使用的模型网关。"""

import asyncio
from collections.abc import Callable

from app.logging_config import logged_operation
from app.services.model_credential_service import ModelCredentialService
from app.services.model_gateway import ModelGateway
from app.validators import validate_positive_integer


class UserModelGatewayFactory:
    @logged_operation("user_model_gateway_factory.__init__", log_success=False, expected_errors=())
    def __init__(
        self,
        credential_service: ModelCredentialService,
        gateway_factory: Callable[..., ModelGateway] = ModelGateway,
    ):
        # 保存协作者，不保存当前用户ID或个人Key。
        self.credential_service = credential_service
        # 正常运行使用ModelGateway；测试时传入假工厂，避免创建真实模型客户端。
        self.gateway_factory = gateway_factory

    @logged_operation("user_model_gateway_factory.create_for_user")
    async def create_for_user(self, user_id: int) -> ModelGateway:
        """返回新网关对象；本方法不发送问题，不调用invoke。"""
        # TODO 1：用validate_positive_integer校验user_id。
        # 参数依次为user_id、"用户 ID"；校验结果仍用user_id接收。
        user_id = validate_positive_integer(user_id, "用户 ID")
        # TODO 2：await调用self.credential_service.get_api_key(user_id)。
        # 用api_key接收结果；这个方法已经完成查询和解密。
        # 没有配置、解密失败或数据库错误时，让异常继续向上传递。
        api_key = await self.credential_service.get_api_key(user_id=user_id)
        # TODO 3：await asyncio.to_thread(...)创建网关，用gateway接收。
        # 第一个参数传self.gateway_factory本身，不提前加括号调用。
        # 再以关键字参数api_key=api_key传入当前用户的Key。
        gatway = await asyncio.to_thread(
            self.gateway_factory,
            api_key=api_key,
        )
        # TODO 4：返回gateway对象；不是gateway.model，也不是api_key。
        # 填写完成后删除下面的NotImplementedError。
        return gatway
