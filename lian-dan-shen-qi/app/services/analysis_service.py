"""独立分析执行：只读冻结报告，字段格式最多纠正一次，按租约原子保存结果。"""

import asyncio
import logging

from mysql.connector import Error as MySQLError
from openai import OpenAIError, APITimeoutError
from pydantic import ValidationError

from app.errors import ModelCredentialNotConfiguredError, ModelCredentialDecryptionError, ModelCredentialConfigurationError, ChatToolProtocolError
from app.logging_config import logged_operation, log_failure
from app.models.analysis import (AnalysisSettings, AnalysisLeaseLostError, AnalysisProtocolError, AnalysisStorageError, ModelReplyError,
    evidence_messages, render_analysis, validation_fields)
from app.models.event_bus import EventPayloadError, EventNotFoundError
from app.models.context_state import ContextTooLargeError

logger = logging.getLogger(__name__)


class AnalysisService:
    stage_name = 'analysis'
    def __init__(self, repository, *, gateway_factory=None, settings=None):
        self.repository = repository
        self.gateway_factory = gateway_factory
        self.settings = settings or AnalysisSettings()

    async def preference(self, user_id, *, enabled=None):
        return await asyncio.to_thread(self.repository.preference, user_id, enabled=enabled)

    async def task(self, user_id, run_id, task_id, *, retry=False):
        return await asyncio.to_thread(self.repository.task, user_id, run_id, task_id, retry=retry)

    async def results(self, user_id, run_id, **options):
        return await asyncio.to_thread(self.repository.results, user_id, run_id, **options)

    async def _renew(self, lease):
        saved = await asyncio.to_thread(self.repository.renew, lease["user_id"], lease["task_id"],
                                       lease["lease_token"], self.settings.lease_seconds)
        if not saved:
            raise AnalysisLeaseLostError("分析租约或用户开关已经失效。")

    async def _heartbeat(self, lease):
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            await self._renew(lease)

    async def _perform(self, lease):
        source = await asyncio.to_thread(self.repository.source, lease["user_id"], lease["task_id"], lease["lease_token"])
        messages, evidence = evidence_messages(source, self.settings)
        if self.gateway_factory is None:
            raise ModelCredentialConfigurationError("尚未组装分析网关。")
        gateway = await self.gateway_factory.create_for_user(lease["user_id"])
        try:
            # Key 加载后再次检查授权；网关仅调用 invoke，不提供训练/SSH/网页工具。
            await self._renew(lease)
            async with asyncio.timeout(self.settings.model_timeout_seconds):
                response = await gateway.invoke(messages, max_tokens=self.settings.max_output_tokens, json_mode=True)
            if type(response) is not dict:
                raise AnalysisProtocolError("分析网关返回结构错误。")
            try:
                content = render_analysis(response.get("content"), evidence)
            except AnalysisProtocolError as error:
                # 仅修正已识别的输出字段结构错误；不重放工具、改动来源或跳过证据校验。
                if not isinstance(error.__cause__,ValidationError):raise
                repaired_messages=messages+[("ai",response['content']),("human",
                    '上次 JSON 字段不符合系统给出的 Schema：'+','.join(validation_fields(error.__cause__))+
                    '。请按 Schema 修正并返回完整 JSON；限制数组数量、文本长度，保留实际证据路径，不添加额外字段。')]
                from app.services.token_budget import Utf8BudgetCounter
                if Utf8BudgetCounter().count_messages(repaired_messages)>self.settings.max_input_tokens:raise
                await self._renew(lease)
                async with asyncio.timeout(self.settings.model_timeout_seconds):
                    response=await gateway.invoke(repaired_messages,max_tokens=self.settings.max_output_tokens,json_mode=True)
                if type(response) is not dict:raise AnalysisProtocolError('分析网关返回结构错误。')
                content=render_analysis(response.get('content'),evidence)
        finally:
            try:
                async with asyncio.timeout(5):
                    await gateway.aclose()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_failure(logger, error, operation="analysis_service.gateway_cleanup")
        await asyncio.to_thread(self.repository.complete, lease["user_id"], lease["run_id"], lease["task_id"],
                                lease["lease_token"], content, self.settings.model_name)

    async def _run_with_heartbeat(self, lease):
        work = asyncio.create_task(self._perform(lease))
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        try:
            async with asyncio.timeout(self.settings.step_timeout_seconds):
                done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
                if work in done:
                    return await work
                await heartbeat  # 续租失败使旧模型请求取消，不以旧令牌写回。
        finally:
            for task in (work, heartbeat):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)

    @staticmethod
    def error_code(error):
        if isinstance(error, (ModelCredentialNotConfiguredError, ModelCredentialDecryptionError, ModelCredentialConfigurationError)):
            return "KEY_UNAVAILABLE", "attention"
        if isinstance(error, ContextTooLargeError):
            return "INPUT_TOO_LARGE", "attention"
        if isinstance(error, (TimeoutError, APITimeoutError)):
            return "MODEL_TIMEOUT", "failed"
        if isinstance(error, ModelReplyError):
            return error.code, "attention"
        if isinstance(error, (AnalysisProtocolError,ChatToolProtocolError)):
            return "INVALID_MODEL_OUTPUT", "attention"
        if isinstance(error, (EventPayloadError, EventNotFoundError)):
            return "INVALID_SOURCE", "attention"
        if isinstance(error, AnalysisLeaseLostError):
            return "ANALYSIS_INTERRUPTED", "attention"
        if isinstance(error, (MySQLError, AnalysisStorageError)):
            return "STORAGE_ERROR", "attention"
        if isinstance(error, OpenAIError):
            return "MODEL_UNAVAILABLE", "failed"
        return "INTERNAL_ERROR", "attention"

    @logged_operation("analysis_service.tick", log_success=False)
    async def tick(self, *, user_id=None, run_id=None):
        # 默认关闭时连领取都不做；查询结果服务无需凭据或模型依赖。
        if not self.settings.enabled:
            return False
        scope = {} if user_id is None and run_id is None else {"user_id": user_id, "run_id": run_id}
        lease = await asyncio.to_thread(self.repository.claim, self.settings.lease_seconds, self.settings.max_attempts, **scope)
        if lease is None:
            return False
        identity = (lease["user_id"], lease["task_id"], lease["lease_token"])
        metadata = {key: lease[key] for key in ("task_id", "run_id", "event_id", "report_version", "attempts") if key in lease}
        logger.info(self.stage_name + '_started', extra=metadata)
        try:
            await self._run_with_heartbeat(lease)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(asyncio.to_thread(self.repository.fail, *identity,
                    status="attention", error_code="WORKER_CANCELLED"))
            except Exception as error:
                log_failure(logger, error, operation="analysis_service.cancel_cleanup")
            raise
        except Exception as error:
            code, status = self.error_code(error)
            log_failure(logger, error, operation="analysis_service.execute")
            saved = await asyncio.to_thread(self.repository.fail, *identity, status=status, error_code=code)
            logger.warning(self.stage_name + '_attention', extra={**metadata, "analysis_code": code, "state_saved": bool(saved)})
        else:
            logger.info(self.stage_name + '_succeeded', extra=metadata)
        return True
