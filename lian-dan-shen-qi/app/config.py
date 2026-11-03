"""后端配置：优先使用服务器环境变量，本地开发可使用 config/.env。"""

import os
import json
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv

from app.errors import ModelCredentialConfigurationError
from app.logging_config import logged_operation
from app.models.context_settings import ContextSettings
from app.models.server_connection import ServerSettings

ENV_FILE = Path(__file__).resolve().parents[1] / "config" / ".env"


def get_model_trust_env() -> bool:
    """模型默认直连；只有明确配置时才使用进程继承的代理设置。"""
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    value=os.getenv('MODEL_TRUST_ENV','false').lower()
    if value not in {'true','false'}:
        raise ValueError('MODEL_TRUST_ENV 必须为 true 或 false。')
    return value=='true'


def get_skill_cache_url():
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    return os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')


def get_analysis_settings():
    from app.models.analysis import AnalysisSettings
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    enabled = os.getenv("ANALYSIS_WORKER_ENABLED", "false").lower()
    if enabled not in {"true", "false"}:
        raise ValueError("后台分析开关应为 true 或 false。")
    values = {"enabled": enabled == "true", "model_name": os.getenv("ANALYSIS_MODEL_NAME", "deepseek-flash")}
    for field in AnalysisSettings.__dataclass_fields__:
        if field in {"enabled", "model_name"}:
            continue
        raw = os.getenv("ANALYSIS_" + field.upper())
        if raw is not None:
            if not raw.isascii() or not raw.isdecimal():
                raise ValueError("后台分析数值配置必须为有界正整数。")
            values[field] = int(raw)
    return AnalysisSettings(**values)


def get_revision_settings():
    from app.models.revision_run import RevisionSettings
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    path=Path(os.getenv('REVISION_SCRIPTS_FILE',str(ENV_FILE.parent/'revision_scripts.json')))
    text='{}'
    if path.exists():
        if path.stat().st_size>16384:raise ValueError('修改后训练登记过大。')
        text=path.read_text(encoding='utf-8')
    elif 'REVISION_SCRIPTS_FILE' in os.environ:raise ValueError('修改后训练登记不存在。')
    return RevisionSettings(text)


def get_code_change_settings():
    from app.models.code_change import CodeChangeSettings
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    path = Path(os.getenv('CODE_CHANGE_CHECKS_FILE', str(ENV_FILE.parent/'code_change_checks.json')))
    text = '{}'
    if path.exists():
        if path.stat().st_size > 65536: raise ValueError('项目检查登记文件过大。')
        text = path.read_text(encoding='utf-8')
    elif 'CODE_CHANGE_CHECKS_FILE' in os.environ:
        raise ValueError('指定项目检查登记文件不存在。')
    raw = os.getenv('CODE_CHANGE_MODEL_TIMEOUT_SECONDS', '60')
    if not raw.isascii() or not raw.isdecimal(): raise ValueError('修改模型超时应为有界整数。')
    return CodeChangeSettings(text, os.getenv('CODE_CHANGE_MODEL_NAME', 'deepseek-flash'), int(raw))


def get_suggestion_settings():
    from app.models.suggestion import SuggestionSettings
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    enabled = os.getenv('SUGGESTION_WORKER_ENABLED', 'false').lower()
    if enabled not in {'true', 'false'}:
        raise ValueError('建议执行开关应为 true 或 false。')
    values = {'enabled': enabled == 'true', 'model_name': os.getenv('SUGGESTION_MODEL_NAME', 'deepseek-flash')}
    for field in SuggestionSettings.__dataclass_fields__:
        if field in {'enabled', 'model_name'}:
            continue
        raw = os.getenv('SUGGESTION_' + field.upper())
        if raw is not None:
            if not raw.isascii() or not raw.isdecimal():
                raise ValueError('建议数值配置必须为有界正整数。')
            values[field] = int(raw)
    return SuggestionSettings(**values)


def get_context_cache_settings():
    from app.models.context_cache import ContextCacheSettings
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    enabled = os.getenv("CHAT_CONTEXT_CACHE_ENABLED", "true").lower()
    if enabled not in {"true", "false"}:
        raise ValueError("上下文缓存开关应为 true 或 false。")
    values = {"enabled": enabled == "true", "redis_url": os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
              "prefix": os.getenv("CHAT_CONTEXT_CACHE_PREFIX", "experiment_assistant")}
    for field, variable in (("ttl_seconds", "CHAT_CONTEXT_CACHE_TTL_SECONDS"),
                            ("max_entry_bytes", "CHAT_CONTEXT_CACHE_MAX_ENTRY_BYTES"),
                            ("failure_backoff_seconds", "CHAT_CONTEXT_CACHE_FAILURE_BACKOFF_SECONDS")):
        raw = os.getenv(variable)
        if raw is not None:
            if not raw.isascii() or not raw.isdecimal():
                raise ValueError("上下文缓存数值配置应为有界正整数。")
            values[field] = int(raw)
    return ContextCacheSettings(**values)


def get_event_bus_settings():
    from app.models.event_bus import EventBusSettings
    load_dotenv(dotenv_path=ENV_FILE,override=False,interpolate=False)
    values={'redis_url':os.getenv('REDIS_URL','redis://127.0.0.1:6379/0'),
            'prefix':os.getenv('AGENT_EVENT_PREFIX','experiment_assistant')}
    for field in ('block_ms','reclaim_idle_ms','lease_seconds','max_failures','reconcile_seconds'):
        raw=os.getenv('AGENT_EVENT_'+field.upper())
        if raw is not None:
            if not raw.isascii() or not raw.isdecimal():raise ValueError('事件通信配置必须为正整数。')
            values[field]=int(raw)
    return EventBusSettings(**values)


def get_monitor_settings():
    from app.models.monitor import MonitorSettings
    load_dotenv(dotenv_path=ENV_FILE,override=False,interpolate=False)
    values={}
    for field in MonitorSettings.__dataclass_fields__:
        raw=os.getenv('MONITOR_'+field.upper())
        if raw is not None:
            if not raw.isascii() or not raw.isdecimal():raise ValueError('监控配置必须为有界正整数。')
            values[field]=int(raw)
    return MonitorSettings(**values)


def get_research_enabled() -> bool:
    """部署级公开资料开关；不会读取某个用户的查询或 Key。"""
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    value=os.getenv('RESEARCH_ENABLED','true').strip().lower()
    if value not in {'true','false'}:raise ValueError('RESEARCH_ENABLED 应为 true 或 false。')
    return value=='true'


def get_runner_settings():
    from app.models.runner import RunnerSettings
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    file = Path(os.getenv('RUNNER_SCRIPTS_FILE', str(ENV_FILE.parent / 'runner_scripts.json')))
    text = '{}'
    if file.exists():
        if file.stat().st_size > 131072:
            raise ValueError('脚本注册表过大。')
        text = file.read_text(encoding='utf-8')
    elif 'RUNNER_SCRIPTS_FILE' in os.environ:
        raise ValueError('指定脚本注册表不存在。')
    return RunnerSettings(text, os.getenv('RUNNER_PYTHON_COMMAND', 'python3'),
                          os.getenv('RUNNER_OUTPUT_DIRECTORY', 'training_outputs'))


def get_server_settings() -> ServerSettings:
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    raw=os.getenv('SSH_ALLOWED_NETWORKS')
    if raw is None:
        path=ENV_FILE.parent/'ssh_allowed_networks.json'
        networks=()
        if path.exists():
            if path.stat().st_size>4096:raise ValueError('SSH 允许网段配置过大。')
            data=json.loads(path.read_text(encoding='utf-8'))
            if type(data) is not list or len(data)>32 or any(type(item) is not str for item in data):
                raise ValueError('SSH 允许网段配置必须为有界的字符串列表。')
            networks=tuple(data)
    else:
        # An explicitly empty environment variable disables SSH; file cannot override it.
        networks=tuple(value.strip() for value in raw.split(',') if value.strip())
    values = {}
    for field, name in [('timeout_seconds', 'SSH_TEST_TIMEOUT_SECONDS'), ('max_connections', 'SSH_MAX_CONNECTIONS')]:
        raw = os.getenv(name)
        if raw is not None:
            if not raw.isascii() or not raw.isdecimal():
                raise ValueError('SSH 部署数值配置格式不正确。')
            values[field] = int(raw)
    return ServerSettings(allowed_networks=networks, **values)


@logged_operation("config.get_context_settings", log_success=False)
def get_context_settings() -> ContextSettings:
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    fields = {
        "max_input_tokens": "CHAT_MAX_INPUT_TOKENS",
        "compact_at_tokens": "CHAT_COMPACT_AT_TOKENS",
        "max_summary_tokens": "CHAT_MAX_SUMMARY_TOKENS",
        "keep_recent_turns": "CHAT_KEEP_RECENT_TURNS",
        "model_request_tokens": "CHAT_MODEL_REQUEST_TOKENS",
        "max_summary_calls": "CHAT_MAX_SUMMARY_CALLS",
        "timeout_seconds": "CHAT_CONTEXT_TIMEOUT_SECONDS",
        "lease_seconds": "CHAT_CONTEXT_LEASE_SECONDS",
    }
    values = {}
    for field, variable in fields.items():
        raw = os.getenv(variable)
        if raw is not None:
            if not raw.isascii() or not raw.isdecimal():
                raise ValueError("上下文部署配置必须为正整数字符串。")
            values[field] = int(raw)
    return ContextSettings(**values)


@logged_operation("config.get_mysql_password", log_success=False, expected_errors=())
def get_mysql_password() -> str:
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    password = os.getenv("MYSQL_PASSWORD")
    if password is None or password == "":
        raise ValueError(
            "后端尚未配置 MYSQL_PASSWORD，请在服务器环境变量或 config/.env 中设置。"
        )
    return password


@logged_operation("config.get_model_encryption_key", log_success=False, expected_errors=())
def get_model_encryption_key() -> str:
    """读取部署者持有的固定主密钥；缺失时拒绝，不自动生成或覆盖配置。"""
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    master_key = os.getenv("MODEL_CREDENTIAL_ENCRYPTION_KEY")
    if not isinstance(master_key, str) or len(master_key) != 44 or master_key != master_key.strip():
        raise ModelCredentialConfigurationError("后端模型凭据加密主密钥缺失或格式不正确。")
    try:
        # 使用库验证密钥编码与长度，校验失败时不暴露原文。
        Fernet(master_key.encode("ascii"))
    except (TypeError, ValueError):
        raise ModelCredentialConfigurationError("后端模型凭据加密主密钥缺失或格式不正确。") from None
    return master_key


def get_deepseek_api_key() -> str:
    # TODO 1：加载 ENV_FILE
    load_dotenv(dotenv_path=ENV_FILE, override=False, interpolate=False)
    # TODO 2：读取环境变量 DEEPSEEK_API_KEY
    api_key = os.getenv("DEESEEK_API_KEY")
    # TODO 3：没有配置时抛出 ValueError
    if api_key is None or api_key == "":
        raise ValueError("尚未配置API")
    # TODO 4：返回读取到的字符串
    return api_key
