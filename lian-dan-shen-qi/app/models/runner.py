"""Runner 的部署脚本注册表、不可变启动快照与远端协议校验。"""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import PurePosixPath
import re

from app.models.experiment_run import canonical_parameters


def script_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value):
        raise ValueError('脚本编号格式不正确。')
    return value


def output_directory(value):
    """部署者指定项目内的可见相对目录，不接受任意服务器绝对路径。"""
    if (type(value) is not str or not 1 <= len(value) <= 200
            or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', value)):
        raise ValueError('产物目录必须是项目内的可见相对目录，例如 training_outputs。')
    return value


@dataclass(frozen=True)
class RunnerSettings:
    registry_json: str = '{}'
    python_command: str = 'python3'
    output_directory: str = 'training_outputs'

    def __post_init__(self):
        output_directory(self.output_directory)
        if not isinstance(self.python_command, str) or not re.fullmatch(r'(?:/[A-Za-z0-9_.-]+)*/?[A-Za-z0-9_.-]+', self.python_command):
            raise ValueError('远端 Python 必须是固定程序名或绝对路径。')
        registry = json.loads(self.registry_json)
        if type(registry) is not dict or len(registry) > 100:
            raise ValueError('脚本注册表格式不正确。')
        for name, spec in registry.items():
            script_identifier(name)
            fields = {'path', 'sha256', 'parameters', 'max_runtime_seconds', 'max_log_bytes'}
            if type(spec) is not dict or set(spec) not in (fields, fields | {'project'}):
                raise ValueError('脚本注册字段不完整。')
            path = spec['path']
            if (not isinstance(path, str) or len(path) > 512 or '\\' in path or not path.endswith('.py')
                    or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
                    or any(ord(c) < 32 or ord(c) == 127 for c in path)):
                raise ValueError('脚本必须是工作目录内的相对 Python 路径。')
            if not isinstance(spec['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', spec['sha256']):
                raise ValueError('必须登记脚本 SHA256。')
            for field, maximum in [('max_runtime_seconds', 604800), ('max_log_bytes', 10485760)]:
                if type(spec[field]) is not int or not 1 <= spec[field] <= maximum:
                    raise ValueError('脚本运行或日志预算不符合要求。')
            rules = spec['parameters']
            if type(rules) is not dict or len(rules) > 100:
                raise ValueError('参数注册表格式不正确。')
            for key, rule in rules.items():
                script_identifier(key)
                if type(rule) is not dict or rule.get('type') not in {'int', 'float', 'str', 'bool'}:
                    raise ValueError('参数规则类型不符合要求。')
                allowed = {'type', 'required', 'min', 'max'} if rule['type'] in {'int', 'float'} else {'type', 'required', 'max_length'} if rule['type'] == 'str' else {'type', 'required'}
                if set(rule) - allowed or type(rule.get('required', False)) is not bool:
                    raise ValueError('参数规则字段不符合要求。')
                if rule['type'] in {'int', 'float'}:
                    for bound in ('min', 'max'):
                        if bound in rule and (type(rule[bound]) not in (int, float) or not math.isfinite(rule[bound])):
                            raise ValueError('参数范围必须为有限数值。')
                    if 'min' in rule and 'max' in rule and rule['min'] > rule['max']:
                        raise ValueError('参数范围不正确。')
                if rule['type'] == 'str' and (type(rule.get('max_length', 1024)) is not int or not 1 <= rule.get('max_length', 1024) <= 4096):
                    raise ValueError('参数文本预算不正确。')
            if 'project' in spec:
                from app.models.training_project import validate_project_spec
                validate_project_spec(spec)

    def registry(self):
        return json.loads(self.registry_json)

    def resolve(self, name, parameters):
        from app.errors import RunnerScriptNotAllowedError
        script_identifier(name)
        spec = self.registry().get(name)
        if spec is None:
            raise RunnerScriptNotAllowedError('脚本尚未登记。')
        parameters = json.loads(canonical_parameters(parameters))
        rules = spec['parameters']
        if set(parameters) - set(rules):
            raise ValueError('存在未登记的运行参数。')
        for key, rule in rules.items():
            if key not in parameters:
                if rule.get('required', False):
                    raise ValueError('缺少必填运行参数。')
                continue
            value = parameters[key]
            expected = rule['type']
            valid = (type(value) is int if expected == 'int' else type(value) in (int, float) if expected == 'float'
                     else type(value) is str if expected == 'str' else type(value) is bool)
            if not valid:
                raise ValueError('运行参数类型不符合登记规则。')
            if expected in {'int', 'float'} and (('min' in rule and value < rule['min']) or ('max' in rule and value > rule['max'])):
                raise ValueError('运行参数超出登记范围。')
            if expected == 'str' and len(value) > rule.get('max_length', 1024):
                raise ValueError('运行参数文本过长。')
        if 'project' in spec:
            from app.models.training_project import validate_project_spec, validate_arguments
            validate_arguments(validate_project_spec(spec), parameters)
        return spec, parameters


def launch_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def validate_observation(value):
    from app.errors import RunnerProtocolError
    if type(value) is not dict:
        raise RunnerProtocolError('远端响应格式不正确。')
    status = value.get('status')
    if status not in {'missing', 'queued', 'running', 'succeeded', 'failed', 'cancelled'}:
        raise RunnerProtocolError('远端状态不正确。')
    code = value.get('exit_code')
    if code is not None and (type(code) is not int or not -2147483648 <= code <= 2147483647):
        raise RunnerProtocolError('远端退出码不正确。')
    if status == 'succeeded' and code != 0:
        raise RunnerProtocolError('远端成功状态缺少零退出码。')
    if (type(value.get('started')) is not bool or (status in {'running', 'succeeded'} and not value['started'])
            or type(value.get('stale')) is not bool or type(value.get('log_bytes')) is not int or not 0 <= value['log_bytes'] <= 10485760):
        raise RunnerProtocolError('远端观测字段不正确。')
    if value.get('error_code') not in {None, 'SCRIPT_CHANGED', 'SCRIPT_MISSING', 'PYTHON_MISSING', 'RUNTIME_LIMIT', 'RUNNER_FAILURE', 'REVISION_SOURCE_CHANGED'}:
        raise RunnerProtocolError('远端错误码不正确。')
    if 'outputs' in value:
        outputs = value['outputs']
        if (type(outputs) is not dict or set(outputs) != {'status', 'directory', 'error_code', 'file_count'}
                or outputs['status'] not in {'not_ready', 'published', 'unavailable'}
                or type(outputs['directory']) is not str or not outputs['directory'].startswith('/')
                or len(outputs['directory']) > 2048 or any(ord(c) < 32 for c in outputs['directory'])
                or outputs['error_code'] not in {None, 'OUTPUT_PUBLICATION_FAILED'}
                or type(outputs['file_count']) is not int or not 0 <= outputs['file_count'] <= 256
                or (outputs['status'] == 'unavailable') != (outputs['error_code'] is not None)):
            raise RunnerProtocolError('远端产物信息不符合要求。')
    return {key: value[key] for key in ('status', 'started', 'exit_code', 'error_code', 'stale', 'log_bytes', 'outputs',
            'content', 'next_offset', 'has_more', 'truncated', 'raw_base64', 'log_size', 'anchor_sha256', 'source_changed') if key in value}
