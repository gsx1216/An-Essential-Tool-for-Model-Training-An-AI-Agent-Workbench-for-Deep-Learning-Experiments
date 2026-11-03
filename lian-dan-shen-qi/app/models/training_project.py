"""用户项目的 Python 启动配置；参数作为 argv 传递，不解释 shell。"""
import re
import json
from string import Formatter
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.runner.source_reader import relative_path
from app.models.evaluation import Identifier, MetricName, lowercase_identifier

BUILTINS = {'source_root', 'input_root', 'artifact_dir', 'config_file'}
IDENTIFIER = r'^[a-z][a-z0-9_]{0,63}$'
PROJECT_TASK_EXAMPLE = {
    'name':'我的训练项目',
    'experiment_id':'00000000-0000-4000-8000-000000000001',
    'server_id':'00000000-0000-4000-8000-000000000002',
    'project':{
        'entrypoint':'train.py', 'python':'python3',
        'arguments':['--epochs','{epochs}','--lr','{learning_rate}','--output','{artifact_dir}'],
        'parameter_rules':{
            'epochs':{'type':'int','required':True,'min':1,'max':200},
            'learning_rate':{'type':'float','required':True,'min':0.000001,'max':1.0}},
        'cuda_visible_devices':None, 'metric_rules':[], 'exclude_directories':['datasets'],
        'max_runtime_seconds':86400,
    },
    'default_parameters':{'epochs':20,'learning_rate':0.001},
}


class MetricRule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pattern: Annotated[str, Field(strict=True, min_length=1, max_length=512)]
    dataset: Identifier
    split: str = 'train'
    metrics: Annotated[list[MetricName], Field(min_length=1, max_length=16)]

    @field_validator('split', mode='before')
    @classmethod
    def normalize_split(cls, value):
        return lowercase_identifier(value)

    @model_validator(mode='after')
    def groups(self):
        if self.split not in {'train', 'validation', 'test'}:
            raise ValueError('指标 split 只能是 train、validation 或 test。')
        try:
            original_groups = re.compile(self.pattern, re.IGNORECASE).groupindex
            groups = {name.lower() for name in original_groups}
        except re.error as error:
            raise ValueError('指标正则表达式不正确。') from error
        if len(groups) != len(original_groups):
            raise ValueError('正则命名组忽略大小写后不能重名。')
        if not ({'step', 'epoch'} & groups):
            raise ValueError('指标正则需要命名组 step 或 epoch。')
        if (len(set(self.metrics)) != len(self.metrics)
                or any(not re.fullmatch(IDENTIFIER, name) or name in {'type','step','epoch','dataset','split'}
                       or name not in groups for name in self.metrics)):
            raise ValueError('每个指标必须对应一个有效的正则命名组。')
        return self


class MetricParserRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rules: Annotated[list[MetricRule], Field(max_length=8)] = Field(default_factory=list)


class ProjectLaunch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entrypoint: str = Field(description='服务器已配置 work_dir 内的相对 Python 文件，例如 tools/train.py。')
    main_network: str | None = Field(default=None, description='主网络 Python 文件，相对于项目工作目录；用于代码分析，不作为训练入口。')
    python: str = Field(default='python3', description='远端项目使用的 Python 程序或绝对路径。')
    arguments: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list)
    parameter_rules: dict = Field(default_factory=dict)
    cuda_visible_devices: str | None = Field(default=None, description='如 0、1,2；空字符串隐藏 GPU；null 继承服务器环境。')
    metric_rules: Annotated[list[MetricRule], Field(max_length=8)] = Field(default_factory=list)
    exclude_directories: Annotated[list[str], Field(max_length=16)] = Field(
        default_factory=list, description='准备修改副本时跳过的数据/产物目录，相对项目根目录。')
    max_runtime_seconds: Annotated[int, Field(strict=True, ge=1, le=604800)] = 86400
    max_log_bytes: Annotated[int, Field(strict=True, ge=1, le=10485760)] = 10485760

    @field_validator('entrypoint')
    @classmethod
    def entry(cls, value):
        value = relative_path(value)
        if not value.endswith('.py'):
            raise ValueError('当前通用入口支持 Python 文件。')
        return value

    @field_validator('main_network')
    @classmethod
    def main_network_file(cls,value):
        from app.models.source_code import MainNetworkRequest
        return MainNetworkRequest(path=value).path if value is not None else None

    @field_validator('python')
    @classmethod
    def interpreter(cls, value):
        if not re.fullmatch(r'(?:/[A-Za-z0-9_.-]+)*/?[A-Za-z0-9_.-]+', value):
            raise ValueError('Python 必须是程序名或绝对路径。')
        return value

    @field_validator('cuda_visible_devices')
    @classmethod
    def devices(cls, value):
        if value is not None and not re.fullmatch(r'(?:[0-9]{1,3}(?:,[0-9]{1,3})*)?', value):
            raise ValueError('GPU 配置应为设备编号列表或空字符串。')
        return value

    @field_validator('exclude_directories')
    @classmethod
    def exclusions(cls, value):
        from app.models.code_change import ProjectConfiguration
        return ProjectConfiguration(exclude_directories=value).exclude_directories

    @model_validator(mode='after')
    def arguments_and_rules(self):
        from app.models.runner import RunnerSettings
        if set(self.parameter_rules) & BUILTINS:
            raise ValueError('参数名不能覆盖系统路径占位符。')
        # 复用已支持的 int/float/str/bool、必填和范围规则。
        RunnerSettings(json.dumps({'check': {
            'path': self.entrypoint, 'sha256': '0'*64, 'parameters': self.parameter_rules,
            'max_runtime_seconds': self.max_runtime_seconds, 'max_log_bytes': self.max_log_bytes}}))
        allowed = BUILTINS | set(self.parameter_rules)
        for argument in self.arguments:
            if len(argument) > 4096 or any(ord(c)<32 for c in argument):
                raise ValueError('启动参数包含控制字符或过长。')
            try:
                for _, name, format_spec, conversion in Formatter().parse(argument):
                    if name is not None and (name not in allowed or format_spec or conversion):
                        raise ValueError('启动参数使用了未知或复杂占位符。')
            except ValueError as error:
                raise ValueError('启动参数占位符不正确。') from error
        return self

    def spec(self, sha256):
        project=self.model_dump(exclude={'entrypoint','parameter_rules','max_runtime_seconds','max_log_bytes'})
        if project['main_network'] is None:project.pop('main_network')
        return {
            'path': self.entrypoint, 'sha256': sha256, 'parameters': self.parameter_rules,
            'max_runtime_seconds': self.max_runtime_seconds, 'max_log_bytes': self.max_log_bytes,
            'project': project,
        }


def validate_project_spec(spec):
    if type(spec['project']) is not dict:
        raise ValueError('项目启动配置必须是对象。')
    project = ProjectLaunch(
        **spec['project'], entrypoint=spec['path'], parameter_rules=spec['parameters'],
        max_runtime_seconds=spec['max_runtime_seconds'], max_log_bytes=spec['max_log_bytes'])
    if project.spec(spec['sha256']) != spec:
        raise ValueError('用户项目启动快照不完整。')
    return project


def validate_arguments(project, parameters):
    values = {key: '/resolved-by-runner' for key in BUILTINS} | parameters
    try:
        for argument in project.arguments:
            argument.format_map(values)
    except KeyError as error:
        raise ValueError('启动模板引用的运行参数尚未提供。') from error


class ProjectDirectoryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    server_id: UUID
    path: str = '.'
    offset: Annotated[int, Field(strict=True, ge=0, le=4096)] = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 25

    @field_validator('path')
    @classmethod
    def path_value(cls, value):
        return relative_path(value, directory=True)


class ProjectFileRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    server_id: UUID
    path: str
    start_line: Annotated[int, Field(strict=True, ge=1, le=1048577)] = 1
    line_count: Annotated[int, Field(strict=True, ge=1, le=60)] = 30
    expected_sha256: Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')] | None = None

    @field_validator('path')
    @classmethod
    def path_value(cls, value):
        return relative_path(value)


class TrainingServersRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    before_id: UUID | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 5


class ProjectEnvironmentRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    server_id: UUID


class PythonEnvironment(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    name: Annotated[str,Field(min_length=1,max_length=80)]
    python: Annotated[str,Field(min_length=1,max_length=512)]
    kind: Literal['system','conda','venv']

    @field_validator('python')
    @classmethod
    def executable(cls,value):
        if not value.startswith('/'):raise ValueError('环境必须返回绝对 Python 路径。')
        return ProjectLaunch.interpreter(value)


class PythonEnvironments(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    items: Annotated[list[PythonEnvironment],Field(max_length=32)]
    truncated: bool


class ProjectEnvironmentResponse(PythonEnvironments):
    server_id: str
    work_dir: str


class GpuInfo(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    index: Annotated[int,Field(ge=0,le=999)]
    name: Annotated[str,Field(min_length=1,max_length=160)]
    memory_total_mb: Annotated[int,Field(ge=0)]
    memory_used_mb: Annotated[int,Field(ge=0)]
    utilization_percent: Annotated[int,Field(ge=0,le=100)] | None


class GpuResources(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    gpu_status: Literal['available','not_installed','unavailable']
    gpus: Annotated[list[GpuInfo],Field(max_length=32)]


class ProjectResourceResponse(GpuResources):
    server_id: str
    work_dir: str

