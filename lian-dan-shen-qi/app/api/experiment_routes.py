"""实验定义路由：复用真实登录身份，只访问本人的实验记录。"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_experiment_service
from app.api.experiment_schemas import (CreateExperimentRequest, ExperimentListItem,
                                        ExperimentListResponse, ExperimentResponse)
from app.api.schemas import UserResponse
from app.errors import ExperimentNotFoundError, ExperimentStorageError, InvalidTokenError
from app.logging_config import log_failure
from app.services.experiment_service import ExperimentService


router = APIRouter(prefix='/experiments', tags=['实验管理'])
logger = logging.getLogger(__name__)
COMMON_RESPONSES = {400: {'description': '请求参数不符合要求。'},
                    401: {'description': '登录已失效，请重新登录。'},
                    404: {'description': '实验不存在或不属于当前用户。'},
                    503: {'description': '请稍后重试。'}}


def _raise_http(error, operation):
    is_service_failure = isinstance(error, (MySQLError, ExperimentStorageError))
    log_failure(logger, error, operation=operation,
                event='database_failure' if is_service_failure else 'operation_rejected',
                level=logging.ERROR if is_service_failure else logging.WARNING)
    if isinstance(error, ExperimentNotFoundError):
        raise HTTPException(404, '实验不存在或不属于当前用户。') from error
    if isinstance(error, InvalidTokenError):
        raise HTTPException(401, '登录已失效，请重新登录。', headers={'WWW-Authenticate': 'Bearer'}) from error
    if is_service_failure:
        raise HTTPException(503, '请稍后重试。') from error
    raise HTTPException(400, '请求参数不符合要求。') from error


def _no_cache(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'


@router.post('', response_model=ExperimentResponse, status_code=201,
             summary='创建实验记录', responses=COMMON_RESPONSES)
async def create_experiment(request: CreateExperimentRequest, response: Response,
                            current_user: UserResponse = Depends(require_current_user),
                            service: ExperimentService = Depends(get_experiment_service)):
    try:
        options = {} if request.evaluation is None else {'evaluation': request.evaluation.model_dump(mode='json')}
        result = await service.create_experiment(current_user.user_id, request.title, request.kind, request.description, **options)
    except (ValueError, MySQLError, ExperimentStorageError) as error:
        _raise_http(error, 'experiment_routes.create')
    _no_cache(response)
    response.headers['Location'] = '/experiments/' + result.experiment_id
    return ExperimentResponse.model_validate(result)


@router.get('', response_model=ExperimentListResponse, summary='分页查看自己的实验', responses=COMMON_RESPONSES)
async def list_experiments(response: Response,
                           limit: Annotated[int, Query(ge=1, le=100)] = 20,
                           before_id: UUID | None = None,
                           current_user: UserResponse = Depends(require_current_user),
                           service: ExperimentService = Depends(get_experiment_service)):
    try:
        result = await service.list_experiments(current_user.user_id, limit=limit,
                                               before_id=before_id.hex if before_id else None)
    except (ValueError, MySQLError, ExperimentStorageError) as error:
        _raise_http(error, 'experiment_routes.list')
    _no_cache(response)
    return ExperimentListResponse(items=[ExperimentListItem.model_validate(item) for item in result.items],
                                  next_cursor=result.next_cursor)


@router.get('/{experiment_id}', response_model=ExperimentResponse,
            summary='查看自己的实验详情', responses=COMMON_RESPONSES)
async def get_experiment(experiment_id: UUID, response: Response,
                         current_user: UserResponse = Depends(require_current_user),
                         service: ExperimentService = Depends(get_experiment_service)):
    try:
        result = await service.get_experiment(current_user.user_id, experiment_id.hex)
    except (ValueError, MySQLError, ExperimentStorageError) as error:
        _raise_http(error, 'experiment_routes.get')
    _no_cache(response)
    return ExperimentResponse.model_validate(result)
