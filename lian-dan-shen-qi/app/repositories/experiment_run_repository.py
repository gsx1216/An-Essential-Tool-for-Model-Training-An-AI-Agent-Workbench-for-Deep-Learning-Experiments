"""运行仓储：归属复合外键、幂等创建、分页、按本人范围查最近训练。"""

import json
from pathlib import Path
from uuid import uuid4

from mysql.connector import IntegrityError, errorcode

from app.database import get_connection
from app.errors import ExperimentNotFoundError, RunNotFoundError, RunRequestConflictError, RunStorageError
from app.logging_config import logged_operation
from app.models.evaluation import verified_evaluation, evaluation_digest
from app.models.experiment import normalize_experiment_id, validate_experiment_limit
from app.models.experiment_run import (RunCreation, RunOverview, RunPage, RunRecord, canonical_parameters,
                                      normalize_run_uuid, run_input_digest, validate_run_status)
from app.repositories.experiment_repository import _utc
from app.validators import validate_positive_integer


def _overview(row):
    if validate_run_status(row['status']) is None:
        raise ValueError('运行记录缺少状态。')
    values = {key: row[key] for key in ('run_id', 'experiment_id', 'experiment_title', 'experiment_kind',
                                       'status', 'exit_code', 'error_code', 'result_version')}
    values.update({key: _utc(row[key]) if row[key] is not None else None
                   for key in ('created_at', 'updated_at', 'started_at', 'finished_at')})
    values.update(has_logs=bool(row.get('has_logs', row.get('log_ref') is not None)),
                  has_result=bool(row.get('has_result', row['result_version'] > 0)))
    return values


def _read_row(row, *, overview=False):
    try:
        values = _overview(row)
        if overview:
            return RunOverview(**values)
        text = row['parameters_json']
        normalized = canonical_parameters(json.loads(text))
        if text != normalized or run_input_digest(row['experiment_id'], text) != row['input_digest']:
            raise ValueError('参数快照或摘要不一致。')
        return RunRecord(**values, request_id=row['request_id'],
                         experiment_description=row['experiment_description'], parameters_json=text, evaluation_json=verified_evaluation(row))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        raise RunStorageError('运行记录格式或快照校验失败。') from error


class ExperimentRunRepository:
    def __init__(self, password: str):
        self.password = password
        root = Path(__file__).resolve().parents[2] / 'sql'
        for name in ('get_run_experiment', 'lock_run_experiment', 'create_experiment_run', 'get_experiment_run',
                     'get_run_by_request', 'get_run_cursor', 'list_experiment_runs', 'list_experiment_runs_before',
                     'latest_training_run', 'latest_training_run_status', 'latest_training_run_experiment',
                     'latest_training_run_experiment_status', 'save_run_evaluation'):
            setattr(self, name + '_sql', (root / f'{name}.sql').read_text(encoding='utf-8'))

    def _require_experiment(self, cursor, user_id, experiment_id, *, lock=False):
        cursor.execute(self.lock_run_experiment_sql if lock else self.get_run_experiment_sql, (user_id, experiment_id))
        row = cursor.fetchone()
        if row is None:
            raise ExperimentNotFoundError('实验不存在或不属于当前用户。')
        return row

    @logged_operation('experiment_run_repository.create_run')
    def create_run(self, user_id: int, experiment_id: str, request_id: str, parameters: dict) -> RunCreation:
        user_id = validate_positive_integer(user_id, '用户 ID')
        experiment_id = normalize_experiment_id(experiment_id)
        request_id = normalize_run_uuid(request_id, '运行请求编号')
        text = canonical_parameters(parameters)
        digest = run_input_digest(experiment_id, text)
        identifier = uuid4().hex
        duplicate = None
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    source = self._require_experiment(cursor, user_id, experiment_id, lock=True)
                    try:
                        evaluation_text = verified_evaluation(source)
                    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
                        raise RunStorageError('实验评测配置校验失败。') from error
                    cursor.execute(self.create_experiment_run_sql, (identifier, request_id, digest, text, user_id, experiment_id))
                    if cursor.rowcount != 1:
                        raise ExperimentNotFoundError('实验不存在或不属于当前用户。')
                    if evaluation_text is not None:
                        cursor.execute(self.save_run_evaluation_sql, (identifier, user_id, evaluation_text, evaluation_digest(evaluation_text)))
                    cursor.execute(self.get_experiment_run_sql, (user_id, identifier))
                    row = cursor.fetchone()
                    if row is None:
                        raise RunStorageError('创建后无法读取运行记录。')
                    record = _read_row(row)
                connection.commit()
                return RunCreation(record, True)
            except IntegrityError as error:
                connection.rollback()
                if error.errno != errorcode.ER_DUP_ENTRY:
                    raise
                duplicate = error
            except Exception:
                connection.rollback()
                raise
        # 唯一键冲突后的查询使用新事务，避免重复请求读取旧的一致性快照。
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.get_run_by_request_sql, (user_id, request_id))
                row = cursor.fetchone()
                if row is None:
                    raise duplicate
                record = _read_row(row)
                if row['input_digest'] != digest or record.experiment_id != experiment_id:
                    raise RunRequestConflictError('同一请求编号已用于其他运行配置。')
                return RunCreation(record, False)

    @logged_operation('experiment_run_repository.get_run')
    def get_run(self, user_id: int, run_id: str) -> RunRecord:
        user_id = validate_positive_integer(user_id, '用户 ID')
        run_id = normalize_run_uuid(run_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.get_experiment_run_sql, (user_id, run_id))
                row = cursor.fetchone()
                if row is None:
                    raise RunNotFoundError('运行不存在或不属于当前用户。')
                return _read_row(row)

    @logged_operation('experiment_run_repository.list_runs')
    def list_runs(self, user_id: int, experiment_id: str, *, limit=20, before_id=None) -> RunPage:
        user_id = validate_positive_integer(user_id, '用户 ID')
        experiment_id = normalize_experiment_id(experiment_id)
        limit = validate_experiment_limit(limit)
        before_id = normalize_run_uuid(before_id) if before_id is not None else None
        with get_connection(self.password) as connection:
            connection.start_transaction(readonly=True, isolation_level='REPEATABLE READ', consistent_snapshot=True)
            try:
                with connection.cursor(dictionary=True) as cursor:
                    self._require_experiment(cursor, user_id, experiment_id)
                    if before_id is None:
                        cursor.execute(self.list_experiment_runs_sql, (user_id, experiment_id, limit + 1))
                    else:
                        cursor.execute(self.get_run_cursor_sql, (user_id, experiment_id, before_id))
                        boundary = cursor.fetchone()
                        if boundary is None:
                            raise RunNotFoundError('运行不存在或不属于当前用户。')
                        stamp = boundary['created_at']
                        cursor.execute(self.list_experiment_runs_before_sql,
                                       (user_id, experiment_id, stamp, stamp, before_id, limit + 1))
                    rows = cursor.fetchall()
                    items = tuple(_read_row(row, overview=True) for row in rows[:limit])
                    return RunPage(items, items[-1].run_id if len(rows) > limit else None)
            finally:
                connection.rollback()

    @logged_operation('experiment_run_repository.latest_training_run')
    def latest_training_run(self, user_id: int, *, experiment_id=None, status=None) -> RunRecord | None:
        user_id = validate_positive_integer(user_id, '用户 ID')
        experiment_id = normalize_experiment_id(experiment_id) if experiment_id is not None else None
        status = validate_run_status(status)
        name, params = 'latest_training_run', [user_id]
        if experiment_id is not None:
            name += '_experiment'
            params.append(experiment_id)
        if status is not None:
            name += '_status'
            params.append(status)
        with get_connection(self.password) as connection:
            connection.start_transaction(readonly=True, isolation_level='REPEATABLE READ', consistent_snapshot=True)
            try:
                with connection.cursor(dictionary=True) as cursor:
                    if experiment_id is not None:
                        self._require_experiment(cursor, user_id, experiment_id)
                    cursor.execute(getattr(self, name + '_sql'), tuple(params))
                    row = cursor.fetchone()
                    return _read_row(row) if row is not None else None
            finally:
                connection.rollback()
