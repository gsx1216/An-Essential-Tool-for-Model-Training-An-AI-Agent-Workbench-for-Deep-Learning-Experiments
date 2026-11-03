"""早期 JSON 聊天存储练习；正式应用使用 app.repositories.message_repository。"""

from app.logging_config import logged_operation

import json
from pathlib import Path


class ChatRepository:
    @logged_operation("chat_repository.__init__", log_success=False, expected_errors=())
    def __init__(self, file_path: str):
        self.file_path = Path(file_path)

    @logged_operation("chat_repository.save")
    def save(self, messages: list) -> None:
        self.file_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with self.file_path.open("w", encoding="utf-8") as file:
            json.dump(
                messages,
                file,
                ensure_ascii=False,
                indent=2,
            )

    @logged_operation("chat_repository.load")
    def load(self) -> list:
        if not self.file_path.exists():
            return []

        content = self.file_path.read_text(encoding="utf-8")

        if not content.strip():
            return []

        data = json.loads(content)

        return [tuple(message) for message in data]
