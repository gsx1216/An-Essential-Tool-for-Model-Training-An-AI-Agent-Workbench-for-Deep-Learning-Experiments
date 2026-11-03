"""聊天会话业务练习：由已认证身份创建、列出或删除会话；填写后再接入 HTTP。"""

import asyncio

from app.logging_config import logged_operation
from app.errors import SessionNotFoundError
from app.repositories.session_repository import SessionRepository
from app.validators import normalize_required_text, normalize_session_id, validate_positive_integer


class ChatSessionService:
    @logged_operation("chat_session_service.__init__", log_success=False, expected_errors=())
    def __init__(self, repository: SessionRepository, *, context_cache=None):
        # TODO 1：把传入的 repository 保存在 self.repository。
        # 完成后删除下面的占位异常。
        self.repository = repository
        self.context_cache = context_cache

    @logged_operation("chat_session_service.create_session")
    async def create_session(self, user_id: int, title: str = "新聊天") -> str:
        # TODO 2：用 validate_positive_integer 校验 user_id，字段名为“用户 ID”。
        # 使用返回值更新 user_id。
        user_id = validate_positive_integer(user_id,"新用户")
        # TODO 3：用 normalize_required_text 规范 title。
        # 参数依次为 title、“会话标题”、200；使用返回值更新 title。
        title = normalize_required_text(title,"会话标题",200)
        # TODO 4：await asyncio.to_thread(...) 调用仓储 create_session。
        # 第一个参数是 self.repository.create_session 这个函数本身；
        # 其后用关键字参数传入 user_id=user_id、title=title。
        # 使用 session_id 接收结果，然后返回 session_id。
        # UUID 和数据库事务均由仓储负责，这里不再生成 UUID 或 commit。
        session_id = await asyncio.to_thread(
            self.repository.create_session,
            user_id=user_id,
            title=title,
        )

        return session_id
    @logged_operation("chat_session_service.list_sessions")
    async def list_sessions(self, user_id: int) -> list[tuple[str, str]]:
        # TODO 5：用 validate_positive_integer 校验并更新 user_id。
        user_id = validate_positive_integer(user_id,"用户ID")
        # TODO 6：await asyncio.to_thread(...) 调用仓储 list_sessions。
        # 传入函数本身 self.repository.list_sessions，以及参数 user_id。
        # 使用 sessions 接收结果，并直接返回；空列表 [] 也是正常结果。
        # 不在 for 循环中 return，否则只会返回第一条会话。
        sessions = await asyncio.to_thread(
            self.repository.list_sessions,
            user_id=user_id,
        )
        return sessions
    @logged_operation("chat_session_service.delete_session")
    async def delete_session(self, user_id: int, session_id: str) -> None:
        # TODO 7：校验并更新 user_id；使用 normalize_session_id 规范并更新 session_id。
        user_id = validate_positive_integer(user_id,"用户ID")
        session_id = normalize_session_id(session_id)
        # TODO 8：await asyncio.to_thread(...) 调用 self.repository.delete_session。
        # 传入 user_id 和 session_id；用 deleted 接收返回的 bool。
        # 仓储已检查归属并在同一事务中删除，不需要先 get_session 再删。
        deleted = await asyncio.to_thread(
            self.repository.delete_session,
            user_id,
            session_id,
        )
        # TODO 9：如果 deleted 为 False，抛出 SessionNotFoundError。
        # 固定提示“会话不存在或不属于当前用户。”，不透露他人的会话是否存在。
        # 正常删除成功则 return None；None 表示完成，不表示失败。
        if not deleted:
            raise SessionNotFoundError("会话不存在或不属于当前用户。")
        if self.context_cache is not None:
            await asyncio.to_thread(self.context_cache.invalidate, user_id, session_id)
        return None
