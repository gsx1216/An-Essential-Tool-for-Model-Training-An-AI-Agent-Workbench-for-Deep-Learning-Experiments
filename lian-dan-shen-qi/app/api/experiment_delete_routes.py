"""永久删除本人实验/运行；远端服务器文件不在删除范围内。"""
import asyncio
from functools import lru_cache
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from mysql.connector import Error as MySQLError
from redis.exceptions import RedisError

from app.api.auth_dependencies import require_current_user
from app.api.schemas import UserResponse
from app.config import get_mysql_password, get_event_bus_settings
from app.errors import ExperimentNotFoundError, RunNotFoundError
from app.logging_config import log_failure
from app.repositories.experiment_purge_repository import ExperimentPurgeRepository, PurgeConflictError
from app.services.redis_event_bus import RedisEventBus

router = APIRouter(tags=['实验与运行永久删除'])
logger = logging.getLogger(__name__)
RESPONSES = {401:{'description':'请先登录'},404:{'description':'记录不存在或不可访问'},
             409:{'description':'训练、修改或事件仍在处理，或存在派生运行'},503:{'description':'请稍后重试'}}


@lru_cache(maxsize=1)
def get_purge_repository():
    try:return ExperimentPurgeRepository(get_mysql_password())
    except (ValueError,OSError) as error:
        log_failure(logger,error,operation='experiment_delete.dependencies')
        raise HTTPException(503,'请稍后重试。') from error


async def clear_events(message_ids):
    if not message_ids:return
    bus=None
    try:
        settings=get_event_bus_settings()
        bus=RedisEventBus(settings)
        await bus.client.xdel(settings.stream,*message_ids)
        await bus.client.xack(settings.stream,settings.analysis_group,*message_ids)
    except (RedisError,ValueError) as error:
        # 数据库是事实来源；残留的消息只包含事件 ID，消费者查询不到报告就不会生成任务。
        log_failure(logger,error,operation='experiment_delete.redis_cleanup',level=logging.WARNING)
    finally:
        if bus is not None:
            try:await bus.aclose()
            except RedisError as error:log_failure(logger,error,operation='experiment_delete.redis_close',level=logging.WARNING)


async def remove(repository,user_id,identifier,*,experiment=False):
    try:
        ids=await asyncio.to_thread(repository.delete,user_id,identifier,experiment=experiment)
    except (ExperimentNotFoundError,RunNotFoundError) as error:
        raise HTTPException(404,'记录不存在或不可访问。') from error
    except PurgeConflictError as error:
        raise HTTPException(409,str(error)) from error
    except (MySQLError,ValueError) as error:
        log_failure(logger,error,operation='experiment_delete.database')
        raise HTTPException(503,'请稍后重试。') from error
    await clear_events(ids)
    return Response(status_code=204,headers={'Cache-Control':'no-store'})


@router.delete('/experiments/{experiment_id}',status_code=204,response_class=Response,responses=RESPONSES,
               summary='永久删除本人实验、全部运行和所属训练配置及关联数据；保留服务器文件')
async def delete_experiment(experiment_id:UUID,user:UserResponse=Depends(require_current_user),repository=Depends(get_purge_repository)):
    return await remove(repository,user.user_id,experiment_id.hex,experiment=True)


@router.delete('/runs/{run_id}',status_code=204,response_class=Response,responses=RESPONSES,
               summary='永久删除本人单条运行及指标、报告、分析和关联数据；保留服务器文件')
async def delete_run(run_id:UUID,user:UserResponse=Depends(require_current_user),repository=Depends(get_purge_repository)):
    return await remove(repository,user.user_id,run_id.hex)
