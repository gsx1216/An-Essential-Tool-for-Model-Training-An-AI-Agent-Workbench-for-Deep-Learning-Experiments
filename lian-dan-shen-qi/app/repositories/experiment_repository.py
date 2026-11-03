"""实验仓储：参数化 SQL、用户归属、原子创建与稳定排序的有界分页。"""

from datetime import timezone
from pathlib import Path
from uuid import uuid4

from mysql.connector import IntegrityError, errorcode

from app.database import get_connection
from app.errors import ExperimentNotFoundError, ExperimentStorageError, InvalidTokenError
from app.logging_config import logged_operation
from app.models.evaluation import canonical_evaluation, evaluation_digest, verified_evaluation
from app.models.experiment import (ExperimentOverview, ExperimentPage, ExperimentRecord,
                                   normalize_experiment_id, validate_experiment_fields,
                                   validate_experiment_limit)
from app.validators import validate_positive_integer


def _utc(value):
    # DATETIME 不包含时区；本表 INSERT 统一用 UTC_TIMESTAMP(6)。
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _record(row, *, overview=False):
    values = {name: row[name] for name in ('experiment_id', 'title', 'kind')}
    values.update(created_at=_utc(row['created_at']), updated_at=_utc(row['updated_at']))
    if overview:
        return ExperimentOverview(**values)
    try:
        text = verified_evaluation(row)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ExperimentStorageError('评测配置读取失败。') from error
    return ExperimentRecord(**values, description=row['description'], evaluation_json=text)


class ExperimentRepository:
    def __init__(self, password: str):
        self.password = password
        sql_root = Path(__file__).resolve().parents[2] / 'sql'
        for name in ('create_experiment', 'get_experiment', 'get_experiment_cursor',
                     'list_experiments', 'list_experiments_before', 'save_experiment_evaluation'):
            setattr(self, name + '_sql', (sql_root / (name + '.sql')).read_text(encoding='utf-8'))

    @logged_operation('experiment_repository.create_experiment')
    def create_experiment(self, user_id: int, title: str, kind: str = 'script', description: str = '', *, evaluation=None) -> ExperimentRecord:
        user_id = validate_positive_integer(user_id, '用户 ID')
        title, kind, description = validate_experiment_fields(title, kind, description)
        text = canonical_evaluation(evaluation)
        experiment_id = uuid4().hex
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.create_experiment_sql, (experiment_id, title, kind, description, user_id))
                    if cursor.rowcount != 1:
                        raise InvalidTokenError('登录已失效，请重新登录。')
                    if text is not None:
                        cursor.execute(self.save_experiment_evaluation_sql, (experiment_id, user_id, text, evaluation_digest(text)))
                    cursor.execute(self.get_experiment_sql, (user_id, experiment_id))
                    row = cursor.fetchone()
                    if row is None:
                        raise ExperimentStorageError('创建后未能读取实验记录。')
                    result = _record(row)
                connection.commit()
            except IntegrityError as error:
                connection.rollback()
                if error.errno == errorcode.ER_NO_REFERENCED_ROW_2:
                    raise InvalidTokenError('登录已失效，请重新登录。') from error
                raise
            except Exception:
                connection.rollback()
                raise
        return result

    @logged_operation('experiment_repository.get_experiment')
    def get_experiment(self, user_id: int, experiment_id: str) -> ExperimentRecord:
        user_id = validate_positive_integer(user_id, '用户 ID')
        experiment_id = normalize_experiment_id(experiment_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.get_experiment_sql, (user_id, experiment_id))
                row = cursor.fetchone()
                if row is None:
                    raise ExperimentNotFoundError('实验不存在或不属于当前用户。')
                return _record(row)

    @logged_operation('experiment_repository.list_experiments')
    def list_experiments(self, user_id: int, *, limit: int = 20, before_id: str | None = None) -> ExperimentPage:
        user_id = validate_positive_integer(user_id, '用户 ID')
        limit = validate_experiment_limit(limit)
        if before_id is not None:
            before_id = normalize_experiment_id(before_id)
        with get_connection(self.password) as connection:
            connection.start_transaction(readonly=True, isolation_level='REPEATABLE READ', consistent_snapshot=True)
            try:
                with connection.cursor(dictionary=True) as cursor:
                    if before_id is None:
                        cursor.execute(self.list_experiments_sql, (user_id, limit + 1))
                    else:
                        cursor.execute(self.get_experiment_cursor_sql, (user_id, before_id))
                        boundary = cursor.fetchone()
                        if boundary is None:
                            raise ExperimentNotFoundError('实验不存在或不属于当前用户。')
                        stamp = boundary['created_at']
                        cursor.execute(self.list_experiments_before_sql, (user_id, stamp, stamp, before_id, limit + 1))
                    rows = cursor.fetchall()
                    items = tuple(_record(row, overview=True) for row in rows[:limit])
                    next_cursor = items[-1].experiment_id if len(rows) > limit else None
                    result = ExperimentPage(items, next_cursor)
            finally:
                connection.rollback()
        return result
