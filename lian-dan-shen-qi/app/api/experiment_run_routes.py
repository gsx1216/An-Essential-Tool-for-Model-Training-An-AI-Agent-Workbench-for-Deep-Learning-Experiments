"""运行记录路由；本人权限、幂等创建、分页及最近训练查询。"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_experiment_run_service
from app.api.experiment_run_schemas import (CreateRunRequest, LatestTrainingRunResponse, RunCreationResponse,
                                            RunListItem, RunListResponse, RunResponse)
from app.api.schemas import UserResponse
from app.errors import ExperimentNotFoundError, RunNotFoundError, RunRequestConflictError, RunStorageError
from app.logging_config import log_failure
from app.models.experiment_run import RunStatus
from app.services.experiment_run_service import ExperimentRunService


router = APIRouter(tags=['实验运行记录'])
logger = logging.getLogger(__name__)
RESPONSES = {400: {'description': '请求参数不符合要求。'}, 401: {'description': '登录已失效，请重新登录。'},
             404: {'description': '实验或运行不存在或不属于当前用户。'},
             409: {'description': '同一请求编号已用于其他运行配置。'}, 503: {'description': '请稍后重试。'}}


def _raise_http(error, operation):
    failure = isinstance(error, (MySQLError, RunStorageError))
    log_failure(logger, error, operation=operation, event='database_failure' if failure else 'operation_rejected',
                level=logging.ERROR if failure else logging.WARNING)
    if isinstance(error, ExperimentNotFoundError):
        raise HTTPException(404, '实验不存在或不属于当前用户。') from error
    if isinstance(error, RunNotFoundError):
        raise HTTPException(404, '运行不存在或不属于当前用户。') from error
    if isinstance(error, RunRequestConflictError):
        raise HTTPException(409, '该请求编号已用于其他运行配置，请为新运行生成新的 request_id。') from error
    if failure:
        raise HTTPException(503, '请稍后重试。') from error
    raise HTTPException(400, '请求参数不符合要求。') from error


def _no_cache(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'


@router.post('/experiments/{experiment_id}/runs', response_model=RunCreationResponse, status_code=201,
             summary='创建一次运行记录，不启动训练',
             responses={**RESPONSES, 200: {'description': '同一请求重试，返回已有运行。'}})
async def create_run(experiment_id: UUID, request: CreateRunRequest, response: Response,
                     current_user: UserResponse = Depends(require_current_user),
                     service: ExperimentRunService = Depends(get_experiment_run_service)):
    try:
        result = await service.create_run(current_user.user_id, experiment_id.hex, request.request_id.hex, request.parameters)
    except (ValueError, MySQLError, RunStorageError) as error:
        _raise_http(error, 'experiment_run_routes.create')
    response.status_code = 201 if result.created else 200
    response.headers['Location'] = '/runs/' + result.run.run_id
    _no_cache(response)
    return RunCreationResponse(run=RunResponse.model_validate(result.run), replayed=not result.created)


@router.get('/experiments/{experiment_id}/runs', response_model=RunListResponse,
            summary='分页查看自己的实验运行记录', responses=RESPONSES)
async def list_runs(experiment_id: UUID, response: Response,
                    limit: Annotated[int, Query(ge=1, le=100)] = 20, before_id: UUID | None = None,
                    current_user: UserResponse = Depends(require_current_user),
                    service: ExperimentRunService = Depends(get_experiment_run_service)):
    try:
        result = await service.list_runs(current_user.user_id, experiment_id.hex, limit=limit,
                                         before_id=before_id.hex if before_id else None)
    except (ValueError, MySQLError, RunStorageError) as error:
        _raise_http(error, 'experiment_run_routes.list')
    _no_cache(response)
    return RunListResponse(items=[RunListItem.model_validate(item) for item in result.items], next_cursor=result.next_cursor)


# 静态 latest-training 必须排在 /runs/{run_id} 前，避免被当成 UUID 路径。
@router.get('/runs/latest-training', response_model=LatestTrainingRunResponse,
            summary='查询本人最近提交的训练运行，可按实验或状态筛选', responses=RESPONSES)
async def latest_training_run(response: Response, experiment_id: UUID | None = None, status: RunStatus | None = None,
                               current_user: UserResponse = Depends(require_current_user),
                               service: ExperimentRunService = Depends(get_experiment_run_service)):
    try:
        result = await service.latest_training_run(current_user.user_id,
                                                   experiment_id=experiment_id.hex if experiment_id else None, status=status)
    except (ValueError, MySQLError, RunStorageError) as error:
        _raise_http(error, 'experiment_run_routes.latest_training')
    _no_cache(response)
    return LatestTrainingRunResponse(run=RunResponse.model_validate(result) if result is not None else None)


@router.get('/runs/{run_id}', response_model=RunResponse, summary='查看自己的运行详情', responses=RESPONSES)
async def get_run(run_id: UUID, response: Response,
                   current_user: UserResponse = Depends(require_current_user),
                   service: ExperimentRunService = Depends(get_experiment_run_service)):
    try:
        result = await service.get_run(current_user.user_id, run_id.hex)
    except (ValueError, MySQLError, RunStorageError) as error:
        _raise_http(error, 'experiment_run_routes.get')
    _no_cache(response)
    return RunResponse.model_validate(result)
