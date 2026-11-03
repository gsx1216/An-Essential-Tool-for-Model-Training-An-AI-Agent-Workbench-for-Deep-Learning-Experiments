"""模拟网络覆盖超时、取消、并发、固定指纹和禁止隐式本地配置。"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh

from app.errors import (ServerAddressNotAllowedError, ServerAuthenticationError, ServerBusyError,
                        ServerConnectionError, ServerConnectionTimeoutError, ServerHostKeyError)
from app.models.server_connection import ServerSettings
from app.services import ssh_connector
from app.services.ssh_connector import SSHConnector, _PinnedClient
from tests.test_server_connection_core import profile


class SSHConnectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connector = SSHConnector(ServerSettings(('127.0.0.1/32',), timeout_seconds=1, max_connections=1))
        self.connection = MagicMock()
        self.connection.wait_closed = AsyncMock()

    async def test_success_pins_host_disables_implicit_config_agent_and_key_lookup(self):
        with patch.object(ssh_connector.asyncssh, 'connect', new_callable=AsyncMock, return_value=self.connection) as connect:
            await self.connector.test_connection(profile(), {'password': 'PRIVATE'})
        options = connect.call_args.kwargs
        self.assertIsNone(options['config'])
        self.assertIsNone(options['agent_path'])
        self.assertEqual(options['client_keys'], [])
        self.assertIsNotNone(options['known_hosts'])
        self.assertTrue(options['known_hosts'])
        self.assertEqual(options['preferred_auth'], ['password'])
        self.connection.close.assert_called_once()
        self.connection.abort.assert_called_once()
        self.connection.run.assert_not_called()

    async def test_network_denial_happens_before_any_connect_call(self):
        with patch.object(ssh_connector.asyncssh, 'connect', new_callable=AsyncMock) as connect:
            with self.assertRaises(ServerAddressNotAllowedError):
                await SSHConnector(ServerSettings()).test_connection(profile(), {'password': 'PRIVATE'})
        connect.assert_not_awaited()

    async def test_host_auth_network_and_timeout_errors_are_typed(self):
        for error, expected in ((asyncssh.HostKeyNotVerifiable('PRIVATE'), ServerHostKeyError),
                                (asyncssh.PermissionDenied('PRIVATE'), ServerAuthenticationError),
                                (OSError('PRIVATE'), ServerConnectionError), (TimeoutError(), ServerConnectionTimeoutError)):
            with patch.object(ssh_connector.asyncssh, 'connect', new_callable=AsyncMock, side_effect=error):
                with patch.object(self.connector, '_matching_host_key_algorithm', new=AsyncMock(side_effect=ServerHostKeyError())):
                    with self.assertRaises(expected):
                        await self.connector.test_connection(profile(), {'password': 'PRIVATE'})

    async def test_multikey_server_selects_matching_pin_and_caches_only_algorithm(self):
        key=MagicMock();key.get_fingerprint.return_value=profile().host_fingerprint
        with patch.object(ssh_connector.asyncssh,'get_server_host_key',new=AsyncMock(return_value=key)) as probe, \
             patch.object(ssh_connector.asyncssh,'connect',new=AsyncMock(side_effect=[
                 asyncssh.HostKeyNotVerifiable('other algorithm'),self.connection,self.connection])) as connect:
            await self.connector.test_connection(profile(),{'password':'PRIVATE'})
            await self.connector.test_connection(profile(),{'password':'PRIVATE'})
        self.assertEqual(probe.await_count,1)
        self.assertEqual(connect.call_args_list[1].kwargs['server_host_key_algs'],['ssh-ed25519'])
        self.assertEqual(connect.call_args_list[2].kwargs['server_host_key_algs'],['ssh-ed25519'])
        self.assertIsNotNone(connect.call_args.kwargs['known_hosts'])
        self.assertNotIn('PRIVATE',str(self.connector._algorithms))
        self.assertNotIn('password',probe.call_args.kwargs)

    async def test_probe_does_not_trust_nonmatching_key_or_send_password(self):
        key=MagicMock();key.get_fingerprint.return_value='SHA256:wrong'
        with patch.object(ssh_connector.asyncssh,'get_server_host_key',new=AsyncMock(return_value=key)) as probe, \
             patch.object(ssh_connector.asyncssh,'connect',new=AsyncMock(side_effect=asyncssh.HostKeyNotVerifiable('wrong'))) as connect:
            with self.assertRaises(ServerHostKeyError):
                await self.connector.test_connection(profile(),{'password':'PRIVATE'})
        self.assertEqual(connect.await_count,1)
        self.assertEqual(probe.await_count,6)
        self.assertEqual(self.connector._algorithms,{})

    async def test_probe_skips_unsupported_algorithm_and_keeps_matching_signature_algorithm(self):
        key=MagicMock();key.get_fingerprint.return_value=profile().host_fingerprint
        with patch.object(ssh_connector.asyncssh,'get_server_host_key',new=AsyncMock(side_effect=[
            asyncssh.KeyExchangeFailed('unsupported'),key])):
            self.assertEqual(await self.connector._matching_host_key_algorithm(profile()),'ecdsa-sha2-nistp256')

    async def test_probe_timeout_releases_semaphore(self):
        with patch.object(ssh_connector.asyncssh,'connect',new=AsyncMock(side_effect=asyncssh.HostKeyNotVerifiable('mismatch'))), \
             patch.object(self.connector,'_matching_host_key_algorithm',new=AsyncMock(side_effect=TimeoutError())):
            with self.assertRaises(ServerConnectionTimeoutError):
                await self.connector.test_connection(profile(),{'password':'PRIVATE'})
        with patch.object(ssh_connector.asyncssh,'connect',new=AsyncMock(return_value=self.connection)):
            await self.connector.test_connection(profile(),{'password':'PRIVATE'})

    async def test_budget_timeout_releases_connection_slot(self):
        async def pending(*args, **kwargs):
            await asyncio.sleep(10)
        with patch.object(ssh_connector.asyncssh, 'connect', side_effect=pending):
            with self.assertRaises(ServerConnectionTimeoutError):
                await self.connector.test_connection(profile(), {'password': 'PRIVATE'})
        with patch.object(ssh_connector.asyncssh, 'connect', new_callable=AsyncMock, return_value=self.connection):
            await self.connector.test_connection(profile(), {'password': 'PRIVATE'})

    async def test_busy_and_cancelled_calls_do_not_leak_slots(self):
        entered = asyncio.Event()
        async def pending(*args, **kwargs):
            entered.set()
            await asyncio.sleep(10)
        with patch.object(ssh_connector.asyncssh, 'connect', side_effect=pending):
            task = asyncio.create_task(self.connector.test_connection(profile(), {'password': 'PRIVATE'}))
            await entered.wait()
            with self.assertRaises(ServerBusyError):
                await self.connector.test_connection(profile(), {'password': 'PRIVATE'})
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with patch.object(ssh_connector.asyncssh, 'connect', new_callable=AsyncMock, return_value=self.connection):
            await self.connector.test_connection(profile(), {'password': 'PRIVATE'})

    async def test_fingerprint_callback_rejects_other_host_key(self):
        first = asyncssh.generate_private_key('ssh-ed25519')
        other = asyncssh.generate_private_key('ssh-ed25519')
        selected = _PinnedClient(first.get_fingerprint('sha256'))
        self.assertTrue(selected.validate_host_public_key('host', '127.0.0.1', 22, first))
        self.assertFalse(selected.validate_host_public_key('host', '127.0.0.1', 22, other))


if __name__ == '__main__':
    unittest.main()
