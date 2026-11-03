"""仅测试：本进程回环 SSH 服务，独立生成临时密钥，不关联操作系统账号。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncssh


@asynccontextmanager
async def local_ssh_server():
    host_key = asyncssh.generate_private_key('ssh-ed25519')
    user_key = asyncssh.generate_private_key('ssh-ed25519')
    state = SimpleNamespace(sessions=0, authenticated=0, connections=set(),
                            password='LOCAL_TEST_PASSWORD', passphrase='LOCAL_TEST_PASSPHRASE')

    class Server(asyncssh.SSHServer):
        def connection_made(self, connection):
            self.connection = connection
            state.connections.add(connection)

        def connection_lost(self, exc):
            state.connections.discard(self.connection)

        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def public_key_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == 'trainer' and password == state.password

        def validate_public_key(self, username, key):
            return username == 'trainer' and key == user_key.convert_to_public()

        def auth_completed(self):
            state.authenticated += 1

        def session_requested(self):
            state.sessions += 1
            return False  # 根本不创建远程命令执行器。

    state.fingerprint = host_key.get_fingerprint('sha256')
    state.private_key = user_key.export_private_key('openssh').decode()
    state.encrypted_private_key = user_key.export_private_key('openssh', passphrase=state.passphrase).decode()
    listener = await asyncssh.create_server(Server, '127.0.0.1', 0, server_host_keys=[host_key])
    state.port = listener.get_port()
    try:
        yield state
    finally:
        for connection in list(state.connections):
            connection.abort()
            await connection.wait_closed()
        listener.close()
        await listener.wait_closed()
