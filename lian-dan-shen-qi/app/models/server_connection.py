"""SSH 配置、部署策略及输入校验；用户名与认证凭据分开处理。"""

import base64
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import ipaddress
import json
from pathlib import PurePosixPath
import re
from typing import Literal
from uuid import UUID

from app.validators import normalize_required_text


AuthMethod = Literal['password', 'private_key']


def normalize_server_id(value):
    if not isinstance(value, str):
        raise ValueError('服务器 ID 必须是 UUID。')
    try:
        return UUID(value.strip()).hex
    except ValueError:
        raise ValueError('服务器 ID 格式不正确。') from None


def normalize_host(value):
    if not isinstance(value, str) or '%' in value:
        raise ValueError('请填写服务器 IPv4 或 IPv6 地址。')
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError('第一版服务器地址只支持 IP，不支持域名。') from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_unspecified or address.is_multicast:
        raise ValueError('服务器地址不能为未指定或组播地址。')
    return str(address)


def normalize_fingerprint(value):
    if not isinstance(value, str) or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', value):
        raise ValueError('请提供服务器主机密钥的 SHA256 指纹。')
    decoded = base64.b64decode(value[7:] + '=', validate=True)
    if len(decoded) != 32 or base64.b64encode(decoded).decode().rstrip('=') != value[7:]:
        raise ValueError('服务器主机指纹格式不正确。')
    return value


def validate_server_fields(name, host, port, username, auth_method, host_fingerprint, work_dir):
    name = normalize_required_text(name, '服务器名称', 100)
    host = normalize_host(host)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('SSH 端口必须为 1～65535。')
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', username):
        raise ValueError('SSH 用户名格式不正确；私钥登录也需提供用户名。')
    if auth_method not in ('password', 'private_key'):
        raise ValueError('SSH 认证方式不受支持。')
    host_fingerprint = normalize_fingerprint(host_fingerprint)
    if not isinstance(work_dir, str) or not work_dir.startswith('/') or len(work_dir) > 1024:
        raise ValueError('工作目录必须是 Linux 绝对路径，且不超过 1024 字符。')
    if any(ord(c) < 32 or ord(c) == 127 for c in work_dir) or '..' in PurePosixPath(work_dir).parts:
        raise ValueError('工作目录不能包含控制字符或上级路径。')
    try:
        name.encode('utf-8'); work_dir.encode('utf-8')
    except UnicodeError:
        raise ValueError('配置包含非法字符。') from None
    return name, host, port, username, auth_method, host_fingerprint, str(PurePosixPath(work_dir))


def validate_credentials(auth_method, password=None, private_key=None, passphrase=None):
    if auth_method == 'password':
        if not isinstance(password, str) or not password or len(password.encode('utf-8')) > 4096:
            raise ValueError('密码模式必须提供非空 SSH 密码，且不超过 4096 字节。')
        if private_key is not None or passphrase is not None:
            raise ValueError('密码模式不能同时提交私钥。')
    elif auth_method == 'private_key':
        if password is not None or not isinstance(private_key, str) or not private_key:
            raise ValueError('私钥模式需要私钥，不能同时提交密码。')
        if len(private_key.encode('utf-8')) > 32768 or not private_key.startswith('-----BEGIN '):
            raise ValueError('私钥格式或长度不符合要求。')
        if passphrase is not None and (not isinstance(passphrase, str) or len(passphrase.encode('utf-8')) > 4096):
            raise ValueError('私钥口令格式或长度不符合要求。')
    else:
        raise ValueError('SSH 认证方式不受支持。')
    return {'password': password, 'private_key': private_key, 'passphrase': passphrase}


@dataclass(frozen=True)
class ServerSettings:
    allowed_networks: tuple[str, ...] = ()
    timeout_seconds: int = 15
    max_connections: int = 4

    def __post_init__(self):
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 60:
            raise ValueError('SSH 超时配置必须为 1～60 秒。')
        if type(self.max_connections) is not int or not 1 <= self.max_connections <= 32:
            raise ValueError('SSH 并发配置必须为 1～32。')
        if type(self.allowed_networks) is not tuple or any(not isinstance(v, str) for v in self.allowed_networks):
            raise ValueError('SSH 允许网段配置格式不正确。')
        for value in self.allowed_networks:
            ipaddress.ip_network(value, strict=True)

    def permits(self, host):
        address = ipaddress.ip_address(normalize_host(host))
        return any(address in ipaddress.ip_network(value, strict=True) for value in self.allowed_networks)


@dataclass(frozen=True)
class ServerOverview:
    server_id: str
    name: str
    host: str
    port: int
    username: str
    auth_method: AuthMethod
    host_fingerprint: str
    work_dir: str
    created_at: datetime

    def binding(self):
        values = [self.server_id, self.host, self.port, self.username, self.auth_method,
                  self.host_fingerprint, self.work_dir]
        return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class ServerRecord(ServerOverview):
    encrypted_credentials: str = field(repr=False)


@dataclass(frozen=True)
class ServerPage:
    items: tuple[ServerOverview, ...]
    next_cursor: str | None
