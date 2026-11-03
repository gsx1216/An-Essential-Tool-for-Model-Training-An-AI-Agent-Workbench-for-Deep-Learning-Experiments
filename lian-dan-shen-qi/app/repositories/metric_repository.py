"""指标游标/事件原子提交、本人分页与版本化事实报告；数据库事务不跨 SSH。"""

import base64
import hashlib
import json
import math
from pathlib import Path
import re

from app.database import get_connection
from app.models.evaluation import verified_evaluation
from app.errors import MetricStorageError, ReportNotFoundError, RunNotFoundError
from app.logging_config import logged_operation
from app.models.experiment_run import normalize_run_uuid
from app.models.training_metrics import MAX_LOG, build_report, initial_stream, metric_line, run_signature, strict_json
from app.repositories.experiment_repository import _utc
from app.validators import validate_positive_integer
from app.repositories.event_repository import report_event_id
from app.models.training_project import MetricParserRequest
from app.errors import MetricSyncConflictError


def encode(value):
    text = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(',', ':'))
    return text, hashlib.sha256(text.encode()).hexdigest()


def validate_stream(state):
    if type(state) is not dict or set(state) != set(initial_stream()):
        raise ValueError('invalid stream shape')
    for key in ('offset', 'valid', 'invalid', 'ignored', 'oversized', 'regressions'):
        if type(state[key]) is not int or not 0 <= state[key] <= MAX_LOG:
            raise ValueError('invalid stream count')
    for key in ('discarding', 'complete', 'caught_up', 'truncated', 'stale'):
        if type(state[key]) is not bool:
            raise ValueError('invalid stream flag')
    for key, maximum in [('pending', 8192), ('tail', 128)]:
        if not isinstance(state[key], str) or len(base64.b64decode(state[key], validate=True)) > maximum:
            raise ValueError('invalid buffered bytes')
    if len(base64.b64decode(state['tail'])) != min(state['offset'], 128) or len(base64.b64decode(state['pending'])) > state['offset']:
        raise ValueError('invalid byte position')
    if type(state['series']) is not list or len(state['series']) > 64 or len(set(state['series'])) != len(state['series']):
        raise ValueError('invalid series')
    if any(not isinstance(v, str) or not re.fullmatch('([a-z0-9][a-z0-9_.-]{0,63}/)?(train|validation|test)/[a-z][a-z0-9_]{0,63}', v) for v in state['series']):
        raise ValueError('invalid series name')
    if type(state['last_steps']) is not dict or any(not isinstance(k, str) or not re.fullmatch('([a-z0-9][a-z0-9_.-]{0,63}/)?(train|validation|test)', k) for k in state['last_steps']):
        raise ValueError('invalid steps')
    if any(type(v) is not int or not 0 <= v <= 2**53-1 for v in state['last_steps'].values()):
        raise ValueError('invalid step')
    if state['complete'] and (not state['caught_up'] or state['stale'] or state['truncated'] or state['pending'] or state['discarding']):
        raise ValueError('invalid completeness')
    return state


class MetricRepository:
    def __init__(self, password):
        self.password = password
        root = Path(__file__).resolve().parents[2] / 'sql'
        for name in ('lock_metric_run', 'get_metric_stream', 'save_metric_stream', 'save_metric_event', 'list_metric_events',
                     'aggregate_run_metrics', 'get_run_report', 'save_run_report', 'link_run_report','enqueue_report_event',
                     'get_run_metric_parser','save_run_metric_parser'):
            setattr(self, name + '_sql', (root / (name + '.sql')).read_text(encoding='utf-8'))

    def _enqueue_report_event(self,cursor,run,report):
        """与报告保存同一事务，Redis 不可用时也不丢事件；只发布完整终态训练报告。"""
        data=report['data']
        if (run['experiment_kind']!='training' or data.get('schema_version')!=2
                or data.get('status') not in {'succeeded','failed','cancelled'}
                or data.get('collection_complete') is not True):return
        text,digest=encode(data)
        cursor.execute(self.enqueue_report_event_sql,(report_event_id(run['run_id'],report['version']),
            run['user_id'],run['run_id'],report['version'],text,digest))

    def _run(self, cursor, user_id, run_id, lock=False):
        cursor.execute(self.lock_metric_run_sql if lock else self.lock_metric_run_sql.replace(' FOR UPDATE', ''), (user_id, run_id))
        row = cursor.fetchone()
        if row is None:
            raise RunNotFoundError('运行不存在或不属于当前用户。')
        try:
            row['evaluation_json'] = verified_evaluation(row)
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise MetricStorageError('运行评测快照校验失败。') from error
        for key in ('started_at', 'finished_at'):
            if row[key] is not None:
                row[key] = _utc(row[key])
        return row

    def _stream(self, cursor, user_id, run_id):
        cursor.execute(self.get_metric_stream_sql, (user_id, run_id))
        row = cursor.fetchone()
        if row is None:
            return {'revision': 0, 'state': initial_stream(), 'updated_at': None}
        try:
            text = row['state_json']
            if len(text.encode()) > 32768 or hashlib.sha256(text.encode()).hexdigest() != row['state_digest']:
                raise ValueError('stream digest mismatch')
            state = validate_stream(strict_json(text))
            if state['offset'] != row['source_offset'] or row['revision'] < 1:
                raise ValueError('stream position mismatch')
            return {'revision': row['revision'], 'state': state, 'updated_at': _utc(row['updated_at'])}
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError) as error:
            raise MetricStorageError('指标游标数据校验失败。') from error

    @staticmethod
    def _identity(user_id, run_id):
        return validate_positive_integer(user_id, '用户 ID'), normalize_run_uuid(run_id)

    @logged_operation('metric_repository.snapshot')
    def snapshot(self, user_id, run_id):
        user_id, run_id = self._identity(user_id, run_id)
        with get_connection(self.password) as connection:
            connection.start_transaction(readonly=True, isolation_level='REPEATABLE READ', consistent_snapshot=True)
            try:
                with connection.cursor(dictionary=True) as cursor:
                    self._run(cursor, user_id, run_id)
                    return {**self._stream(cursor, user_id, run_id),'metric_rules':self._parser(cursor,user_id,run_id)}
            finally:
                connection.rollback()

    def _parser(self,cursor,user_id,run_id):
        cursor.execute(self.get_run_metric_parser_sql,(user_id,run_id))
        row=cursor.fetchone()
        if row is None:return []
        try:
            return MetricParserRequest(rules=strict_json(row['rules_json'])).model_dump()['rules']
        except (ValueError,TypeError,KeyError) as error:
            raise MetricStorageError('指标解析规则读取失败。') from error

    @logged_operation('metric_repository.get_parser')
    def get_parser(self,user_id,run_id):
        user_id,run_id=self._identity(user_id,run_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                self._run(cursor,user_id,run_id)
                return {'rules':self._parser(cursor,user_id,run_id)}

    @logged_operation('metric_repository.set_parser')
    def set_parser(self,user_id,run_id,rules):
        user_id,run_id=self._identity(user_id,run_id)
        rules=MetricParserRequest(rules=rules).model_dump()['rules']
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    self._run(cursor,user_id,run_id,lock=True)
                    old=self._parser(cursor,user_id,run_id)
                    if old==rules:
                        connection.rollback()
                        return {'rules':rules}
                    before=self._stream(cursor,user_id,run_id)
                    if before['state']['valid']:
                        raise MetricSyncConflictError('已有有效指标，不能通过修改解析规则覆盖现有数据。')
                    text,digest=encode(initial_stream())
                    rule_text,_=encode(rules)
                    cursor.execute(self.save_run_metric_parser_sql,(run_id,user_id,rule_text))
                    # 只有 0 个有效指标时重置；revision 增加，旧采集任务的 CAS 不能写回。
                    cursor.execute(self.save_metric_stream_sql,(run_id,user_id,before['revision']+1,0,text,digest))
                connection.commit()
                return {'rules':rules}
            except Exception:
                connection.rollback()
                raise

    @logged_operation('metric_repository.commit_page')
    def commit_page(self, user_id, run_id, revision, state, events):
        user_id, run_id = self._identity(user_id, run_id)
        validate_stream(state)
        if type(revision) is not int or revision < 0 or len(events) > 600:
            raise ValueError('指标提交范围不正确。')
        text, digest = encode(state)
        if len(text.encode()) > 32768:
            raise ValueError('指标状态超过预算。')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    self._run(cursor, user_id, run_id, lock=True)
                    before = self._stream(cursor, user_id, run_id)
                    if before['revision'] != revision:
                        connection.rollback()
                        return False
                    if state == before['state']:
                        if events:
                            raise MetricStorageError('未推进游标却产生新事件。')
                        connection.rollback()
                        return True
                    if state['offset'] < before['state']['offset']:
                        raise MetricStorageError('指标游标不能回退。')
                    if any(state[key] < before['state'][key] for key in ('valid', 'invalid', 'ignored', 'oversized', 'regressions')) or state['valid']-before['state']['valid'] != len(events):
                        raise MetricStorageError('指标事件和计数不一致。')
                    last_position = before['state']['offset'] - len(base64.b64decode(before['state']['pending'])) - 1
                    for event in events:
                        position = event['source_offset']
                        checked = metric_line(json.dumps({'type': 'metric', 'step': event['step'], 'epoch': event['epoch'], 'split': event['split'], 'dataset': event.get('dataset', 'unspecified'), **event['values']}).encode())
                        if type(position) is not int or not last_position < position < state['offset'] or checked is None:
                            raise MetricStorageError('指标来源位置不一致。')
                        last_position = position
                        values, _ = encode(event['values'])
                        cursor.execute(self.save_metric_event_sql, (run_id, user_id, event['source_offset'], event['step'],
                                                                   event['epoch'], event['split'], event.get('dataset', 'unspecified'), values))
                    cursor.execute(self.save_metric_stream_sql, (run_id, user_id, revision + 1, state['offset'], text, digest))
                connection.commit()
                return True
            except Exception:
                connection.rollback(); raise

    @logged_operation('metric_repository.list_events')
    def list_events(self, user_id, run_id, *, after=-1, limit=100, split=None, metric_name=None, dataset=None):
        user_id, run_id = self._identity(user_id, run_id)
        if type(after) is not int or not -1 <= after <= MAX_LOG or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('指标分页范围不正确。')
        split = split.lower() if isinstance(split, str) else split
        dataset = dataset.lower() if isinstance(dataset, str) else dataset
        metric_name = metric_name.lower() if isinstance(metric_name, str) else metric_name
        if split is not None and split not in {'train', 'validation', 'test'}:
            raise ValueError('指标分组不正确。')
        if dataset is not None and (not isinstance(dataset, str) or not re.fullmatch('[a-z0-9][a-z0-9_.-]{0,63}', dataset)):
            raise ValueError('数据集 ID 不正确。')
        if metric_name is not None and (not isinstance(metric_name, str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}', metric_name)):
            raise ValueError('指标名不正确。')
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                self._run(cursor, user_id, run_id)
                cursor.execute(self.list_metric_events_sql, (user_id, run_id, after, split, split, metric_name, metric_name, dataset, dataset, limit+1))
                rows = cursor.fetchall()
                items = [self._event_row(row) for row in rows[:limit]]
                return {'items': items, 'next_offset': items[-1]['source_offset'] if len(rows) > limit else None}

    @staticmethod
    def _event_row(row):
        try:
            values = strict_json(row['values_json'])
            if type(values) is not dict or len(row['values_json'].encode()) > 8192:
                raise ValueError('invalid stored metric values')
            checked = metric_line(json.dumps({'type': 'metric', 'step': row['step'], 'epoch': row['epoch'], 'split': row['split'], 'dataset': row.get('dataset', 'unspecified'), **values}).encode())
            if checked is None or type(row['source_offset']) is not int or not 0 <= row['source_offset'] <= MAX_LOG:
                raise ValueError('invalid stored metric event')
            return {'source_offset': row['source_offset'], **checked}
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError) as error:
            raise MetricStorageError('已存指标事件校验失败。') from error

    @logged_operation('metric_repository.invalidate_source')
    def invalidate_source(self, user_id, run_id):
        """来源变化直接在短锁内标记最新版本，不让并发 CAS 覆盖已知不确定性。"""
        user_id, run_id = self._identity(user_id, run_id)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    self._run(cursor, user_id, run_id, lock=True)
                    before = self._stream(cursor, user_id, run_id)
                    state = {**before['state'], 'complete': False, 'caught_up': False, 'stale': True}
                    if state != before['state']:
                        text, digest = encode(state)
                        cursor.execute(self.save_metric_stream_sql, (run_id, user_id, before['revision']+1, state['offset'], text, digest))
                connection.commit()
            except Exception:
                connection.rollback(); raise

    def _summaries(self, cursor, user_id, run_id):
        cursor.execute(self.aggregate_run_metrics_sql, (user_id, run_id))
        rows = cursor.fetchall()
        try:
            if len(rows) > 64 or any(not math.isfinite(row[key]) for row in rows for key in ('first_value', 'last_value', 'min_value', 'max_value')):
                raise ValueError('invalid aggregate values')
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise MetricStorageError('指标聚合超过范围。') from error
        return rows

    @logged_operation('metric_repository.latest')
    def latest(self, user_id, run_id):
        user_id, run_id = self._identity(user_id, run_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                self._run(cursor, user_id, run_id)
                return {'items': self._summaries(cursor, user_id, run_id)}

    @staticmethod
    def _report_row(row, run_id):
        if row is None:
            return None
        try:
            if hashlib.sha256(row['report_json'].encode()).hexdigest() != row['report_digest']:
                raise ValueError('report digest mismatch')
            data = strict_json(row['report_json'])
            if data['run_id'] != run_id or row['version'] < 1:
                raise ValueError('report identity mismatch')
            return {'version': row['version'], 'generated_at': _utc(row['generated_at']), 'data': data}
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            raise MetricStorageError('报告校验失败。') from error

    @logged_operation('metric_repository.report')
    def report(self, user_id, run_id, *, generate=False):
        user_id, run_id = self._identity(user_id, run_id)
        with get_connection(self.password) as connection:
            if not generate:
                connection.start_transaction(readonly=True, isolation_level='REPEATABLE READ', consistent_snapshot=True)
            try:
                with connection.cursor(dictionary=True) as cursor:
                    run = self._run(cursor, user_id, run_id, lock=generate)
                    snapshot = self._stream(cursor, user_id, run_id)
                    cursor.execute(self.get_run_report_sql, (user_id, run_id))
                    row = cursor.fetchone()
                    existing = self._report_row(row, run_id)
                    if existing is not None and existing['version'] != run['result_version']:
                        raise MetricStorageError('报告版本与运行引用不一致。')
                    signature = run_signature(run)
                    evaluation = json.loads(run['evaluation_json']) if run['evaluation_json'] else None
                    stale = existing is not None and (
                        row['source_revision'] != snapshot['revision'] or row['run_signature'] != signature
                        or existing['data'].get('schema_version') != 2
                        or existing['data'].get('evaluation') != evaluation
                    )
                    if not generate:
                        if existing is None:
                            raise ReportNotFoundError('报告尚未生成。')
                        return {**existing, 'is_stale': stale}
                    if existing is not None and not stale:
                        self._enqueue_report_event(cursor,run,existing)
                        connection.commit()
                        return {**existing, 'is_stale': False}
                    data = build_report(run, snapshot['state'] if snapshot['revision'] else None, self._summaries(cursor, user_id, run_id))
                    text, digest = encode(data)
                    if len(text.encode()) > 131072:
                        raise MetricStorageError('报告超过预算。')
                    version = run['result_version'] + 1
                    cursor.execute(self.save_run_report_sql, (run_id, user_id, version, snapshot['revision'], signature, text, digest))
                    cursor.execute(self.link_run_report_sql, (version, 'report:' + run_id, user_id, run_id))
                    cursor.execute(self.get_run_report_sql, (user_id, run_id))
                    result = self._report_row(cursor.fetchone(), run_id)
                    if result is None:
                        raise MetricStorageError('报告保存后无法读取。')
                    self._enqueue_report_event(cursor,run,result)
                connection.commit()
                return {**result, 'is_stale': False}
            except Exception:
                connection.rollback(); raise
            finally:
                # 包含复用既有报告的提前 return；不遗留读事务或 run 行锁。
                connection.rollback()
