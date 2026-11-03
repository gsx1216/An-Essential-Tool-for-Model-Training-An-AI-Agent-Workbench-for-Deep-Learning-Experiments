"""个人聊天业务：校验输入，按用户创建网关，复用聊天持久化和重试流程。"""

from contextlib import asynccontextmanager
import asyncio
import re

from app.logging_config import logged_operation
from app.repositories.message_repository import MessageRepository
from app.repositories.context_repository import ContextRepository
from app.models.context_settings import ContextSettings
from app.services.chat_context_factory import ChatContextFactory
from app.services.chat_service import ChatService
from app.services.user_model_gateway_factory import UserModelGatewayFactory
from app.validators import normalize_session_id, validate_message_content, validate_positive_integer
from app.models.skill import SKILL_ID_PATTERN, SKILL_UNSET, SkillNotFound


class UserChatService:
    @logged_operation("user_chat_service.__init__", log_success=False, expected_errors=())
    def __init__(self, gateway_factory: UserModelGatewayFactory, repository: MessageRepository, *,
                 context_repository: ContextRepository | None = None, context_settings: ContextSettings | None = None, tool_service=None, context_cache=None, skill_catalog=None):
        self.gateway_factory = gateway_factory
        self.repository = repository
        self.context_repository = context_repository
        self.context_cache = context_cache
        self.tool_service = tool_service
        self.skill_catalog = skill_catalog
        self.context_settings = context_settings if context_settings is not None else ContextSettings()

    @logged_operation("user_chat_service.send_message")
    async def send_message(
        self, user_id: int, session_id: str, content: str,
        request_id: str | None = None, *, skill_id: str | None = None,on_delta=None,
    ) -> str:
        """使用本人的Key发送新问题；请求去重标识原样交给原聊天业务。"""
        user_id = validate_positive_integer(user_id, "用户 ID")
        session_id = normalize_session_id(session_id)
        content = validate_message_content(content)
        if skill_id is not None and (not isinstance(skill_id,str) or not re.fullmatch(SKILL_ID_PATTERN,skill_id)):
            raise ValueError('Skill标识不符合要求。')

        # request_id 就是轮次 ID。只读快查校验归属、原问题和已存回复；不要求个人 Key。
        snapshot=SKILL_UNSET
        if self.skill_catalog is not None:
            state=None
            if request_id is not None:
                state=await asyncio.to_thread(self.repository.inspect_skill_request,user_id,session_id,request_id,content,skill_id)
                if state.reply is not None:return state.reply
            if state is not None and state.existing:
                snapshot=state.skill
            elif skill_id is not None:
                snapshot=await asyncio.to_thread(self.skill_catalog.get,skill_id,user_id)
            else:
                snapshot=None
        elif skill_id is not None:
            raise SkillNotFound('当前未启用Skill目录。')
        elif request_id is not None:
            reply = await asyncio.to_thread(
                self.repository.find_request_reply, user_id, session_id, request_id, content,
            )
            if reply is not None:
                return reply
        # async with取得本次ChatService，返回或失败时均执行网关清理。
        async with self._open_chat(user_id) as chat:
            return await chat.send_message(user_id, session_id, content, request_id,
                **({'skill_snapshot':snapshot} if snapshot is not SKILL_UNSET else {}),
                **({'on_delta':on_delta} if on_delta is not None else {}))

    @logged_operation("user_chat_service.retry_message")
    async def retry_message(self, user_id: int, session_id: str, *,on_delta=None) -> str:
        """重试已保存的未完成问题，不新增一条用户消息。"""
        user_id = validate_positive_integer(user_id, "用户 ID")
        session_id = normalize_session_id(session_id)

        async with self._open_chat(user_id) as chat:
            return await chat.retry_message(user_id, session_id,**({'on_delta':on_delta} if on_delta is not None else {}))

    @asynccontextmanager
    async def _open_chat(self, user_id: int):
        """资源管理已提供：取得本人的新网关后，在聊天结束时尝试关闭。"""
        gateway = await self.gateway_factory.create_for_user(user_id)
        try:
            context_factory = None if self.context_repository is None else ChatContextFactory(
                gateway, self.context_repository, self.context_settings,
                context_cache=self.context_cache,
                **({} if self.tool_service is None else {"input_reserve": self.tool_service.input_reserve}),
            )
            yield ChatService(gateway=gateway, repository=self.repository, context_factory=context_factory,
                              **({} if self.tool_service is None else {"tool_service": self.tool_service}),
                              **({'skills_enabled':True} if self.skill_catalog is not None else {}))
        except BaseException as operation_error:
            # 包含取消异常；清理失败不应覆盖原来的业务失败。
            try:
                await gateway.aclose()
            except BaseException as cleanup_error:
                raise operation_error from cleanup_error
            raise
        else:
            await gateway.aclose()
