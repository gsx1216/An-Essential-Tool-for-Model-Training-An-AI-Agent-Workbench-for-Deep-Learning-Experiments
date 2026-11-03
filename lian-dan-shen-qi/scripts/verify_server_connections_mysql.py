"""真实 HTTP+临时 MySQL+回环 SSH 联调；无用户服务器访问和训练命令。"""

import asyncio
from dataclasses import replace
from datetime import datetime
import logging
import sys
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app import database
from app.config import get_mysql_password
from app.errors import ServerCredentialDecryptionError
from app.logging_config import log_failure
from app.models.server_connection import ServerSettings
from app.repositories import server_connection_repository
from app.services.ssh_connector import SSHConnector
from scripts._chat_api_test_app import temporary_chat_api
from scripts._ssh_test_server import local_ssh_server


def require(value, message):
    if not value:
        raise AssertionError(message)


def run_checks(password, remote):
    groups = []
    secrets = [password, 'Test1234', remote.password, remote.private_key, remote.encrypted_private_key, remote.passphrase]
    with temporary_chat_api(password) as (application, state):
        name = state['database_name']
        with TestClient(application) as client:
            owners, headers = [], []
            for username in ('ssh_alice', 'ssh_bob'):
                reply = client.post('/auth/register', json={'username': username, 'password': 'Test1234'})
                require(reply.status_code == 201, '注册失败')
                owners.append(reply.json()['user_id'])
                reply = client.post('/auth/login', json={'username': username, 'password': 'Test1234'})
                require(reply.status_code == 200, '登录失败')
                token = reply.json()['access_token']
                secrets.append(token)
                headers.append({'Authorization': 'Bearer ' + token})
            alice, bob = headers
            base = dict(name='local SSH', host='127.0.0.1', port=remote.port, username='trainer',
                        work_dir='/tmp/training', host_fingerprint=remote.fingerprint,
                        auth_method='private_key', private_key=remote.private_key)
            for method, path, options in (('post', '/servers', {'json': base}), ('get', '/servers', {}),
                                          ('get', '/servers/' + uuid4().hex, {}), ('delete', '/servers/' + uuid4().hex, {}),
                                          ('post', '/servers/' + uuid4().hex + '/test', {})):
                require(getattr(client, method)(path, **options).status_code == 401, '缺少登录保护')
            groups.append('all five SSH configuration operations require actual login')

            def create(owner, **overrides):
                body = {**base, **overrides}
                if body['auth_method'] == 'password':
                    body.pop('private_key', None)
                reply = client.post('/servers', headers=owner, json=body)
                require(reply.status_code == 201, '服务器保存失败')
                require(not any(field in reply.json() for field in ('password', 'private_key', 'passphrase', 'encrypted_credentials')), '公开响应带凭据字段')
                require(reply.headers['Cache-Control'] == 'no-store', '响应可缓存')
                return reply.json()['server_id']

            key_id = create(alice)
            password_id = create(alice, auth_method='password', password=remote.password)
            encrypted_id = create(alice, private_key=remote.encrypted_private_key, passphrase=remote.passphrase)
            foreign = create(bob)
            for identifier in (key_id, password_id, encrypted_id):
                reply = client.post('/servers/' + identifier + '/test', headers=alice)
                require(reply.status_code == 200 and reply.json()['connected'] and reply.json()['fingerprint_verified'], '真实 SSH 测试未通过')
            require(remote.sessions == 0, 'SSH 测试执行了 session')
            groups.append('persisted private-key/password/encrypted-key profiles authenticate over real loopback SSH without commands')

            for path in ('/servers/' + key_id, '/servers/' + key_id + '/test'):
                operation = client.post if path.endswith('/test') else client.get
                require(operation(path, headers=bob).status_code == 404, '跨用户读取或连接未拒绝')
            require(client.delete('/servers/' + key_id, headers=bob).status_code == 404, '跨用户删除未拒绝')
            require(client.get('/servers?user_id=' + str(owners[0]), headers=bob).json()['items'][0]['server_id'] == foreign, '伪造用户改变归属')
            require(client.get('/servers?before_id=' + foreign, headers=alice).status_code == 404, '他人分页游标未拒绝')
            groups.append('owner isolation for details, connection test, deletion and cursor; token identity cannot be overridden')

            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'SELECT encrypted_credentials FROM `{name}`.server_connections WHERE id=%s', (key_id,))
                    ciphertext = cursor.fetchone()[0]
            secrets.append(ciphertext)
            require(remote.private_key not in ciphertext and not ciphertext.startswith('-----'), '数据库保存了明文私钥')
            profile = state['servers'].get(owners[0], key_id)
            require(state['server_service'].cipher.decrypt(owners[0], profile)['private_key'] == remote.private_key, '密文不能恢复')
            for wrong_user, changed in ((owners[1], profile), (owners[0], replace(profile, host='127.0.0.2'))):
                try:
                    state['server_service'].cipher.decrypt(wrong_user, changed)
                except ServerCredentialDecryptionError:
                    pass
                else:
                    raise AssertionError('密文未绑定用户和端点')
            groups.append('database holds ciphertext only; decryption binds owner, profile id and connection metadata')

            bad_password = create(alice, auth_method='password', password='wrong password')
            require(client.post('/servers/' + bad_password + '/test', headers=alice).status_code == 400, '错误密码未拒绝')
            import asyncssh
            bad_pin = create(alice, host_fingerprint=asyncssh.generate_private_key('ssh-ed25519').get_fingerprint('sha256'))
            require(client.post('/servers/' + bad_pin + '/test', headers=alice).status_code == 409, '错误主机指纹未拒绝')
            original = state['server_service'].connector
            state['server_service'].connector = SSHConnector(ServerSettings())
            require(client.post('/servers/' + key_id + '/test', headers=alice).status_code == 403, '空网段策略未拒绝')
            state['server_service'].connector = original
            groups.append('authentication, host-key mismatch and deployment network denial have safe distinct responses')

            for body, expected in (({**base, 'username': ''}, 422), ({**base, 'port': True}, 422),
                                    ({**base, 'host': 'example.com'}, 400), ({**base, 'work_dir': '/tmp/../root'}, 400),
                                    ({**base, 'user_id': owners[1]}, 422), ({**base, 'command': 'train'}, 422),
                                    ({**base, 'password': remote.password}, 400)):
                require(client.post('/servers', headers=alice, json=body).status_code == expected, '非法配置未拒绝')
            groups.append('typed fields, IP-only target, work directory and forbidden identity/command inputs validated')

            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE `{name}`.server_connections SET created_at=%s WHERE user_id=%s', (datetime(2024, 1, 1), owners[0]))
                    cursor.execute(f'SELECT id FROM `{name}`.server_connections WHERE user_id=%s ORDER BY created_at DESC,id DESC', (owners[0],))
                    expected_ids = [row[0] for row in cursor.fetchall()]
                connection.commit()
            collected, before = [], None
            for _ in range(20):
                params = {'limit': 2}
                if before:
                    params['before_id'] = before
                reply = client.get('/servers', headers=alice, params=params)
                require(reply.status_code == 200, '列表分页失败')
                require(all(not any(k in row for k in ('private_key', 'password', 'encrypted_credentials')) for row in reply.json()['items']), '列表泄漏凭据')
                collected += [row['server_id'] for row in reply.json()['items']]
                before = reply.json()['next_cursor']
                if not before:
                    break
            require(collected == expected_ids, '同时间分页重复或缺失')
            groups.append('stable bounded cursor pagination excludes credential columns')

            with patch.object(server_connection_repository, 'get_connection', side_effect=MySQLError(msg='PRIVATE_SQL_FAULT', errno=1146)):
                for method, path, options in (('post', '/servers', {'json': base}), ('get', '/servers', {}),
                                              ('get', '/servers/' + key_id, {}), ('delete', '/servers/' + key_id, {}),
                                              ('post', '/servers/' + key_id + '/test', {})):
                    reply = getattr(client, method)(path, headers=alice, **options)
                    require(reply.status_code == 503 and reply.json() == {'detail': '请稍后重试。'}, '数据库错误泄漏细节')
            groups.append('database failures of all operations return generic 503')

            with database.get_connection(password) as connection:
                with patch.object(database, '_read_sql', return_value=(database.PROJECT_ROOT / 'sql/create_server_connections.sql').read_text(encoding='utf-8').replace('experiment_assistant', name)):
                    database._ensure_server_connections(connection)
                    database._ensure_server_connections(connection)
            require(client.get('/servers/' + key_id, headers=alice).status_code == 200, '重复建表删除旧配置')
            require(client.delete('/servers/' + key_id, headers=alice).status_code == 204, '删除失败')
            require(client.get('/servers/' + key_id, headers=alice).status_code == 404, '删除后仍可查看')
            require(client.post('/servers/' + key_id + '/test', headers=alice).status_code == 404, '删除后仍发起连接')
            require(not state['builder'].created and remote.sessions == 0, '配置操作启动了模型或命令')
            groups.append('repeat migration preserves records; explicit deletion blocks later connection tests')

            require(client.post('/auth/logout', headers=alice).status_code == 204, '退出失败')
            require(client.get('/servers', headers=alice).status_code == 401, '退出后仍可查看服务器')
            logs = '\n'.join(p.read_text(encoding='utf-8') for p in state['log_dir'].rglob('*.txt'))
            require(all(value not in logs for value in secrets + ['PRIVATE_SQL_FAULT']), '日志包含敏感信息')
            paths = application.openapi()['paths']
            require(paths['/servers/{server_id}/test']['post']['security'] == [{'BearerAuth': []}], '缺少接口身份声明')
            groups.append('logout, OpenAPI security and persistent credential-safe logs verified')
    for group in groups:
        print('PASS:', group)
    print('PASS:', len(groups), 'HTTP/MySQL/loopback-SSH groups; temporary database cleaned; no training/model calls.')


async def verify():
    password = get_mysql_password()
    async with local_ssh_server() as remote:
        await asyncio.to_thread(run_checks, password, remote)


if __name__ == '__main__':
    try:
        asyncio.run(verify())
    except (AssertionError, ValueError, MySQLError, OSError) as error:
        log_failure(logging.getLogger('app.scripts.verify_server_connections_mysql'), error,
                    operation='verify_server_connections_mysql.main')
        print('服务器配置验收失败，请查看安全日志。', file=sys.stderr)
        raise SystemExit(1)
