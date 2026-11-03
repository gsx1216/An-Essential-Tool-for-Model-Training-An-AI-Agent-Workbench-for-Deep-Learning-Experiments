from app.logging_config import logged_operation

from app.repositories.message_repository import MessageRepository
from app.repositories.session_repository import SessionRepository


@logged_operation("session_service.prepare_session")
def prepare_session(
    password: str,
    user_id: int,
    entered_id: str = "",
) -> tuple[str, list[tuple[str, str]]]:
    if not isinstance(entered_id, str):
        raise ValueError("会话 ID 输入必须是字符串。")

    entered_id = entered_id.strip()

    session_repository = SessionRepository(password)
    message_repository = MessageRepository(password)

    if entered_id:
        # 输入已有会话 ID：仓储会检查用户存在、UUID 格式和会话归属。
        session = session_repository.get_session(user_id, entered_id)
        if session is None:
            raise ValueError("会话不存在或不属于当前用户。")
        session_id = session[0]
    else:
        session_id = session_repository.create_session(user_id=user_id)
        return session_id, []

    messages = message_repository.load_messages(user_id, session_id)
    return session_id, messages
