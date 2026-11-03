"""本人后台分析开关、分析结果和失败恢复；查询不会构造模型或启动训练。"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_analysis_service
from app.api.schemas import UserResponse
from app.errors import RunNotFoundError
from app.logging_config import log_failure
from app.models.analysis import (AnalysisPreference, AnalysisResponse, AnalysisTaskResponse, AnalysisRetryResponse,
    AnalysisNotFoundError, AnalysisConflictError, AnalysisStorageError, AnalysisProtocolError)
from app.models.event_bus import EventPayloadError

router = APIRouter(tags=["后台训练分析"])
logger = logging.getLogger(__name__)
ERRORS = {401: {"description": "登录失效，请重新登录。"}, 404: {"description": "记录不存在或不可访问。"},
          409: {"description": "任务已完成、仍在执行或本人尚未启用。"}, 503: {"description": "请稍后重试。"}}
KNOWN = (ValueError, MySQLError, AnalysisStorageError, AnalysisProtocolError, EventPayloadError)


async def operate(response, operation, *args, **kwargs):
    try:
        value = await operation(*args, **kwargs)
    except KNOWN as error:
        if isinstance(error, (RunNotFoundError, AnalysisNotFoundError)):
            status, message = 404, "记录不存在或不可访问。"
        elif isinstance(error, AnalysisConflictError):
            status, message = 409, "任务已完成、仍在执行或本人尚未启用，请检查后重试。"
        else:
            status, message = 503, "请稍后重试。"
        log_failure(logger, error, operation="analysis_routes", level=logging.ERROR if status == 503 else logging.WARNING)
        raise HTTPException(status, message) from error
    response.headers["Cache-Control"] = "no-store"
    return value


@router.get("/me/analysis-settings", response_model=AnalysisPreference, summary="查看本人后台模型分析开关；默认关闭", responses=ERRORS)
async def preference(response: Response, user: UserResponse = Depends(require_current_user), service=Depends(get_analysis_service)):
    return await operate(response, service.preference, user.user_id)


@router.put("/me/analysis-settings", response_model=AnalysisPreference,
            summary="设置本人后台分析开关；启用后可用本人 Key 处理包括历史在内的待分析队列", responses=ERRORS)
async def set_preference(request: AnalysisPreference, response: Response,
                         user: UserResponse = Depends(require_current_user), service=Depends(get_analysis_service)):
    return await operate(response, service.preference, user.user_id, enabled=request.enabled)


@router.get("/runs/{run_id}/analyses", response_model=list[AnalysisResponse], summary="查看本人运行已保存分析；不调用模型", responses=ERRORS)
async def results(run_id: UUID, response: Response, limit: int = Query(10, ge=1, le=20), offset: int = Query(0, ge=0, le=10000),
                  user: UserResponse = Depends(require_current_user), service=Depends(get_analysis_service)):
    return await operate(response, service.results, user.user_id, run_id.hex, limit=limit, offset=offset)


@router.get("/runs/{run_id}/analysis-tasks/{task_id}", response_model=AnalysisTaskResponse,
            summary="查看本人分析任务状态、尝试次数与安全错误分类", responses=ERRORS)
async def task(run_id: UUID, task_id: UUID, response: Response,
               user: UserResponse = Depends(require_current_user), service=Depends(get_analysis_service)):
    return await operate(response, service.task, user.user_id, run_id.hex, task_id.hex)


@router.post("/runs/{run_id}/analysis-tasks/{task_id}/retry", response_model=AnalysisRetryResponse, status_code=202,
             summary="重新排队本人未完成分析；不重新训练或覆盖已保存分析", responses=ERRORS)
async def retry(run_id: UUID, task_id: UUID, response: Response,
                user: UserResponse = Depends(require_current_user), service=Depends(get_analysis_service)):
    return await operate(response, service.task, user.user_id, run_id.hex, task_id.hex, retry=True)
