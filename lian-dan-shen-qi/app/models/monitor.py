"""后台监控配置、公开状态与安全错误码；与训练运行状态分开。"""
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class MonitorNotFoundError(ValueError):
    pass


class MonitorBusyError(ValueError):
    pass


class MonitorLeaseLostError(RuntimeError):
    pass


ERROR_CODES={None,'SOURCE_UNAVAILABLE','SOURCE_CHANGED','CONNECTION_ERROR',
    'TIMEOUT','SYNC_CONFLICT','AUTHENTICATION_FAILED','HOST_KEY_MISMATCH','ADDRESS_NOT_ALLOWED',
    'CREDENTIALS_UNAVAILABLE','CONFIGURATION_CHANGED','PROTOCOL_ERROR','STORAGE_ERROR','INTERNAL_ERROR',
    'WORKER_CANCELLED','LOG_TRUNCATED','OUTPUT_PUBLICATION_FAILED'}


@dataclass(frozen=True)
class MonitorSettings:
    poll_seconds:int=15
    idle_seconds:int=2
    lease_seconds:int=120
    step_timeout_seconds:int=90
    max_pages:int=4
    max_failures:int=8
    backoff_seconds:int=5
    max_backoff_seconds:int=300

    def __post_init__(self):
        limits={'poll_seconds':(1,300),'idle_seconds':(1,60),'lease_seconds':(30,600),
            'step_timeout_seconds':(5,540),'max_pages':(1,8),'max_failures':(1,50),
            'backoff_seconds':(1,300),'max_backoff_seconds':(1,3600)}
        for name,(lower,upper) in limits.items():
            value=getattr(self,name)
            if type(value) is not int or not lower<=value<=upper:raise ValueError('监控配置超出允许范围。')
        if self.step_timeout_seconds+10>self.lease_seconds:
            raise ValueError('监控租约必须比单步超时至少多 10 秒。')
        if self.max_backoff_seconds<self.backoff_seconds:raise ValueError('监控退避范围不正确。')

    def backoff(self,failures):
        if type(failures) is not int or not 1<=failures<=50:raise ValueError('失败次数不符合要求。')
        return min(self.max_backoff_seconds,self.backoff_seconds*2**min(failures-1,12))


class MonitorResponse(BaseModel):
    run_id:str
    status:Literal['scheduled','running','completed','attention']
    attempts:int
    failure_count:int
    next_poll_at:datetime
    last_attempt_at:datetime|None
    last_success_at:datetime|None
    last_error_code:str|None
    report_version:int|None
    lease_active:bool
    updated_at:datetime
    completed_at:datetime|None
