"""短事务固定启动快照、连接归属；远端状态只前进，不因旧响应回退终态。"""

import json
from pathlib import Path
from uuid import uuid4

from app.database import get_connection
from app.errors import RunNotFoundError, RunnerConflictError, RunnerNotStartedError, RunnerProtocolError
from app.logging_config import logged_operation
from app.models.experiment_run import normalize_run_uuid
from app.models.runner import RunnerSettings, launch_digest, output_directory, script_identifier, validate_observation
from app.repositories.experiment_repository import _utc
from app.models.server_connection import normalize_server_id
from app.validators import validate_positive_integer


class RunnerRepository:
    def __init__(self, password):
        self.password = password
        root = Path(__file__).resolve().parents[2] / 'sql'
        for name in ('lock_runner_run', 'get_run_execution', 'create_run_execution', 'queue_execution_run',
                     'observe_execution_run', 'touch_run_execution', 'enqueue_run_monitor'):
            setattr(self, name + '_sql', (root / (name + '.sql')).read_text(encoding='utf-8'))

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        try:
            text = row['launch_json']
            if launch_digest(text) != row['launch_digest']:
                raise ValueError('snapshot digest mismatch')
            launch = json.loads(text)
            if launch['run_id'] != row['run_id'] or launch['server_id'] != row['server_id'] or launch['script_id'] != row['script_id']:
                raise ValueError('snapshot identity mismatch')
            fields = {'version', 'run_id', 'user_id', 'server_id', 'server_binding', 'script_id', 'work_dir', 'spec', 'parameters'}
            # 历史执行快照中的辅助源码只按原样读取，不再提供专用适配器。
            extra = {'revision','revision_helper_source'} | ({'revision_adapter_source'} if 'revision_adapter_source' in launch else set()) if 'revision' in launch else set()
            if (set(launch) not in (fields | extra, fields | extra | {'output_directory'})
                    or type(launch['version']) is not int or launch['version'] != 1
                    or type(launch['user_id']) is not int or launch['user_id'] != row['user_id']
                    or not isinstance(launch['server_binding'], str) or len(launch['server_binding']) != 64
                    or not isinstance(launch['work_dir'], str)):
                raise ValueError('snapshot shape mismatch')
            output_directory(launch.get('output_directory', 'training_outputs'))
            RunnerSettings(json.dumps({row['script_id']: launch['spec']})).resolve(row['script_id'], launch['parameters'])
            return {**row, 'launch': launch, 'created_at': _utc(row['created_at']),
                    'checked_at': _utc(row['checked_at']) if row['checked_at'] else None}
        except (ValueError, KeyError, TypeError) as error:
            raise RunnerProtocolError('启动快照校验失败。') from error

    @logged_operation('runner_repository.get')
    def get(self, user_id, run_id, *, required=True):
        user_id = validate_positive_integer(user_id, '用户 ID'); run_id = normalize_run_uuid(run_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.lock_runner_run_sql.replace(' FOR UPDATE', ''), (user_id, run_id))
                if cursor.fetchone() is None:
                    raise RunNotFoundError('运行不存在或不属于当前用户。')
                cursor.execute(self.get_run_execution_sql, (user_id, run_id))
                result = self._decode(cursor.fetchone())
                if result is None and required:
                    raise RunnerNotStartedError('该运行尚未绑定执行任务。')
                return result

    @logged_operation('runner_repository.claim')
    def claim(self, user_id, run_id, profile, script_id, launch):
        user_id = validate_positive_integer(user_id, '用户 ID'); run_id = normalize_run_uuid(run_id)
        server_id = normalize_server_id(profile.server_id); script_id = script_identifier(script_id)
        text = json.dumps(launch, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
        if len(text.encode()) > 131072 or launch.get('run_id') != run_id or launch.get('server_id') != server_id or launch.get('script_id') != script_id:
            raise ValueError('启动快照不符合要求。')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.lock_runner_run_sql, (user_id, run_id))
                    run = cursor.fetchone()
                    if run is None:
                        raise RunNotFoundError('运行不存在或不属于当前用户。')
                    cursor.execute(self.get_run_execution_sql, (user_id, run_id))
                    existing = self._decode(cursor.fetchone())
                    if existing is not None:
                        if existing['server_id'] != server_id or existing['script_id'] != script_id:
                            raise RunnerConflictError('运行已绑定其他执行目标。')
                        connection.rollback()
                        return existing
                    if run['status'] != 'created':
                        raise RunnerConflictError('运行状态不允许首次启动。')
                    cursor.execute(self.create_run_execution_sql, (run_id, server_id, script_id, uuid4().hex, text,
                                                                  launch_digest(text), user_id, run_id, user_id, server_id))
                    if cursor.rowcount != 1:
                        from app.errors import ServerNotFoundError
                        raise ServerNotFoundError('服务器不存在或不属于当前用户。')
                    cursor.execute(self.queue_execution_run_sql, (user_id, run_id))
                    # 与执行绑定同事务入队，提交后才允许发起远端启动。
                    cursor.execute(self.enqueue_run_monitor_sql,(user_id,run_id))
                    cursor.execute(self.get_run_execution_sql, (user_id, run_id))
                    result = self._decode(cursor.fetchone())
                connection.commit()
                return result
            except Exception:
                connection.rollback(); raise

    @logged_operation('runner_repository.observe')
    def observe(self, user_id, run_id, observation):
        user_id = validate_positive_integer(user_id, '用户 ID'); run_id = normalize_run_uuid(run_id)
        validate_observation(observation)
        # missing/stale 是观测不确定，不代表训练失败，不制造 finished_at。
        status = observation['status']
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    if status != 'missing' and not observation['stale']:
                        cursor.execute(self.observe_execution_run_sql, (status, observation['started'], status, status,
                            observation['exit_code'], observation['error_code'], observation['log_bytes'],
                            run_id, user_id, run_id, status))
                    cursor.execute(self.touch_run_execution_sql, (user_id, run_id))
                connection.commit()
            except Exception:
                connection.rollback(); raise
