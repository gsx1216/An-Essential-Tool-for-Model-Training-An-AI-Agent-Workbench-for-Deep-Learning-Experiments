"""上下文应用服务：读快照 → 准备上下文 → 有新摘要则按版本及可选任务校验保存。"""

import asyncio

from app.errors import NoPendingReplyError
from app.logging_config import logged_operation
from app.models.context_state import PreparedContext
from app.models.context_snapshot import ContextAttempt, ContextSnapshot
from app.models.context_state import ContextMessage
from app.repositories.context_repository import ContextRepository
from app.services.context_manager import ContextManager
from app.validators import normalize_session_id, validate_positive_integer


class ContextService:
    def __init__(self, repository: ContextRepository, manager: ContextManager, *, cache=None):
        self.repository = repository
        self.manager = manager
        self.cache = cache
        self._cached_snapshot = None
        self._cache_identity = None

    @logged_operation("context_service.prepare")
    async def prepare(self, user_id: int, session_id: str, *, attempt: ContextAttempt | None = None) -> PreparedContext:
        # user_id 应来自已认证服务端身份；仓储仍独立检查归属，不能信任浏览器历史。
        user_id = validate_positive_integer(user_id, "用户 ID")
        session_id = normalize_session_id(session_id)
        task_options = {} if attempt is None else {"attempt": attempt}
        cached = None if self.cache is None else await asyncio.to_thread(self.cache.load, user_id, session_id)
        if cached is None:
            snapshot = await asyncio.to_thread(self.repository.load_context, user_id, session_id, **task_options)
        else:
            snapshot = await asyncio.to_thread(self.repository.reconcile_context, user_id, session_id, cached, **task_options)
        if not snapshot.messages or snapshot.messages[-1].role != "human":
            raise NoPendingReplyError("当前会话没有等待回复的问题。")
        # 经 MySQL 校验后才续期，缓存不能绕过用户、会话和生成租约校验。
        if self.cache is not None:
            await asyncio.to_thread(self.cache.store, user_id, session_id, snapshot)
        prepared = await self.manager.prepare(list(snapshot.messages), snapshot.summary)
        if prepared.was_compacted:
            if prepared.summary is None:
                raise RuntimeError("上下文管理器未返回新生成的摘要。")
            await asyncio.to_thread(
                self.repository.save_summary,
                user_id, session_id, prepared.summary,
                expected_version=snapshot.version,
                expected_tail_seq=snapshot.tail_seq,
                **task_options,
            )
            snapshot = ContextSnapshot(prepared.summary,
                tuple(message for message in snapshot.messages if message.seq > prepared.summary.summarized_until_seq),
                snapshot.tail_seq)
            if self.cache is not None:
                await asyncio.to_thread(self.cache.store, user_id, session_id, snapshot)
        self._cached_snapshot = snapshot
        self._cache_identity = (user_id, session_id)
        # 原文始终保留；精简当前问题不会写回原消息，也不会误保存为历史摘要。
        return prepared

    async def cache_reply(self, user_id: int, session_id: str, content: str) -> None:
        """仅在 complete_turn 提交成功后调用；缓存真实回复，不缓存工具临时消息。"""
        if self.cache is None or self._cached_snapshot is None:
            return
        if self._cache_identity != (user_id, session_id):
            raise ValueError("缓存更新会话与已准备上下文不一致。")
        snapshot = self._cached_snapshot
        updated = ContextSnapshot(snapshot.summary,
            snapshot.messages + (ContextMessage(snapshot.tail_seq + 1, "ai", content),), snapshot.tail_seq + 1)
        await asyncio.to_thread(self.cache.store, user_id, session_id, updated)
