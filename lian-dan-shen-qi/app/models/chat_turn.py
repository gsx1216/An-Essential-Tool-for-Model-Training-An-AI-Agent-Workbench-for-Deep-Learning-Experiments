from dataclasses import dataclass


class TurnAlreadyCompletedError(ValueError):
    """重复发送已完成请求时，让服务直接读取原回复。"""


@dataclass(frozen=True)
class ChatTurn:
    """一轮对话的状态；lease_active 由数据库时间判断。"""

    turn_id: str
    status: str
    user_seq: int
    attempt_id: str | None
    lease_active: bool
    error_code: str | None
