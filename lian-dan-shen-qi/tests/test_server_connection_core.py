"""服务器字段、网络策略、密文绑定、异步业务边界与删除竞争。"""

from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet

from app import config
from app.errors import ServerAuthenticationError, ServerCredentialDecryptionError, ServerNotFoundError
from app.models.server_connection import (ServerOverview, ServerRecord, ServerSettings, normalize_fingerprint,
                                          normalize_host, normalize_server_id, validate_credentials, validate_server_fields)
from app.security.api_key_cipher import ApiKeyCipher
from app.security.server_credentials import ServerCredentialCipher, validate_server_ciphertext
from app.services.server_connection_service import ServerConnectionService


ID = 'a' * 32
PIN = 'SHA256:' + 'A' * 43


def profile():
    return ServerOverview(ID, 'test', '127.0.0.1', 22, 'trainer', 'password', PIN, '/tmp/training', datetime.now(timezone.utc))


def record(cipher, user=7):
    item = profile()
    token = cipher.encrypt(user, item, dict(password='PRIVATE_SSH_PASSWORD', private_key=None, passphrase=None))
    return ServerRecord(**vars(item), encrypted_credentials=token)


class ServerValidationTests(unittest.TestCase):
    def test_normalize_ip_uuid_and_fingerprint(self):
        self.assertEqual(normalize_host('::ffff:127.0.0.1'), '127.0.0.1')
        self.assertEqual(normalize_host('2001:db8::1'), '2001:db8::1')
        self.assertEqual(normalize_server_id('AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA'), ID)
        self.assertEqual(normalize_fingerprint(PIN), PIN)

    def test_bad_hosts_and_fingerprints_rejected(self):
        for value in ('example.com', '0.0.0.0', '::', '224.0.0.1', 'fe80::1%eth0', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_host(value)
        for value in ('SHA256:bad', PIN + '=', ' SHA256:' + 'A' * 43, 'MD5:test'):
            with self.assertRaises(ValueError):
                normalize_fingerprint(value)

    def test_username_port_and_directory_require_valid_values(self):
        base = dict(name='test', host='127.0.0.1', port=22, username='trainer', auth_method='private_key', host_fingerprint=PIN, work_dir='/tmp/training')
        for overrides in ({'port': True}, {'port': 0}, {'username': ''}, {'username': '-x'},
                          {'work_dir': 'relative'}, {'work_dir': '/tmp/../root'}, {'work_dir': '/tmp\npath'}, {'auth_method': 'none'}):
            with self.subTest(fields=overrides), self.assertRaises(ValueError):
                validate_server_fields(**{**base, **overrides})

    def test_selected_auth_mode_requires_only_its_own_credentials(self):
        self.assertEqual(validate_credentials('password', password=' password ')['password'], ' password ')
        self.assertTrue(validate_credentials('private_key', private_key='-----BEGIN OPENSSH PRIVATE KEY-----\n...'))
        for method, kwargs in [('password', {}), ('password', dict(password='p', private_key='k')),
                               ('private_key', dict(private_key='path/to/key')), ('private_key', dict(private_key='-----BEGIN KEY', password='p'))]:
            with self.assertRaises(ValueError):
                validate_credentials(method, **kwargs)

    def test_default_network_denial_and_explicit_ipv4_ipv6_policy(self):
        self.assertFalse(ServerSettings().permits('127.0.0.1'))
        settings = ServerSettings(('127.0.0.1/32', '2001:db8::/64'))
        self.assertTrue(settings.permits('::ffff:127.0.0.1'))
        self.assertFalse(settings.permits('127.0.0.2'))
        self.assertTrue(settings.permits('2001:db8::3'))

    def test_bad_deployment_policy_limits_and_env_are_rejected(self):
        for kwargs in (dict(allowed_networks=('127.0.0.1/24',)), dict(timeout_seconds=True), dict(timeout_seconds=61), dict(max_connections=0)):
            with self.assertRaises(ValueError):
                ServerSettings(**kwargs)
        with patch.object(config, 'load_dotenv'), patch.dict('os.environ', {'SSH_ALLOWED_NETWORKS': '127.0.0.1/32', 'SSH_TEST_TIMEOUT_SECONDS': '5'}, clear=True):
            self.assertEqual(config.get_server_settings().timeout_seconds, 5)
        with patch.object(config, 'load_dotenv'), patch.dict('os.environ', {'SSH_MAX_CONNECTIONS': 'abc'}, clear=True):
            with self.assertRaises(ValueError):
                config.get_server_settings()


class ServerCipherTests(unittest.TestCase):
    def setUp(self):
        self.master = Fernet.generate_key().decode()
        self.cipher = ServerCredentialCipher(self.master)
        self.profile = record(self.cipher)

    def test_encryption_roundtrip_and_no_secret_in_repr(self):
        self.assertEqual(self.cipher.decrypt(7, self.profile)['password'], 'PRIVATE_SSH_PASSWORD')
        self.assertNotIn('PRIVATE_SSH_PASSWORD', self.profile.encrypted_credentials)
        self.assertNotIn(self.profile.encrypted_credentials, repr(self.profile))
        self.assertEqual(validate_server_ciphertext(self.profile.encrypted_credentials), self.profile.encrypted_credentials)

    def test_owner_server_id_endpoint_and_master_are_bound(self):
        for user, changed in ((8, self.profile), (7, replace(self.profile, server_id='b' * 32)),
                              (7, replace(self.profile, username='other')), (7, replace(self.profile, host='127.0.0.2')),
                              (7, replace(self.profile, work_dir='/elsewhere'))):
            with self.assertRaises(ServerCredentialDecryptionError):
                self.cipher.decrypt(user, changed)
        with self.assertRaises(ServerCredentialDecryptionError):
            ServerCredentialCipher(Fernet.generate_key().decode()).decrypt(7, self.profile)

    def test_model_api_key_ciphertext_cannot_be_reused_as_server_credential(self):
        api_token = ApiKeyCipher(self.master).encrypt(7, 'sk-TEST_ONLY')
        with self.assertRaises(ServerCredentialDecryptionError):
            self.cipher.decrypt(7, replace(self.profile, encrypted_credentials=api_token))

    def test_plaintext_invalid_envelope_and_tampering_are_rejected(self):
        for value in ('PRIVATE_SSH_PASSWORD', 'a' * 200):
            with self.assertRaises(ValueError):
                validate_server_ciphertext(value)
        altered = self.profile.encrypted_credentials[:-8] + 'AAAAAAA='
        with self.assertRaises(ServerCredentialDecryptionError):
            self.cipher.decrypt(7, replace(self.profile, encrypted_credentials=altered))


class ServerServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = MagicMock()
        self.cipher = ServerCredentialCipher(Fernet.generate_key().decode())
        self.profile = record(self.cipher)
        self.repository.get.return_value = self.profile
        self.connector = MagicMock()
        self.connector.test_connection = AsyncMock(return_value=1.0)
        self.service = ServerConnectionService(self.repository, self.cipher, self.connector)

    async def test_create_saves_ciphertext_without_connecting(self):
        await self.service.create(7, name='test', host='127.0.0.1', port=22, username='trainer', auth_method='password',
                                  host_fingerprint=PIN, work_dir='/tmp/training', password='PRIVATE_SSH_PASSWORD')
        user, item, token = self.repository.create.call_args.args
        self.assertEqual(user, 7)
        self.assertNotIn('PRIVATE_SSH_PASSWORD', token)
        self.assertEqual(self.cipher.decrypt(7, ServerRecord(**vars(item), encrypted_credentials=token))['password'], 'PRIVATE_SSH_PASSWORD')
        self.connector.test_connection.assert_not_awaited()

    async def test_test_connection_checks_owner_before_ssh_and_rechecks_afterward(self):
        result = await self.service.test_connection(7, ID)
        self.assertTrue(result['connected'])
        self.assertEqual(self.repository.get.call_count, 2)
        self.connector.test_connection.assert_awaited_once()
        self.assertEqual(self.connector.test_connection.call_args.args[1], {})

    async def test_missing_or_foreign_config_never_reaches_connector(self):
        self.repository.get.side_effect = ServerNotFoundError('PRIVATE_OWNER')
        with self.assertRaises(ServerNotFoundError):
            await self.service.test_connection(8, ID)
        self.connector.test_connection.assert_not_awaited()

    async def test_delete_during_connection_is_reported_as_missing(self):
        self.repository.get.side_effect = [self.profile, ServerNotFoundError('deleted')]
        with self.assertRaises(ServerNotFoundError):
            await self.service.test_connection(7, ID)

    async def test_ssh_failure_clears_credentials_and_does_not_return_success(self):
        self.connector.test_connection.side_effect = ServerAuthenticationError('PRIVATE')
        with self.assertRaises(ServerAuthenticationError):
            await self.service.test_connection(7, ID)
        self.assertEqual(self.connector.test_connection.call_args.args[1], {})

    async def test_invalid_list_scope_rejected_before_repository_call(self):
        with self.assertRaises(ValueError):
            await self.service.list(True)
        with self.assertRaises(ValueError):
            await self.service.list(7, limit=101)
        self.repository.list.assert_not_called()


if __name__ == '__main__':
    unittest.main()
