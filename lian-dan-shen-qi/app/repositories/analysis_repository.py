"""分析任务租约与不可变结果；身份来自服务器，模型等待不持有数据库事务。"""

import hashlib
from pathlib import Path
import re
from uuid import uuid4

from app.database import get_connection
from app.errors import RunNotFoundError
from app.logging_config import logged_operation
from app.models.analysis import (AnalysisNotFoundError, AnalysisConflictError, AnalysisLeaseLostError, AnalysisProtocolError,
    AnalysisStorageError, AnalysisTaskResponse, compact, strict_json, verify_saved_content)
from app.models.experiment_run import normalize_run_uuid
from app.repositories.event_repository import decode_event
from app.repositories.experiment_repository import _utc
from app.validators import validate_positive_integer


ERROR_CODES = {"MODEL_UNAVAILABLE", "MODEL_TIMEOUT", "INVALID_MODEL_OUTPUT", "INVALID_SOURCE", "KEY_UNAVAILABLE",
               "ANALYSIS_INTERRUPTED", "WORKER_CANCELLED", "INTERNAL_ERROR", "STORAGE_ERROR", "INPUT_TOO_LARGE", "EMPTY_MODEL_OUTPUT", "MODEL_OUTPUT_TOO_LARGE"}


class AnalysisRepository:
    agent_name = 'analysis'
    validate_content = staticmethod(verify_saved_content)

    def __init__(self, password):
        self.password = password
        self._manual_scope = None
        root = Path(__file__).resolve().parents[2] / "sql"
        for name in ("get_analysis_preference", "save_analysis_preference", "reap_analysis_tasks", "next_analysis_task", "next_run_analysis_task",
                     "claim_analysis_task", "get_analysis_task", "renew_analysis_task", "load_analysis_source",
                     "save_analysis_result", "finish_analysis_task", "retry_analysis_task", "list_analysis_results",
                     "event_owned_run", "get_user"):
            setattr(self, name + "_sql", (root / (name + ".sql")).read_text(encoding="utf-8"))
        self.create_suggestion_task_sql = (root / 'create_suggestion_task.sql').read_text(encoding='utf-8')

    def manual_for(self,user_id,run_id,event_id):
        """只在用户点击后创建的独立仓储实例使用；不改用户自动处理偏好。"""
        self._manual_scope=(validate_positive_integer(user_id,'用户 ID'),normalize_run_uuid(run_id))
        self._manual_event=normalize_run_uuid(event_id)
        for name in ('next_run_analysis_task_sql','renew_analysis_task_sql','load_analysis_source_sql'):
            sql=getattr(self,name)
            sql=re.sub(r'JOIN [A-Za-z0-9_]+\.(?:analysis|suggestion)_preferences AS p ON p.user_id=t.user_id AND p.enabled=1\s*','',sql)
            setattr(self,name,sql)
        return self

    def source_row(self, row):
        return decode_event(row)

    def prepare_retry(self, cursor, row):
        if row['status'] == 'succeeded' or row['lease_active']:
            raise AnalysisConflictError('任务已完成或仍在处理中。')

    def _owner(self, cursor, user_id, run_id, *, lock=False):
        if self._manual_scope is not None and self._manual_scope!=(user_id,run_id):
            raise RunNotFoundError('手动分析仅授权当前用户的本次运行。')
        sql = self.event_owned_run_sql.rstrip().rstrip(";") + (" FOR UPDATE" if lock else "")
        cursor.execute(sql, (user_id, run_id))
        if cursor.fetchone() is None:
            raise RunNotFoundError("运行不存在或不可访问。")

    @logged_operation("analysis_repository.preference")
    def preference(self, user_id, *, enabled=None):
        user_id = validate_positive_integer(user_id, "用户 ID")
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("后台分析开关必须为布尔值。")
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.get_user_sql, (user_id,))
                    if cursor.fetchone() is None:
                        raise AnalysisNotFoundError("用户不存在。")
                    if enabled is not None:
                        cursor.execute(self.save_analysis_preference_sql, (user_id, enabled, enabled))
                    cursor.execute(self.get_analysis_preference_sql, (user_id,))
                    row = cursor.fetchone()
                connection.commit()
                return {"enabled": bool(row["enabled"]) if row else False}
            except Exception:
                connection.rollback()
                raise

    @logged_operation("analysis_repository.claim", log_success=False)
    def claim(self, lease_seconds, max_attempts, *, user_id=None, run_id=None):
        if type(lease_seconds) is not int or not 30 <= lease_seconds <= 600 or type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("分析领取配置不符合要求。")
        scoped = user_id is not None or run_id is not None
        if scoped:
            user_id = validate_positive_integer(user_id, "用户 ID")
            run_id = normalize_run_uuid(run_id)
        if self._manual_scope is not None and (not scoped or self._manual_scope!=(user_id,run_id)):
            raise ValueError('手动分析不能领取其他用户或运行。')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    if scoped:
                        self._owner(cursor, user_id, run_id)
                        sql=self.next_run_analysis_task_sql
                        args=(user_id,run_id,max_attempts)
                        if self._manual_scope is not None:
                            sql=sql.replace('ORDER BY','AND t.event_id=%s ORDER BY')
                            args+= (self._manual_event,)
                        cursor.execute(sql,args)
                    else:
                        cursor.execute(self.reap_analysis_tasks_sql, (max_attempts,))
                        cursor.execute(self.next_analysis_task_sql, (max_attempts,))
                    row = cursor.fetchone()
                    if row is None:
                        connection.commit()
                        return None
                    token = uuid4().hex
                    cursor.execute(self.claim_analysis_task_sql, (token, lease_seconds, row["id"]))
                    cursor.execute(self.get_analysis_task_sql, (row["user_id"], row["run_id"], row["id"]))
                    result = cursor.fetchone()
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _identity(user_id, task_id, token):
        return validate_positive_integer(user_id, "用户 ID"), normalize_run_uuid(task_id), normalize_run_uuid(token)

    @logged_operation("analysis_repository.renew", log_success=False)
    def renew(self, user_id, task_id, token, lease_seconds):
        user_id, task_id, token = self._identity(user_id, task_id, token)
        if type(lease_seconds) is not int or not 30 <= lease_seconds <= 600:
            raise ValueError("分析租约不符合要求。")
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.renew_analysis_task_sql, (lease_seconds, task_id, user_id, token))
                changed = cursor.rowcount == 1
            connection.commit()
            return changed

    @logged_operation("analysis_repository.source", log_success=False)
    def source(self, user_id, task_id, token):
        user_id, task_id, token = self._identity(user_id, task_id, token)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.load_analysis_source_sql, (task_id, user_id, token))
                row = cursor.fetchone()
                if row is None:
                    raise AnalysisLeaseLostError("分析授权或租约失效。")
                return self.source_row(row)

    @logged_operation("analysis_repository.finish", log_success=False)
    def fail(self, user_id, task_id, token, *, status, error_code):
        user_id, task_id, token = self._identity(user_id, task_id, token)
        if status not in {"failed", "attention"} or error_code not in ERROR_CODES:
            raise ValueError("分析失败状态不符合要求。")
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.finish_analysis_task_sql, (status, error_code, task_id, user_id, token))
                saved = cursor.rowcount == 1
            connection.commit()
            return saved

    @logged_operation("analysis_repository.complete")
    def complete(self, user_id, run_id, task_id, token, content, model_name):
        user_id, task_id, token = self._identity(user_id, task_id, token)
        run_id = normalize_run_uuid(run_id)
        if type(model_name) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", model_name):
            raise ValueError("分析模型标识错误。")
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    # 与报告/事件接收统一先锁 run；不持有锁调用模型。
                    self._owner(cursor, user_id, run_id, lock=True)
                    cursor.execute(self.get_analysis_task_sql.rstrip().rstrip(";") + " FOR UPDATE", (user_id, run_id, task_id))
                    task = cursor.fetchone()
                    if not task or task["status"] != "running" or task["lease_token"] != token or not task["lease_active"]:
                        raise AnalysisLeaseLostError("旧分析尝试不可写回。")
                    # 共享锁使关闭本人开关与最终提交有明确先后关系。
                    cursor.execute(self.get_analysis_preference_sql.rstrip().rstrip(";") + " FOR SHARE", (user_id,))
                    preference = cursor.fetchone()
                    if (preference is None or not preference["enabled"]) and self._manual_scope is None:
                        raise AnalysisLeaseLostError("本人后台分析已关闭。")
                    cursor.execute(self.load_analysis_source_sql, (task_id, user_id, token))
                    row = self.source_row(cursor.fetchone())
                    validated = self.validate_content(content, row)
                    text = compact(validated)
                    if len(text.encode("utf-8")) > 131072:
                        raise AnalysisStorageError("分析结果超出存储预算。")
                    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    cursor.execute(self.save_analysis_result_sql, (task_id, text, digest, model_name))
                    cursor.execute(self.finish_analysis_task_sql, ("succeeded", None, task_id, user_id, token))
                    if cursor.rowcount != 1:
                        raise AnalysisLeaseLostError("分析结果提交时租约已失效。")
                    if self.agent_name == 'analysis':
                        # 同一事务交接：建议仅排队，开关仍默认关闭，不在此调用模型或 SSH。
                        cursor.execute(self.create_suggestion_task_sql,
                            (uuid4().hex, user_id, run_id, row['event_id'], row['report_version']))
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @logged_operation("analysis_repository.task")
    def task(self, user_id, run_id, task_id, *, retry=False):
        user_id = validate_positive_integer(user_id, "用户 ID")
        run_id, task_id = normalize_run_uuid(run_id), normalize_run_uuid(task_id)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    self._owner(cursor, user_id, run_id, lock=retry)
                    sql = self.get_analysis_task_sql.rstrip().rstrip(";") + (" FOR UPDATE" if retry else "")
                    cursor.execute(sql, (user_id, run_id, task_id))
                    row = cursor.fetchone()
                    if row is None:
                        raise AnalysisNotFoundError("分析任务不存在或不可访问。")
                    if retry:
                        cursor.execute(self.get_analysis_preference_sql, (user_id,))
                        pref = cursor.fetchone()
                        if (not pref or not pref["enabled"]) and self._manual_scope is None:
                            raise AnalysisConflictError("请先启用本人后台分析。")
                        self.prepare_retry(cursor, row)
                        cursor.execute(self.retry_analysis_task_sql, (task_id, user_id, run_id))
                    else:
                        keys = AnalysisTaskResponse.model_fields
                        row = {key: row[key] for key in keys}
                        row["lease_active"] = bool(row["lease_active"])
                        for key in ("created_at", "started_at", "finished_at"):
                            if row[key] is not None:
                                row[key] = _utc(row[key])
                connection.commit()
                return {"task_id": task_id, "status": "queued"} if retry else row
            except Exception:
                connection.rollback()
                raise

    @logged_operation("analysis_repository.results")
    def results(self, user_id, run_id, *, limit=10, offset=0):
        user_id = validate_positive_integer(user_id, "用户 ID")
        run_id = normalize_run_uuid(run_id)
        if type(limit) is not int or not 1 <= limit <= 20 or type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("分析结果分页不符合要求。")
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                self._owner(cursor, user_id, run_id)
                cursor.execute(self.list_analysis_results_sql, (user_id, run_id, limit, offset))
                result = []
                for row in cursor.fetchall():
                    try:
                        source = self.source_row(row)
                        text = row["content_json"]
                        if len(text.encode()) > 131072 or hashlib.sha256(text.encode()).hexdigest() != row["content_digest"]:
                            raise ValueError("digest mismatch")
                        content = self.validate_content(strict_json(text), source)
                        result.append({"task_id": row["task_id"], "run_id": run_id, "report_version": row["report_version"],
                            "model_name": row["model_name"], "created_at": _utc(row["created_at"]), "content": content,
                            "is_stale": row["current_report_version"] != row["report_version"] or row["current_run_status"] != source["report"]["status"]})
                    except (ValueError, TypeError, KeyError, IndexError, AnalysisStorageError, AnalysisProtocolError) as error:
                        raise AnalysisStorageError("已存分析内容校验失败。") from error
                return result
