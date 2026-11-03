"""独立修改记录、幂等计划与执行领取；网络和模型等待均不持有 SQL 事务。"""
from pathlib import Path
from uuid import uuid4

from app.database import get_connection
from app.logging_config import logged_operation
from app.models.analysis import compact, strict_json
from app.models.code_change import (ChangeNotFound, ChangeConflict, ChangeStorageError,
    ChangeResponse, ChangePlan, require_instruction)
from app.models.context_snapshot import ContextAttempt
from app.models.experiment_run import normalize_run_uuid
from app.models.research import content_hash
from app.models.suggestion import verify_suggestions
from app.repositories.context_repository import ContextRepository
from app.repositories.event_repository import decode_event
from app.repositories.experiment_repository import _utc
from app.services.code_change_transport import validate_result
from app.validators import validate_positive_integer, normalize_session_id


class CodeChangeRepository(ContextRepository):
    def __init__(self, password):
        super().__init__(password)
        root = Path(__file__).resolve().parents[2]/'sql'
        for name in ('get_code_change', 'get_code_change_request', 'list_code_changes', 'latest_session_code_change',
            'insert_code_change', 'claim_code_change', 'save_code_change_plan', 'save_code_change_result', 'fail_code_change',
            'get_code_change_claim', 'insert_code_change_claim', 'get_change_suggestion', 'event_owned_run', 'get_training_question'):
            setattr(self, name+'_sql', (root/(name+'.sql')).read_text(encoding='utf-8'))

    def _chat(self, connection, user_id, chat):
        if chat is None: return None
        session_id, attempt = chat['session_id'], chat['attempt']
        normalize_session_id(session_id)
        if not isinstance(attempt, ContextAttempt): raise ValueError('缺少当前聊天生成尝试。')
        with connection.cursor() as cursor:
            self._require_session(cursor, user_id, session_id)
            self._require_attempt(cursor, session_id, attempt)
            cursor.execute(self.get_training_question_sql, (session_id, attempt.turn_id))
            question = cursor.fetchone()
            if not question: raise ChangeConflict('当前人类提问不存在。')
            return question[0]

    @logged_operation('code_change_repository.question')
    def question(self, user_id, chat):
        with get_connection(self.password) as connection:
            text = self._chat(connection, user_id, chat)
            connection.commit(); return text

    def _owner(self, cursor, user_id, run_id, *, lock=False):
        cursor.execute(self.event_owned_run_sql.rstrip().rstrip(';') + (' FOR UPDATE' if lock else ''), (user_id, run_id))
        if cursor.fetchone() is None: raise ChangeNotFound('运行不存在或不可访问。')

    @staticmethod
    def _decode(row):
        if row is None: raise ChangeNotFound('修改记录不存在或不可访问。')
        result = dict(row)
        try:
            source_text = result['source_json']
            if len(source_text.encode()) > 65536 or content_hash(source_text) != result['source_digest']:
                raise ValueError('source integrity')
            result['_source'] = strict_json(source_text)
            if result['_source']['run_id'] != row['run_id'] or result['_source']['suggestion_task_id'] != row['suggestion_task_id']:
                raise ValueError('source identity')
            result['plan'] = ChangePlan.model_validate(strict_json(row['plan_json'])).model_dump() if row['plan_json'] else None
            if result['plan'] is not None and content_hash(compact(result['plan'])) != row['plan_digest']:
                raise ValueError('plan integrity')
            result['result'] = None
            if row['result_json']:
                result['result'] = validate_result({'ok': True, 'data': strict_json(row['result_json'])},
                    {'change_id': row['id'], 'plan': result['plan'], 'plan_digest': row['plan_digest'], 'work_dir': result['_source']['work_dir']})
            result['change_id'] = row['id']
            result['lease_active'] = bool(row['lease_active'])
            for key in ('created_at', 'updated_at'): result[key] = _utc(row[key])
            return result
        except (ValueError, TypeError, KeyError, RuntimeError) as error:
            raise ChangeStorageError('修改记录内容无法核对。') from error

    @staticmethod
    def public(record):
        return ChangeResponse(**{key: record[key] for key in ChangeResponse.model_fields}).model_dump(mode='json')

    @logged_operation('code_change_repository.suggestion')
    def suggestion(self, user_id, run_id, task_id, index):
        user_id = validate_positive_integer(user_id, '用户 ID')
        run_id, task_id = normalize_run_uuid(run_id), normalize_run_uuid(task_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                self._owner(cursor, user_id, run_id)
                cursor.execute(self.get_change_suggestion_sql, (user_id, run_id, task_id))
                row = cursor.fetchone()
                if not row: raise ChangeNotFound('已保存建议不存在或不可访问。')
                source = decode_event(row)
                text = row['content_json']
                if content_hash(text) != row['content_digest']: raise ChangeStorageError('建议摘要无法核对。')
                content = verify_suggestions(strict_json(text), source)
                if row['current_report_version'] != row['report_version'] or row['current_run_status'] != source['report']['status']:
                    raise ChangeConflict('建议属于旧报告，请先核对当前运行。')
                if content['evidence_status'] != 'supported' or not 1 <= index <= len(content['suggestions']):
                    raise ChangeConflict('该条具体建议不存在或证据不足。')
                return content

    @logged_operation('code_change_repository.get')
    def get(self, user_id, change_id):
        user_id, change_id = validate_positive_integer(user_id, '用户 ID'), normalize_run_uuid(change_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.get_code_change_sql, (user_id, change_id)); return self._decode(cursor.fetchone())

    def by_request(self, user_id, request_id, run_id, task_id, index):
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.get_code_change_request_sql, (user_id, request_id))
                    row = cursor.fetchone()
                    if row and (row['run_id'], row['suggestion_task_id'], row['suggestion_index']) != (run_id, task_id, index):
                        raise ChangeConflict('请求编号已用于另一条修改。')
                    result = self._decode(row) if row else None
                connection.commit(); return result
            except Exception:
                connection.rollback(); raise

    @logged_operation('code_change_repository.list')
    def list(self, user_id, run_id, *, limit=10, offset=0):
        user_id, run_id = validate_positive_integer(user_id, '用户 ID'), normalize_run_uuid(run_id)
        if type(limit) is not int or not 1 <= limit <= 20 or type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError('修改列表分页不符合要求。')
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                self._owner(cursor, user_id, run_id)
                cursor.execute(self.list_code_changes_sql, (user_id, run_id, limit, offset))
                return [self.public(self._decode(row)) for row in cursor.fetchall()]

    @logged_operation('code_change_repository.prepare')
    def prepare(self, user_id, run_id, task_id, index, request_id, source, *, chat=None):
        user_id = validate_positive_integer(user_id, '用户 ID')
        run_id, task_id, request_id = map(normalize_run_uuid, (run_id, task_id, request_id))
        with get_connection(self.password) as connection:
            try:
                question = self._chat(connection, user_id, chat)
                if question is not None:
                    require_instruction(question, index)
                    if chat.get('bound') != run_id and run_id not in question.replace('-', '').lower():
                        raise ChangeConflict('请明确要修改的运行，或先在聊天中选择该运行。')
                with connection.cursor(dictionary=True) as cursor:
                    self._owner(cursor, user_id, run_id, lock=True)
                    cursor.execute(self.get_code_change_request_sql, (user_id, request_id)); row = cursor.fetchone()
                    claimed = False
                    if row:
                        if (row['run_id'], row['suggestion_task_id'], row['suggestion_index']) != (run_id, task_id, index):
                            raise ChangeConflict('请求编号已用于另一条修改，请为新修改生成新编号。')
                        if row['plan_json'] is None and not row['lease_active']:
                            token = uuid4().hex
                            cursor.execute(self.claim_code_change_sql, ('planning', token, row['id'], user_id)); claimed = True
                    else:
                        identifier, token = uuid4().hex, uuid4().hex
                        text = compact(source)
                        cursor.execute(self.insert_code_change_sql, (identifier, user_id, run_id, task_id, index, request_id,
                            chat['session_id'] if chat else None, text, content_hash(text), token)); claimed = True
                        row = {'id': identifier}
                    cursor.execute(self.get_code_change_sql, (user_id, row['id'])); result = self._decode(cursor.fetchone())
                connection.commit(); return result, claimed
            except Exception:
                connection.rollback(); raise

    @logged_operation('code_change_repository.claim_apply')
    def claim_apply(self, user_id, change_id, *, chat=None):
        user_id, change_id = validate_positive_integer(user_id, '用户 ID'), normalize_run_uuid(change_id)
        with get_connection(self.password) as connection:
            try:
                question = self._chat(connection, user_id, chat)
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.get_code_change_sql.rstrip().rstrip(';') + ' FOR UPDATE', (user_id, change_id))
                    record = self._decode(cursor.fetchone())
                    if question is not None:
                        cursor.execute(self.latest_session_code_change_sql, (user_id, chat['session_id']))
                        latest = cursor.fetchone()
                        require_instruction(question, record['suggestion_index'], change_id=change_id,
                            same_session_latest=bool(latest and latest['id'] == change_id))
                        named = change_id in question.replace('-', '').lower() or record['run_id'] in question.replace('-', '').lower()
                        if not named and chat.get('bound') != record['run_id']:
                            raise ChangeConflict('当前聊天没有选择此运行。')
                        named_change = change_id in question.replace('-', '').lower()
                        current_request = content_hash('code-change:'+chat['session_id']+':'+chat['attempt'].turn_id)[:32]
                        this_turn = record['origin_session_id'] == chat['session_id'] and record['request_id'] == current_request
                        referential = bool(latest and latest['id'] == change_id and '这个修改' in question)
                        if not (named_change or this_turn or referential):
                            raise ChangeConflict('请应用本轮刚准备的计划，或明确指定已有修改 ID。')
                        cursor.execute(self.get_code_change_claim_sql, (chat['session_id'], chat['attempt'].turn_id))
                        claim = cursor.fetchone()
                        if claim and claim['change_id'] != change_id:
                            raise ChangeConflict('这一轮已经应用另一份修改，请在新的提问中继续。')
                        if not claim:
                            cursor.execute(self.insert_code_change_claim_sql, (chat['session_id'], chat['attempt'].turn_id, user_id, change_id))
                    claimed = False
                    if record['status'] not in {'applied', 'checks_failed'}:
                        if record['plan'] is None: raise ChangeConflict('修改计划尚未准备好。')
                        if record['lease_active']: raise ChangeConflict('修改仍在处理中，请查询同一记录。')
                        cursor.execute(self.claim_code_change_sql, ('applying', uuid4().hex, change_id, user_id)); claimed = True
                        cursor.execute(self.get_code_change_sql, (user_id, change_id)); record = self._decode(cursor.fetchone())
                connection.commit(); return record, claimed
            except Exception:
                connection.rollback(); raise

    @logged_operation('code_change_repository.finish')
    def finish(self, user_id, change_id, token, *, plan=None, result=None, error_code=None):
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    if plan is not None:
                        text = compact(ChangePlan.model_validate(plan).model_dump())
                        cursor.execute(self.save_code_change_plan_sql, (text, content_hash(text), change_id, user_id, token))
                    elif result is not None:
                        cursor.execute(self.save_code_change_result_sql, ('checks_failed' if result['check_status'] == 'failed' else 'applied',
                            compact(result), change_id, user_id, token))
                    else:
                        allowed = {'KEY_UNAVAILABLE','MODEL_UNAVAILABLE','INVALID_MODEL_OUTPUT','SOURCE_CHANGED','TARGET_CHANGED','STORAGE_ERROR','OPERATION_TIMEOUT','READ_UNAVAILABLE','WORKSPACE_BUSY',
                            'WORKSPACE_INCOMPLETE','WORKSPACE_CONFLICT','REPLACEMENT_CONFLICT','EDIT_UNAVAILABLE','INVALID_PLAN',
                            'PATH_NOT_ALLOWED','SNAPSHOT_TOO_LARGE','DIFF_TOO_LARGE','FILE_TOO_LARGE','SENSITIVE_CONTENT',
                            'UNSUPPORTED_TEXT','UNSUPPORTED_PLATFORM','RESULT_TOO_LARGE','NOT_FOUND','LINE_TOO_LONG','WORKER_CANCELLED','INTERNAL_ERROR'}
                        if error_code not in allowed: error_code = 'INTERNAL_ERROR'
                        cursor.execute(self.fail_code_change_sql, ('conflict' if error_code in {'SOURCE_CHANGED','TARGET_CHANGED','REPLACEMENT_CONFLICT','WORKSPACE_CONFLICT'} else 'attention',
                            error_code, change_id, user_id, token))
                    if cursor.rowcount != 1: raise ChangeConflict('修改生成尝试已失效，不能覆盖当前结果。')
                connection.commit()
            except Exception:
                connection.rollback(); raise
