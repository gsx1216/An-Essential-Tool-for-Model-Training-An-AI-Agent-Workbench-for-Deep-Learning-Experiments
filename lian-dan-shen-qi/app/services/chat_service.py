from app.logging_config import logged_operation

import asyncio

from app.errors import NoPendingReplyError
from app.models.chat_turn import TurnAlreadyCompletedError
from app.models.research import ResearchReply
from app.models.context_snapshot import ContextAttempt
from app.models.skill import SKILL_UNSET
from app.repositories.message_repository import MessageRepository
from app.services.model_gateway import ModelGateway
from app.services.chat_context_factory import ChatContextFactory
from app.validators import (
    normalize_session_id,
    validate_message_content,
    validate_positive_integer,
)


class ChatService:
    @logged_operation("chat_service.__init__", log_success=False, expected_errors=())
    def __init__(self, gateway: ModelGateway, repository: MessageRepository, *, context_factory: ChatContextFactory | None = None, tool_service=None, skills_enabled=False):
        self.gateway = gateway
        self.repository = repository
        self.context_factory = context_factory
        self.tool_service = tool_service
        self.skills_enabled = skills_enabled

    @logged_operation("chat_service.send_message")
    async def send_message(
        self, user_id: int, session_id: str, content: str,
        request_id: str | None = None, *, skill_snapshot=SKILL_UNSET,on_delta=None,
    ) -> str:
        user_id = validate_positive_integer(user_id, "用户 ID")
        session_id = normalize_session_id(session_id)
        content = validate_message_content(content)

        # 这一步会提交问题；后面的模型失败不会撤销它。
        turn_id = await asyncio.to_thread(
            self.repository.start_turn, user_id, session_id, content, request_id,
            **({'skill_snapshot':skill_snapshot} if skill_snapshot is not SKILL_UNSET else {}),
        )
        return await self._generate_reply(user_id, session_id, turn_id,**({'on_delta':on_delta} if on_delta is not None else {}))

    @logged_operation("chat_service.retry_message")
    async def retry_message(self, user_id: int, session_id: str, *, on_delta=None) -> str:
        user_id = validate_positive_integer(user_id, "用户 ID")
        session_id = normalize_session_id(session_id)
        turn = await asyncio.to_thread(
            self.repository.get_unfinished_turn, user_id, session_id,
        )
        if turn is None:
            raise NoPendingReplyError("当前会话没有等待回复的问题。")
        # 重试不再调用 start_turn，也不另存一条“继续”。
        return await self._generate_reply(user_id, session_id, turn.turn_id,**({'on_delta':on_delta} if on_delta is not None else {}))

    def _build_model_messages(self, messages: list[tuple[str, str]]) -> list:
        # 保留你已经写好的消息组装；这里的 messages 已包含本轮用户提问。
        history = [("system", "你是一个乐于助人的ai助手")]
        for message in messages:
            history.append(message)
        return history

    async def _generate_reply(self, user_id: int, session_id: str, turn_id: str, *, on_delta=None) -> str:
        try:
            claim_options = {} if self.context_factory is None else {
                "lease_seconds": self.context_factory.settings.lease_seconds, "load_history": False,
            }
            attempt_id, messages = await asyncio.to_thread(
                self.repository.claim_turn, user_id, session_id, turn_id,
                **claim_options,
            )
        except TurnAlreadyCompletedError:
            # 同一请求已成功，只读取原结果，不重复调用模型。
            reply = await asyncio.to_thread(
                self.repository.get_completed_reply, user_id, session_id, turn_id,
            )
            if reply is None:
                raise ValueError("这一轮已完成，但未找到已保存的回复。")
            return reply

        failure_code = "MODEL_REQUEST_FAILED"
        research_reply = None
        try:
            answer_gateway = self.gateway
            skill=None
            if self.skills_enabled:
                skill=await asyncio.to_thread(self.repository.load_turn_skill,user_id,session_id,turn_id,attempt_id)
            if self.context_factory is None:
                # 仅供旧 CLI/独立兼容调用；正式 HTTP 依赖始终提供 context_factory。
                history = self._build_model_messages(messages)
                if skill is not None:history[0]=(history[0][0],history[0][1]+skill.prompt())
            else:
                failure_code = "CONTEXT_PREPARATION_FAILED"
                attempt = ContextAttempt(turn_id, attempt_id)
                context, answer_gateway = self.context_factory.create(user_id, session_id, attempt,
                    **({'skill_snapshot':skill} if skill is not None else {}))
                prepared = await context.prepare(user_id, session_id, attempt=attempt)
                history = prepared.model_messages
            failure_code = "MODEL_REQUEST_FAILED"
            if self.tool_service is None:
                response = await answer_gateway.invoke(history,**({'on_delta':on_delta} if on_delta is not None else {}))
                ai_content = validate_message_content(response['content'])
            else:
                if self.context_factory is None:
                    raise ValueError('工具聊天必须提供上下文工厂。')
                failure_code = 'CHAT_TOOL_QUERY_FAILED'
                ai_content = await self.tool_service.answer(history, user_id=user_id, session_id=session_id,
                    attempt=attempt, gateway=answer_gateway,
                    max_input_tokens=min(self.context_factory.settings.max_input_tokens, self.context_factory.settings.model_request_tokens),
                    **({'on_delta':on_delta} if on_delta is not None else {}))
                if isinstance(ai_content,ResearchReply):
                    research_reply=ai_content
                    ai_content=research_reply.text
        except asyncio.CancelledError as error:
            await self._record_failure(user_id, session_id, turn_id, attempt_id,
                                       "MODEL_REQUEST_CANCELLED", error)
            raise
        except Exception as error:
            await self._record_failure(user_id, session_id, turn_id, attempt_id,
                                       failure_code, error)
            raise

        # 数据库写回失败不属于模型失败；不据此再次生成。
        await asyncio.to_thread(
            self.repository.complete_turn,
            user_id, session_id, turn_id, attempt_id, ai_content,
            **({"research_reply":research_reply} if research_reply is not None else {}),
        )
        if self.context_factory is not None and getattr(context, "cache", None) is not None:
            await context.cache_reply(user_id, session_id, ai_content)
        return ai_content

    async def _record_failure(self, user_id, session_id, turn_id, attempt_id, code, original_error):
        try:
            await asyncio.to_thread(
                self.repository.fail_turn, user_id, session_id, turn_id, attempt_id, code,
            )
        except Exception as state_error:
            # 同时保留原始失败和状态写入失败，不能静默吞掉；租约仍可用于后续恢复。
            raise original_error from state_error
