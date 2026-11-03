"""服务器配置业务；测试连接不执行命令，也不自动开始实验。"""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from app.logging_config import logged_operation
from app.models.experiment import validate_experiment_limit
from app.models.server_connection import (ServerOverview, normalize_server_id, validate_credentials,
                                          validate_server_fields)
from app.validators import validate_positive_integer


class ServerConnectionService:
    def __init__(self, repository, cipher, connector):
        self.repository, self.cipher, self.connector = repository, cipher, connector

    @logged_operation('server_connection_service.create')
    async def create(self, user_id, *, name, host, port, username, auth_method,
                     host_fingerprint, work_dir, password=None, private_key=None, passphrase=None):
        user_id = validate_positive_integer(user_id, '用户 ID')
        values = validate_server_fields(name, host, port, username, auth_method, host_fingerprint, work_dir)
        credentials = validate_credentials(auth_method, password, private_key, passphrase)
        profile = ServerOverview(uuid4().hex, *values, datetime.now(timezone.utc))
        encrypted = self.cipher.encrypt(user_id, profile, credentials)
        return await asyncio.to_thread(self.repository.create, user_id, profile, encrypted)

    @logged_operation('server_connection_service.get')
    async def get(self, user_id, server_id):
        return await asyncio.to_thread(self.repository.get, validate_positive_integer(user_id, '用户 ID'), normalize_server_id(server_id))

    @logged_operation('server_connection_service.list')
    async def list(self, user_id, *, limit=20, before_id=None):
        user_id = validate_positive_integer(user_id, '用户 ID')
        limit = validate_experiment_limit(limit)
        before_id = normalize_server_id(before_id) if before_id is not None else None
        return await asyncio.to_thread(self.repository.list, user_id, limit=limit, before_id=before_id)

    @logged_operation('server_connection_service.delete')
    async def delete(self, user_id, server_id):
        await asyncio.to_thread(self.repository.delete, validate_positive_integer(user_id, '用户 ID'), normalize_server_id(server_id))

    @logged_operation('server_connection_service.test')
    async def test_connection(self, user_id, server_id):
        user_id = validate_positive_integer(user_id, '用户 ID')
        profile = await self.get(user_id, server_id)
        credentials = self.cipher.decrypt(user_id, profile)
        try:
            duration = await self.connector.test_connection(profile, credentials)
        finally:
            credentials.clear()  # 尽快释放引用；不承诺 Python 内存可被安全擦除。
        await self.get(user_id, profile.server_id)  # 连接期间删除配置时，不报告当前配置仍可用。
        return {'server_id': profile.server_id, 'connected': True, 'auth_method': profile.auth_method,
                'fingerprint_verified': True, 'checked_at': datetime.now(timezone.utc), 'duration_ms': duration}
