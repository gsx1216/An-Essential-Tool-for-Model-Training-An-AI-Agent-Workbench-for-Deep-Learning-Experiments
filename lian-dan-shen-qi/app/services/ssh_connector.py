"""受控 SSH 适配器：只连接与认证，不开启 session、shell、SFTP 或远程命令。"""

import asyncio
from contextlib import asynccontextmanager
import hmac
from threading import BoundedSemaphore, Lock
from time import perf_counter

import asyncssh

from app.errors import (ServerAddressNotAllowedError, ServerAuthenticationError, ServerBusyError,
                        ServerConnectionError, ServerConnectionTimeoutError, ServerHostKeyError)
from app.models.server_connection import ServerSettings


class _PinnedClient(asyncssh.SSHClient):
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    def validate_host_public_key(self, host, addr, port, key):
        return hmac.compare_digest(key.get_fingerprint('sha256'), self.fingerprint)


class SSHConnector:
    def __init__(self, settings: ServerSettings):
        self.settings = settings
        # 非阻塞地限制每进程并发，避免把 asyncio 锁绑定到已关闭的测试事件循环。
        self._slots = BoundedSemaphore(settings.max_connections)
        # Only public fingerprint -> algorithm metadata is cached, never credentials/connections.
        self._algorithms = {}
        self._algorithm_lock = Lock()

    async def _matching_host_key_algorithm(self, profile):
        """Probe KEX keys without authentication; the real connection still checks the pin."""
        probe_options = asyncssh.SSHClientConnectionOptions(
            config=None, client_keys=[], agent_path=None, password=None,
            public_key_auth=False, password_auth=False, kbdint_auth=False,
            host_based_auth=False, gss_auth=False)
        for algorithm in ('ssh-ed25519', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384',
                          'ecdsa-sha2-nistp521', 'rsa-sha2-512', 'rsa-sha2-256'):
            try:
                key = await asyncssh.get_server_host_key(profile.host, profile.port, config=None,
                    server_host_key_algs=[algorithm], options=probe_options)
            except asyncssh.KeyExchangeFailed:
                continue
            if key is not None and hmac.compare_digest(key.get_fingerprint('sha256'), profile.host_fingerprint):
                return algorithm
        raise ServerHostKeyError('服务器提供的主机密钥均与已保存指纹不匹配。')

    @asynccontextmanager
    async def connection(self, profile, credentials):
        if not self.settings.permits(profile.host):
            raise ServerAddressNotAllowedError('该 IP 不在部署者允许的 SSH 网段内。')
        if not self._slots.acquire(blocking=False):
            raise ServerBusyError('SSH 连接测试繁忙。')
        connection = None
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                identity = (profile.host, profile.port, profile.host_fingerprint)
                with self._algorithm_lock:
                    algorithm = self._algorithms.get(identity, 'default')
                options = dict(username=profile.username, known_hosts=asyncssh.import_known_hosts(''),
                               client_factory=lambda: _PinnedClient(profile.host_fingerprint),
                               server_host_key_algs=algorithm, config=None, agent_path=None,
                               agent_forwarding=False, client_keys=[], password=None,
                               kbdint_auth=False, host_based_auth=False,
                               connect_timeout=self.settings.timeout_seconds,
                               login_timeout=self.settings.timeout_seconds)
                if profile.auth_method == 'private_key':
                    try:
                        key = await asyncio.to_thread(asyncssh.import_private_key, credentials['private_key'], credentials['passphrase'])
                    except (ValueError, asyncssh.KeyImportError) as error:
                        raise ServerAuthenticationError('私钥或口令不正确。') from error
                    options.update(client_keys=[key], preferred_auth=['publickey'], password_auth=False)
                else:
                    options.update(password=credentials['password'], preferred_auth=['password'], public_key_auth=False)
                try:
                    connection = await asyncssh.connect(profile.host, profile.port, **options)
                except asyncssh.HostKeyNotVerifiable:
                    # A server may offer RSA and Ed25519 simultaneously. A saved Ed25519
                    # fingerprint must not be rejected just because RSA negotiated first.
                    selected = await self._matching_host_key_algorithm(profile)
                    options['server_host_key_algs'] = [selected]
                    connection = await asyncssh.connect(profile.host, profile.port, **options)
                    with self._algorithm_lock:
                        if len(self._algorithms) >= 128:
                            self._algorithms.pop(next(iter(self._algorithms)))
                        self._algorithms[identity] = [selected]
                yield connection
                connection.close()
                await connection.wait_closed()
        except asyncssh.HostKeyNotVerifiable as error:
            raise ServerHostKeyError('服务器主机指纹不匹配。') from error
        except asyncssh.PermissionDenied as error:
            raise ServerAuthenticationError('SSH 认证失败。') from error
        except TimeoutError as error:
            raise ServerConnectionTimeoutError('SSH 连接或认证超时。') from error
        except (asyncssh.Error, OSError) as error:
            raise ServerConnectionError('SSH 连接失败。') from error
        finally:
            try:
                if connection is not None:
                    connection.abort()  # 包括取消和超时路径，不留下后台连接。
            finally:
                self._slots.release()

    async def test_connection(self, profile, credentials):
        started = perf_counter()
        async with self.connection(profile, credentials):
            pass
        return round((perf_counter() - started) * 1000, 3)
