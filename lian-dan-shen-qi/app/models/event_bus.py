"""部署级事件配置与公开 DTO；事件只引用数据库中的不可变报告，不携带凭据。"""
from dataclasses import dataclass, field, replace
from datetime import datetime
import re
from typing import Literal
from urllib.parse import urlsplit,urlunsplit,quote,unquote
from uuid import UUID

from pydantic import BaseModel


class EventNotFoundError(ValueError):
    pass


class EventBusyError(ValueError):
    pass


class EventPayloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class EventBusSettings:
    redis_url: str = field(default='redis://127.0.0.1:6379/0', repr=False)
    prefix: str = 'experiment_assistant'
    block_ms: int = 2000
    reclaim_idle_ms: int = 60000
    lease_seconds: int = 30
    max_failures: int = 8
    reconcile_seconds: int = 60

    def __post_init__(self):
        try:
            parsed=urlsplit(self.redis_url)
            valid=parsed.scheme in {'redis','rediss'} and parsed.hostname and parsed.port != 0
        except (ValueError,TypeError):valid=False
        if not valid:raise ValueError('Redis 连接配置不符合要求。')
        if not isinstance(self.prefix,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}',self.prefix):
            raise ValueError('Redis 命名空间格式不正确。')
        for name,low,high in [('block_ms',1,5000),('reclaim_idle_ms',1000,600000),('lease_seconds',10,300),
                              ('max_failures',1,50),('reconcile_seconds',10,3600)]:
            if type(getattr(self,name)) is not int or not low<=getattr(self,name)<=high:
                raise ValueError('事件通信配置超出范围。')

    @property
    def stream(self):return self.prefix+':agent-events'

    @property
    def dead_stream(self):return self.prefix+':agent-events-dead'

    @property
    def analysis_group(self):return self.prefix+':analysis-dispatch'


class EventResponse(BaseModel):
    event_id:UUID
    run_id:UUID
    event_type:Literal['training.report_ready']
    report_version:int
    status:Literal['pending','sending','sent','received','attention']
    attempts:int
    failure_count:int
    last_error_code:str|None
    next_attempt_at:datetime
    sent_at:datetime|None
    lease_active:bool
    created_at:datetime


def with_redis_password(settings,password):
    """本机交互式密码只供本次进程使用，不保存到文件、参数或数据库。"""
    if type(password) is not str or not 1<=len(password)<=4096 or '\x00' in password:
        raise ValueError('Redis 密码为空或格式不正确。')
    parsed=urlsplit(settings.redis_url)
    host=parsed.hostname
    if ':' in host:host='['+host+']'
    if parsed.port is not None:host+=':'+str(parsed.port)
    username=quote(unquote(parsed.username),safe='') if parsed.username else ''
    netloc=username+':'+quote(password,safe='')+'@'+host
    return replace(settings,redis_url=urlunsplit((parsed.scheme,netloc,parsed.path,parsed.query,parsed.fragment)))


class AgentTaskResponse(BaseModel):
    task_id:UUID
    event_id:UUID
    run_id:UUID
    agent_name:Literal['analysis','suggestion']
    report_version:int
    status:Literal['queued','running','succeeded','failed','attention']
    lease_active:bool=False
    last_error_code:str|None=None
    created_at:datetime


class EventRetryResponse(BaseModel):
    event_id:UUID
    status:Literal['pending']='pending'

