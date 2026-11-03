"""上下文缓存配置和严格序列化；缓存只包含摘要与真实消息，不包含凭据。"""

from dataclasses import dataclass, field
import json
import re
from urllib.parse import urlsplit

from app.models.context_snapshot import ContextSnapshot, validate_summary
from app.models.context_state import ContextMessage, ContextSummary, HistoryGap
from app.validators import normalize_session_id, validate_message_content, validate_positive_integer


class CacheEntryTooLarge(ValueError):
    """正常大输入可绕过缓存，不应触发整个进程的故障熔断。"""


@dataclass(frozen=True)
class ContextCacheSettings:
    enabled: bool = True
    redis_url: str = field(default="redis://127.0.0.1:6379/0", repr=False)
    prefix: str = "experiment_assistant"
    ttl_seconds: int = 604800  # 7 天；有效聊天和成功写回后续期。
    max_entry_bytes: int = 1048576
    failure_backoff_seconds: int = 10

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("上下文缓存开关必须为布尔值。")
        try:
            parsed = urlsplit(self.redis_url)
            if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname or parsed.port == 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("上下文缓存连接配置不正确。") from None
        if type(self.prefix) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", self.prefix):
            raise ValueError("上下文缓存前缀不正确。")
        for name, low, high in (("ttl_seconds", 60, 604800), ("max_entry_bytes", 4096, 8388608),
                                ("failure_backoff_seconds", 1, 300)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("上下文缓存配置超出允许范围。")


def _integer(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= 2**64 - 1:
        raise ValueError("缓存序号或版本不正确。")
    return value


def validate_snapshot(snapshot):
    if not isinstance(snapshot, ContextSnapshot) or type(snapshot.messages) is not tuple:
        raise ValueError("缓存快照结构不正确。")
    tail = _integer(snapshot.tail_seq, zero=True)
    covered = 0
    if snapshot.summary is not None:
        validate_summary(snapshot.summary)
        covered = snapshot.summary.summarized_until_seq
        if covered >= tail:
            raise ValueError("缓存摘要不能覆盖消息末尾。")
    if len(snapshot.messages) > 10000:
        raise CacheEntryTooLarge("缓存消息数量过多。")
    previous = covered
    for message in snapshot.messages:
        if not isinstance(message, ContextMessage):
            raise ValueError("缓存消息结构不正确。")
        seq = _integer(message.seq)
        if seq <= previous or message.role not in {"human", "ai"}:
            raise ValueError("缓存消息顺序或角色不正确。")
        validate_message_content(message.content)
        previous = seq
    if previous != tail:
        raise ValueError("缓存末尾不一致。")


def encode_snapshot(user_id, session_id, snapshot, max_bytes):
    user_id = validate_positive_integer(user_id, "用户 ID")
    session_id = normalize_session_id(session_id)
    validate_snapshot(snapshot)
    summary = snapshot.summary
    value = {
        "schema_version": 1, "user_id": user_id, "session_id": session_id,
        "tail_seq": snapshot.tail_seq,
        "summary": None if summary is None else {
            "content": summary.content, "summarized_until_seq": summary.summarized_until_seq,
            "version": summary.version,
            "missing_ranges": [{"start_seq": gap.start_seq, "end_seq": gap.end_seq}
                               for gap in summary.missing_ranges],
        },
        "messages": [{"seq": message.seq, "role": message.role, "content": message.content}
                     for message in snapshot.messages],
    }
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > max_bytes:
        raise CacheEntryTooLarge("上下文超出缓存条目上限，保留数据库读取。")
    return raw


def decode_snapshot(raw, user_id, session_id, max_bytes):
    if type(raw) is not bytes or len(raw) > max_bytes:
        raise ValueError("缓存条目过大或类型错误。")
    # 拒绝重复键，避免不同解析器对同一份数据产生不同理解。
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("缓存包含重复字段。")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if (type(value) is not dict or set(value) != {
            "schema_version", "user_id", "session_id", "tail_seq", "summary", "messages"}
        or type(value["schema_version"]) is not int or value["schema_version"] != 1
        or type(value["user_id"]) is not int or value["user_id"] != user_id
        or value["session_id"] != normalize_session_id(session_id)):
        raise ValueError("缓存版本或归属不一致。")
    summary = value["summary"]
    if summary is not None:
        if type(summary) is not dict or set(summary) != {"content", "summarized_until_seq", "version", "missing_ranges"}:
            raise ValueError("缓存摘要字段不正确。")
        ranges = summary["missing_ranges"]
        if type(ranges) is not list or len(ranges) > 10000:
            raise ValueError("缓存缺口结构不正确。")
        gaps = []
        for item in ranges:
            if type(item) is not dict or set(item) != {"start_seq", "end_seq"}:
                raise ValueError("缓存缺口字段不正确。")
            gaps.append(HistoryGap(item["start_seq"], item["end_seq"]))
        summary = ContextSummary(summary["content"], summary["summarized_until_seq"],
                                 summary["version"], tuple(gaps))
    messages = value["messages"]
    if type(messages) is not list or len(messages) > 10000:
        raise ValueError("缓存消息结构不正确。")
    result = []
    for item in messages:
        if type(item) is not dict or set(item) != {"seq", "role", "content"}:
            raise ValueError("缓存消息字段不正确。")
        result.append(ContextMessage(item["seq"], item["role"], item["content"]))
    snapshot = ContextSnapshot(summary, tuple(result), value["tail_seq"])
    validate_snapshot(snapshot)
    return snapshot
