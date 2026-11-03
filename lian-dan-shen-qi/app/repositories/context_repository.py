"""上下文仓储：短事务读取一致快照，按会话归属、版本和真实原文校验摘要写入。"""

import json
import logging
from pathlib import Path

from app.database import get_connection
from app.errors import SessionNotFoundError
from app.logging_config import logged_operation
from app.models.context_snapshot import ContextAttempt, ContextSnapshot, validate_summary
from app.models.context_cache import validate_snapshot
from app.models.context_state import (
    ContextConflictError, ContextMessage, ContextStorageError, ContextSummary, HistoryGap,
)
from app.validators import normalize_session_id, validate_message_content, validate_positive_integer

logger = logging.getLogger(__name__)


class ContextRepository:
    @logged_operation("context_repository.__init__", log_success=False, expected_errors=())
    def __init__(self, password: str):
        self.password = password
        sql_root = Path(__file__).resolve().parents[2] / "sql"
        for name in ("lock_session", "load_context_summary", "load_context_messages",
                     "load_context_covered_seqs", "last_message_seq",
                     "insert_context_summary", "update_context_summary", "get_turn_for_update", "load_context_marker"):
            setattr(self, name + "_sql", (sql_root / f"{name}.sql").read_text(encoding="utf-8"))

    def _require_session(self, cursor, user_id, session_id):
        # 用户不存在、会话不存在、归属不符统一处理，不泄露其他用户的会话。
        cursor.execute(self.lock_session_sql, (user_id, session_id))
        if cursor.fetchone() is None:
            raise SessionNotFoundError("会话不存在或不属于该用户。")

    def _load_summary(self, cursor, user_id, session_id):
        cursor.execute(self.load_context_summary_sql, (user_id, session_id))
        row = cursor.fetchone()
        if row is None:
            return None
        try:
            gaps_data = json.loads(row[3])
            if not isinstance(gaps_data, list):
                raise ValueError("摘要缺口必须为 JSON 数组。")
            gaps = []
            for item in gaps_data:
                if not isinstance(item, dict) or set(item) != {"start_seq", "end_seq"}:
                    raise ValueError("缺失范围字段不正确。")
                gaps.append(HistoryGap(item["start_seq"], item["end_seq"]))
            summary = ContextSummary(row[0], row[1], row[2], tuple(gaps))
            validate_summary(summary)
            return summary
        except (ValueError, TypeError, IndexError) as error:
            raise ContextStorageError("已存上下文格式异常，请稍后重试。") from error

    def _require_attempt(self, cursor, session_id, attempt):
        if attempt is None:
            return  # 独立仓储工具可读取普通快照；HTTP 链路必须传入已领取的任务。
        if not isinstance(attempt, ContextAttempt):
            raise ValueError("生成尝试必须为 ContextAttempt 对象。")
        cursor.execute(self.get_turn_for_update_sql, (session_id, attempt.turn_id))
        row = cursor.fetchone()
        if (row is None or row[1] != "running" or row[3] != attempt.attempt_id or not row[4]
            or self._tail_seq(cursor, session_id) != row[2]):
            raise ContextConflictError("生成任务已失效，请读取当前会话状态后重试。")

    @logged_operation("context_repository.check_attempt")
    def check_attempt(self, user_id: int, session_id: str, attempt: ContextAttempt) -> None:
        user_id = validate_positive_integer(user_id, "用户 ID")
        session_id = normalize_session_id(session_id)
        if not isinstance(attempt, ContextAttempt):
            raise ValueError("必须提供已领取的生成任务。")
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._require_session(cursor, user_id, session_id)
                    self._require_attempt(cursor, session_id, attempt)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _tail_seq(self, cursor, session_id):
        cursor.execute(self.last_message_seq_sql, (session_id,))
        row = cursor.fetchone()
        if row is None:
            return 0
        try:
            return validate_positive_integer(row[0], "消息末尾序号")
        except (ValueError, TypeError, IndexError) as error:
            raise ContextStorageError("已存消息顺序异常，请稍后重试。") from error

    def _read_messages(self, cursor, user_id, session_id, after_seq, tail_seq, prefix=()):
        cursor.execute(self.load_context_messages_sql, (user_id, session_id, after_seq))
        messages = list(prefix)
        previous_seq = after_seq
        try:
            for row in cursor.fetchall():
                seq = validate_positive_integer(row[0], "消息序号")
                content = validate_message_content(row[2])
                if seq <= previous_seq or row[1] not in ("human", "ai"):
                    raise ValueError("消息顺序或角色不正确。")
                messages.append(ContextMessage(seq, row[1], content))
                previous_seq = seq
        except (ValueError, TypeError, IndexError) as error:
            raise ContextStorageError("已存消息格式异常，请稍后重试。") from error
        if previous_seq != tail_seq:
            raise ContextStorageError("上下文快照与消息末尾不一致，请稍后重试。")
        return tuple(messages)

    def _read_snapshot(self, cursor, user_id, session_id):
        summary = self._load_summary(cursor, user_id, session_id)
        tail_seq = self._tail_seq(cursor, session_id)
        after_seq = 0 if summary is None else summary.summarized_until_seq
        if summary is not None and after_seq >= tail_seq:
            raise ContextStorageError("摘要覆盖边界与现有消息不一致，请稍后重试。")
        messages = self._read_messages(cursor, user_id, session_id, after_seq, tail_seq)
        return ContextSnapshot(summary, messages, tail_seq)

    @logged_operation("context_repository.reconcile_context")
    def reconcile_context(self, user_id: int, session_id: str, cached: ContextSnapshot, *,
                          attempt: ContextAttempt | None = None) -> ContextSnapshot:
        """Redis 只是历史前缀；锁内校验归属/租约/摘要版本，原文只读增量。"""
        user_id = validate_positive_integer(user_id, "用户 ID")
        session_id = normalize_session_id(session_id)
        validate_snapshot(cached)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._require_session(cursor, user_id, session_id)
                    self._require_attempt(cursor, session_id, attempt)
                    cursor.execute(self.load_context_marker_sql, (user_id, session_id))
                    marker = cursor.fetchone()
                    if marker is None:
                        version, covered = 0, 0
                    else:
                        try:
                            version = validate_positive_integer(marker[0], "摘要版本")
                            covered = validate_positive_integer(marker[1], "摘要覆盖序号")
                        except (ValueError, TypeError, IndexError) as error:
                            raise ContextStorageError("已存上下文版本异常，请稍后重试。") from error
                    tail = self._tail_seq(cursor, session_id)
                    cache_covered = 0 if cached.summary is None else cached.summary.summarized_until_seq
                    if version != cached.version or covered != cache_covered or tail < cached.tail_seq:
                        snapshot = self._read_snapshot(cursor, user_id, session_id)
                        mode = "reload"
                    else:
                        messages = cached.messages if tail == cached.tail_seq else self._read_messages(
                            cursor, user_id, session_id, cached.tail_seq, tail, cached.messages)
                        snapshot = ContextSnapshot(cached.summary, messages, tail)
                        mode = "delta" if tail > cached.tail_seq else "unchanged"
                connection.commit()
                logger.info("cache_reconciled", extra={"user_id": user_id, "session_id": session_id,
                            "cache_mode": mode, "tail_seq": snapshot.tail_seq})
                return snapshot
            except Exception:
                connection.rollback()
                raise

    @logged_operation("context_repository.load_context")
    def load_context(self, user_id: int, session_id: str, *, attempt: ContextAttempt | None = None) -> ContextSnapshot:
        user_id = validate_positive_integer(user_id, "用户 ID")
        session_id = normalize_session_id(session_id)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._require_session(cursor, user_id, session_id)
                    self._require_attempt(cursor, session_id, attempt)
                    snapshot = self._read_snapshot(cursor, user_id, session_id)
                connection.commit()  # 模型调用前释放锁，不能跨网络等待持有事务。
                return snapshot
            except Exception:
                connection.rollback()
                raise

    @logged_operation("context_repository.save_summary")
    def save_summary(
        self, user_id: int, session_id: str, summary: ContextSummary, *,
        expected_version: int, expected_tail_seq: int,
        attempt: ContextAttempt | None = None,
    ) -> None:
        user_id = validate_positive_integer(user_id, "用户 ID")
        session_id = normalize_session_id(session_id)
        validate_summary(summary)
        if type(expected_version) is not int or not 0 <= expected_version < 2**64 - 1:
            raise ValueError("预期摘要版本必须为可递增的非负整数。")
        expected_tail_seq = validate_positive_integer(expected_tail_seq, "预期消息末尾")
        if summary.version != expected_version + 1:
            raise ValueError("新摘要版本必须为预期版本加 1。")
        if summary.summarized_until_seq >= expected_tail_seq:
            raise ValueError("摘要不能覆盖当前待回复问题或未来消息。")
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._require_session(cursor, user_id, session_id)
                    self._require_attempt(cursor, session_id, attempt)
                    current = self._load_summary(cursor, user_id, session_id)
                    version = 0 if current is None else current.version
                    covered = 0 if current is None else current.summarized_until_seq
                    if version != expected_version or self._tail_seq(cursor, session_id) != expected_tail_seq:
                        raise ContextConflictError("会话上下文已更新，请重新读取后再试。")
                    if summary.summarized_until_seq <= covered:
                        raise ValueError("新摘要必须向前扩展覆盖范围。")
                    cursor.execute(self.load_context_covered_seqs_sql,
                                   (user_id, session_id, covered, summary.summarized_until_seq))
                    rows = cursor.fetchall()
                    if not rows or rows[-1][0] != summary.summarized_until_seq:
                        raise ValueError("摘要覆盖终点不是现存的历史消息。")
                    # 只核对新增覆盖范围，旧范围沿用已校验的缺口元数据。
                    gaps = [] if current is None else list(current.missing_ranges)
                    expected_seq = covered + 1
                    for (seq,) in rows:
                        if seq > expected_seq:
                            gaps.append(HistoryGap(expected_seq, seq - 1))
                        expected_seq = seq + 1
                    if summary.missing_ranges != tuple(gaps):
                        raise ValueError("摘要缺失范围与原始消息不一致。")
                    gaps_json = json.dumps([{"start_seq": gap.start_seq, "end_seq": gap.end_seq}
                                            for gap in summary.missing_ranges], ensure_ascii=False)
                    values = (summary.content, summary.summarized_until_seq, summary.version, gaps_json,
                              user_id, session_id)
                    if current is None:
                        cursor.execute(self.insert_context_summary_sql, values)
                    else:
                        cursor.execute(self.update_context_summary_sql,
                                       (*values, expected_version, summary.summarized_until_seq))
                    if cursor.rowcount != 1:
                        raise ContextConflictError("会话上下文已更新，本次摘要未保存，请重新读取后再试。")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
