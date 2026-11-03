"""受登录保护的执行接口；脚本来自部署注册表，不接受 shell/命令/路径。"""

import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from mysql.connector import Error as MySQLError
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_runner_service
from app.api.schemas import UserResponse
from app.api.server_connection_routes import _raise_http as server_error
from app.errors import (RunNotFoundError, RunStorageError, RunnerConflictError, RunnerNotStartedError,
                        RunnerProtocolError, RunnerScriptNotAllowedError, ServerAddressNotAllowedError,
                        ServerAuthenticationError, ServerBusyError, ServerConnectionError,
                        ServerConnectionTimeoutError, ServerCredentialDecryptionError, ServerHostKeyError,
                        ServerNotFoundError, ServerStorageError)
from app.logging_config import log_failure
from app.services.runner_service import RunnerService
from app.models.experiment_run import RunStatus

router = APIRouter(tags=['训练执行 Runner'])
logger = logging.getLogger(__name__)
KNOWN = (ValueError, MySQLError, RunStorageError, RunnerProtocolError, ServerAuthenticationError, ServerBusyError,
         ServerConnectionError, ServerConnectionTimeoutError, ServerCredentialDecryptionError, ServerHostKeyError, ServerStorageError)


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    server_id: UUID
    script_id: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')]


class ExecutionResponse(BaseModel):
    run_id: str
    server_id: str
    script_id: str
    status: RunStatus
    exit_code: int | None
    error_code: str | None
    created_at: datetime
    checked_at: datetime | None
    output_directory: str | None = Field(default=None, description='可见产物的预定目录；是否已发布以 refresh 的 observation.outputs 为准。')


class OperationResponse(BaseModel):
    execution: ExecutionResponse
    observation: dict | None


def _fail(error):
    if isinstance(error, (RunNotFoundError, RunnerNotStartedError)):
        status, text = 404, '运行或执行任务不存在或不属于当前用户。'
    elif isinstance(error, RunnerConflictError):
        status, text = 409, '该运行已绑定其他目标或状态不允许启动。'
    elif isinstance(error, RunnerScriptNotAllowedError):
        status, text = 403, '该脚本尚未被部署者允许执行。'
    elif isinstance(error, (RunnerProtocolError, RunStorageError)):
        status, text = 503, '请稍后重试。'
    else:
        server_error(error)
    log_failure(logger, error, operation='runner_routes', level=logging.ERROR if status == 503 else logging.WARNING)
    raise HTTPException(status, text) from error


def _no_cache(response):
    response.headers['Cache-Control'] = 'no-store'


@router.get('/runner/scripts', summary='查看部署者登记的脚本及参数规则')
async def scripts(response: Response, user: UserResponse = Depends(require_current_user), service: RunnerService = Depends(get_runner_service)):
    _no_cache(response)
    return {'items': [{'script_id': name, 'parameters': spec['parameters'], 'max_runtime_seconds': spec['max_runtime_seconds']}
                      for name, spec in service.settings.registry().items()]}


@router.post('/runs/{run_id}/start', response_model=OperationResponse, status_code=202, summary='幂等启动已登记脚本；重复请求不重复训练')
async def start_run(run_id: UUID, request: StartRunRequest, response: Response,
                    user: UserResponse = Depends(require_current_user), service: RunnerService = Depends(get_runner_service)):
    try:
        result = await service.start(user.user_id, run_id.hex, request.server_id.hex, request.script_id)
    except KNOWN as error:
        _fail(error)
    _no_cache(response)
    return result


@router.get('/runs/{run_id}/execution', response_model=ExecutionResponse, summary='读取本人持久化执行信息，不连接 SSH')
async def get_execution(run_id: UUID, response: Response, user: UserResponse = Depends(require_current_user), service: RunnerService = Depends(get_runner_service)):
    try:
        result = await service.get(user.user_id, run_id.hex)
    except KNOWN as error:
        _fail(error)
    _no_cache(response)
    return result


@router.post('/runs/{run_id}/refresh', response_model=OperationResponse, summary='查询真实远端状态并更新持久化记录')
async def refresh(run_id: UUID, response: Response, user: UserResponse = Depends(require_current_user), service: RunnerService = Depends(get_runner_service)):
    try:
        result = await service.operate(user.user_id, run_id.hex, 'status')
    except KNOWN as error:
        _fail(error)
    _no_cache(response)
    return result


@router.post('/runs/{run_id}/stop', response_model=OperationResponse, status_code=202, summary='提交幂等停止请求；最终结果需刷新确认')
async def stop(run_id: UUID, response: Response, user: UserResponse = Depends(require_current_user), service: RunnerService = Depends(get_runner_service)):
    try:
        result = await service.operate(user.user_id, run_id.hex, 'stop')
    except KNOWN as error:
        _fail(error)
    _no_cache(response)
    return result


@router.get('/runs/{run_id}/logs', response_model=OperationResponse, summary='按字节游标读取本人远端有界日志')
async def logs(run_id: UUID, response: Response, offset: Annotated[int, Query(ge=0, le=10485760)] = 0,
               limit: Annotated[int, Query(ge=1, le=16384)] = 4096,
               user: UserResponse = Depends(require_current_user), service: RunnerService = Depends(get_runner_service)):
    try:
        result = await service.operate(user.user_id, run_id.hex, 'logs', offset=offset, limit=limit)
    except KNOWN as error:
        _fail(error)
    _no_cache(response)
    return result
