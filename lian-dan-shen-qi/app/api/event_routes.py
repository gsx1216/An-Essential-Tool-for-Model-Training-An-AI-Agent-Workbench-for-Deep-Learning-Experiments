"""本人事件与待分析任务的只读状态；重投递不会启动训练或直接调用模型。"""
import asyncio
from functools import lru_cache
import logging
from uuid import UUID

from fastapi import APIRouter,Depends,HTTPException,Query,Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import require_current_user
from app.api.schemas import UserResponse
from app.config import get_mysql_password
from app.errors import RunNotFoundError
from app.logging_config import log_failure
from app.models.event_bus import EventResponse,AgentTaskResponse,EventRetryResponse,EventBusyError,EventNotFoundError
from app.repositories.event_repository import EventRepository

router=APIRouter(tags=['Agent事件通信'])
logger=logging.getLogger(__name__)
ERRORS={401:{'description':'登录失效，请重新登录。'},404:{'description':'事件或运行不存在或不可访问。'},
        409:{'description':'事件仍有有效投递租约，请稍后重试。'},503:{'description':'请稍后重试。'}}


@lru_cache(maxsize=1)
def get_event_repository():
    try:return EventRepository(get_mysql_password())
    except (ValueError,OSError) as error:
        log_failure(logger,error,operation='event_routes.dependencies')
        raise HTTPException(503,'请稍后重试。') from error


def fail(error):
    if isinstance(error,(RunNotFoundError,EventNotFoundError)):status,text=404,'事件或运行不存在或不可访问。'
    elif isinstance(error,EventBusyError):status,text=409,'事件正在投递，请稍后重试。'
    else:status,text=503,'请稍后重试。'
    log_failure(logger,error,operation='event_routes',level=logging.ERROR if status==503 else logging.WARNING)
    raise HTTPException(status,text) from error


@router.get('/runs/{run_id}/events',response_model=list[EventResponse],summary='查看本人运行的报告事件与投递状态',responses=ERRORS)
async def events(run_id:UUID,response:Response,limit:int=Query(20,ge=1,le=50),offset:int=Query(0,ge=0,le=10000),
                 user:UserResponse=Depends(require_current_user),repository:EventRepository=Depends(get_event_repository)):
    try:rows=await asyncio.to_thread(repository.list,user.user_id,run_id.hex,limit=limit,offset=offset)
    except (RunNotFoundError,MySQLError,ValueError) as error:fail(error)
    response.headers['Cache-Control']='no-store'
    return rows


@router.get('/runs/{run_id}/agent-tasks',response_model=list[AgentTaskResponse],summary='查看本人运行的持久分析任务；queued不代表已分析',responses=ERRORS)
async def tasks(run_id:UUID,response:Response,limit:int=Query(20,ge=1,le=50),offset:int=Query(0,ge=0,le=10000),
                user:UserResponse=Depends(require_current_user),repository:EventRepository=Depends(get_event_repository)):
    try:rows=await asyncio.to_thread(repository.list,user.user_id,run_id.hex,tasks=True,limit=limit,offset=offset)
    except (RunNotFoundError,MySQLError,ValueError) as error:fail(error)
    response.headers['Cache-Control']='no-store'
    return rows


@router.post('/runs/{run_id}/events/{event_id}/retry',response_model=EventRetryResponse,status_code=202,summary='重新排队本人报告事件；不重新训练或直接调用模型',responses=ERRORS)
async def retry(run_id:UUID,event_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),
                repository:EventRepository=Depends(get_event_repository)):
    try:await asyncio.to_thread(repository.retry,user.user_id,run_id.hex,event_id.hex)
    except (RunNotFoundError,EventNotFoundError,EventBusyError,MySQLError) as error:fail(error)
    response.headers['Cache-Control']='no-store'
    return {'event_id':str(event_id),'status':'pending'}
