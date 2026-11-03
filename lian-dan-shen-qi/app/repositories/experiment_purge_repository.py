"""实验和运行硬删除：先校验归属并锁定范围，再按外键顺序在单一事务中清理。"""
from pathlib import Path

from app.database import get_connection
from app.errors import ExperimentNotFoundError, RunNotFoundError
from app.logging_config import logged_operation
from app.models.experiment_run import normalize_run_uuid
from app.validators import validate_positive_integer


class PurgeConflictError(ValueError):
    pass


class ExperimentPurgeRepository:
    def __init__(self, password):
        self.password = password
        root = Path(__file__).resolve().parents[2]/'sql'
        for name in ('purge_database','purge_experiment_owner','purge_run_parent','purge_selected_runs',
                     'purge_external_revisions','purge_active_changes','purge_report_events',
                     'purge_runs','purge_experiment'):
            setattr(self, name, (root/(name+'.sql')).read_text(encoding='utf-8'))

    @logged_operation('experiment_purge_repository.delete')
    def delete(self, user_id, identifier, *, experiment=False):
        user_id = validate_positive_integer(user_id, '用户 ID')
        identifier = normalize_run_uuid(identifier)
        # 只允许程序选择这两个固定列名；用户值始终作为 SQL 参数。
        target = 'experiment_id' if experiment else 'id'
        params = (user_id, identifier)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.purge_database)
                    parent = identifier
                    if not experiment:
                        cursor.execute(self.purge_run_parent, params)
                        row = cursor.fetchone()
                        if row is None:raise RunNotFoundError('运行不存在或不可访问。')
                        parent = row['experiment_id']
                    cursor.execute(self.purge_experiment_owner, (user_id,parent))
                    if cursor.fetchone() is None:raise ExperimentNotFoundError('实验不存在或不可访问。')
                    cursor.execute(self.purge_selected_runs.format(target=target), params)
                    runs = cursor.fetchall()
                    if not experiment and not runs:raise RunNotFoundError('运行不存在或不可访问。')
                    if any(row['status'] in {'queued','running'} for row in runs):
                        raise PurgeConflictError('存在排队中或运行中的训练，请先停止并确认结束后再删除。')
                    cursor.execute(self.purge_external_revisions.format(target=target), (*params,identifier))
                    if cursor.fetchone():
                        raise PurgeConflictError('存在未包含在本次删除范围内的派生运行，请先删除派生运行。')
                    cursor.execute(self.purge_active_changes.format(target=target), params)
                    if cursor.fetchone():raise PurgeConflictError('代码修改正在处理中，请等待完成后再删除。')
                    cursor.execute(self.purge_report_events.format(target=target), params)
                    events = cursor.fetchall()
                    if any(row['publishing'] for row in events):
                        raise PurgeConflictError('报告事件正在投递，请稍后重试删除。')
                    for statement in self.purge_runs.split(';'):
                        if statement.strip():cursor.execute(statement.format(target=target), params)
                    if experiment:
                        for statement in self.purge_experiment.split(';'):
                            if statement.strip():cursor.execute(statement, params)
                connection.commit()
                return [row['redis_message_id'] for row in events if row['redis_message_id']]
            except Exception:
                connection.rollback()
                raise
