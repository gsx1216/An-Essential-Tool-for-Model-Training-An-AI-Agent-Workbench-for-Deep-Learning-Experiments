"""真实登录、实验 HTTP 与 MySQL 联调；只写隔离临时库，不调用模型。"""

from datetime import datetime
import logging
import sys
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app import database
from app.config import get_mysql_password
from app.errors import InvalidTokenError
from app.logging_config import log_failure
from app.repositories import experiment_repository
from scripts._chat_api_test_app import temporary_chat_api


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    password = get_mysql_password()
    groups = []
    secrets = [password, 'Test1234', 'PRIVATE_DESCRIPTION_NOT_FOR_LOGS', 'PRIVATE_DB_ERROR_NOT_FOR_CLIENT']
    with temporary_chat_api(password) as (application, state):
        name, repository = state['database_name'], state['experiments']
        # name 由测试应用内部随机生成；SQL 重写已经把所有实验仓储语句限制到此库。
        require(all(name in value and 'experiment_assistant' not in value
                    for key, value in vars(repository).items() if key.endswith('_sql')), '实验 SQL 未隔离')
        with TestClient(application) as client:
            owners, headers = [], []
            for username in ('experiment_alice', 'experiment_bob'):
                response = client.post('/auth/register', json={'username': username, 'password': 'Test1234'})
                require(response.status_code == 201, '注册失败')
                owners.append(response.json()['user_id'])
                response = client.post('/auth/login', json={'username': username, 'password': 'Test1234'})
                require(response.status_code == 200, '登录失败')
                token = response.json()['access_token']
                secrets.append(token)
                headers.append({'Authorization': 'Bearer ' + token})
            alice, bob = headers
            for method, path, options in (('post', '/experiments', {'json': {'title': 'test'}}),
                                           ('get', '/experiments', {}),
                                           ('get', '/experiments/' + uuid4().hex, {})):
                response = getattr(client, method)(path, **options)
                require(response.status_code == 401 and response.headers.get('WWW-Authenticate') == 'Bearer', '缺少登录保护')
            groups.append('actual registration/login and all experiment operations require bearer')

            def create(owner, **values):
                response = client.post('/experiments', headers=owner, json=values)
                require(response.status_code == 201, '实验创建失败')
                require(response.headers.get('Cache-Control') == 'no-store', '创建响应未禁用缓存')
                result = response.json()
                require(UUID(result['experiment_id']).version == 4, '实验编号不是服务端 UUID4')
                require(result['created_at'].endswith('Z'), '实验时间不是显式 UTC')
                return result

            first = create(alice, title='  第一次训练  ', kind='training', description='PRIVATE_DESCRIPTION_NOT_FOR_LOGS')
            require(first['title'] == '第一次训练', '标题未规范化')
            detail_path = '/experiments/' + first['experiment_id']
            detail = client.get(detail_path, headers=alice)
            require(detail.status_code == 200 and detail.json() == first, '实验详情与创建结果不一致')
            require(client.get('/experiments', headers=bob).json() == {'items': [], 'next_cursor': None}, '空列表错误或跨用户泄漏')
            groups.append('create/read persisted metadata with UUID4, title normalization and UTC')

            other = create(bob, title='Bob experiment')
            unknown = uuid4().hex
            a = client.get(detail_path, headers=bob)
            b = client.get('/experiments/' + unknown, headers=bob)
            require(a.status_code == b.status_code == 404 and a.json() == b.json(), '详情泄漏他人实验是否存在')
            require(client.get('/experiments', headers=alice, params={'before_id': other['experiment_id']}).status_code == 404, '跨用户分页游标未拒绝')
            smuggle = client.get('/experiments', headers=bob, params={'user_id': owners[0]})
            require([row['experiment_id'] for row in smuggle.json()['items']] == [other['experiment_id']], '请求参数覆盖登录用户')
            groups.append('owner isolation for detail, list, missing records and foreign cursor')

            invalids = [({'title': 'bad', 'user_id': owners[1]}, 422), ({'title': 'bad', 'command': 'python train.py'}, 422),
                        ({'title': 'bad', 'kind': 'shell'}, 422), ({'title': 2}, 422),
                        ({'title': 'x' * 201}, 422), ({'title': '  '}, 400),
                        ({'title': 'bad', 'description': 'x' * 10001}, 422)]
            for body, status in invalids:
                response = client.post('/experiments', headers=alice, json=body)
                require(response.status_code == status, '非法创建参数未拒绝')
            for query in ({'limit': 0}, {'limit': 101}, {'before_id': 'not UUID'}):
                require(client.get('/experiments', headers=alice, params=query).status_code == 422, '非法分页参数未拒绝')
            require(client.get('/experiments/not-uuid', headers=alice).status_code == 422, '非法实验 UUID 未拒绝')
            groups.append('field limits/types, blank title, forbidden owner/command and cursor validation')

            unicode = create(alice, title='🧪' * 200, description='🧪' * 10000)
            require(len(unicode['description']) == 10000, '多字节说明未能完整保存')
            injection = create(alice, title="'); DROP TABLE experiments; --", description='  preserve indentation\n')
            require(client.get('/experiments/' + injection['experiment_id'], headers=alice).json()['description'] == '  preserve indentation\n', '原始说明被改写')
            groups.append('UTF-8 maximum length, preserved description and parameterized SQL literals')

            tied = [create(alice, title='same title')['experiment_id'] for _ in range(5)]
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    stamp = datetime(2024, 1, 1)
                    cursor.executemany(f'UPDATE `{name}`.experiments SET created_at=%s, updated_at=%s WHERE user_id=%s AND id=%s',
                                       [(stamp, stamp, owners[0], identifier) for identifier in tied])
                    cursor.execute(f'SELECT id FROM `{name}`.experiments WHERE user_id=%s ORDER BY created_at DESC, id DESC', (owners[0],))
                    expected = [row[0] for row in cursor.fetchall()]
                connection.commit()
            result = client.get('/experiments?limit=2', headers=alice).json()
            collected = [row['experiment_id'] for row in result['items']]
            require(all('description' not in row for row in result['items']), '列表不应返回完整说明')
            # 两页之间插入更晚的实验，不应让已开始的向前分页重复或倒退。
            create(alice, title='newly inserted between pages')
            for _ in range(20):
                if result['next_cursor'] is None:
                    break
                response = client.get('/experiments', headers=alice, params={'limit': 2, 'before_id': result['next_cursor']})
                require(response.status_code == 200, '后续分页失败')
                result = response.json()
                collected.extend(row['experiment_id'] for row in result['items'])
            require(collected == expected and len(collected) == len(set(collected)), '同时间戳或并发新增导致漏项/重复')
            require([value for value in collected if value in tied] == sorted(tied, reverse=True), '时间戳相同时 UUID 次排序失效')
            groups.append('same-title records, timestamp ties, stable keyset pages across new inserts')

            fresh = type(repository)(password)
            for key, value in vars(fresh).copy().items():
                if key.endswith('_sql'):
                    setattr(fresh, key, value.replace('experiment_assistant', name))
            require(fresh.get_experiment(owners[0], first['experiment_id']).description == 'PRIVATE_DESCRIPTION_NOT_FOR_LOGS', '重建服务后无法恢复实验')
            try:
                fresh.create_experiment(2**63, 'owner does not exist')
            except InvalidTokenError:
                pass
            else:
                raise AssertionError('不存在的用户仍能创建实验')
            groups.append('repository reconstruction restores persisted data; unknown owner cannot create')

            with database.get_connection(password) as connection:
                with patch.object(database, '_read_sql', return_value=(database.PROJECT_ROOT / 'sql/create_experiments.sql').read_text(encoding='utf-8').replace('experiment_assistant', name)):
                    database._ensure_experiments(connection)
                    database._ensure_experiments(connection)
            require(fresh.get_experiment(owners[0], first['experiment_id']).description == 'PRIVATE_DESCRIPTION_NOT_FOR_LOGS', '重复建表改变旧数据')
            groups.append('repeat selective schema migration preserves experimental records')

            with patch.object(experiment_repository, 'get_connection', side_effect=MySQLError(msg='PRIVATE_DB_ERROR_NOT_FOR_CLIENT', errno=1146)):
                for method, path, kwargs in (('post', '/experiments', dict(json={'title': 'unavailable'})),
                                             ('get', '/experiments', {}), ('get', detail_path, {})):
                    response = getattr(client, method)(path, headers=alice, **kwargs)
                    require(response.status_code == 503 and response.json() == {'detail': '请稍后重试。'}, '数据库失败向客户端泄漏细节')
            groups.append('all three operations convert database faults into generic 503')

            # 回滚验收：INSERT 成功后 SELECT 故意失败，整个创建都应撤销。
            broken = type(repository)(password)
            for key, value in vars(fresh).items():
                if key.endswith('_sql'):
                    setattr(broken, key, value)
            rollback_id = uuid4().hex
            broken.get_experiment_sql = f'SELECT nonexistent_column FROM `{name}`.experiments WHERE user_id=%s AND id=%s'
            with patch.object(experiment_repository, 'uuid4') as generate:
                generate.return_value.hex = rollback_id
                try:
                    broken.create_experiment(owners[0], 'must roll back')
                except MySQLError:
                    pass
                else:
                    raise AssertionError('读回失败未上抛')
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'SELECT COUNT(*) FROM `{name}`.experiments WHERE id=%s', (rollback_id,))
                    require(cursor.fetchone()[0] == 0, '失败创建留下半条记录')
            groups.append('readback failure rolls back preceding INSERT atomically')

            require(client.post('/auth/logout', headers=alice).status_code == 204, '退出登录失败')
            require(client.get('/experiments', headers=alice).status_code == 401, '注销 Token 仍可查看实验')
            require(not state['builder'].created, '实验管理不应构造模型网关')
            groups.append('revoked login blocked and experiment APIs never construct a model gateway')
            for path, method in (('/experiments', 'post'), ('/experiments', 'get'), ('/experiments/{experiment_id}', 'get')):
                require(application.openapi()['paths'][path][method]['security'] == [{'BearerAuth': []}], 'OpenAPI 未展示认证要求')
            log_text = '\n'.join(p.read_text(encoding='utf-8') for p in state['log_dir'].rglob('*.txt'))
            require(all(secret not in log_text for secret in secrets), '安全日志包含敏感值')
            require('experiment_repository' in log_text and 'experiment_routes' in log_text, '缺少实验操作日志')
            groups.append('OpenAPI security and persistent operation logs omit private values')
    for group in groups:
        print('PASS:', group)
    print('PASS:', len(groups), 'experiment HTTP/MySQL verification groups; temporary database removed; no model calls.')


if __name__ == '__main__':
    try:
        main()
    except (AssertionError, ValueError, MySQLError, OSError) as error:
        log_failure(logging.getLogger('app.scripts.verify_experiment_api_mysql'), error,
                    operation='verify_experiment_api_mysql.main')
        print('实验接口验收失败，请查看服务器安全日志。', file=sys.stderr)
        raise SystemExit(1)
