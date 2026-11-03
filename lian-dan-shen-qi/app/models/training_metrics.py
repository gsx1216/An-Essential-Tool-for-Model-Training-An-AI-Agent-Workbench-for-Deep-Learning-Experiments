"""有界 JSONL 指标解析；保留字节位置、半行和质量计数，不推测缺失数值。"""

import base64
from copy import deepcopy
import hashlib
import json
import math
import re
from app.models.evaluation import annotate_series

MAX_LOG = 10485760
MAX_LINE = 8192
MAX_SERIES = 64
TERMINAL = {'succeeded', 'failed', 'cancelled'}


def initial_stream():
    return {'offset': 0, 'pending': '', 'tail': '', 'discarding': False, 'valid': 0, 'invalid': 0,
            'ignored': 0, 'oversized': 0, 'regressions': 0, 'series': [], 'last_steps': {},
            'complete': False, 'caught_up': False, 'truncated': False, 'stale': False}


def strict_json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate JSON key')
            value[key] = item
        return value
    return json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite JSON')))


def metric_line(raw):
    """普通日志返回 None；结构化指标错误抛 ValueError，由调用方记数并继续。"""
    if not raw.strip().startswith(b'{'):
        return None
    item = strict_json(raw.decode('utf-8'))
    if type(item) is not dict:
        return None
    normalized = {key.lower(): value for key, value in item.items()}
    if len(normalized) != len(item):
        raise ValueError('duplicate metric key ignoring case')
    item = normalized
    kind = item.get('type')
    if not isinstance(kind, str) or kind.lower() != 'metric':
        return None
    step, epoch, split = item.get('step'), item.get('epoch'), item.get('split', 'train')
    if type(step) is not int or not 0 <= step <= 2**53 - 1:
        raise ValueError('invalid step')
    if epoch is not None and (type(epoch) is not int or not 0 <= epoch <= 2**53 - 1):
        raise ValueError('invalid epoch')
    split = split.lower() if isinstance(split, str) else split
    if not isinstance(split, str) or split not in {'train', 'validation', 'test'}:
        raise ValueError('invalid split')
    dataset = item.get('dataset', 'unspecified')
    dataset = dataset.lower() if isinstance(dataset, str) else dataset
    if not isinstance(dataset, str) or not re.fullmatch('[a-z0-9][a-z0-9_.-]{0,63}', dataset):
        raise ValueError('invalid dataset')
    values = {key: value for key, value in item.items() if key not in {'type', 'step', 'epoch', 'split', 'dataset'}}
    if not 1 <= len(values) <= 16:
        raise ValueError('invalid metric count')
    for key, value in values.items():
        if not re.fullmatch('[a-z][a-z0-9_]{0,63}', key) or type(value) not in (int, float) or abs(value) > 1e100 or not math.isfinite(value):
            raise ValueError('invalid metric')
    return {'step': step, 'epoch': epoch, 'split': split, 'dataset': dataset, 'values': values}


def parse_page(stream, data, observation, *, rules=None):
    """data 必须是已验证原始字节。返回新状态和事件，不修改传入对象。"""
    state = deepcopy(stream)
    # Old state had split/name and split; conversion changes only namespace, never byte cursor.
    state['series'] = sorted({('unspecified/' + x if x.count('/') == 1 else x) for x in state['series']})
    state['last_steps'] = {('unspecified/' + k if '/' not in k else k): v for k, v in state['last_steps'].items()}
    old_offset = state['offset']
    if type(data) is not bytes or len(data) > 16384 or old_offset + len(data) > MAX_LOG:
        raise ValueError('invalid log page')
    pending = base64.b64decode(state['pending'], validate=True)
    tail = base64.b64decode(state['tail'], validate=True)
    buf, base = pending + data, old_offset - len(pending)
    events, pos = [], 0
    normalizer = None
    if rules:
        from app.runner.remote_agent import MetricNormalizer
        normalizer = MetricNormalizer(rules)

    def consume(line, offset):
        if len(line) > MAX_LINE:
            state['oversized'] += 1
            return
        try:
            event = metric_line(line)
            if event is None and normalizer is not None:
                converted = normalizer.metric(line).splitlines()
                # 同一原始行只有一个 source_offset，使用第一条匹配规则。
                if converted:
                    event = metric_line(converted[0])
            if event is None:
                state['ignored'] += 1
                return
            group = event['dataset'] + '/' + event['split']
            keys = {group + '/' + name for name in event['values']}
            merged = set(state['series']) | keys
            if len(merged) > MAX_SERIES:
                raise ValueError('series limit')
            previous = state['last_steps'].get(group)
            if previous is not None and event['step'] < previous:
                state['regressions'] += 1
            state['last_steps'][group] = event['step']
            state['series'] = sorted(merged)
            state['valid'] += 1
            events.append({'source_offset': offset, **event})
        except (ValueError, UnicodeError, TypeError, OverflowError, RecursionError):
            state['invalid'] += 1

    if state['discarding']:
        end = buf.find(b'\n')
        if end == -1:
            pos = len(buf)
        else:
            pos = end + 1
            state['discarding'] = False
    while pos < len(buf):
        end = buf.find(b'\n', pos)
        if end == -1:
            break
        consume(buf[pos:end].removesuffix(b'\r'), base + pos)
        pos = end + 1
    remaining = buf[pos:]
    if len(remaining) > MAX_LINE:
        state['oversized'] += 1
        state['discarding'] = True
        remaining = b''
    state['offset'] = old_offset + len(data)
    state['caught_up'] = state['offset'] == observation['log_size']
    state['truncated'] = observation['truncated']
    state['stale'] = observation['stale'] or observation['status'] == 'missing'
    final = observation['status'] in TERMINAL and not state['stale'] and state['caught_up'] and not state['truncated']
    if final:
        if remaining and not state['discarding']:
            consume(remaining, base + pos)
        remaining = b''
        state['discarding'] = False
    state['pending'] = base64.b64encode(remaining).decode()
    state['tail'] = base64.b64encode((tail + data)[-128:]).decode()
    state['complete'] = final
    return state, events


def anchor_for(stream):
    return hashlib.sha256(base64.b64decode(stream['tail'], validate=True)).hexdigest()


def run_signature(run):
    keys = ('status', 'exit_code', 'error_code', 'started_at', 'finished_at')
    return hashlib.sha256(json.dumps({key: str(run[key]) if run[key] is not None else None for key in keys}, sort_keys=True).encode()).hexdigest()


def build_report(run, stream, series):
    warnings = []
    if stream is None:
        warnings.append('NOT_SYNCED')
        stream = initial_stream()
    if not stream['valid']:
        warnings.append('NO_METRICS')
    for field, warning in [('invalid', 'INVALID_LINES'), ('oversized', 'OVERSIZED_LINES'),
                           ('regressions', 'STEP_REGRESSION'), ('truncated', 'LOG_TRUNCATED'), ('stale', 'STALE_SOURCE')]:
        if stream[field]:
            warnings.append(warning)
    if not stream['complete']:
        warnings.append('SOURCE_INCOMPLETE')
    if stream['pending'] or stream['discarding']:
        warnings.append('PENDING_TAIL')
    for item in series:
        item['delta'] = item['last_value'] - item['first_value']
    spec = json.loads(run['evaluation_json']) if run.get('evaluation_json') else None
    annotate_series(spec, series)
    if spec is None:
        warnings.append('EVALUATION_UNSPECIFIED')
    return {'schema_version': 2, 'evaluation': spec, 'run_id': run['run_id'], 'experiment_id': run['experiment_id'], 'title': run['experiment_title'],
            'kind': run['experiment_kind'], 'status': run['status'], 'exit_code': run['exit_code'], 'error_code': run['error_code'],
            'started_at': run['started_at'].isoformat() if run['started_at'] else None,
            'finished_at': run['finished_at'].isoformat() if run['finished_at'] else None,
            'parameters': json.loads(run['parameters_json']), 'collection_complete': stream['complete'],
            'source_offset': stream['offset'], 'counts': {key: stream[key] for key in ('valid', 'invalid', 'ignored', 'oversized', 'regressions')},
            'warnings': warnings, 'series': series, 'interpretation': '仅汇总脚本上报数值，不据此认定收敛、泛化改善或异常原因。'}
