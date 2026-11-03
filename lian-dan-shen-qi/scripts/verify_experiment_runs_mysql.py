"""真实登录和运行接口联调；状态由临时库模拟，不启动训练或收费模型。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import logging
import re
import sys
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app import database
from app.config import get_mysql_password
from app.errors import RunRequestConflictError
from app.logging_config import log_failure
from app.repositories import experiment_run_repository
from scripts._chat_api_test_app import temporary_chat_api


def require(value, message):
    if not value:
        raise AssertionError(message)


def main():
    password = get_mysql_password()
    groups = []
    secrets = [password, 'Test1234', 'PRIVATE_RUN_DESCRIPTION', 'PRIVATE_DATASET_VALUE', 'PRIVATE_SQL_FAULT']
    with temporary_chat_api(password) as (application, state):
        name, repository = state['database_name'], state['runs']
        require(re.fullmatch(r'_codex_chat_http_[0-9a-f]{32}', name), '未生成隔离临时库名')
        require(all(name in value and 'experiment_assistant' not in value for key, value in vars(repository).items() if key.endswith('_sql')), '运行 SQL 未隔离')
        with TestClient(application) as client:
            headers, users = [], []
            for username in ('run_alice', 'run_bob', 'run_empty'):
                reply = client.post('/auth/register', json={'username': username, 'password': 'Test1234'})
                require(reply.status_code == 201, '注册失败')
                users.append(reply.json()['user_id'])
                reply = client.post('/auth/login', json={'username': username, 'password': 'Test1234'})
                require(reply.status_code == 200, '登录失败')
                token = reply.json()['access_token']
                secrets.append(token)
                headers.append({'Authorization': 'Bearer ' + token})
            alice, bob, empty = headers

            def experiment(owner, kind='training'):
                reply = client.post('/experiments', headers=owner,
                                    json={'title': 'run experiment', 'kind': kind, 'description': 'PRIVATE_RUN_DESCRIPTION'})
                require(reply.status_code == 201, '创建实验失败')
                return reply.json()['experiment_id']

            training, second_training, script, foreign = experiment(alice), experiment(alice), experiment(alice, 'script'), experiment(bob)
            route = f'/experiments/{training}/runs'
            request = uuid4().hex
            body = {'request_id': request, 'parameters': {'learning_rate': .001, 'epochs': 3, 'dataset': 'PRIVATE_DATASET_VALUE'}}
            for method, path, options in (('post', route, {'json': body}), ('get', route, {}),
                                         ('get', '/runs/' + uuid4().hex, {}), ('get', '/runs/latest-training', {})):
                require(getattr(client, method)(path, **options).status_code == 401, '缺少登录保护')
            require(client.get('/runs/latest-training', headers=empty).json() == {'run': None}, '无训练记录应为空')
            groups.append('actual login required by all four operations; empty latest result is explicit')

            first = client.post(route, headers=alice, json=body)
            require(first.status_code == 201 and first.json()['replayed'] is False, '首次创建状态错误')
            record = first.json()['run']
            identifier = record['run_id']
            require(record['status'] == 'created' and not record['has_logs'] and not record['has_result'], '登记运行不能伪装已启动')
            require(record['parameters'] == body['parameters'] and record['created_at'].endswith('Z'), '参数/UTC 未保存')
            require('log_ref' not in record and 'input_digest' not in record, '内部字段不应公开')
            require(client.get('/runs/' + identifier, headers=alice).json() == record, '详情不一致')
            require(first.headers['Location'] == '/runs/' + identifier and first.headers['Cache-Control'] == 'no-store', '响应头错误')
            groups.append('atomic run creation, immutable parameter/definition snapshots, created state and safe response')

            replay = client.post(route, headers=alice, json={'request_id': request, 'parameters': dict(reversed(list(body['parameters'].items())))})
            require(replay.status_code == 200 and replay.json()['replayed'] and replay.json()['run'] == record, '幂等重试未复用原运行')
            conflict = client.post(route, headers=alice, json={**body, 'parameters': {'epochs': 4}})
            require(conflict.status_code == 409, '修改参数复用请求编号未拒绝')
            require(client.post(f'/experiments/{second_training}/runs', headers=alice, json=body).status_code == 409, '跨实验复用本人请求编号未拒绝')
            foreign_created = client.post(f'/experiments/{foreign}/runs', headers=bob, json=body)
            require(foreign_created.status_code == 201, '不同用户的同一请求编号应相互独立')
            foreign_id = foreign_created.json()['run']['run_id']
            groups.append('canonical idempotency, payload/experiment conflicts and independent per-user request keys')

            unknown = uuid4().hex
            require(client.get('/runs/' + identifier, headers=bob).json() == client.get('/runs/' + unknown, headers=bob).json(), '详情泄漏他人运行是否存在')
            require(client.get('/runs/' + identifier, headers=bob).status_code == 404, '读取他人运行未拒绝')
            require(client.get(route, headers=bob).status_code == 404, '列出他人实验运行未拒绝')
            require(client.post(route, headers=bob, json=body).status_code == 404, '为他人实验创建运行未拒绝')
            require(client.get('/runs/latest-training', headers=bob, params={'user_id': users[0]}).json()['run']['run_id'] == foreign_id, '伪造 user_id 改变查询身份')
            require(client.get('/runs/latest-training', headers=alice, params={'experiment_id': foreign}).status_code == 404, '他人实验范围未拒绝')
            groups.append('ownership enforced for creation/detail/list/latest scope; user query cannot override token')

            bad = [({'parameters': {}}, 422), ({**body, 'status': 'succeeded'}, 422),
                   ({**body, 'command': 'python train.py'}, 422), ({**body, 'parameters': []}, 422),
                   ({**body, 'parameters': {'api_key': 'not stored'}}, 400),
                   ({**body, 'parameters': {'text': '中' * 20000}}, 400)]
            for payload, code in bad:
                require(client.post(route, headers=alice, json=payload).status_code == code, '非法参数未被拒绝')
            for url in ('/runs/bad-id', route + '?before_id=bad', route + '?limit=101', '/runs/latest-training?status=unknown'):
                require(client.get(url, headers=alice).status_code == 422, '非法路径或筛选参数未拒绝')
            groups.append('forbidden status/command/secret fields, parameter budgets and typed path/filter validation')

            # 实际只在临时库里模拟运行成功、失败和正在运行；没有真实进程。
            def create(owner, parent):
                reply = client.post(f'/experiments/{parent}/runs', headers=owner, json={'request_id': uuid4().hex})
                require(reply.status_code == 201, '测试运行创建失败')
                return reply.json()['run']['run_id']

            succeeded, failed, running = [create(alice, training) for _ in range(3)]
            other_training = create(alice, second_training)
            script_id = create(alice, script)
            stamp = datetime(2024, 1, 1)
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    for index, (run, status, exit_code) in enumerate(((identifier, 'created', None), (succeeded, 'succeeded', 0),
                                                                     (other_training, 'created', None), (failed, 'failed', 1),
                                                                     (running, 'running', None), (script_id, 'created', None))):
                        created = stamp + timedelta(seconds=index * 10)
                        started = created + timedelta(seconds=1) if status in {'succeeded', 'running', 'failed'} else None
                        finished = created + timedelta(seconds=2) if status in {'succeeded', 'failed'} else None
                        cursor.execute(f'UPDATE `{name}`.experiment_runs SET status=%s, created_at=%s, updated_at=%s, started_at=%s, finished_at=%s, exit_code=%s WHERE id=%s',
                                       (status, created, created + timedelta(seconds=3), started, finished, exit_code, run))
                connection.commit()
            latest = client.get('/runs/latest-training', headers=alice).json()['run']
            require(latest['run_id'] == running and latest['status'] == 'running', '最近训练不应回退到旧成功运行或脚本')
            require(client.get('/runs/latest-training?status=failed', headers=alice).json()['run']['run_id'] == failed, '失败筛选错误')
            require(client.get('/runs/latest-training?status=succeeded', headers=alice).json()['run']['run_id'] == succeeded, '成功筛选错误')
            require(client.get('/runs/latest-training', headers=alice, params={'experiment_id': second_training}).json()['run']['run_id'] == other_training, '实验范围错误')
            require(client.get('/runs/latest-training', headers=alice, params={'experiment_id': training, 'status': 'succeeded'}).json()['run']['run_id'] == succeeded, '实验与状态联合筛选错误')
            require(client.get('/runs/latest-training', headers=alice, params={'experiment_id': script}).json() == {'run': None}, '普通脚本不应被当作训练')
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"UPDATE `{name}`.experiment_runs SET status='failed', finished_at=UTC_TIMESTAMP(6), updated_at=UTC_TIMESTAMP(6), exit_code=1 WHERE id=%s", (running,))
                connection.commit()
            latest_failed = client.get('/runs/latest-training', headers=alice).json()['run']
            require(latest_failed['run_id'] == running and latest_failed['status'] == 'failed', '最新失败训练被旧成功结果替代')
            groups.append('latest submitted training includes running/failed and excludes scripts; explicit experiment/status filters')

            # 相同时间戳通过 ID 排序，翻页不重复；游标必须属于当前实验。
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE `{name}`.experiment_runs SET created_at=%s WHERE experiment_id=%s', (stamp, training))
                    cursor.execute(f'SELECT id FROM `{name}`.experiment_runs WHERE experiment_id=%s ORDER BY created_at DESC,id DESC', (training,))
                    expected = [row[0] for row in cursor.fetchall()]
                connection.commit()
            response = client.get(route + '?limit=2', headers=alice).json()
            seen = [item['run_id'] for item in response['items']]
            require(all('parameters' not in item for item in response['items']), '列表不应带完整参数')
            while response['next_cursor']:
                response = client.get(route, headers=alice, params={'limit': 2, 'before_id': response['next_cursor']}).json()
                seen.extend(item['run_id'] for item in response['items'])
            require(seen == expected and len(seen) == len(set(seen)), '同时间分页漏项或重复')
            require(client.get(route, headers=alice, params={'before_id': other_training}).status_code == 404, '其他实验游标未拒绝')
            groups.append('stable keyset pagination across timestamp ties and cursor experiment isolation')

            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE `{name}`.experiments SET title=%s, description=%s WHERE id=%s', ('new definition', 'new description', training))
                connection.commit()
            unchanged = client.post(route, headers=alice, json=body).json()['run']
            require(unchanged['experiment_title'] == record['experiment_title'] and unchanged['experiment_description'] == record['experiment_description'], '重试覆盖了定义快照')
            local = repository.get_run(users[0], identifier)
            changed = local.parameters
            changed['epochs'] = 999
            require(local.parameters['epochs'] == 3, '调用方可以改写冻结快照')
            groups.append('definition edits and caller dictionary mutation cannot overwrite saved snapshots')

            barrier = Barrier(2)
            concurrent_request = uuid4().hex
            def repeat():
                barrier.wait(timeout=10)
                return repository.create_run(users[0], training, concurrent_request, {'epochs': 2})
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(repeat) for _ in range(2)]
                results = [future.result(timeout=20) for future in futures]
            require(sum(item.created for item in results) == 1 and results[0].run.run_id == results[1].run.run_id, '并发幂等创建产生重复运行')
            require(not state['builder'].created, '运行记录操作不应构造模型')
            groups.append('concurrent identical requests persist exactly one run; no model/runner launched')

            barrier = Barrier(2)
            conflict_request = uuid4().hex
            def submit_conflicting(epochs):
                barrier.wait(timeout=10)
                try:
                    return repository.create_run(users[0], training, conflict_request, {'epochs': epochs})
                except RunRequestConflictError:
                    return 'conflict'
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(submit_conflicting, number) for number in (2, 3)]
                results = [future.result(timeout=20) for future in futures]
            require(sum(item == 'conflict' for item in results) == 1
                    and sum(item != 'conflict' and item.created for item in results) == 1,
                    '并发不同参数复用了同一个请求却未冲突')
            groups.append('concurrent conflicting payloads yield one persisted run and one request conflict')

            boundary = client.post(route, headers=alice, json={'request_id': uuid4().hex, 'parameters': {'x': 'a' * 32760}})
            require(boundary.status_code == 201 and len(boundary.json()['run']['parameters']['x']) == 32760, '参数 32768 字节边界不能持久化')
            broken = type(repository)(password)
            for key, value in vars(repository).items():
                if key.endswith('_sql'):
                    setattr(broken, key, value)
            broken.get_experiment_run_sql = f'SELECT missing_field FROM `{name}`.experiment_runs WHERE user_id=%s AND id=%s'
            rollback_id = uuid4().hex
            with patch.object(experiment_run_repository, 'uuid4') as generate:
                generate.return_value.hex = rollback_id
                try:
                    broken.create_run(users[0], training, uuid4().hex, {})
                except MySQLError as error:
                    require(error.errno == 1054, '读回失败错误类型异常')
                else:
                    raise AssertionError('读回失败未传播')
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'SELECT COUNT(*) FROM `{name}`.experiment_runs WHERE id=%s', (rollback_id,))
                    require(cursor.fetchone()[0] == 0, '读回失败后仍保留半完成运行')
            groups.append('parameter byte-budget boundary persists exactly; readback failure rolls back creation')

            with patch.object(experiment_run_repository, 'get_connection', side_effect=MySQLError(msg='PRIVATE_SQL_FAULT', errno=1146)):
                for method, path, options in (('post', route, {'json': {'request_id': uuid4().hex}}), ('get', route, {}),
                                              ('get', '/runs/' + identifier, {}), ('get', '/runs/latest-training', {})):
                    reply = getattr(client, method)(path, headers=alice, **options)
                    require(reply.status_code == 503 and reply.json() == {'detail': '请稍后重试。'}, '数据库异常未安全转换')
            groups.append('all four operation failures become generic 503 without raw SQL details')

            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    try:
                        cursor.execute(f'UPDATE `{name}`.experiment_runs SET user_id=%s, request_id=%s WHERE id=%s', (users[1], uuid4().hex, identifier))
                    except MySQLError as error:
                        require(error.errno == 1452, '复合归属外键错误类型不符')
                    else:
                        raise AssertionError('数据库允许把运行伪造到其他用户')
                    connection.rollback()
                    try:
                        cursor.execute(f'UPDATE `{name}`.experiment_runs SET status=%s WHERE id=%s', ('succeeded', identifier))
                    except MySQLError as error:
                        require(error.errno == 3819, '状态时间约束错误类型不符')
                    else:
                        raise AssertionError('未启动记录可被直接标成成功')
                    connection.rollback()
            groups.append('database composite ownership and status/time checks reject inconsistent records')

            # 重建仓储恢复运行与参数；公开响应不暴露受控文件引用。
            rebuilt = type(repository)(password)
            for key, value in vars(repository).items():
                if key.endswith('_sql'):
                    setattr(rebuilt, key, value)
            require(rebuilt.get_run(users[0], identifier).parameters == body['parameters'], '仓储重建后参数丢失')
            paths = application.openapi()['paths']
            for path, method in (('/experiments/{experiment_id}/runs', 'post'), ('/experiments/{experiment_id}/runs', 'get'),
                                  ('/runs/latest-training', 'get'), ('/runs/{run_id}', 'get')):
                require(paths[path][method]['security'] == [{'BearerAuth': []}], 'OpenAPI 缺少身份要求')
            require(client.post('/auth/logout', headers=alice).status_code == 204, '退出失败')
            require(client.get('/runs/latest-training', headers=alice).status_code == 401, '注销凭据仍可读取运行')
            logs = '\n'.join(path.read_text(encoding='utf-8') for path in state['log_dir'].rglob('*.txt'))
            require(all(value not in logs for value in secrets), '运行日志包含私密参数或凭据')
            require('experiment_run_repository' in logs, '缺少运行操作日志')
            groups.append('persistent reconstruction, mounted OpenAPI, logout and redacted operation logs')

            # 重复迁移保留运行；再在本次临时库模拟旧父表无复合索引的升级。
            def read_sql(filename):
                return (database.PROJECT_ROOT / 'sql' / filename).read_text(encoding='utf-8').replace('experiment_assistant', name)
            with database.get_connection(password) as connection:
                with patch.object(database, '_read_sql', side_effect=read_sql):
                    database._ensure_experiment_runs(connection)
                    database._ensure_experiment_runs(connection)
                require(rebuilt.get_run(users[0], identifier).run_id == identifier, '重复迁移删除运行')
                with connection.cursor() as cursor:
                    # 只清理本脚本创建的模拟运行表，当前业务库从未进入此路径。
                    require(re.fullmatch(r'_codex_chat_http_[0-9a-f]{32}', name), '临时库名不安全')
                    # Runner 新增子表外键；旧结构模拟须先移除本临时库内的空子表。
                    for child in ('chat_run_bindings', 'run_evaluation_specs', 'experiment_evaluation_specs', 'run_reports', 'run_metric_events', 'run_metric_streams'):
                        cursor.execute(f'DROP TABLE `{name}`.`{child}`')
                    cursor.execute(f'DROP TABLE `{name}`.research_advice')
                    cursor.execute(f'DROP TABLE `{name}`.research_references')
                    cursor.execute(f'DROP TABLE `{name}`.run_executions')
                    cursor.execute(f'DROP TABLE `{name}`.experiment_runs')
                    cursor.execute(f'ALTER TABLE `{name}`.experiments DROP INDEX uq_experiments_id_owner')
                with patch.object(database, '_read_sql', side_effect=read_sql):
                    database._ensure_experiment_runs(connection)
                    database._ensure_run_executions(connection)
                    database._ensure_metrics(connection)
                    database._ensure_evaluation(connection)
                    database._ensure_chat_tools(connection)
                    database._ensure_research(connection)
                with connection.cursor() as cursor:
                    cursor.execute(f'SELECT title FROM `{name}`.experiments WHERE id=%s', (training,))
                    require(cursor.fetchone()[0] == 'new definition', '父表升级改变实验内容')
            groups.append('repeat migration retains runs; old experiment schema gains ownership index without changing definitions')
    for group in groups:
        print('PASS:', group)
    print('PASS:', len(groups), 'run HTTP/MySQL groups; simulated states; temporary database removed; no paid calls.')


if __name__ == '__main__':
    try:
        main()
    except (AssertionError, ValueError, MySQLError, OSError) as error:
        log_failure(logging.getLogger('app.scripts.verify_experiment_runs_mysql'), error,
                    operation='verify_experiment_runs_mysql.main')
        print('运行记录验收失败，请查看服务器安全日志。', file=sys.stderr)
        raise SystemExit(1)
