"""运行记录、不可变参数快照及入口校验；不启动训练或执行参数内容。"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re
from typing import Literal
from uuid import UUID


RunStatus = Literal['created', 'queued', 'running', 'succeeded', 'failed', 'cancelled']
RUN_STATUSES = {'created', 'queued', 'running', 'succeeded', 'failed', 'cancelled'}
MAX_PARAMETER_BYTES = 32768
MAX_PARAMETER_DEPTH = 8
MAX_PARAMETER_NODES = 1000


def normalize_run_uuid(value: str, label: str = '运行 ID') -> str:
    if not isinstance(value, str):
        raise ValueError(f'{label}必须是 UUID 字符串。')
    try:
        return UUID(value.strip()).hex
    except ValueError:
        raise ValueError(f'{label}格式不正确。') from None


def validate_run_status(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or value not in RUN_STATUSES):
        raise ValueError('运行状态不符合要求。')
    return value


def canonical_parameters(parameters: dict) -> str:
    """只允许有界 JSON 对象；确定排序后存原始文本，避免数据库 JSON 重排影响快照。"""
    if type(parameters) is not dict:
        raise ValueError('运行参数必须是 JSON 对象。')
    nodes, text_bytes = 0, 0
    active = set()

    def text(value):
        nonlocal text_bytes
        if len(value) > MAX_PARAMETER_BYTES:
            raise ValueError('运行参数超过字节预算。')
        try:
            text_bytes += len(value.encode('utf-8'))
        except UnicodeEncodeError:
            raise ValueError('运行参数包含非法字符。') from None
        if text_bytes > MAX_PARAMETER_BYTES:
            raise ValueError('运行参数超过字节预算。')

    def visit(value, depth):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_PARAMETER_NODES or depth > MAX_PARAMETER_DEPTH:
            raise ValueError('运行参数结构过大或嵌套过深。')
        if value is None or type(value) is bool:
            return
        if type(value) is str:
            text(value)
            return
        if type(value) is int:
            if not -2**63 <= value <= 2**64 - 1:
                raise ValueError('运行参数整数超出支持范围。')
            return
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError('运行参数不能包含 NaN 或无穷大。')
            return
        if type(value) not in (dict, list):
            raise ValueError('运行参数只能包含 JSON 支持的数据类型。')
        if id(value) in active:
            raise ValueError('运行参数不能循环引用。')
        active.add(id(value))
        try:
            if type(value) is dict:
                for key, item in value.items():
                    if not isinstance(key, str) or not key or len(key) > 128:
                        raise ValueError('运行参数键必须是 1～128 字符的字符串。')
                    text(key)
                    normalized = re.sub(r'[^a-z0-9]', '', key.lower())
                    if normalized in {'password', 'passwd', 'authorization', 'token', 'secret', 'accesskey', 'privatekey'} or normalized.endswith(('apikey', 'accesstoken', 'refreshtoken')):
                        raise ValueError('运行参数不接收凭据字段，请使用独立凭据配置。')
                    visit(item, depth + 1)
            else:
                for item in value:
                    visit(item, depth + 1)
        finally:
            active.remove(id(value))

    visit(parameters, 0)
    value = json.dumps(parameters, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
    if len(value.encode('utf-8')) > MAX_PARAMETER_BYTES:
        raise ValueError('运行参数超过 32768 字节预算。')
    return value


def run_input_digest(experiment_id: str, parameters_json: str) -> str:
    return hashlib.sha256((experiment_id + '\n' + parameters_json).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class RunOverview:
    run_id: str
    experiment_id: str
    experiment_title: str
    experiment_kind: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    error_code: str | None
    has_logs: bool
    has_result: bool
    result_version: int


@dataclass(frozen=True)
class RunRecord(RunOverview):
    request_id: str
    experiment_description: str
    parameters_json: str
    evaluation_json: str | None = None

    @property
    def evaluation(self):
        return json.loads(self.evaluation_json) if self.evaluation_json is not None else None

    @property
    def parameters(self) -> dict:
        # 每次返回新对象，调用方无法修改本对象内的原始快照字符串。
        return json.loads(self.parameters_json)


@dataclass(frozen=True)
class RunPage:
    items: tuple[RunOverview, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class RunCreation:
    run: RunRecord
    created: bool
