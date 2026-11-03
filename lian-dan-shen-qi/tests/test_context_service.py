"""上下文应用服务的异步边界、权限失败、摘要保存和异常传播；不连接真实依赖。"""

import asyncio
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from app.errors import NoPendingReplyError, SessionNotFoundError
from app.models.context_snapshot import ContextSnapshot
from app.models.context_state import ContextConflictError, ContextMessage, ContextSummary, PreparedContext
from app.services.context_service import ContextService


SID = "b4c4e67554ee4c1981740a1e901ec614"


class ContextServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old = ContextSummary("旧摘要", 2, 1)
        self.snapshot = ContextSnapshot(self.old, (ContextMessage(3, "human", "问题"),
            ContextMessage(4, "ai", "回复"), ContextMessage(5, "human", "当前问题")), 5)
        self.prepared = PreparedContext([("human", "当前问题")], ContextSummary("新摘要", 4, 2), 100, True)
        self.repository = SimpleNamespace(load_context=Mock(return_value=self.snapshot), save_summary=Mock())
        self.manager = SimpleNamespace(prepare=AsyncMock(return_value=self.prepared))
        self.service = ContextService(self.repository, self.manager)

    async def test_reads_snapshot_prepares_and_conditionally_saves_new_summary(self):
        result = await self.service.prepare(7, SID)
        self.assertIs(result, self.prepared)
        self.repository.load_context.assert_called_once_with(7, SID)
        self.manager.prepare.assert_awaited_once_with(list(self.snapshot.messages), self.old)
        self.repository.save_summary.assert_called_once_with(7, SID, self.prepared.summary,
            expected_version=1, expected_tail_seq=5)

    async def test_first_summary_uses_expected_version_zero(self):
        self.repository.load_context.return_value = ContextSnapshot(None, self.snapshot.messages, 5)
        await self.service.prepare(7, SID)
        self.assertEqual(self.repository.save_summary.call_args.kwargs["expected_version"], 0)

    async def test_short_input_or_only_current_input_compressed_does_not_write_summary(self):
        for compressed in (False, True):
            self.manager.prepare.return_value = PreparedContext([("human", "问题")], self.old,
                100, False, input_compressed=compressed)
            await self.service.prepare(7, SID)
        self.repository.save_summary.assert_not_called()

    async def test_blocking_database_calls_run_in_worker_thread(self):
        main_thread = threading.get_ident()
        threads = []
        def load(*args):
            threads.append(threading.get_ident())
            return self.snapshot
        def save(*args, **kwargs):
            threads.append(threading.get_ident())
        self.repository.load_context.side_effect = load
        self.repository.save_summary.side_effect = save
        await self.service.prepare(7, SID)
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(value != main_thread for value in threads))

    async def test_unauthorized_or_failed_read_never_calls_model_or_save(self):
        self.repository.load_context.side_effect = SessionNotFoundError("会话不可用")
        with self.assertRaises(SessionNotFoundError):
            await self.service.prepare(7, SID)
        self.manager.prepare.assert_not_awaited()
        self.repository.save_summary.assert_not_called()

    async def test_empty_or_answered_chat_has_no_pending_question(self):
        for messages in ((), (ContextMessage(2, "ai", "回复"),)):
            self.repository.load_context.return_value = ContextSnapshot(None, messages, 2 if messages else 0)
            with self.assertRaises(NoPendingReplyError):
                await self.service.prepare(7, SID)
        self.manager.prepare.assert_not_awaited()

    async def test_generation_failure_or_cancellation_does_not_write_summary(self):
        for error in (RuntimeError("TEST_ONLY_MODEL_FAILURE"), asyncio.CancelledError()):
            self.manager.prepare.side_effect = error
            with self.assertRaises(type(error)):
                await self.service.prepare(7, SID)
        self.repository.save_summary.assert_not_called()

    async def test_write_conflict_propagates_without_automatic_paid_retry(self):
        self.repository.save_summary.side_effect = ContextConflictError("上下文已更新")
        with self.assertRaises(ContextConflictError):
            await self.service.prepare(7, SID)
        self.manager.prepare.assert_awaited_once()

    async def test_invalid_ids_never_reach_database(self):
        for user_id, session_id in ((True, SID), (0, SID), (7, "wrong")):
            with self.assertRaises(ValueError):
                await self.service.prepare(user_id, session_id)
        self.repository.load_context.assert_not_called()

    async def test_missing_new_summary_is_not_reported_as_success(self):
        self.manager.prepare.return_value = PreparedContext([], None, 10, True)
        with self.assertRaises(RuntimeError):
            await self.service.prepare(7, SID)
        self.repository.save_summary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
