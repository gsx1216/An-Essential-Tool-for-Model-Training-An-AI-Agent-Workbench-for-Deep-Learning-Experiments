"""真实回环 SSH 命令通道，将固定 Python bootstrap 交给本机微型进程执行。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import sys

import asyncssh

from app.services.runner_transport import BOOTSTRAP
import shlex


@asynccontextmanager
async def local_runner_ssh():
    host_key = asyncssh.generate_private_key('ssh-ed25519')
    user_key = asyncssh.generate_private_key('ssh-ed25519')
    state = SimpleNamespace(port=None, connections=set(), commands=0,
                            private_key=user_key.export_private_key('openssh').decode(), fingerprint=host_key.get_fingerprint('sha256'))

    class Server(asyncssh.SSHServer):
        def connection_made(self, connection):
            self.connection=connection; state.connections.add(connection)
        def connection_lost(self, exc):
            state.connections.discard(self.connection)
        def begin_auth(self, username):
            return True
        def public_key_auth_supported(self):
            return True
        def validate_public_key(self, username, key):
            return username=='trainer' and key==user_key.convert_to_public()

    async def execute(process):
        child = None
        try:
            if process.command != shlex.join(['python3','-c',BOOTSTRAP]):
                process.exit(1); return
            state.commands += 1
            data=await process.stdin.read()
            child=await asyncio.create_subprocess_exec(sys.executable,'-B','-c',BOOTSTRAP,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            output,error=await child.communicate(data)
            process.stdout.write(output); process.stderr.write(error); process.exit(child.returncode)
        finally:
            if child is not None and child.returncode is None:
                child.kill(); await child.wait()

    listener=await asyncssh.create_server(Server,'127.0.0.1',0,server_host_keys=[host_key],process_factory=execute,encoding=None)
    state.port=listener.get_port()
    try:
        yield state
    finally:
        for connection in list(state.connections):
            connection.abort(); await connection.wait_closed()
        listener.close(); await listener.wait_closed()
