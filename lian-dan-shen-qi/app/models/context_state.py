"""上下文练习的数据结构；不访问数据库，不保存用户凭据。"""

from dataclasses import dataclass
from typing import Literal


ModelMessage = tuple[str, str]


class ContextTooLargeError(ValueError):
    """必要内容超过应用输入预算，不能通过丢弃当前问题来继续。"""


class ContextCompactionError(RuntimeError):
    """摘要为空、过长或压缩无效；保留旧摘要和原始消息。"""


class InputCompressionError(RuntimeError):
    """当前问题精简器未配置或调用失败；不能改写原文后假装成功。"""


class ContextConflictError(ValueError):
    """摘要版本或会话消息已变化；拒绝将旧结果覆盖新状态。"""


class ContextStorageError(RuntimeError):
    """数据库中的摘要或原文结构无效，不能静默当作空历史继续。"""


@dataclass(frozen=True)
class ContextMessage:
    """使用真实数据库 seq；role 为 human 或 ai，content 保留空格与换行。"""

    seq: int
    role: Literal["human", "ai"]
    content: str


@dataclass(frozen=True)
class HistoryGap:
    """缺失的真实序号范围；仅表示未知，不代表补写了一条聊天消息。"""

    start_seq: int
    end_seq: int


@dataclass(frozen=True)
class ContextSummary:
    """仅覆盖截至 summarized_until_seq 的完整历史；不能覆盖当前待答问题。"""

    content: str
    summarized_until_seq: int
    version: int
    missing_ranges: tuple[HistoryGap, ...] = ()


@dataclass(frozen=True)
class PreparedContext:
    """本次准备结果；调用方以后负责条件写入摘要，再交给模型网关。"""

    model_messages: list[ModelMessage]
    summary: ContextSummary | None
    input_tokens: int
    was_compacted: bool
    missing_ranges: tuple[HistoryGap, ...] = ()
    input_compressed: bool = False
