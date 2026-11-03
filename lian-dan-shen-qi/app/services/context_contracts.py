"""计数与摘要的接口约定；真正实现和练习假对象使用同样的方法名。"""

from typing import Protocol

from app.models.context_state import ContextMessage, ContextSummary, HistoryGap, ModelMessage


class TokenCounter(Protocol):
    def count_messages(self, messages: list[ModelMessage]) -> int:
        """返回完整模型输入的 token 数或明确标注的估算值，包含消息格式开销。"""
        ...


class InputCompressor(Protocol):
    async def compress(self, content: str, *, target_tokens: int) -> str:
        """只精简当前输入，返回派生文本；不保存或替换原文。"""
        ...


class SummaryGenerator(Protocol):
    async def summarize(
        self,
        *,
        previous_summary: ContextSummary | None,
        messages: list[ContextMessage],
        max_tokens: int,
        missing_ranges: tuple[HistoryGap, ...] = (),
    ) -> str:
        """总结旧摘要和原文；明确说明 missing_ranges 未知，不能编造内容；不写库。"""
        ...
