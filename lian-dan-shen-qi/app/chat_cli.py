import asyncio
import logging

import mysql.connector

from app.config import get_mysql_password
from app.logging_config import log_failure, logged_operation
from app.repositories.session_repository import SessionRepository
from app.services.session_service import prepare_session


@logged_operation("chat_cli.main", log_success=False)
async def main():
    try:
        password = get_mysql_password()
        user_id = int(input("请输入已有用户 ID：").strip())

        repository = SessionRepository(password)
        sessions = await asyncio.to_thread(repository.list_sessions, user_id)

        print("\n0. 新聊天")
        for number, (_session_id, title) in enumerate(sessions, start=1):
            print(f"{number}. {title}")

        entered_choice = input("请选择序号，直接回车创建新聊天：").strip()
        try:
            choice = int(entered_choice or "0")
        except ValueError as error:
            raise ValueError("请输入列表中的数字序号，0 表示新聊天。") from error

        if not 0 <= choice <= len(sessions):
            raise ValueError("所选序号不在会话列表中，请重新运行后选择。")

        # 新聊天不传已有 ID，由仓储生成 UUID；历史聊天使用列表中的 ID。
        entered_id = "" if choice == 0 else sessions[choice - 1][0]

        session_id, history = await asyncio.to_thread(
            prepare_session,
            password=password,
            user_id=user_id,
            entered_id=entered_id,
        )

    except (ValueError, mysql.connector.Error) as error:
        log_failure(logging.getLogger("app.chat_cli"), error,
                    level=logging.WARNING if isinstance(error, ValueError) else logging.ERROR)
        print("进入会话失败：", error)
        return

    print("当前会话 ID：", session_id)
    print("历史消息数量：", len(history))

    for role, content in history:
        print(f"{role}：{content}")


if __name__ == "__main__":
    asyncio.run(main())
