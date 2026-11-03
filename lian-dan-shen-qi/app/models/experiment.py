"""实验定义的数据对象与共享校验；不依赖 HTTP 或数据库连接。"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.validators import normalize_required_text


ExperimentKind = Literal['training', 'script']
MAX_DESCRIPTION_LENGTH = 10000


def normalize_experiment_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('实验 ID 必须是 UUID 字符串。')
    try:
        return UUID(value.strip()).hex
    except ValueError:
        raise ValueError('实验 ID 格式不正确，请复制完整的 UUID。') from None


def validate_experiment_fields(title: str, kind: str, description: str) -> tuple[str, str, str]:
    title = normalize_required_text(title, '实验标题', 200)
    if not isinstance(kind, str) or kind not in {'training', 'script'}:
        raise ValueError('实验类型只能是 training 或 script。')
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError('实验说明必须是字符串，且不超过一万字符。')
    try:
        title.encode('utf-8')
        description.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('实验文本包含无法编码的字符。') from None
    return title, kind, description  # 说明原样保留，允许空白和代码缩进。


def validate_experiment_limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('每页实验数量必须是 1～100 的整数。')
    return limit


@dataclass(frozen=True)
class ExperimentOverview:
    experiment_id: str
    title: str
    kind: ExperimentKind
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ExperimentRecord(ExperimentOverview):
    description: str
    evaluation_json: str | None = None

    @property
    def evaluation(self):
        return json.loads(self.evaluation_json) if self.evaluation_json is not None else None


@dataclass(frozen=True)
class ExperimentPage:
    items: tuple[ExperimentOverview, ...]
    next_cursor: str | None
