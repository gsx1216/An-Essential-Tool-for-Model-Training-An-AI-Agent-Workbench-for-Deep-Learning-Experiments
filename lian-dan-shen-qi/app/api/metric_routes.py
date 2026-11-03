"""本人指标与事实报告；不接受客户端伪造指标。"""

from datetime import datetime
import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, BeforeValidator

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_metric_service
from app.api.schemas import UserResponse
from app.api.runner_routes import KNOWN as RUNNER_ERRORS, _fail as runner_error
from app.errors import (MetricSourceChangedError, MetricSourceUnavailableError, MetricStorageError,
                        MetricSyncConflictError, ReportNotFoundError)
from app.logging_config import log_failure
from app.services.metric_service import MetricService
from app.models.training_project import MetricParserRequest
from app.models.evaluation import Identifier, MetricName, lowercase_identifier

router = APIRouter(tags=['训练指标与报告'])
logger = logging.getLogger(__name__)
ERRORS = RUNNER_ERRORS + (MetricStorageError, MetricSourceUnavailableError)
RESPONSES = {400: {'description': '参数不符合要求。'}, 401: {'description': '登录已失效。'},
             404: {'description': '本人运行或报告不存在。'}, 409: {'description': '日志变化或并发冲突。'},
             502: {'description': '远端来源暂不可用。'}, 503: {'description': '请稍后重试。'}, 504: {'description': '连接超时。'}}


class ReportResponse(BaseModel):
    version: int
    generated_at: datetime
    is_stale: bool
    data: dict


class MetricSyncResponse(BaseModel):
    run_id:str
    revision:int
    pages:int
    new_events:int
    stream:dict


def _fail(error):
    if isinstance(error, ReportNotFoundError):
        status, text = 404, '该运行尚未生成报告。'
    elif isinstance(error, MetricSourceChangedError):
        status, text = 409, '日志来源已变化，已保留采集记录，请核对原任务。'
    elif isinstance(error, MetricSyncConflictError):
        status, text = 409, '采集状态冲突，或已有有效指标无法修改解析规则；请刷新后检查。'
    elif isinstance(error, MetricSourceUnavailableError):
        status, text = 502, '暂时无法确认远端日志，请稍后重试。'
    elif isinstance(error, MetricStorageError):
        status, text = 503, '请稍后重试。'
    else:
        runner_error(error)
    log_failure(logger, error, operation='metric_routes', level=logging.ERROR if status >= 500 else logging.WARNING)
    raise HTTPException(status, text) from error


def _no_cache(response):
    response.headers['Cache-Control'] = 'no-store'


@router.post('/runs/{run_id}/metrics/sync',response_model=MetricSyncResponse, summary='有界采集本人远端日志中的指标', responses=RESPONSES)
async def sync_metrics(run_id: UUID, response: Response, max_pages: Annotated[int, Query(ge=1, le=8)] = 4,
                       user: UserResponse = Depends(require_current_user), service: MetricService = Depends(get_metric_service)):
    try:
        result = await service.sync(user.user_id, run_id.hex, max_pages=max_pages)
    except ERRORS as error:
        _fail(error)
    _no_cache(response)
    return result


@router.get('/runs/{run_id}/metrics/parser',response_model=MetricParserRequest,summary='读取本次运行的普通日志解析规则',responses=RESPONSES)
async def get_metric_parser(run_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),service:MetricService=Depends(get_metric_service)):
    try:result=await service.get_parser(user.user_id,run_id.hex)
    except ERRORS as error:_fail(error)
    _no_cache(response)
    return result


@router.put('/runs/{run_id}/metrics/parser',response_model=MetricParserRequest,
            summary='为零有效指标的运行设置日志解析规则并回到日志开头补采，不重启训练',responses=RESPONSES)
async def set_metric_parser(run_id:UUID,request:MetricParserRequest,response:Response,
                            user:UserResponse=Depends(require_current_user),service:MetricService=Depends(get_metric_service)):
    try:result=await service.set_parser(user.user_id,run_id.hex,request.model_dump()['rules'])
    except (*ERRORS,MetricSyncConflictError) as error:_fail(error)
    _no_cache(response)
    return result


@router.get('/runs/{run_id}/metrics', summary='分页读取已采集指标，不连接 SSH', responses=RESPONSES)
async def metrics(run_id: UUID, response: Response, after: Annotated[int, Query(ge=-1, le=10485760)] = -1,
                  limit: Annotated[int, Query(ge=1, le=500)] = 100, split: Annotated[Literal['train', 'validation', 'test'], BeforeValidator(lowercase_identifier)] | None = None,
                  metric_name: MetricName | None = None,
                  dataset: Identifier | None = None,
                  user: UserResponse = Depends(require_current_user), service: MetricService = Depends(get_metric_service)):
    try:
        result = await service.events(user.user_id, run_id.hex, after=after, limit=limit, split=split, metric_name=metric_name, **({} if dataset is None else {'dataset': dataset}))
    except ERRORS as error:
        _fail(error)
    _no_cache(response)
    return result


@router.get('/runs/{run_id}/metrics/latest', summary='读取分组指标的末值和数值统计，不连接 SSH', responses=RESPONSES)
async def latest_metrics(run_id: UUID, response: Response, user: UserResponse = Depends(require_current_user), service: MetricService = Depends(get_metric_service)):
    try:
        result = await service.latest(user.user_id, run_id.hex)
    except ERRORS as error:
        _fail(error)
    _no_cache(response)
    return result


@router.post('/runs/{run_id}/report', response_model=ReportResponse, summary='生成或复用事实报告，不调用模型或 SSH', responses=RESPONSES)
async def generate_report(run_id: UUID, response: Response, user: UserResponse = Depends(require_current_user), service: MetricService = Depends(get_metric_service)):
    try:
        result = await service.report(user.user_id, run_id.hex, generate=True)
    except ERRORS as error:
        _fail(error)
    _no_cache(response)
    return result


@router.get('/runs/{run_id}/report', response_model=ReportResponse, summary='读取已存报告并标注是否过期', responses=RESPONSES)
async def get_report(run_id: UUID, response: Response, user: UserResponse = Depends(require_current_user), service: MetricService = Depends(get_metric_service)):
    try:
        result = await service.report(user.user_id, run_id.hex)
    except ERRORS as error:
        _fail(error)
    _no_cache(response)
    return result
