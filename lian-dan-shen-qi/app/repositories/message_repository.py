from app.logging_config import logged_operation

from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import mysql.connector
from mysql.connector import IntegrityError, errorcode

from app.errors import RequestIdConflictError, SessionNotFoundError, UnfinishedTurnError
from app.database import get_connection
from app.models.chat_turn import ChatTurn, TurnAlreadyCompletedError
from app.models.research import ResearchReply,content_hash
from app.models.skill import (SKILL_UNSET, MAX_SNAPSHOT_BYTES, SkillSnapshot, SkillRequestState,
                              SkillTurnNotFound, SkillStorageError, SkillSelectionConflict, saved_snapshot)
import json
from app.validators import (
    normalize_required_text,
    normalize_session_id,
    validate_message_content,
    validate_positive_integer,
)


class MessageRepository:
    @logged_operation("message_repository.__init__", log_success=False, expected_errors=())
    def __init__(self, password: str):
        self.password = password
        sql_root = Path(__file__).resolve().parents[2] / "sql"
        self.save_message_sql = (sql_root / "save_message.sql").read_text(encoding="utf-8")
        self.update_session_time_sql = (sql_root / "update_session_time.sql").read_text(encoding="utf-8")
        self.load_messages_sql = (sql_root / "load_messages.sql").read_text(encoding="utf-8")
        self.load_history_page_sql = (sql_root / "load_history_page.sql").read_text(encoding="utf-8")
        self.get_user_sql = (sql_root / "get_user.sql").read_text(encoding="utf-8")
        self.get_session_sql = (sql_root / "get_session.sql").read_text(encoding="utf-8")
        self.lock_session_sql = (sql_root / "lock_session.sql").read_text(encoding="utf-8")
        self.last_message_seq_sql = (sql_root / "last_message_seq.sql").read_text(encoding="utf-8")
        self.create_turn_sql = (sql_root / "create_turn.sql").read_text(encoding="utf-8")
        self.get_turn_sql = (sql_root / "get_turn_for_update.sql").read_text(encoding="utf-8")
        self.get_unfinished_turn_sql = (sql_root / "get_unfinished_turn.sql").read_text(encoding="utf-8")
        self.lock_unfinished_turn_sql = (sql_root / "get_unfinished_turn_for_update.sql").read_text(encoding="utf-8")
        self.claim_turn_sql = (sql_root / "claim_turn.sql").read_text(encoding="utf-8")
        self.complete_turn_sql = (sql_root / "complete_turn.sql").read_text(encoding="utf-8")
        self.fail_turn_sql = (sql_root / "fail_turn.sql").read_text(encoding="utf-8")
        self.get_message_sql = (sql_root / "get_message_for_update.sql").read_text(encoding="utf-8")
        self.lock_messages_sql = (sql_root / "load_messages_for_update.sql").read_text(encoding="utf-8")
        self.save_research_advice_sql = (sql_root / 'save_research_advice.sql').read_text(encoding='utf-8')
        self.get_research_reference_sql = (sql_root / 'get_research_reference.sql').read_text(encoding='utf-8')
        self.get_completed_reply_sql = (sql_root / "get_completed_reply.sql").read_text(encoding="utf-8")
        self.find_request_reply_sql = (sql_root / "find_request_reply.sql").read_text(encoding="utf-8")
        for name in ('inspect_chat_skill_request','get_turn_skill','save_turn_skill'):
            setattr(self,name+'_sql',(sql_root/(name+'.sql')).read_text(encoding='utf-8'))

    @logged_operation('message_repository.inspect_skill_request')
    def inspect_skill_request(self,user_id,session_id,request_id,content,skill_id):
        """读取重放回复/冻结选择；同一请求不得改变原问题或Skill ID。"""
        user_id,session_id=self._validate_ids(user_id,session_id)
        request_id=self._uuid(request_id,'请求 ID');content=validate_message_content(content)
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.inspect_chat_skill_request_sql,(request_id,user_id,session_id));row=cursor.fetchone()
        if row is None:raise SessionNotFoundError('会话不存在或不属于该用户。')
        if row[0] is None:return SkillRequestState(False)
        if (row[1],row[2])!=('human',content):raise RequestIdConflictError('同一请求不能更换问题或Skill。')
        snapshot=saved_snapshot(row[4])
        if (snapshot.skill_id if snapshot else None)!=skill_id:
            raise SkillSelectionConflict('同一请求不能更换Skill。')
        return SkillRequestState(True,validate_message_content(row[3]) if row[0]=='completed' else None,snapshot)

    @logged_operation('message_repository.load_turn_skill',log_success=False)
    def load_turn_skill(self,user_id,session_id,turn_id,attempt_id=None):
        user_id,session_id=self._validate_ids(user_id,session_id);turn_id=self._uuid(turn_id,'轮次 ID')
        if attempt_id is not None:attempt_id=self._uuid(attempt_id,'生成尝试 ID')
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.get_turn_skill_sql,(user_id,session_id,turn_id,attempt_id,attempt_id));row=cursor.fetchone()
        if row is None:
            if attempt_id is not None:
                from app.models.context_state import ContextConflictError
                raise ContextConflictError('当前生成尝试已失效。')
            raise SkillTurnNotFound('轮次不存在或不可访问。')
        return saved_snapshot(row[0])

    @logged_operation("message_repository.find_request_reply")
    def find_request_reply(self, user_id: int, session_id: str, request_id: str, content: str) -> str | None:
        """一条只读 SQL 识别已完成重放；新请求不写入，仍由 start_turn 原子去重。"""
        user_id, session_id = self._validate_ids(user_id, session_id)
        request_id = self._uuid(request_id, "请求 ID")
        content = validate_message_content(content)
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.find_request_reply_sql, (request_id, user_id, session_id))
                row = cursor.fetchone()
                if row is None:
                    raise SessionNotFoundError("会话不存在或不属于该用户。")
                if row[0] is None:
                    return None
                if (row[1], row[2]) != ("human", content):
                    raise RequestIdConflictError("同一个请求 ID 不能用于不同的问题。")
                return validate_message_content(row[3]) if row[0] == "completed" else None

    def _require_session(self, cursor, user_id: int, session_id: str, lock: bool = False) -> None:
        cursor.execute(self.get_user_sql, (user_id,))
        if cursor.fetchone() is None:
            raise ValueError(f"用户 ID {user_id} 不存在。")
        sql = self.lock_session_sql if lock else self.get_session_sql
        cursor.execute(sql, (user_id, session_id))
        if cursor.fetchone() is None:
            raise SessionNotFoundError("会话不存在或不属于该用户。")

    @staticmethod
    def _validate_ids(user_id, session_id):
        return validate_positive_integer(user_id, "用户 ID"), normalize_session_id(session_id)

    @staticmethod
    def _uuid(value, label):
        if not isinstance(value, str):
            raise ValueError(f"{label}必须是 UUID 字符串。")
        try:
            return UUID(value.strip()).hex
        except ValueError as error:
            raise ValueError(f"{label}格式不正确。") from error

    @staticmethod
    def _turn(row):
        if row is None:
            return None
        return ChatTurn(row[0], row[1], row[2], row[3], bool(row[4]), row[5])

    @contextmanager
    def _session_transaction(self, user_id, session_id):
        """每次写入单独提交；数据库锁不会跨越模型请求。"""
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._require_session(cursor, user_id, session_id, lock=True)
                    yield cursor
                connection.commit()
            except IntegrityError as error:
                connection.rollback()
                if error.errno == errorcode.ER_DUP_ENTRY:
                    raise ValueError("请求或消息序号已存在，请重新读取会话状态后重试。") from error
                raise
            except Exception:
                connection.rollback()
                raise

    def _locked_turn(self, cursor, session_id, turn_id):
        cursor.execute(self.get_turn_sql, (session_id, turn_id))
        turn = self._turn(cursor.fetchone())
        if turn is None:
            raise ValueError("对话轮次不存在或不属于该会话。")
        return turn

    def _require_tail(self, cursor, session_id, turn):
        cursor.execute(self.last_message_seq_sql, (session_id,))
        last = cursor.fetchone()
        if last is None or last[0] != turn.user_seq:
            raise ValueError("会话消息顺序已变化，不能继续处理这一轮。")

    @staticmethod
    def _require_attempt(turn, attempt_id, check_lease=True):
        if turn.attempt_id != attempt_id:
            raise ValueError("这次生成任务已失效，请读取当前轮次状态。")
        if turn.status != "running":
            raise ValueError("这一轮不在生成中，请先领取任务。")
        if check_lease and not turn.lease_active:
            raise ValueError("生成任务已过期，请重新领取后重试。")

    @staticmethod
    def _require_updated(cursor):
        if cursor.rowcount != 1:
            raise ValueError("轮次状态已变化，本次写入已撤销。")

    @logged_operation("message_repository.load_messages")
    def load_messages(self, user_id: int, session_id: str) -> list[tuple[str, str]]:
        user_id, session_id = self._validate_ids(user_id, session_id)
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                self._require_session(cursor, user_id, session_id)
                cursor.execute(self.load_messages_sql, (user_id, session_id))
                return cursor.fetchall()

    @logged_operation('message_repository.check_session',log_success=False)
    def check_session(self,user_id,session_id):
        user_id,session_id=self._validate_ids(user_id,session_id)
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:self._require_session(cursor,user_id,session_id)

    @logged_operation("message_repository.load_history_page")
    def load_history_page(
        self, user_id: int, session_id: str, after_seq: int = 0, limit: int = 50,
    ) -> list[tuple[int, str, str]]:
        """为历史界面读取一页；空会话返回 []，会话不存在或归属不符抛业务异常。"""
        user_id, session_id = self._validate_ids(user_id, session_id)
        if type(after_seq) is not int or not 0 <= after_seq <= 2**64 - 1:
            raise ValueError("历史消息起点必须为 0 或 BIGINT UNSIGNED 范围内的正整数。")
        limit = validate_positive_integer(limit, "每页消息数")
        if limit > 100:
            raise ValueError("每次最多读取 100 条消息。")
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                self._require_session(cursor, user_id, session_id)
                cursor.execute(self.load_history_page_sql, (user_id, session_id, after_seq, limit))
                return cursor.fetchall()

    @logged_operation("message_repository.get_unfinished_turn")
    def get_unfinished_turn(self, user_id: int, session_id: str) -> ChatTurn | None:
        user_id, session_id = self._validate_ids(user_id, session_id)
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                self._require_session(cursor, user_id, session_id)
                cursor.execute(self.get_unfinished_turn_sql, (session_id,))
                return self._turn(cursor.fetchone())

    @logged_operation("message_repository.start_turn")
    def start_turn(self, user_id: int, session_id: str, content: str,
                   request_id: str | None = None, *, skill_snapshot=SKILL_UNSET) -> str:
        """先提交用户提问和 pending 轮次；网络重试请复用 request_id。"""
        user_id, session_id = self._validate_ids(user_id, session_id)
        content = validate_message_content(content)
        if request_id is not None:
            request_id = self._uuid(request_id, "请求 ID")
        if skill_snapshot is not SKILL_UNSET and skill_snapshot is not None and not isinstance(skill_snapshot,SkillSnapshot):
            raise ValueError('缺少有效Skill快照。')
        snapshot_json=skill_snapshot.model_dump_json() if isinstance(skill_snapshot,SkillSnapshot) else None
        if snapshot_json is not None and len(snapshot_json.encode())>MAX_SNAPSHOT_BYTES:raise ValueError('Skill快照超过预算。')
        with self._session_transaction(user_id, session_id) as cursor:
            turn_id = request_id if request_id is not None else uuid4().hex
            cursor.execute(self.get_turn_sql, (session_id, turn_id))
            existing = self._turn(cursor.fetchone())
            if existing is not None:
                cursor.execute(self.get_message_sql, (session_id, existing.user_seq))
                if cursor.fetchone() != ("human", content):
                    raise RequestIdConflictError("同一个请求 ID 不能用于不同的问题。")
                if skill_snapshot is not SKILL_UNSET:
                    # 会话锁后读取当前已提交值，不能读事务开始时的旧一致性快照。
                    cursor.execute(self.get_turn_skill_sql.rstrip().rstrip(';')+' FOR UPDATE',(user_id,session_id,turn_id,None,None))
                    row=cursor.fetchone()
                    if row is None:raise SkillStorageError('轮次Skill记录无法读取。')
                    original=saved_snapshot(row[0])
                    if (original.skill_id if original else None)!=(skill_snapshot.skill_id if skill_snapshot else None):
                        raise SkillSelectionConflict('同一请求不能更换Skill。')
                return existing.turn_id

            cursor.execute(self.lock_unfinished_turn_sql, (session_id,))
            if cursor.fetchone() is not None:
                raise UnfinishedTurnError("该会话还有未完成的问题，请先重试或新建聊天。")

            cursor.execute(self.last_message_seq_sql, (session_id,))
            last = cursor.fetchone()
            user_seq = 1 if last is None else last[0] + 1
            validate_positive_integer(user_seq, "用户消息序号")
            validate_positive_integer(user_seq + 1, "预留的回复消息序号")
            cursor.execute(self.save_message_sql, (session_id, user_seq, "human", content))
            cursor.execute(self.create_turn_sql, (turn_id, session_id, user_seq))
            if skill_snapshot is not SKILL_UNSET and snapshot_json is not None:
                cursor.execute(self.save_turn_skill_sql,(snapshot_json,session_id,turn_id))
                self._require_updated(cursor)
            cursor.execute(self.update_session_time_sql, (session_id,))
            return turn_id

    @logged_operation("message_repository.claim_turn")
    def claim_turn(self, user_id: int, session_id: str, turn_id: str,
                   lease_seconds: int = 600, *, load_history: bool = True) -> tuple[str, list[tuple[str, str]]]:
        """新请求与重试都先领取；返回本次 attempt_id 和包含原问题的完整历史。"""
        user_id, session_id = self._validate_ids(user_id, session_id)
        turn_id = self._uuid(turn_id, "轮次 ID")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("生成任务有效期必须是 1～3600 秒的整数。")
        if type(load_history) is not bool:
            raise ValueError("是否读取全部历史必须为布尔值。")
        with self._session_transaction(user_id, session_id) as cursor:
            turn = self._locked_turn(cursor, session_id, turn_id)
            if turn.status == "completed":
                raise TurnAlreadyCompletedError("这一轮已经完成，不能重复生成。")
            if turn.status == "running" and turn.lease_active:
                raise ValueError("模型正在回复，请等待；生成任务过期后可以重试。")
            self._require_tail(cursor, session_id, turn)
            if load_history:
                cursor.execute(self.lock_messages_sql, (user_id, session_id))
                messages = cursor.fetchall()
                if not messages or messages[-1][0] != "human":
                    raise ValueError("没有找到这一轮已保存的用户提问。")
            else:
                # 正式上下文链路稍后按摘要覆盖点读取原文，不在领取时又读一遍全部历史。
                cursor.execute(self.get_message_sql, (session_id, turn.user_seq))
                question = cursor.fetchone()
                if question is None or question[0] != "human":
                    raise ValueError("没有找到这一轮已保存的用户提问。")
                validate_message_content(question[1])
                messages = []
            attempt_id = uuid4().hex
            cursor.execute(self.claim_turn_sql, (attempt_id, lease_seconds, session_id, turn_id))
            self._require_updated(cursor)
            return attempt_id, messages

    @logged_operation("message_repository.complete_turn")
    def complete_turn(self, user_id: int, session_id: str, turn_id: str,
                      attempt_id: str, assistant_content: str, *, research_reply: ResearchReply | None = None) -> None:
        """只补充回复；回复和 completed 状态在同一事务中提交。"""
        user_id, session_id = self._validate_ids(user_id, session_id)
        turn_id = self._uuid(turn_id, "轮次 ID")
        attempt_id = self._uuid(attempt_id, "生成任务 ID")
        assistant_content = validate_message_content(assistant_content)
        with self._session_transaction(user_id, session_id) as cursor:
            turn = self._locked_turn(cursor, session_id, turn_id)
            if turn.status == "completed" and turn.attempt_id == attempt_id:
                cursor.execute(self.get_message_sql, (session_id, turn.user_seq + 1))
                if cursor.fetchone() != ("ai", assistant_content):
                    raise ValueError("已完成轮次的回复与本次提交内容不一致。")
                return
            self._require_attempt(turn, attempt_id)
            self._require_tail(cursor, session_id, turn)
            if research_reply is not None:
                if not isinstance(research_reply,ResearchReply) or research_reply.text!=assistant_content:
                    raise ValueError('建议正文与聊天回复不一致。')
                payload=json.loads(research_reply.payload_json)
                if len(research_reply.payload_json.encode())>32768 or len(assistant_content.encode())>32768:
                    raise ValueError('建议超过保存预算。')
                if payload['run_id']!=research_reply.run_id or set(research_reply.reference_ids)!={row['reference_id'] for row in payload['sources']}:
                    raise ValueError('建议来源索引不一致。')
                for source in payload['sources']:
                    cursor.execute(self.get_research_reference_sql,(user_id,session_id,source['reference_id']))
                    row=cursor.fetchone()
                    # Display title can be a bounded prefix of the stored title.
                    if row is None or (row[1],row[3],row[5])!=(source['url'],source['kind'],source['content_sha256']) or not row[2].startswith(source['title']):
                        raise ValueError('建议所引用资料不存在或快照已变化。')
                cursor.execute(self.save_research_advice_sql,(uuid4().hex,session_id,user_id,turn_id,attempt_id,
                    research_reply.run_id,research_reply.payload_json,assistant_content,content_hash(research_reply.payload_json)))
            cursor.execute(self.save_message_sql,
                           (session_id, turn.user_seq + 1, "ai", assistant_content))
            cursor.execute(self.complete_turn_sql, (session_id, turn_id, attempt_id))
            self._require_updated(cursor)
            cursor.execute(self.update_session_time_sql, (session_id,))

    @logged_operation("message_repository.get_completed_reply")
    def get_completed_reply(self, user_id: int, session_id: str, turn_id: str) -> str | None:
        user_id, session_id = self._validate_ids(user_id, session_id)
        turn_id = self._uuid(turn_id, "轮次 ID")
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                self._require_session(cursor, user_id, session_id)
                cursor.execute(self.get_completed_reply_sql, (user_id, session_id, turn_id))
                row = cursor.fetchone()
                return None if row is None else row[0]

    @logged_operation("message_repository.fail_turn")
    def fail_turn(self, user_id: int, session_id: str, turn_id: str,
                  attempt_id: str, error_code: str = "MODEL_REQUEST_FAILED") -> None:
        """保留已经提交的问题，只标记失败；不得覆盖新任务或已完成的回复。"""
        user_id, session_id = self._validate_ids(user_id, session_id)
        turn_id = self._uuid(turn_id, "轮次 ID")
        attempt_id = self._uuid(attempt_id, "生成任务 ID")
        error_code = normalize_required_text(error_code, "错误代码", 64)
        with self._session_transaction(user_id, session_id) as cursor:
            turn = self._locked_turn(cursor, session_id, turn_id)
            if turn.status == "failed" and turn.attempt_id == attempt_id:
                return
            self._require_attempt(turn, attempt_id, check_lease=False)
            cursor.execute(self.fail_turn_sql, (error_code, session_id, turn_id, attempt_id))
            self._require_updated(cursor)
            cursor.execute(self.update_session_time_sql, (session_id,))


if __name__ == "__main__":
    from app.config import get_mysql_password
    from app.repositories.session_repository import SessionRepository

    try:
        password = get_mysql_password()
        user_id = int(input("请输入已有用户 ID：").strip())
        sessions = SessionRepository(password)
        session_id = sessions.create_session(user_id, title="可恢复消息测试")
        repository = MessageRepository(password)
        turn_id = repository.start_turn(user_id, session_id, "你好，这是需要保留的问题")
        attempt_id, _ = repository.claim_turn(user_id, session_id, turn_id)
        repository.fail_turn(user_id, session_id, turn_id, attempt_id)
        print("模拟失败后，问题仍然存在：", repository.load_messages(user_id, session_id))
        attempt_id, _ = repository.claim_turn(user_id, session_id, turn_id)
        repository.complete_turn(user_id, session_id, turn_id, attempt_id, "这是重试后的模拟回复")
        print("重试成功后的历史：", repository.load_messages(user_id, session_id))
    except (ValueError, mysql.connector.Error) as error:
        print("操作失败：", error)
