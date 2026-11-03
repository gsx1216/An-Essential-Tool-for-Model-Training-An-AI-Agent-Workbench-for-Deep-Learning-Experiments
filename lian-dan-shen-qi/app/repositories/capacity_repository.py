"""维护者只读诊断：固定表元数据、有界消息统计及安全投影的 EXPLAIN。"""

from datetime import datetime, timezone
from pathlib import Path
import re
from time import perf_counter

from app.database import get_connection
from app.errors import SessionNotFoundError
from app.logging_config import logged_operation
from app.validators import normalize_session_id, validate_positive_integer


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 'experiment_assistant'
TABLES = ('users', 'login_sessions', 'chat_sessions', 'chat_messages',
          'chat_turns', 'model_credentials', 'chat_context_summaries', 'experiments', 'experiment_runs', 'server_connections', 'run_executions', 'run_metric_streams', 'run_metric_events', 'run_reports', 'experiment_evaluation_specs', 'run_evaluation_specs', 'chat_run_bindings', 'chat_tool_calls',
          'research_references', 'research_advice', 'research_rate_limits', 'training_tasks', 'chat_training_plans', 'training_launch_claims', 'run_monitor_tasks', 'event_outbox', 'agent_tasks', 'analysis_preferences', 'analysis_results')


def bounded_integer(value, label, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{label}必须是 {minimum}～{maximum} 的整数。')
    return value


def safe_identifier(value):
    """报告仅接受普通标识符；不转储原始查询计划和可能携带常量的 ref/Extra。"""
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_]{1,64}', value) else '未提供'


def safe_plan(rows):
    result = []
    for row in rows:
        extra = row.get('Extra') or ''
        estimated = row.get('rows')
        result.append({
            'table': safe_identifier(row.get('table')),
            'access_type': safe_identifier(row.get('type')),
            'key': safe_identifier(row.get('key')),
            'estimated_rows': int(estimated) if estimated is not None else None,
            'filesort': 'Using filesort' in extra,
            'temporary': 'Using temporary' in extra,
        })
    return result


class CapacityRepository:
    def __init__(self, password: str, *, schema: str = SCHEMA):
        # 可替换的库名仅供本项目临时库验收，CLI 不接受任意库名或 SQL。
        if schema != SCHEMA and not (isinstance(schema, str) and re.fullmatch(r'_codex_capacity_[0-9a-f]{32}', schema)):
            raise ValueError('容量检查只允许项目库或本次验收临时库。')
        self.password = password
        self.schema = schema
        names = ('capacity_tables', 'capacity_indexes', 'capacity_owned_session',
                 'capacity_summary_state', 'capacity_message_sample', 'capacity_has_more',
                 'load_history_page', 'load_context_messages')
        self.sql = {name: (ROOT / 'sql' / f'{name}.sql').read_text(encoding='utf-8')
                    .replace(SCHEMA, schema) for name in names}

    @logged_operation('capacity_repository.inspect')
    def inspect(self, *, user_id=None, session_id=None, after_seq=0, page_size=50,
                sample_limit=1000, max_execution_ms=2000):
        """默认只读元数据；只有显式指定归属会话时才读有界消息窗口。"""
        if (user_id is None) != (session_id is None):
            raise ValueError('用户 ID 和会话 ID 必须一起提供。')
        if user_id is not None:
            user_id = validate_positive_integer(user_id, '用户 ID')
            session_id = normalize_session_id(session_id)
        after_seq = bounded_integer(after_seq, '起始序号', 0, 2**64 - 1)
        page_size = bounded_integer(page_size, '分页条数', 1, 100)
        sample_limit = bounded_integer(sample_limit, '统计上限', 1, 10000)
        max_execution_ms = bounded_integer(max_execution_ms, 'SELECT 超时毫秒数', 100, 30000)

        started = perf_counter()
        with get_connection(self.password) as connection:
            # 仅影响本次连接；不改全局变量，不刷新统计信息，不申请排他行锁。
            with connection.cursor() as cursor:
                cursor.execute('SET SESSION max_execution_time = %s', (max_execution_ms,))
                cursor.execute('SET SESSION lock_wait_timeout = 3')
            connection.start_transaction(isolation_level='REPEATABLE READ',
                                         consistent_snapshot=True, readonly=True)
            try:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.sql['capacity_tables'], (self.schema,))
                    tables = []
                    for row in cursor.fetchall():
                        if row['table_name'] not in TABLES:
                            continue
                        tables.append({
                            'table_name': row['table_name'], 'engine': safe_identifier(row['engine']),
                            **{name: int(row[name]) if row[name] is not None else None
                               for name in ('estimated_rows', 'data_bytes', 'index_bytes')},
                        })
                    cursor.execute(self.sql['capacity_indexes'], (self.schema,))
                    indexes = [{
                        'table_name': row['table_name'],
                        'index_name': safe_identifier(row['index_name']),
                        'column_name': safe_identifier(row['column_name']),
                        'position': int(row['position']), 'unique': not bool(row['non_unique']),
                        'visible': row['is_visible'] == 'YES',
                    } for row in cursor.fetchall() if row['table_name'] in TABLES]
                    report = {
                        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                        'tables': tables, 'indexes': indexes,
                        'missing_tables': [name for name in TABLES if name not in {row['table_name'] for row in tables}],
                        'session': None,
                        'tracked_table_count': len(TABLES),
                        'settings': dict(after_seq=after_seq, page_size=page_size,
                                         sample_limit=sample_limit, max_execution_ms=max_execution_ms),
                    }
                    if session_id is not None:
                        report['session'] = self._inspect_session(cursor, user_id, session_id,
                                                                 after_seq, page_size, sample_limit)
            finally:
                connection.rollback()  # 成功也只结束读取快照；从不提交业务写入。
        report['elapsed_ms'] = round((perf_counter() - started) * 1000, 3)
        return report

    def _inspect_session(self, cursor, user_id, session_id, after_seq, page_size, sample_limit):
        cursor.execute(self.sql['capacity_owned_session'], (user_id, session_id))
        if cursor.fetchone() is None:
            raise SessionNotFoundError('会话不存在或不属于指定用户。')
        cursor.execute(self.sql['capacity_summary_state'], (session_id,))
        summary = cursor.fetchone()
        covered = int(summary['summarized_until_seq']) if summary else 0
        cursor.execute(self.sql['capacity_message_sample'], (user_id, session_id, after_seq, sample_limit))
        sample = cursor.fetchone()
        sample = {name: int(value) if value is not None else None for name, value in sample.items()}
        cursor.execute(self.sql['capacity_has_more'], (user_id, session_id, sample['last_seq'] or after_seq))
        has_more = cursor.fetchone() is not None
        plans = {}
        for name, params in (
            ('load_history_page', (user_id, session_id, after_seq, page_size)),
            ('load_context_messages', (user_id, session_id, covered)),
        ):
            # 普通 EXPLAIN，不执行历史正文查询，也不使用 EXPLAIN ANALYZE。
            cursor.execute('EXPLAIN FORMAT=TRADITIONAL ' + self.sql[name], params)
            plans[name] = safe_plan(cursor.fetchall())
        return {**sample, 'has_more': has_more, 'summarized_until_seq': covered,
                'summary_version': int(summary['version']) if summary else None, 'plans': plans}
