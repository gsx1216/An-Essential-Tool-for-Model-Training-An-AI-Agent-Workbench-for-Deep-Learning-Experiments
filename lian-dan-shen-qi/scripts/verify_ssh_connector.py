"""真实 SSH 协议的回环验证；不访问用户服务器、不执行远程命令。"""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import asyncssh

from app.errors import ServerAddressNotAllowedError, ServerAuthenticationError, ServerHostKeyError
from app.models.server_connection import ServerOverview, ServerSettings
from app.services.ssh_connector import SSHConnector
from scripts._ssh_test_server import local_ssh_server


async def verify():
    async with local_ssh_server() as state:
        connector = SSHConnector(ServerSettings(('127.0.0.1/32',), timeout_seconds=5))
        def profile(method, fingerprint=None):
            return ServerOverview(uuid4().hex, 'local test', '127.0.0.1', state.port, 'trainer', method,
                                  fingerprint or state.fingerprint, '/tmp/training', datetime.now(timezone.utc))
        await connector.test_connection(profile('password'), {'password': state.password})
        await connector.test_connection(profile('private_key'), {'private_key': state.private_key, 'passphrase': None})
        await connector.test_connection(profile('private_key'), {'private_key': state.encrypted_private_key, 'passphrase': state.passphrase})
        print('PASS: password, private key, encrypted private key authentication over actual loopback SSH.')
        bad_fingerprint = asyncssh.generate_private_key('ssh-ed25519').get_fingerprint('sha256')
        for expected, selected, credentials in (
            (ServerHostKeyError, profile('password', bad_fingerprint), {'password': state.password}),
            (ServerAuthenticationError, profile('password'), {'password': 'wrong'}),
            (ServerAuthenticationError, profile('private_key'), {'private_key': state.encrypted_private_key, 'passphrase': 'wrong'}),
        ):
            try:
                await connector.test_connection(selected, credentials)
            except expected:
                pass
            else:
                raise AssertionError('SSH failure was not rejected')
        print('PASS: mismatched host key, wrong password and wrong key passphrase rejected.')
        denied = SSHConnector(ServerSettings())
        try:
            await denied.test_connection(profile('password'), {'password': state.password})
        except ServerAddressNotAllowedError:
            pass
        else:
            raise AssertionError('Empty network policy permitted connection')
        if state.sessions != 0:
            raise AssertionError('Connection test requested a command session')
        print('PASS: default network denial; no shell/session/command started; listeners cleaned on exit.')


if __name__ == '__main__':
    asyncio.run(verify())
