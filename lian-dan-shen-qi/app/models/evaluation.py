"""有界评测声明及不可变 JSON 快照；声明是数据，不是命令或模型指令。"""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

def lowercase_identifier(value):
    return value.lower() if isinstance(value, str) else value


Identifier = Annotated[str, Field(strict=True, pattern=r'^[a-z0-9][a-z0-9_.-]{0,63}$'), BeforeValidator(lowercase_identifier)]
MetricName = Annotated[str, Field(strict=True, pattern=r'^[a-z][a-z0-9_]{0,63}$'), BeforeValidator(lowercase_identifier)]
ShortText = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-1e100, le=1e100)]


class Declaration(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class DatasetDefinition(Declaration):
    id: Identifier
    display_name: ShortText
    version: ShortText

    @model_validator(mode='after')
    def reserved(self):
        if self.id == 'unspecified':
            raise ValueError('unspecified 为旧记录和未标注数据集保留。')
        return self


class MetricDefinition(Declaration):
    name: MetricName
    display_name: ShortText
    unit: ShortText
    direction: Literal['minimize', 'maximize', 'none']
    min_value: Number | None = None
    max_value: Number | None = None
    definition: Annotated[str, Field(strict=True, max_length=1000)] = ''

    @model_validator(mode='after')
    def bounds(self):
        if self.name in {'type', 'step', 'epoch', 'split', 'dataset'}:
            raise ValueError('指标名不能使用日志元数据保留字。')
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValueError('指标下界不能高于上界。')
        return self


class EvaluationSpec(Declaration):
    task_type: Literal['point_cloud_classification', 'point_cloud_segmentation', 'point_cloud_completion',
                       'point_cloud_detection', 'object_tracking', 'object_detection',
                       'camouflaged_object_detection', 'custom']
    protocol_id: Identifier
    protocol_version: ShortText
    implementation: Annotated[str, Field(strict=True, max_length=1000)] = ''
    datasets: Annotated[list[DatasetDefinition], Field(min_length=1, max_length=16)]
    metrics: Annotated[list[MetricDefinition], Field(min_length=1, max_length=64)]
    primary_metric: MetricName | None = None

    @model_validator(mode='after')
    def unique_names(self):
        if len({x.id for x in self.datasets}) != len(self.datasets):
            raise ValueError('数据集 ID 不能重复。')
        if len({x.name for x in self.metrics}) != len(self.metrics):
            raise ValueError('指标名不能重复。')
        if self.primary_metric is not None and self.primary_metric not in {x.name for x in self.metrics}:
            raise ValueError('主指标必须在指标定义中。')
        return self


def canonical_evaluation(value):
    if value is None:
        return None
    data = EvaluationSpec.model_validate(value).model_dump(mode='json')
    text = json.dumps(data, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(',', ':'))
    if len(text.encode()) > 32768:
        raise ValueError('评测配置超过 32768 字节。')
    return text


def evaluation_digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def verified_evaluation(row):
    """读取时同时验证内容结构、规范化和摘要；调用方转换为各自的存储异常。"""
    text, digest = row.get('evaluation_json'), row.get('evaluation_digest')
    if text is None:
        if digest is not None:
            raise ValueError('缺少评测配置正文。')
        return None
    if len(text.encode()) > 32768 or canonical_evaluation(json.loads(text)) != text or evaluation_digest(text) != digest:
        raise ValueError('评测快照校验失败。')
    return text


def annotate_series(spec, series):
    """按数据集/分组/指标注释事实；未知定义不猜测，异常值保留但不选最优。"""
    definitions = {x['name']: x for x in spec['metrics']} if spec else {}
    datasets = {x['id']: x for x in spec['datasets']} if spec else {}
    for item in series:
        item.setdefault('dataset', 'unspecified')
        definition = definitions.get(item['metric_name'])
        item['dataset_definition_id'] = item['dataset'] if item['dataset'] in datasets else None
        item['metric_definition_name'] = item['metric_name'] if definition else None
        issues = []
        if item['dataset'] == 'unspecified':
            issues.append('DATASET_UNSPECIFIED')
        elif item['dataset'] not in datasets:
            issues.append('DATASET_UNDECLARED')
        if definition is None:
            issues.append('METRIC_UNDECLARED')
        elif ((definition['min_value'] is not None and item['min_value'] < definition['min_value']) or
              (definition['max_value'] is not None and item['max_value'] > definition['max_value'])):
            issues.append('VALUE_OUT_OF_RANGE')
        item['quality_flags'] = issues
        direction = definition['direction'] if definition else 'none'
        item['observed_best_value'] = (
            item['min_value' if direction == 'minimize' else 'max_value']
            if direction != 'none' and not issues else None
        )
        item['is_primary_metric'] = bool(spec and spec['primary_metric'] == item['metric_name'])
    return series
