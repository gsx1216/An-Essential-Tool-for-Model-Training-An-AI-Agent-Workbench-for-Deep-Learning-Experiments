"""修改后训练的冻结绑定和按相同评测声明进行的数值对比。"""
import json
import re
from datetime import datetime
from dataclasses import dataclass
from uuid import UUID
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.experiment_run import canonical_parameters
from app.models.runner import script_identifier
from app.models.evaluation import EvaluationSpec
from app.models.experiment_run import RunStatus
from app.models.training_task import TrainingConflictError, TrainingInstructionRequired, require_start_instruction


class RevisionConflict(TrainingConflictError):
    pass


@dataclass(frozen=True)
class RevisionSettings:
    scripts_json: str = '{}'

    def __post_init__(self):
        value = json.loads(self.scripts_json)
        if type(value) is not dict or len(value) > 100:
            raise ValueError('修改后训练入口登记不正确。')
        for name, mode in value.items():
            script_identifier(name)
            if mode != 'cwd':
                raise ValueError('修改后入口仅支持通用 cwd 模式。')

    def mode(self, script_id):
        mode = json.loads(self.scripts_json).get(script_id)
        if mode is None:
            raise RevisionConflict('部署者尚未确认该入口支持修改副本训练。')
        return mode


class PrepareRevision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID = Field(description="新操作生成新 UUID；同一次准备操作重试复用该编号。")
    parameters: dict = Field(default_factory=dict, description='覆盖原运行参数；仅接收已登记参数。空对象保持原参数。')

    @field_validator('parameters')
    @classmethod
    def validate_parameters(cls, value):
        return json.loads(canonical_parameters(value))


def require_revision_start(text, change_id, run_id):
    # 不接受模型自报授权、工具结果或摘要；调用方必须传当前落库的人类原文。
    compact = text.replace('-', '').lower()
    if not (change_id and change_id in compact or run_id and run_id in compact):
        raise TrainingInstructionRequired('请明确指定修改 ID 或新运行 ID 后说启动训练。')
    identifier=change_id if change_id and change_id in compact else run_id
    require_start_instruction(text, identifier, identifier, same_turn=True)


def revision_mode(text):
    return bool(re.search(r'修改|副本|改后|改动|对比|比较|revision|candidate|compare', text, re.I))


def compare_reports(original, candidate, original_run, candidate_run):
    """只比较当前、完整、成功运行的同声明报告；缺项不补零，不比较不同 split。"""
    reasons = []
    reports = (original, candidate)
    runs = (original_run, candidate_run)
    for label, report, run in zip(('original', 'candidate'), reports, runs):
        if report is None:
            reasons.append(label + ':REPORT_NOT_READY')
            continue
        data = report['data']
        if report['is_stale']: reasons.append(label + ':STALE_REPORT')
        if data.get('schema_version') != 2: reasons.append(label + ':LEGACY_REPORT')
        if run.status != 'succeeded' or data.get('status') != run.status:
            reasons.append(label + ':RUN_NOT_SUCCEEDED')
        if not data.get('collection_complete'): reasons.append(label + ':COLLECTION_INCOMPLETE')
        if data.get('warnings'): reasons.append(label + ':REPORT_WARNINGS')
        if data.get('evaluation') != run.evaluation: reasons.append(label + ':EVALUATION_CHANGED')
    spec = original_run.evaluation
    if spec is None or spec != candidate_run.evaluation:
        reasons.append('EVALUATION_MISMATCH_OR_UNSPECIFIED')
    result = {'comparable': not reasons, 'reasons': reasons, 'series': [],
        'original_report_version': original['version'] if original else None,
        'candidate_report_version': candidate['version'] if candidate else None,
        'parameter_changes': {key: {'original': original_run.parameters.get(key), 'candidate': candidate_run.parameters.get(key)}
            for key in sorted(set(original_run.parameters) | set(candidate_run.parameters))
            if original_run.parameters.get(key) != candidate_run.parameters.get(key)},
        'notice': '仅比较已保存报告的观测数值；评测声明一致不证明实际评测实现或数据文件内容一致，单次变化不证明修改产生因果改善。'}
    if reasons: return result
    definitions = {row['name']: row for row in spec['metrics']}
    datasets = {row['id']: row for row in spec['datasets']}
    def keyed(report):
        return {(row['dataset'], row['split'], row['metric_name']): row for row in report['data']['series']}
    before, after = keyed(original), keyed(candidate)
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        dataset, split, name = key
        definition = definitions.get(name)
        reason = None
        if a is None or b is None: reason = 'SERIES_MISSING'
        elif dataset not in datasets or definition is None: reason = 'UNDECLARED_SERIES'
        elif a.get('quality_flags') or b.get('quality_flags'): reason = 'SERIES_QUALITY'
        elif a['last_step'] != b['last_step']: reason = 'STEP_MISMATCH'
        row = {'dataset': dataset, 'split': split, 'metric_name': name, 'comparable': reason is None,
            'reason': reason, 'direction': definition['direction'] if definition else None,
            'original_last': a['last_value'] if a else None, 'candidate_last': b['last_value'] if b else None,
            'original_step': a['last_step'] if a else None, 'candidate_step': b['last_step'] if b else None,
            'delta': None, 'outcome': 'not_compared'}
        if reason is None:
            row['delta'] = b['last_value'] - a['last_value']
            direction = definition['direction']
            row['outcome'] = ('equal' if row['delta'] == 0 else 'direction_unspecified' if direction == 'none'
                else 'improved' if (row['delta'] < 0) == (direction == 'minimize') else 'worsened')
        result['series'].append(row)
    result['comparable'] = bool(result['series']) and all(row['comparable'] for row in result['series'])
    if not result['series']: result['reasons'].append('NO_SERIES')
    return result


class RevisionResponse(BaseModel):
    run_id:str
    original_run_id:str
    change_id:str
    status:RunStatus
    parameters:dict
    evaluation:EvaluationSpec | None
    server_id:str
    script_id:str
    mode:str = Field(description='新运行使用通用 cwd 模式；历史记录保留原始模式值。')
    candidate_directory:str
    source_digest:str
    source_file_count:int
    check_status:Literal['passed','incomplete']
    created_at:datetime
    notice:str


class MetricComparison(BaseModel):
    dataset:str
    split:Literal['train','validation','test']
    metric_name:str
    comparable:bool
    reason:str | None
    direction:Literal['minimize','maximize','none'] | None
    original_last:float | None
    candidate_last:float | None
    original_step:int | None
    candidate_step:int | None
    delta:float | None
    outcome:Literal['not_compared','equal','direction_unspecified','improved','worsened']


class ComparisonResponse(BaseModel):
    original_run_id:str
    run_id:str
    change_id:str
    comparable:bool
    reasons:list[str]
    series:list[MetricComparison]
    original_report_version:int | None
    candidate_report_version:int | None
    parameter_changes:dict
    notice:str
