"""部署级上下文预算，不含用户身份、消息或 Key。数值使用所注入计数器的预算刻度。"""

from dataclasses import dataclass


# 48K 应用输入预算；当前计数器按 UTF-8 字节估算，不是供应商精确 token。
DEFAULT_MAX_INPUT_TOKENS = 48 * 1024
DEFAULT_COMPACT_AT_TOKENS = DEFAULT_MAX_INPUT_TOKENS * 4 // 5


@dataclass(frozen=True)
class ContextSettings:
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    compact_at_tokens: int = DEFAULT_COMPACT_AT_TOKENS
    max_summary_tokens: int = 2000
    keep_recent_turns: int = 4
    model_request_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    max_summary_calls: int = 16
    timeout_seconds: int = 180
    lease_seconds: int = 1800

    def __post_init__(self):
        for value in vars(self).values():
            if type(value) is not int or value <= 0:
                raise ValueError("上下文配置必须全部为正整数。")
        if not self.max_summary_tokens < self.compact_at_tokens <= self.max_input_tokens:
            raise ValueError("摘要预算必须小于触发阈值，阈值不能超过输入预算。")
        if self.max_summary_tokens > 4096:
            raise ValueError("摘要预算不能超过当前网关单次输出上限。")
        if self.lease_seconds > 3600:
            raise ValueError("上下文生成任务有效期不能超过 3600 秒。")
