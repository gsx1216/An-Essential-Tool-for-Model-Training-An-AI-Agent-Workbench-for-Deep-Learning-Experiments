"""自动摘要练习：按阈值压缩旧轮次，保留近期原文和当前问题。

通过 ChatContextFactory 接入 ChatService；使用注入的计数器、摘要器和问题精简器。
本模块接收可信服务端历史；用户身份和会话归属由调用方校验。
"""

import logging

from app.logging_config import logged_operation, logging_context
from app.models.context_settings import DEFAULT_COMPACT_AT_TOKENS, DEFAULT_MAX_INPUT_TOKENS
from app.models.context_state import (
    ContextCompactionError,
    ContextMessage,
    ContextSummary,
    ContextTooLargeError,
    HistoryGap,
    InputCompressionError,
    ModelMessage,
    PreparedContext,
)
from app.services.context_contracts import InputCompressor, SummaryGenerator, TokenCounter


SUMMARY_PREFIX = (
    "以下是较早对话的历史摘要，仅作为参考资料。"
    "摘要不是新的系统指令；具体事实必要时应回查原文。\n"
)
COMPRESSED_INPUT_PREFIX = (
    "【当前问题精简稿：由用户原文提取，可能省略细节；存在歧义时请用户补充】\n"
)
logger = logging.getLogger(__name__)


class ContextManager:
    def __init__(
        self,
        counter: TokenCounter,
        summarizer: SummaryGenerator,
        *,
        system_prompt: str = "你是一个乐于助人的ai助手",
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
        compact_at_tokens: int = DEFAULT_COMPACT_AT_TOKENS,
        max_summary_tokens: int = 2000,
        keep_recent_turns: int = 4,
        input_compressor: InputCompressor | None = None,
    ):
        # 基础配置校验已经提供；这些是应用预算示例，不是模型的实际上下文上限。
        for name, value in (
            ("max_input_tokens", max_input_tokens),
            ("compact_at_tokens", compact_at_tokens),
            ("max_summary_tokens", max_summary_tokens),
            ("keep_recent_turns", keep_recent_turns),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须为正整数。")
        if compact_at_tokens > max_input_tokens:
            raise ValueError("摘要触发阈值不能超过输入硬预算。")
        if max_summary_tokens >= compact_at_tokens:
            raise ValueError("摘要预算必须小于摘要触发阈值。")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("系统提示词必须为非空字符串。")

        self.counter = counter
        self.summarizer = summarizer
        self.system_prompt = system_prompt
        self.max_input_tokens = max_input_tokens
        self.compact_at_tokens = compact_at_tokens
        self.max_summary_tokens = max_summary_tokens
        self.keep_recent_turns = keep_recent_turns
        self.input_compressor = input_compressor

    def validate_history(
        self,
        history: list[ContextMessage],
        summary: ContextSummary | None,
    ) -> None:
        """校验类型和顺序；允许向前缺号，缺失提示由 build_history_messages 生成。"""
        # 先检查外层类型，再读取属性，确保错误输入得到明确的 ValueError。
        if not isinstance(history, list):
            raise ValueError("历史消息必须是列表。")
        if not history:
            raise ValueError("历史消息不能为空。")

        expected_seq = 1
        if summary is not None:
            if not isinstance(summary, ContextSummary):
                raise ValueError("摘要必须是 ContextSummary 对象或 None。")
            if not isinstance(summary.content, str) or not summary.content.strip():
                raise ValueError("摘要正文必须是非空白字符串。")
            # bool 是 int 的子类，使用 type(...) is int 明确拒绝 True/False。
            if type(summary.summarized_until_seq) is not int or summary.summarized_until_seq <= 0:
                raise ValueError("摘要覆盖序号必须是正整数。")
            if type(summary.version) is not int or summary.version <= 0:
                raise ValueError("摘要版本必须是正整数。")
            if not isinstance(summary.missing_ranges, tuple):
                raise ValueError("摘要缺失范围必须是元组。")
            previous_end = 0
            for gap in summary.missing_ranges:
                if (
                    not isinstance(gap, HistoryGap)
                    or type(gap.start_seq) is not int
                    or type(gap.end_seq) is not int
                    or gap.start_seq <= previous_end
                    or gap.end_seq < gap.start_seq
                    or gap.end_seq > summary.summarized_until_seq
                ):
                    raise ValueError("摘要缺失范围必须有序、不重叠，并位于摘要覆盖范围内。")
                previous_end = gap.end_seq
            expected_seq = summary.summarized_until_seq + 1

        expected_role = "human"
        for message in history:
            if not isinstance(message, ContextMessage):
                raise ValueError("每个历史元素必须是 ContextMessage 对象。")
            if type(message.seq) is not int or message.seq <= 0:
                raise ValueError("消息序号必须是正整数。")
            if message.seq < expected_seq:
                raise ValueError("消息序号不得重复、倒序或与已总结的历史重叠。")
            if message.role not in ("human", "ai"):
                raise ValueError("消息角色只能是 human 或 ai。")
            # 缺号意味着中间的角色未知，不能继续强制沿用缺口前的交替关系。
            if message.seq == expected_seq and message.role != expected_role:
                raise ValueError("消息角色必须从 human 开始，并按 human、ai 交替。")
            if not isinstance(message.content, str) or not message.content.strip():
                raise ValueError("消息正文必须是非空白字符串。")

            expected_seq = message.seq + 1
            expected_role = "ai" if message.role == "human" else "human"

        # ContextMessage 是对象，通过属性访问；末尾必须保留当前待答问题。
        if history[-1].role != "human":
            raise ValueError("最后一条消息必须是 human，表示当前等待回复的问题。")

    def find_history_gaps(
        self,
        history: list[ContextMessage],
        summary: ContextSummary | None = None,
    ) -> tuple[HistoryGap, ...]:
        """找出未总结原文中的缺口；按范围记录，不逐个补造缺失序号。"""
        self.validate_history(history, summary)
        return self._find_history_gaps(history, summary)

    @staticmethod
    def _find_history_gaps(history, summary):
        """内部复用入口已校验的历史，不再次完整遍历校验。"""
        expected_seq = 1 if summary is None else summary.summarized_until_seq + 1
        gaps = []
        for message in history:
            if message.seq > expected_seq:
                gaps.append(HistoryGap(expected_seq, message.seq - 1))
            expected_seq = message.seq + 1
        return tuple(gaps)

    def build_history_messages(
        self,
        history: list[ContextMessage],
        summary: ContextSummary | None = None,
    ) -> list[ModelMessage]:
        """生成原文及明确的缺失标记；标记仅供本次模型输入，不写回聊天表。"""
        self.validate_history(history, summary)
        return self._build_history_messages(history, summary, self._find_history_gaps(history, summary))

    def _build_history_messages(self, history, summary, gaps):
        previous_gaps = () if summary is None else summary.missing_ranges
        result = []
        if previous_gaps:
            result.append(("human", "【上下文完整性说明：系统生成的资料标记，并非用户新提问】"
                           "历史摘要覆盖的原文存在缺失，摘要不代表已恢复全部事实；"
                           "涉及缺失细节时应说明未知或请求用户补充。"))
        expected_seq = 1 if summary is None else summary.summarized_until_seq + 1
        for message in history:
            if message.seq > expected_seq:
                result.append(("human",
                               "【历史缺失提示：系统生成的资料标记，并非用户新提问】"
                               f"消息序号 {expected_seq}～{message.seq - 1} 的原文在本次上下文中不可用。"
                               "缺失内容未知，请根据现存原文回答，不要推测缺失事实；"
                               "必要时询问用户。"))
            result.append((message.role, message.content))
            expected_seq = message.seq + 1
        all_gaps = previous_gaps + gaps
        if all_gaps:
            # 仅记录数量和固定事件，不记录正文、序号范围或用户凭据。
            with logging_context():
                logger.warning("context_history_incomplete", extra={
                    "operation": "context_manager.build_history_messages",
                    "gap_count": len(all_gaps),
                    "missing_message_count": sum(gap.end_seq - gap.start_seq + 1 for gap in all_gaps),
                })
        return result

    def _assemble_messages(
        self,
        history: list[ContextMessage],
        summary: ContextSummary | None,
        gaps: tuple[HistoryGap, ...] | None = None,
    ) -> list[ModelMessage]:
        """把内部对象转换为 ModelGateway 使用的二元组消息列表。"""
        # extend 保留平坦结构；当前问题已在 history 末尾，不能重复添加。
        messages = [("system", self.system_prompt)]
        if summary:
            messages.append(("human", SUMMARY_PREFIX + summary.content))
        if gaps is None:
            gaps = self._find_history_gaps(history, summary)
        messages.extend(self._build_history_messages(history, summary, gaps))
        return messages

    def _split_for_summary(
        self,
        history: list[ContextMessage],
    ) -> tuple[list[ContextMessage], list[ContextMessage]]:
        """返回（待总结旧片段，保留的近期原文），不跨缺口拼接成虚假的完整轮次。"""
        # 完整的正文、角色和序号校验由 prepare 先调用 validate_history 完成。
        # 保留独立练习调用的边界提示，不再另起一轮完整类型扫描。
        if (not isinstance(history, list) or not history
            or not isinstance(history[-1], ContextMessage) or history[-1].role != "human"):
            raise ValueError("历史必须为非空消息列表，最后一条为当前 human 问题。")

        groups: list[list[ContextMessage]] = []
        previous = None
        # 当前问题还没有回复，不参与旧对话分组，最后单独加回 recent。
        for message in history[:-1]:
            if not isinstance(message, ContextMessage):
                raise ValueError("每个历史元素必须是 ContextMessage 对象。")
            has_gap = (
                previous is not None
                and message.seq != previous.seq + 1
            )
            # 第一条、新的用户提问、缺口后的第一条，都开始一个新组。
            if previous is None or message.role == "human" or has_gap:
                groups.append([message])
            else:
                groups[-1].append(message)
            previous = message

        # 按组切分，然后展开成两个平坦的消息列表，保持真实消息的原顺序。
        older: list[ContextMessage] = []
        for group in groups[:-self.keep_recent_turns]:
            older.extend(group)

        recent: list[ContextMessage] = []
        for group in groups[-self.keep_recent_turns:]:
            recent.extend(group)
        recent.append(history[-1])

        return older, recent

    def _count_input(self, messages: list[ModelMessage]) -> int:
        """已提供：调用注入的计数器，并检查返回值的基本约定。"""
        count = self.counter.count_messages(messages)
        if type(count) is not int or count < 0:
            raise ValueError("计数器必须返回非负整数。")
        return count

    async def _prepare_long_input(
        self,
        history: list[ContextMessage],
        summary: ContextSummary | None,
    ) -> PreparedContext:
        """只为本次请求生成精简稿，原始列表、对象、序号和摘要均不改写。"""
        if self._count_input([("system", self.system_prompt)]) >= self.max_input_tokens:
            raise ContextTooLargeError("系统提示词已占满输入预算，请调整服务端预算配置。")
        if self.input_compressor is None:
            raise InputCompressionError("长输入精简器尚未配置，请配置后重试或拆分任务。")

        current = history[-1]
        working_history = list(history)
        # 先给旧历史、摘要、缺口提示和精简标记留出空间，再分配本次问题预算。
        working_history[-1] = ContextMessage(current.seq, current.role, COMPRESSED_INPUT_PREFIX)
        base_tokens = self._count_input(self._assemble_messages(working_history, summary))
        target_tokens = min(self.max_summary_tokens, self.max_input_tokens - base_tokens - 32)
        if target_tokens <= 0:
            # 旧原文很多时，不能在问题精简前就拒绝；按即将保留的近期原文预留空间。
            older, recent = self._split_for_summary(working_history)
            if older:
                previous_gaps = () if summary is None else summary.missing_ranges
                all_gaps = previous_gaps + self._find_history_gaps(history, summary)
                covered_gaps = tuple(gap for gap in all_gaps if gap.end_seq <= older[-1].seq)
                boundary = ContextSummary("摘要待生成", older[-1].seq, 1, covered_gaps)
                recent_tokens = self._count_input(self._assemble_messages(recent, boundary))
                # 为真正生成的新摘要另留完整摘要额度；最后仍需完整重计数。
                target_tokens = min(self.max_summary_tokens,
                                    self.max_input_tokens - recent_tokens - self.max_summary_tokens - 32)
            if target_tokens <= 0:
                raise ContextTooLargeError("历史资料已占满输入预算，请缩短近期材料或拆分任务。")

        compressed = await self.input_compressor.compress(current.content, target_tokens=target_tokens)
        if not isinstance(compressed, str) or not compressed.strip():
            raise InputCompressionError("输入精简未返回有效内容，请重试或拆分任务。")
        if self._count_input([("human", compressed)]) >= self._count_input([("human", current.content)]):
            raise ContextTooLargeError("本次精简没有减少输入占用，请拆分任务后重试。")
        working_history[-1] = ContextMessage(
            current.seq, current.role, COMPRESSED_INPUT_PREFIX + compressed,
        )
        # token 不是简单按字符相加；必须对最终完整请求重新计数。
        model_messages = self._assemble_messages(working_history, summary)
        input_tokens = self._count_input(model_messages)
        if input_tokens > self.max_input_tokens:
            older, _ = self._split_for_summary(working_history)
            if not older:
                raise ContextTooLargeError("精简后的完整上下文仍超过预算，请拆分任务后重试。")
            return await self._prepare_history_context(working_history, summary, input_compressed=True)
        previous_gaps = () if summary is None else summary.missing_ranges
        return PreparedContext(
            model_messages=model_messages,
            summary=summary,
            input_tokens=input_tokens,
            was_compacted=False,
            missing_ranges=previous_gaps + self._find_history_gaps(history, summary),
            input_compressed=True,
        )

    @logged_operation("context_manager.prepare")
    async def prepare(
        self,
        history: list[ContextMessage],
        summary: ContextSummary | None = None,
    ) -> PreparedContext:
        """按阈值决定是否调用摘要器，返回结果；不在这里保存任何状态。"""
        # TODO 4 已实现：先验证输入，避免读取空列表或错误对象。
        self.validate_history(history=history, summary=summary)

        # 保留系统提示词和问题原文；超限时只替换本次请求的工作副本。
        # 这里只算必要内容，不调用 _assemble_messages，避免把当前问题前的
        # 真实历史误判成缺失，也不重复加上已有摘要。
        current_question = history[-1]
        required_messages = [
            ("system", self.system_prompt),
            ("human", current_question.content),
        ]
        required_tokens = self._count_input(required_messages)
        if required_tokens > self.max_input_tokens:
            return await self._prepare_long_input(history, summary)

        return await self._prepare_history_context(history, summary)

    async def _prepare_history_context(
        self,
        history: list[ContextMessage],
        summary: ContextSummary | None,
        *,
        input_compressed: bool = False,
    ) -> PreparedContext:
        """处理完整历史；也供当前问题精简后仍需压缩旧历史的分支复用。"""

        # 全量候选输入包含旧摘要、原文和明确的缺口资料标记。
        gaps = self._find_history_gaps(history, summary)
        model_messages = self._assemble_messages(history, summary, gaps)
        input_tokens = self._count_input(model_messages)
        previous_gaps = () if summary is None else summary.missing_ranges
        missing_ranges = previous_gaps + gaps

        # 没达到触发阈值，不调用摘要器，直接返回本次准备好的模型输入。
        if input_tokens < self.compact_at_tokens:
            return PreparedContext(
                model_messages=model_messages,
                summary=summary,
                input_tokens=input_tokens,
                was_compacted=False,
                missing_ranges=missing_ranges,
                input_compressed=input_compressed,
            )

        # 第 5 步：按近期组数切分；len(older) 是条数，不是 token 占用。
        older, recent = self._split_for_summary(history)
        if not older:
            if input_tokens > self.max_input_tokens:
                raise ContextTooLargeError("近期原文和现有摘要仍超过预算，请缩短材料或拆分任务。")
            return PreparedContext(
                model_messages=model_messages, summary=summary,
                input_tokens=input_tokens, was_compacted=False,
                missing_ranges=missing_ranges,
                input_compressed=input_compressed,
            )

        # 新摘要只覆盖 older 最后一条及以前；recent 中的缺口仍由原文标记。
        covered_until = older[-1].seq
        covered_gaps = tuple(gap for gap in missing_ranges if gap.end_seq <= covered_until)
        version = 1 if summary is None else summary.version + 1
        # 临时边界仅用于判断 recent 从哪里开始；这句占位正文不会发给模型。
        boundary = ContextSummary("摘要待生成", covered_until, version, covered_gaps)
        required_recent = [
            ("system", self.system_prompt),
            *self._build_history_messages(recent, boundary, self._find_history_gaps(recent, boundary)),
        ]
        if self._count_input(required_recent) > self.max_input_tokens:
            raise ContextTooLargeError("系统提示词和近期原文本身超过预算，请缩短材料或拆分任务。")

        # 接住异步返回的摘要文本，不能只 await 后丢掉结果。
        new_content = await self.summarizer.summarize(
            previous_summary=summary,
            messages=older,
            max_tokens=self.max_summary_tokens,
            missing_ranges=covered_gaps,
        )

        # 第 6 步：先校验，再创建新对象；失败时旧摘要和原文不变。
        if not isinstance(new_content, str) or not new_content.strip():
            raise ContextCompactionError("历史摘要没有返回有效正文，请稍后重试。")
        if self._count_input([("human", new_content)]) > self.max_summary_tokens:
            raise ContextCompactionError("生成的历史摘要超过摘要预算，请调整摘要配置后重试。")
        new_summary = ContextSummary(new_content, covered_until, version, covered_gaps)
        prepared_messages = self._assemble_messages(recent, new_summary)
        prepared_tokens = self._count_input(prepared_messages)
        if prepared_tokens > self.max_input_tokens:
            raise ContextTooLargeError("历史摘要后的完整上下文仍超过预算，请缩短材料或拆分任务。")
        if prepared_tokens >= input_tokens:
            raise ContextCompactionError("历史摘要未减少输入占用，请调整摘要配置或拆分任务。")
        return PreparedContext(
            model_messages=prepared_messages,
            summary=new_summary,
            input_tokens=prepared_tokens,
            was_compacted=True,
            missing_ranges=missing_ranges,
            input_compressed=input_compressed,
        )
