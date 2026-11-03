"""独立临时库验收；--benchmark 才生成多会话数据并计时，不调用模型。"""

import argparse
from datetime import datetime, timezone
import logging
import math
import re
from statistics import median
import sys
from time import perf_counter
from uuid import uuid4

from mysql.connector import Error as MySQLError

from app.config import get_mysql_password
from app.database import get_connection
from app.errors import SessionNotFoundError
from app.logging_config import configure_logging, log_failure
from app.repositories.capacity_repository import CapacityRepository, ROOT, TABLES, bounded_integer, safe_plan
from app.services.database_capacity_service import render_capacity_report


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def validate_temporary_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'_codex_capacity_[0-9a-f]{32}', name):
        raise ValueError('只允许清理本次生成的容量验收临时库。')


def write_new(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as file:
        file.write(content)


def benchmark(password, name, repository, sessions, messages_per_session, repetitions):
    """所有写入和真实正文 SELECT 仅限本次随机临时库。"""
    validate_temporary_name(name)
    with get_connection(password) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f'INSERT INTO `{name}`.users (username) VALUES (%s)', ('benchmark_owner',))
            owner = cursor.lastrowid
            ids = [uuid4().hex for _ in range(sessions)]
            cursor.executemany(f'INSERT INTO `{name}`.chat_sessions (id, user_id, title) VALUES (%s, %s, %s)',
                               [(sid, owner, 'synthetic benchmark') for sid in ids])
            content = '仅用于容量测试的模拟文本 abc123。' * 8
            for sid in ids:
                rows = [(sid, seq, 'human' if seq % 2 else 'ai', content)
                        for seq in range(1, messages_per_session + 1)]
                for begin in range(0, len(rows), 500):
                    cursor.executemany(f'INSERT INTO `{name}`.chat_messages (session_id, seq, role, content) VALUES (%s, %s, %s, %s)', rows[begin:begin + 500])
        connection.commit()
    target = ids[0]
    covered = messages_per_session // 2
    # 真正执行业务 SELECT，但只统计返回条数，绝不打印或保存正文。
    timings, plans = {}, {}
    with get_connection(password) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SET SESSION max_execution_time = 2000')
            cursor.execute('SET SESSION lock_wait_timeout = 3')
        connection.start_transaction(isolation_level='REPEATABLE READ', readonly=True, consistent_snapshot=True)
        try:
            for query, params, expected in (
                ('load_history_page', (owner, target, 0, 50), min(50, messages_per_session)),
                ('load_context_messages', (owner, target, covered), messages_per_session - covered),
            ):
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute('EXPLAIN FORMAT=TRADITIONAL ' + repository.sql[query], params)
                    plans[query] = safe_plan(cursor.fetchall())
                with connection.cursor() as cursor:
                    cursor.execute(repository.sql[query], params)
                    require(len(cursor.fetchall()) == expected, '预热查询条数错误')
                    values = []
                    for _ in range(repetitions):
                        started = perf_counter()
                        cursor.execute(repository.sql[query], params)
                        count = len(cursor.fetchall())
                        values.append((perf_counter() - started) * 1000)
                        require(count == expected, '重复查询条数错误')
                values.sort()
                timings[query] = (expected, median(values), values[math.ceil(len(values) * .95) - 1])
        finally:
            connection.rollback()
    lines = ['# 隔离临时库查询计时记录', '',
             f'生成时间 UTC：{datetime.now(timezone.utc).isoformat()}', '',
             f'额外模拟数据：{sessions} 个会话，每个 {messages_per_session} 条消息，共 {sessions * messages_per_session:,} 条。',
             f'每个查询预热一次，再顺序测量 {repetitions} 次；包含本机驱动读取结果的耗时。',
             f'上下文查询假定摘要覆盖到 seq={covered}，只是测量过滤 SQL，不生成真实摘要。', '',
             '| 查询 | 每次返回条数 | 中位数 ms | 最近秩 P95 ms | 消息表索引 |',
             '| --- | ---: | ---: | ---: | --- |']
    for query, (count, middle, p95) in timings.items():
        keys = ', '.join(row['key'] for row in plans[query] if row['table'] == 'm') or '未提供'
        lines.append(f'| {query} | {count} | {middle:.3f} | {p95:.3f} | {keys} |')
    lines.extend(['', '该测试是小规模、单连接、预热后的查询计时；没有模拟并发、冷缓存、生产长文本或模型调用。',
                  '少量样本的 P95 不代表生产延迟分布，不能由此承诺系统支持多少用户。',
                  '临时库已在脚本 finally 中请求清理；正常退出时终端会确认完成。', ''])
    return '\n'.join(lines)


def run(args, password):
    name = '_codex_capacity_' + uuid4().hex
    validate_temporary_name(name)
    created = False
    checks = []
    artifact = ROOT / 'data' / 'diagnostics' / f'capacity_verification_{uuid4().hex}'
    try:
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
                created = True
                for filename in ('init.sql', 'create_chat_turns.sql', 'create_login_sessions.sql',
                                 'create_model_credentials.sql', 'create_context_summaries.sql', 'create_experiments.sql', 'create_experiment_runs.sql', 'create_server_connections.sql', 'create_run_executions.sql', 'create_run_metric_streams.sql', 'create_run_metric_events.sql', 'create_run_reports.sql', 'create_experiment_evaluation_specs.sql', 'create_run_evaluation_specs.sql', 'add_chat_tool_session_index.sql', 'add_chat_tool_turn_index.sql', 'create_chat_run_bindings.sql', 'create_chat_tool_calls.sql', 'create_research_references.sql', 'create_research_advice.sql', 'create_research_rate_limits.sql', 'extend_research_call_limit.sql','create_training_tasks.sql','create_chat_training_plans.sql','create_training_launch_claims.sql','create_run_monitor_tasks.sql','create_event_outbox.sql','create_agent_tasks.sql','create_analysis_preferences.sql','create_analysis_results.sql'):
                    sql = (ROOT / 'sql' / filename).read_text(encoding='utf-8').replace('experiment_assistant', name)
                    cursor.execute(sql)
                    while cursor.nextset():
                        pass
                cursor.executemany(f'INSERT INTO `{name}`.users (username) VALUES (%s)', [('capacity_alice',), ('capacity_bob',)])
                cursor.execute(f'SELECT id FROM `{name}`.users ORDER BY id')
                alice, bob = [row[0] for row in cursor.fetchall()]
                sid, empty = uuid4().hex, uuid4().hex
                cursor.executemany(f'INSERT INTO `{name}`.chat_sessions (id, user_id) VALUES (%s, %s)', [(sid, alice), (empty, alice)])
                raw = [(sid, 1, 'human', '机密测试甲'), (sid, 2, 'ai', 'reply'),
                       (sid, 5, 'human', '机密测试乙'), (sid, 6, 'ai', 'final')]
                cursor.executemany(f'INSERT INTO `{name}`.chat_messages (session_id, seq, role, content) VALUES (%s, %s, %s, %s)', raw)
                cursor.execute(f'INSERT INTO `{name}`.chat_context_summaries (session_id, content, summarized_until_seq, version, missing_ranges) VALUES (%s, %s, 2, 1, %s)',
                               (sid, 'PRIVATE_SUMMARY_DO_NOT_PRINT', '[]'))
            connection.commit()

        repository = CapacityRepository(password, schema=name)
        meta = repository.inspect()
        require(meta['session'] is None and not meta['missing_tables'], '默认检查范围错误')
        require({row['table_name'] for row in meta['tables']} == set(TABLES), '表元数据不完整')
        checks.append('default metadata-only check and all expected business tables')

        first = repository.inspect(user_id=alice, session_id=sid, sample_limit=2)
        session = first['session']
        require(session['inspected_messages'] == 2 and session['has_more'], '统计未正确限制范围')
        require(session['content_bytes'] == len(raw[0][3].encode()) + len(raw[1][3].encode()), 'UTF-8 字节统计错误')
        require(session['summarized_until_seq'] == 2 and session['summary_version'] == 1, '摘要边界错误')
        checks.append('bounded statistics, exact UTF-8 byte length, summary coverage')

        latter = repository.inspect(user_id=alice, session_id=sid, after_seq=2, sample_limit=2)['session']
        require(latter['first_seq'] == 5 and latter['last_seq'] == 6 and not latter['has_more'], '缺口范围错误')
        zero = repository.inspect(user_id=alice, session_id=empty)['session']
        require(zero['inspected_messages'] == 0 and zero['content_bytes'] == 0 and zero['summary_version'] is None, '空会话错误')
        checks.append('real sequence gaps, complete suffix, empty chat without summary')

        for owner, target in ((bob, sid), (alice, uuid4().hex)):
            try:
                repository.inspect(user_id=owner, session_id=target)
            except SessionNotFoundError:
                pass
            else:
                raise AssertionError('缺少用户归属校验')
        checks.append('missing and other-owner sessions both rejected')

        for query in ('load_history_page', 'load_context_messages'):
            require(first['session']['plans'][query], '缺少真实查询计划')
        text = render_capacity_report(first)
        for secret in ('机密测试甲', '机密测试乙', 'PRIVATE_SUMMARY_DO_NOT_PRINT', password, sid):
            require(secret not in text, '报告包含敏感值')
        write_new(artifact / 'sample_report.md', text)
        checks.append('actual EXPLAIN projection and report excludes credentials and body')

        with get_connection(password) as connection:
            connection.start_transaction(readonly=True)
            try:
                with connection.cursor() as cursor:
                    try:
                        cursor.execute(f'INSERT INTO `{name}`.users (username) VALUES (%s)', ('forbidden_in_readonly',))
                    except MySQLError as error:
                        require(error.errno == 1792, '只读写入应被服务端拒绝')
                    else:
                        raise AssertionError('服务端只读事务未阻止写入')
            finally:
                connection.rollback()
        checks.append('server read-only transaction rejects DML')

        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute('SET SESSION max_execution_time = 100')
                try:
                    cursor.execute('SELECT SLEEP(0.6)')
                    # SLEEP 是特殊函数：独立被超时中断时可返回 1，而不是抛 3024。
                    require(cursor.fetchone()[0] == 1, 'SELECT 超时没有中断探针')
                except MySQLError as error:
                    require(error.errno == 3024, 'SELECT 超时错误类型不符')
        checks.append('server SELECT timeout enforced without changing global settings')

        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f'SELECT seq, role, content FROM `{name}`.chat_messages WHERE session_id=%s ORDER BY seq', (sid,))
                require(cursor.fetchall() == [(r[1], r[2], r[3]) for r in raw], '诊断改变了原文')
                cursor.execute(f'SELECT content FROM `{name}`.chat_context_summaries WHERE session_id=%s', (sid,))
                require(cursor.fetchone()[0] == 'PRIVATE_SUMMARY_DO_NOT_PRINT', '诊断改变了摘要')
        checks.append('raw messages and summary unchanged after inspections')

        if args.benchmark:
            write_new(artifact / 'benchmark_report.md', benchmark(password, name, repository,
                      args.sessions, args.messages_per_session, args.repetitions))
            checks.append('opt-in synthetic multi-session query timing')
        for check in checks:
            print('PASS:', check)
        print('PASS:', len(checks), 'MySQL verification groups; no model calls.')
        print('Reports:', artifact)
    finally:
        if created:
            validate_temporary_name(name)
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'DROP DATABASE `{name}`')
            print('Temporary capacity verification database removed.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='容量检查临时 MySQL 验收；可选择小规模模拟查询计时。')
    parser.add_argument('--benchmark', action='store_true')
    parser.add_argument('--sessions', type=int, default=24)
    parser.add_argument('--messages-per-session', type=int, default=500)
    parser.add_argument('--repetitions', type=int, default=5)
    args = parser.parse_args(argv)
    configure_logging()
    try:
        bounded_integer(args.sessions, '模拟会话数', 2, 100)
        bounded_integer(args.messages_per_session, '每会话模拟消息数', 2, 2000)
        bounded_integer(args.repetitions, '计时重复次数', 1, 50)
        if args.sessions * args.messages_per_session > 100000:
            raise ValueError('本地检查最多生成十万条模拟消息。')
        run(args, get_mysql_password())
    except (ValueError, MySQLError, OSError, AssertionError) as error:
        log_failure(logging.getLogger('app.scripts.verify_database_capacity_mysql'), error,
                    operation='verify_database_capacity_mysql.main')
        print('容量验收失败，请查看服务器安全日志；未运行模型请求。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
