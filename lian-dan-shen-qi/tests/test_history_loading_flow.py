"""验证查看历史与发送问题的不同读取路径；仓储和模型均为模拟对象。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

from app.repositories.message_repository import MessageRepository
from app.services.chat_history_service import ChatHistoryService
from app.services.chat_service import ChatService


class HistoryLoadingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_view_only_reads_display_page_and_sending_loads_fresh_model_context(self):
        session_id = "b4c4e67554ee4c1981740a1e901ec614"
        turn_id = "430390d71ece4e63ae202043b057e9c2"
        attempt_id = "a" * 32
        repository = create_autospec(MessageRepository, instance=True, spec_set=True)
        display_page = [(1, "human", "旧问题"), (2, "ai", "旧回复")]
        repository.load_history_page.return_value = display_page
        repository.start_turn.return_value = turn_id
        # 页面只加载了两条；发送时数据库已有其他设备新增的消息和本轮问题。
        current_context = [("human", "旧问题"), ("ai", "旧回复"),
                           ("human", "另一设备的新问题"), ("ai", "另一设备的新回复"),
                           ("human", "现在的问题")]
        repository.claim_turn.return_value = (attempt_id, current_context)
        gateway = SimpleNamespace(invoke=AsyncMock(return_value={"content": "本轮回复"}))
        history = ChatHistoryService(repository)
        chat = ChatService(gateway, repository)

        self.assertEqual(await history.load_history_page(7, session_id, limit=2), display_page)
        gateway.invoke.assert_not_awaited()
        repository.start_turn.assert_not_called()
        repository.claim_turn.assert_not_called()
        repository.complete_turn.assert_not_called()

        self.assertEqual(await chat.send_message(7, session_id, "现在的问题", turn_id), "本轮回复")
        repository.claim_turn.assert_called_once_with(7, session_id, turn_id)
        gateway.invoke.assert_awaited_once_with([("system", "你是一个乐于助人的ai助手"), *current_context])
        repository.complete_turn.assert_called_once_with(7, session_id, turn_id, attempt_id, "本轮回复")
        # 模型上下文不复用界面的分页结果，也不依赖浏览器是否已经加载完全部页面。
        repository.load_history_page.assert_called_once_with(7, session_id, 0, 2)
