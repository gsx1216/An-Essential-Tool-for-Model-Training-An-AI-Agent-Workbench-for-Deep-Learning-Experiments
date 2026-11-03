"""监控读取/恢复接口；不允许客户端修改观测事实或重新启动训练。"""
import logging
from uuid import UUID

from fastapi import APIRouter,Depends,HTTPException,Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_monitor_service
from app.api.schemas import UserResponse
from app.errors import RunNotFoundError,RunnerNotStartedError,RunnerProtocolError
from app.logging_config import log_failure
from app.models.monitor import MonitorBusyError,MonitorNotFoundError,MonitorResponse
from app.services.monitor_service import MonitorService

router=APIRouter(tags=['后台训练监控'])
logger=logging.getLogger(__name__)
RESPONSES={401:{'description':'登录无效。'},404:{'description':'本人监控/执行记录不存在。'},
    409:{'description':'仍有有效租约的 Worker 正在处理。'},422:{'description':'UUID 或参数格式错误。'},503:{'description':'请稍后重试。'}}


def fail(error):
    if isinstance(error,(RunNotFoundError,RunnerNotStartedError,MonitorNotFoundError)):status,text=404,'监控任务或运行不存在或不可访问。'
    elif isinstance(error,MonitorBusyError):status,text=409,'监控任务正在处理，请稍后重试。'
    else:status,text=503,'请稍后重试。'
    log_failure(logger,error,operation='monitor_routes',level=logging.ERROR if status>=500 else logging.WARNING)
    raise HTTPException(status,text) from error


@router.get('/runs/{run_id}/monitor',response_model=MonitorResponse,responses=RESPONSES,
    summary='读取本人后台监控状态；不连接 SSH')
async def get_monitor(run_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),service:MonitorService=Depends(get_monitor_service)):
    try:result=await service.get(user.user_id,run_id.hex)
    except (MySQLError,MonitorNotFoundError,RunnerProtocolError) as error:fail(error)
    response.headers['Cache-Control']='no-store'
    return result


@router.post('/runs/{run_id}/monitor/retry',response_model=MonitorResponse,status_code=202,responses=RESPONSES,
    summary='重新排队本人运行的监控采集；不会重新训练')
async def retry_monitor(run_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),service:MonitorService=Depends(get_monitor_service)):
    try:result=await service.retry(user.user_id,run_id.hex)
    except (MySQLError,MonitorNotFoundError,MonitorBusyError,RunNotFoundError,RunnerNotStartedError,RunnerProtocolError) as error:fail(error)
    response.headers['Cache-Control']='no-store'
    return result
