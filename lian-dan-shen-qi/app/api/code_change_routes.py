"""计划/应用分开；POST apply 为当前登录用户显式要求在独立副本写入修改。"""
import logging
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from mysql.connector import Error as MySQLError
from openai import OpenAIError, APITimeoutError, APIStatusError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_code_change_service
from app.api.schemas import UserResponse
from app.errors import ModelCredentialNotConfiguredError, ModelCredentialDecryptionError, RunNotFoundError, RunnerConflictError, RunnerNotStartedError
from app.api.server_connection_routes import _raise_http as server_error
from app.services.read_only_chat_service import UNAVAILABLE
from app.models.code_change import (ChangeResponse, PrepareChange, ChangeNotFound, ChangeConflict,
    ChangeInstructionRequired, ChangeProtocolError, ChangeStorageError)
from app.runner.source_reader import SourceReadError
from app.models.revision_run import RevisionConflict
from app.models.training_task import TrainingInstructionRequired
from app.errors import RunRequestConflictError
from app.models.source_code import SourceProtocolError
from app.logging_config import log_failure

router = APIRouter(tags=['应用建议与代码修改记录'])
logger = logging.getLogger(__name__)
ERRORS = {400:{'description':'请求或个人模型配置不符合要求。'},401:{'description':'请重新登录。'},
    403:{'description':'SSH 地址不在部署者允许范围。'},429:{'description':'SSH 连接繁忙。'},
    404:{'description':'记录不存在或不可访问。'},409:{'description':'版本、请求编号、目标或修改状态冲突。'},
    502:{'description':'模型或远端修改暂不可用。'},503:{'description':'请稍后重试。'},504:{'description':'处理超时。'}}


async def operate(response, operation, *args, **kwargs):
    try:
        value = await operation(*args, **kwargs)
    except (ValueError, MySQLError, OpenAIError, ChangeProtocolError, ChangeStorageError, SourceProtocolError,
            ModelCredentialDecryptionError, *UNAVAILABLE) as error:
        if isinstance(error, (ChangeNotFound, RunNotFoundError)): status, text = 404, '记录不存在或不可访问。'
        elif isinstance(error, (ChangeConflict, ChangeInstructionRequired, RunnerConflictError, RunnerNotStartedError, RevisionConflict, TrainingInstructionRequired, RunRequestConflictError)): status, text = 409, '文件版本、请求编号、修改目标或状态冲突，请查看修改记录。'
        elif isinstance(error, SourceReadError): status, text = 409, '当前源码无法读取或版本发生变化，请重新检查建议对应代码。'
        elif isinstance(error, ModelCredentialNotConfiguredError): status, text = 400, '请先配置个人模型 API Key。'
        elif isinstance(error, APITimeoutError): status, text = 504, '模型处理超时，请查询原记录后重试。'
        elif isinstance(error, APIStatusError) and error.status_code in (401,403): status, text = 400, '个人模型配置不可用，请检查 API Key。'
        elif isinstance(error, APIStatusError) and error.status_code==429: status, text = 429, '模型请求频繁，请稍后重试。'
        elif isinstance(error, (MySQLError, ChangeStorageError, ModelCredentialDecryptionError)): status, text = 503, '请稍后重试。'
        elif isinstance(error, (OpenAIError, ChangeProtocolError, SourceProtocolError)): status, text = 502, '修改计划或远端处理暂未完成，请查看原记录后重试。'
        elif isinstance(error, UNAVAILABLE): server_error(error)
        elif isinstance(error, ValueError): status, text = 400, '请求参数不符合要求。'
        else: server_error(error)
        log_failure(logger, error, operation='code_change_routes', level=logging.ERROR if status>=500 else logging.WARNING)
        raise HTTPException(status, text) from error
    except TimeoutError as error:
        log_failure(logger, error, operation='code_change_routes.timeout')
        raise HTTPException(504, '处理超时，请查询原修改记录后重试。') from error
    response.headers['Cache-Control'] = 'no-store'
    return value


@router.post('/runs/{run_id}/code-changes', response_model=ChangeResponse, responses=ERRORS,
    summary='为本人指定建议生成精确修改计划；不写远端代码')
async def prepare(run_id: UUID, request: PrepareChange, response: Response,
    user: UserResponse = Depends(require_current_user), service=Depends(get_code_change_service)):
    return await operate(response, service.prepare, user.user_id, run_id.hex,
        request.suggestion_task_id.hex, request.suggestion_index, request.request_id.hex)


@router.get('/runs/{run_id}/code-changes', response_model=list[ChangeResponse], responses=ERRORS,
    summary='查看本人运行的修改记录和差异；不调用模型或 SSH')
async def records(run_id: UUID, response: Response, limit: int = Query(10, ge=1, le=20), offset: int = Query(0, ge=0, le=10000),
    user: UserResponse = Depends(require_current_user), service=Depends(get_code_change_service)):
    return await operate(response, service.list, user.user_id, run_id.hex, limit=limit, offset=offset)


@router.get('/code-changes/{change_id}', response_model=ChangeResponse, responses=ERRORS,
    summary='查看本人修改计划、实际 diff 和检查结果')
async def record(change_id: UUID, response: Response,
    user: UserResponse = Depends(require_current_user), service=Depends(get_code_change_service)):
    return await operate(response, service.get, user.user_id, change_id.hex)


@router.post('/code-changes/{change_id}/apply', response_model=ChangeResponse, responses=ERRORS,
    summary='显式应用已存计划到独立目录并检查；不覆盖原项目或启动训练')
async def apply(change_id: UUID, response: Response,
    user: UserResponse = Depends(require_current_user), service=Depends(get_code_change_service)):
    return await operate(response, service.apply, user.user_id, change_id.hex)
