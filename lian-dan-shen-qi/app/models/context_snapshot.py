"""摘要存储的数据快照与边界校验；无数据库连接和模型调用。"""

from dataclasses import dataclass
from uuid import UUID

from app.models.context_state import ContextMessage, ContextSummary, HistoryGap
from app.validators import validate_positive_integer


@dataclass(frozen=True)
class ContextAttempt:
    """当前已领取的轮次与生成尝试；不保存登录 Token 或模型 Key。"""

    turn_id: str
    attempt_id: str

    def __post_init__(self):
        for name in ("turn_id", "attempt_id"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError("生成任务标识必须为 UUID 字符串。")
            try:
                object.__setattr__(self, name, UUID(value.strip()).hex)
            except ValueError as error:
                raise ValueError("生成任务标识格式不正确。") from error


def validate_summary(summary: ContextSummary) -> None:
    if not isinstance(summary, ContextSummary):
        raise ValueError("摘要必须为 ContextSummary 对象。")
    if not isinstance(summary.content, str) or not summary.content.strip():
        raise ValueError("摘要正文必须为非空白字符串。")
    validate_positive_integer(summary.summarized_until_seq, "摘要覆盖序号")
    validate_positive_integer(summary.version, "摘要版本")
    if not isinstance(summary.missing_ranges, tuple):
        raise ValueError("摘要缺失范围必须为元组。")
    previous_end = 0
    for gap in summary.missing_ranges:
        if not isinstance(gap, HistoryGap):
            raise ValueError("缺失范围必须为 HistoryGap 对象。")
        validate_positive_integer(gap.start_seq, "缺失范围起点")
        validate_positive_integer(gap.end_seq, "缺失范围终点")
        if (gap.start_seq <= previous_end or gap.end_seq < gap.start_seq
            or gap.end_seq > summary.summarized_until_seq):
            raise ValueError("摘要缺失范围必须有序、不重叠并位于摘要覆盖范围内。")
        previous_end = gap.end_seq


@dataclass(frozen=True)
class ContextSnapshot:
    """在一次短事务中读取的摘要、未总结原文和消息末尾；不含用户凭据。"""

    summary: ContextSummary | None
    messages: tuple[ContextMessage, ...]
    tail_seq: int

    @property
    def version(self) -> int:
        return 0 if self.summary is None else self.summary.version
