"""本人运行的项目目录与源码片段；不接受根目录、SSH 凭据或远程命令。"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_source_code_service
from app.api.schemas import UserResponse
from app.api.server_connection_routes import _raise_http as server_error
from app.errors import (RunNotFoundError, RunnerNotStartedError, RunnerConflictError, RunnerProtocolError, RunStorageError,
    ServerNotFoundError, ServerStorageError, ServerAuthenticationError, ServerConnectionError, ServerConnectionTimeoutError,
    ServerHostKeyError, ServerAddressNotAllowedError, ServerBusyError, ServerCredentialDecryptionError)
from app.logging_config import log_failure
from app.models.source_code import SourceProtocolError, FileResponse, DirectoryResponse, MainNetworkRequest, MainNetworkResponse
from app.runner.source_reader import SourceReadError

router = APIRouter(tags=['项目源码读取'])
logger = logging.getLogger(__name__)
KNOWN = (ValueError, MySQLError, SourceProtocolError, RunStorageError, RunnerProtocolError,
         ServerStorageError, ServerAuthenticationError, ServerConnectionError, ServerConnectionTimeoutError,
         ServerHostKeyError, ServerAddressNotAllowedError, ServerBusyError, ServerCredentialDecryptionError)
ERRORS = {401: {'description': '请重新登录。'}, 403: {'description': '源码路径不允许或文件可能包含凭据。'},
          400: {'description': '服务器平台或文本编码不支持。'},
          404: {'description': '本人运行、执行绑定或文件不存在。'}, 409: {'description': '文件版本或服务器绑定已变化。'},
          413: {'description': '文件、目录或单行超过读取限制。'}, 429: {'description': 'SSH 连接繁忙。'},
          502: {'description': '无法连接 SSH 服务器。'}, 504: {'description': 'SSH 连接或读取超时。'},
          503: {'description': '请稍后重试。'}}


async def operate(response, service, user_id, run_id, action, **options):
    try:
        if action=='get_network':result=await service.main_network(user_id,run_id)
        elif action=='set_network':result=await service.select_network(user_id,run_id,options['path'])
        else:result = await service.query(user_id, run_id, action, **options)
    except KNOWN as error:
        raise_source_error(error)
    response.headers['Cache-Control'] = 'no-store'
    return result


def raise_source_error(error):
    status, message = 503, '请稍后重试。'
    if isinstance(error, (RunNotFoundError, RunnerNotStartedError, ServerNotFoundError)):
        status, message = 404, '运行、执行绑定或文件不存在或不可访问。'
    elif isinstance(error, RunnerConflictError):
        status, message = 409, '服务器配置与运行绑定不一致。'
    elif isinstance(error, SourceReadError):
        if error.code == 'NOT_FOUND':
            status, message = 404, '文件或目录不存在。'
        elif error.code in {'PATH_NOT_ALLOWED', 'SENSITIVE_CONTENT'}:
            status, message = 403, '路径不允许或文件可能包含凭据，未返回内容。'
        elif error.code == 'SOURCE_CHANGED':
            status, message = 409, '源码已变化，请重新读取文件，勿混用版本。'
        elif error.code in {'FILE_TOO_LARGE', 'DIRECTORY_TOO_LARGE', 'LINE_TOO_LONG'}:
            status, message = 413, '文件、目录或单行超过源码读取限制。'
        elif error.code in {'UNSUPPORTED_PLATFORM', 'UNSUPPORTED_TEXT'}:
            status, message = 400, '当前仅支持 Linux 项目内的 UTF-8 源码文本。'
    elif isinstance(error, (MySQLError, SourceProtocolError, RunStorageError, RunnerProtocolError, ServerStorageError)):
        pass
    elif isinstance(error, ServerAddressNotAllowedError):
        server_error(error)
    elif isinstance(error, ValueError):
        status, message = 422, '源码读取参数不符合要求。'
    else:
        server_error(error)
    log_failure(logger, error, operation='source_code_routes', level=logging.ERROR if status >= 500 else logging.WARNING)
    raise HTTPException(status, message) from error


@router.get('/runs/{run_id}/code/files', response_model=DirectoryResponse, responses=ERRORS,
            summary='列出本人运行绑定项目的源码目录；不执行训练')
async def files(run_id: UUID, response: Response, path: str = Query('.', max_length=512),
                offset: int = Query(0, ge=0, le=4096), limit: int = Query(25, ge=1, le=50),
                user: UserResponse = Depends(require_current_user), service=Depends(get_source_code_service)):
    return await operate(response, service, user.user_id, run_id.hex, 'list', path=path, offset=offset, limit=limit)


@router.get('/runs/{run_id}/code/file', response_model=FileResponse, responses=ERRORS,
            summary='按完整行读取当前源码；省略 path 则读取已登记训练入口')
async def file(run_id: UUID, response: Response, path: str | None = Query(None, max_length=512),
               start_line: int = Query(1, ge=1, le=1048577), line_count: int = Query(30, ge=1, le=60),
               expected_sha256: str | None = Query(None, pattern=r'^[0-9a-f]{64}$'),
               user: UserResponse = Depends(require_current_user), service=Depends(get_source_code_service)):
    return await operate(response, service, user.user_id, run_id.hex, 'read', path=path, start_line=start_line,
                         line_count=line_count, expected_sha256=expected_sha256)


@router.get('/runs/{run_id}/code/main-network', response_model=MainNetworkResponse, responses=ERRORS,
            summary='查看本次运行选择的主网络代码')
async def main_network(run_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),service=Depends(get_source_code_service)):
    return await operate(response,service,user.user_id,run_id.hex,'get_network')

@router.put('/runs/{run_id}/code/main-network', response_model=MainNetworkResponse, responses=ERRORS,
            summary='为已有运行选择主网络；只读校验文件，不修改训练快照或启动训练')
async def select_main_network(run_id:UUID,request:MainNetworkRequest,response:Response,user:UserResponse=Depends(require_current_user),service=Depends(get_source_code_service)):
    return await operate(response,service,user.user_id,run_id.hex,'set_network',path=request.path)
