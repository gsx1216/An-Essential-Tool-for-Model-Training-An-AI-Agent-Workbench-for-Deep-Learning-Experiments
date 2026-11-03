"""无下载依赖的文本预算估算；不是供应商 tokenizer，也不用于计费。"""

from app.models.context_state import ModelMessage


class Utf8BudgetCounter:
    # 为角色和封装预留固定余量；真实供应商模板可能不同，故不宣称精确上界。
    request_overhead = 64
    message_overhead = 24

    def count_messages(self, messages: list[ModelMessage]) -> int:
        if not isinstance(messages, list):
            raise ValueError("模型消息必须为列表。")
        count = self.request_overhead
        try:
            for message in messages:
                if (not isinstance(message, tuple) or len(message) != 2
                    or message[0] not in ("system", "human", "ai")
                    or not isinstance(message[1], str)):
                    raise ValueError("模型消息必须为有效的角色、文本二元组。")
                role, content = message
                count += self.message_overhead + len(role.encode("utf-8")) + len(content.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("消息包含不能编码的字符。") from error
        return count

    def output_budget(self, target_tokens: int) -> tuple[int, str]:
        remaining = target_tokens - self.count_messages([("human", "")])
        if remaining <= 0:
            raise ValueError("生成预算不足以容纳消息格式开销。")
        # 额度与供应商 token 不是同一单位。压低实际输出上限，并提示字节目标；
        # 回包仍要重新计数，不把此换算当作数学保证。
        output_limit = max(1, min(4096, remaining // 8))
        return output_limit, self.output_hint(output_limit)

    def output_hint(self, output_limit: int) -> str:
        return f"\n额外长度限制：正文 UTF-8 编码最多约 {output_limit * 8} 字节；优先压缩为简短要点。"
