"""每次已领取的生成尝试单独组装上下文；共享仓储与配置，不共享个人网关或任务。"""

import asyncio

from app.models.context_settings import ContextSettings
from app.errors import ChatToolBudgetError
from app.models.context_snapshot import ContextAttempt
from app.repositories.context_repository import ContextRepository
from app.services.context_manager import ContextManager
from app.services.context_service import ContextService
from app.services.history_summarizer import ModelHistorySummarizer
from app.services.input_compressor import ModelInputCompressor
from app.services.model_gateway import ModelGateway
from app.services.token_budget import Utf8BudgetCounter


class AttemptGateway:
    """只增加调用前任务校验；不拥有也不关闭原始网关。"""

    def __init__(self, gateway, repository, user_id, session_id, attempt):
        self.gateway = gateway
        self.repository = repository
        self.user_id = user_id
        self.session_id = session_id
        self.attempt = attempt

    async def invoke(self, messages, **options):
        await asyncio.to_thread(self.repository.check_attempt, self.user_id, self.session_id, self.attempt)
        return await self.gateway.invoke(messages, **options)


class ChatContextFactory:
    def __init__(self, gateway: ModelGateway, repository: ContextRepository, settings: ContextSettings, *, input_reserve=0, context_cache=None):
        self.gateway = gateway
        self.repository = repository
        self.settings = settings
        self.input_reserve = input_reserve
        self.cache = context_cache

    def create(self, user_id: int, session_id: str, attempt: ContextAttempt, *, skill_snapshot=None):
        guarded = AttemptGateway(self.gateway, self.repository, user_id, session_id, attempt)
        counter = Utf8BudgetCounter()
        summarizer = ModelHistorySummarizer(
            guarded, counter, max_request_tokens=self.settings.model_request_tokens,
            max_calls=self.settings.max_summary_calls, timeout_seconds=self.settings.timeout_seconds,
        )
        compressor = ModelInputCompressor(
            guarded, counter, max_request_tokens=self.settings.model_request_tokens,
            timeout_seconds=self.settings.timeout_seconds,
        )
        budget = min(self.settings.max_input_tokens, self.settings.model_request_tokens) - self.input_reserve
        if budget <= self.settings.max_summary_tokens:
            raise ChatToolBudgetError("当前输入预算不足以同时容纳摘要和工具，请增加后端预算。")
        manager = ContextManager(
            counter, summarizer, input_compressor=compressor,
            **({'system_prompt':'你是一个乐于助人的ai助手'+skill_snapshot.prompt()} if skill_snapshot is not None else {}),
            max_input_tokens=budget,
            compact_at_tokens=min(self.settings.compact_at_tokens, budget),
            max_summary_tokens=self.settings.max_summary_tokens,
            keep_recent_turns=self.settings.keep_recent_turns,
        )
        return ContextService(self.repository, manager, cache=self.cache), guarded
