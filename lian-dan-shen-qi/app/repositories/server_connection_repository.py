"""服务器配置仓储：只保存密文；所有读取、分页和删除限制本人归属。"""

from pathlib import Path

from app.database import get_connection
from app.errors import InvalidTokenError, ServerNotFoundError, ServerStorageError, ServerInUseError
from app.logging_config import logged_operation
from app.models.experiment import validate_experiment_limit
from app.models.server_connection import ServerOverview, ServerRecord, ServerPage, normalize_server_id, validate_server_fields
from app.security.server_credentials import validate_server_ciphertext
from app.repositories.experiment_repository import _utc
from app.validators import validate_positive_integer


def _record(row, *, secret=True):
    values = {key: row[key] for key in ('server_id', 'name', 'host', 'port', 'username', 'auth_method', 'host_fingerprint', 'work_dir')}
    values['created_at'] = _utc(row['created_at'])
    return ServerRecord(**values, encrypted_credentials=row['encrypted_credentials']) if secret else ServerOverview(**values)


class ServerConnectionRepository:
    def __init__(self, password):
        self.password = password
        root = Path(__file__).resolve().parents[2] / 'sql'
        for name in ('create_server_connection', 'get_server_connection', 'list_server_connections', 'list_server_connections_before', 'delete_server_connection','active_server_executions'):
            setattr(self, name + '_sql', (root / f'{name}.sql').read_text(encoding='utf-8'))

    @logged_operation('server_connection_repository.create')
    def create(self, user_id, profile, encrypted):
        user_id = validate_positive_integer(user_id, '用户 ID')
        if normalize_server_id(profile.server_id) != profile.server_id:
            raise ValueError('服务器 ID 必须是规范 UUID。')
        values = validate_server_fields(profile.name, profile.host, profile.port, profile.username, profile.auth_method,
                                         profile.host_fingerprint, profile.work_dir)
        if values != (profile.name, profile.host, profile.port, profile.username, profile.auth_method, profile.host_fingerprint, profile.work_dir):
            raise ValueError('服务器配置必须先规范化。')
        encrypted = validate_server_ciphertext(encrypted)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.create_server_connection_sql, (profile.server_id, profile.name, profile.host, profile.port,
                        profile.username, profile.auth_method, profile.host_fingerprint, profile.work_dir, encrypted, user_id))
                    if cursor.rowcount != 1:
                        raise InvalidTokenError('登录已失效，请重新登录。')
                    cursor.execute(self.get_server_connection_sql, (user_id, profile.server_id))
                    row = cursor.fetchone()
                    if row is None:
                        raise ServerStorageError('保存后无法读取服务器配置。')
                    result = _record(row)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    @logged_operation('server_connection_repository.get')
    def get(self, user_id, server_id):
        user_id = validate_positive_integer(user_id, '用户 ID')
        server_id = normalize_server_id(server_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.get_server_connection_sql, (user_id, server_id))
                row = cursor.fetchone()
                if row is None:
                    raise ServerNotFoundError('服务器配置不存在或不属于当前用户。')
                return _record(row)

    @logged_operation('server_connection_repository.list')
    def list(self, user_id, *, limit=20, before_id=None):
        user_id = validate_positive_integer(user_id, '用户 ID')
        limit = validate_experiment_limit(limit)
        before_id = normalize_server_id(before_id) if before_id is not None else None
        with get_connection(self.password) as connection:
            connection.start_transaction(readonly=True, isolation_level='REPEATABLE READ', consistent_snapshot=True)
            try:
                with connection.cursor(dictionary=True) as cursor:
                    if before_id is None:
                        cursor.execute(self.list_server_connections_sql, (user_id, limit + 1))
                    else:
                        cursor.execute(self.get_server_connection_sql, (user_id, before_id))
                        boundary = cursor.fetchone()
                        if boundary is None:
                            raise ServerNotFoundError('服务器配置不存在或不属于当前用户。')
                        stamp = boundary['created_at']
                        cursor.execute(self.list_server_connections_before_sql, (user_id, stamp, stamp, before_id, limit + 1))
                    rows = cursor.fetchall()
                    items = tuple(_record(row, secret=False) for row in rows[:limit])
                    return ServerPage(items, items[-1].server_id if len(rows) > limit else None)
            finally:
                connection.rollback()

    @logged_operation('server_connection_repository.delete')
    def delete(self, user_id, server_id):
        user_id = validate_positive_integer(user_id, '用户 ID')
        server_id = normalize_server_id(server_id)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(self.get_server_connection_sql.rstrip().rstrip(';')+' FOR UPDATE',(user_id,server_id))
                    if cursor.fetchone() is None:raise ServerNotFoundError('服务器配置不存在或不属于当前用户。')
                    cursor.execute(self.active_server_executions_sql,(user_id,server_id))
                    if cursor.fetchone() is not None:raise ServerInUseError('服务器仍有排队中或运行中的训练，请先停止并同步状态。')
                    cursor.execute(self.delete_server_connection_sql, (user_id, server_id))
                    if cursor.rowcount != 1:
                        raise ServerNotFoundError('服务器配置不存在或不属于当前用户。')
                connection.commit()
            except Exception:
                connection.rollback()
                raise
