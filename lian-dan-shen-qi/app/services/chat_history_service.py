"""历史消息业务练习：校验身份参数，分页读取自己的会话；填写后再接接口。"""

import asyncio

from app.logging_config import logged_operation
from app.repositories.message_repository import MessageRepository
from app.validators import normalize_session_id, validate_positive_integer


class ChatHistoryService:
    @logged_operation("chat_history_service.__init__", log_success=False, expected_errors=())
    def __init__(self, repository: MessageRepository):
        # TODO 1：把 repository 保存到 self.repository。
        self.repository = repository

    @logged_operation("chat_history_service.load_history_page")
    async def load_history_page(
        self, user_id: int, session_id: str, after_seq: int = 0, limit: int = 50,
    ) -> list[tuple[int, str, str]]:
        # TODO 2：用 validate_positive_integer 校验并更新 user_id，字段名“用户 ID”；
        # 再用 normalize_session_id 规范并更新 session_id。
        user_id = validate_positive_integer(user_id,"用户ID")
        session_id =normalize_session_id(session_id)
        # TODO 3：await asyncio.to_thread(...) 调用仓储 load_history_page。
        # 第一个参数是 self.repository.load_history_page 这个函数本身；
        # 后续传 user_id、session_id、after_seq、limit，可使用同名关键字参数。
        # 使用 messages 接收结果。
        # 仓储已检查归属及分页参数，不需要先查询一次会话再读取历史。
        messages = await asyncio.to_thread(
            self.repository.load_history_page,
            user_id,
            session_id,
            after_seq,
            limit,
        )
        # TODO 4：返回 messages。空列表是有效结果，数据库异常继续向上传播。
        # 每项是 (seq, role, content)，例如 (1, "human", "你好")。
        return messages
