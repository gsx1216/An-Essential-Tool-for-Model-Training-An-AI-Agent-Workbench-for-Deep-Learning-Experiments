"""本人服务器配置和 SSH 连通性测试；没有远程命令执行入口。"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_server_connection_service
from app.api.schemas import UserResponse
from app.api.server_connection_schemas import CreateServerRequest, ServerListResponse, ServerResponse, ServerTestResponse
from app.errors import (InvalidTokenError, ServerAddressNotAllowedError, ServerAuthenticationError, ServerBusyError, ServerInUseError,
                        ServerConnectionError, ServerConnectionTimeoutError, ServerCredentialDecryptionError,
                        ServerHostKeyError, ServerNotFoundError, ServerStorageError)
from app.logging_config import log_failure
from app.services.server_connection_service import ServerConnectionService


router = APIRouter(prefix='/servers', tags=['训练服务器'])
logger = logging.getLogger(__name__)
KNOWN = (ValueError, MySQLError, ServerAuthenticationError, ServerConnectionError, ServerHostKeyError,
         ServerConnectionTimeoutError, ServerCredentialDecryptionError, ServerStorageError, ServerBusyError)
RESPONSES = {400: {'description': '配置或认证不符合要求。'}, 401: {'description': '登录已失效。'},
             403: {'description': '服务器 IP 未被部署者允许。'}, 404: {'description': '配置不存在或不属于当前用户。'},
             409: {'description': '主机指纹不匹配，或删除时服务器仍有活动运行。'}, 429: {'description': '连接测试繁忙。'},
             502: {'description': 'SSH 连接失败。'}, 503: {'description': '请稍后重试。'}, 504: {'description': 'SSH 连接超时。'}}


def _raise_http(error):
    log_failure(logger, error, operation='server_connection_routes',
                level=logging.WARNING if isinstance(error, ValueError) else logging.ERROR)
    if isinstance(error, ServerNotFoundError):
        raise HTTPException(404, '服务器配置不存在或不属于当前用户。') from error
    if isinstance(error, ServerInUseError):
        raise HTTPException(409, '该服务器仍有排队中或运行中的训练，请先停止训练并刷新远端状态，再删除连接配置。') from error
    if isinstance(error, InvalidTokenError):
        raise HTTPException(401, '登录已失效，请重新登录。', headers={'WWW-Authenticate': 'Bearer'}) from error
    for error_type, status, message in (
        (ServerAddressNotAllowedError, 403, '该服务器 IP 尚未被部署者允许连接。'),
        (ServerHostKeyError, 409, '服务器主机指纹不匹配，请核对服务器身份。'),
        (ServerAuthenticationError, 400, 'SSH 认证失败，请检查用户名、私钥或密码。'),
        (ServerBusyError, 429, '连接测试繁忙，请稍后重试。'),
        (ServerConnectionTimeoutError, 504, 'SSH 连接或认证超时，请稍后重试。'),
        (ServerConnectionError, 502, '无法连接 SSH 服务器，请检查地址、端口和网络。'),
    ):
        if isinstance(error, error_type):
            raise HTTPException(status, message) from error
    if isinstance(error, ValueError):
        raise HTTPException(400, '请求参数不符合要求。') from error
    raise HTTPException(503, '请稍后重试。') from error


def _no_cache(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'


@router.post('', response_model=ServerResponse, status_code=201, summary='保存自己的 SSH 配置，不连接或运行命令', responses=RESPONSES)
async def create_server(request: CreateServerRequest, response: Response,
                         user: UserResponse = Depends(require_current_user),
                         service: ServerConnectionService = Depends(get_server_connection_service)):
    values = request.model_dump(exclude={'password', 'private_key', 'passphrase'})
    values.update({name: getattr(request, name).get_secret_value() if getattr(request, name) is not None else None
                   for name in ('password', 'private_key', 'passphrase')})
    try:
        result = await service.create(user.user_id, **values)
    except KNOWN as error:
        _raise_http(error)
    _no_cache(response)
    response.headers['Location'] = '/servers/' + result.server_id
    return ServerResponse.model_validate(result)


@router.get('', response_model=ServerListResponse, summary='分页查看自己的服务器配置', responses=RESPONSES)
async def list_servers(response: Response, limit: Annotated[int, Query(ge=1, le=100)] = 20, before_id: UUID | None = None,
                        user: UserResponse = Depends(require_current_user),
                        service: ServerConnectionService = Depends(get_server_connection_service)):
    try:
        result = await service.list(user.user_id, limit=limit, before_id=before_id.hex if before_id else None)
    except KNOWN as error:
        _raise_http(error)
    _no_cache(response)
    return ServerListResponse(items=[ServerResponse.model_validate(item) for item in result.items], next_cursor=result.next_cursor)


@router.get('/{server_id}', response_model=ServerResponse, summary='查看自己的服务器配置，不返回凭据', responses=RESPONSES)
async def get_server(server_id: UUID, response: Response, user: UserResponse = Depends(require_current_user),
                       service: ServerConnectionService = Depends(get_server_connection_service)):
    try:
        result = await service.get(user.user_id, server_id.hex)
    except KNOWN as error:
        _raise_http(error)
    _no_cache(response)
    return ServerResponse.model_validate(result)


@router.delete('/{server_id}', status_code=204, response_class=Response, summary='清空本人服务器凭据并移出列表，保留历史记录；有活动运行时拒绝', responses=RESPONSES)
async def delete_server(server_id: UUID, user: UserResponse = Depends(require_current_user),
                          service: ServerConnectionService = Depends(get_server_connection_service)):
    try:
        await service.delete(user.user_id, server_id.hex)
    except KNOWN as error:
        _raise_http(error)
    return Response(status_code=204, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})


@router.post('/{server_id}/test', response_model=ServerTestResponse, summary='测试 SSH 连接和认证，不执行命令', responses=RESPONSES)
async def test_server(server_id: UUID, response: Response, user: UserResponse = Depends(require_current_user),
                        service: ServerConnectionService = Depends(get_server_connection_service)):
    try:
        result = await service.test_connection(user.user_id, server_id.hex)
    except KNOWN as error:
        _raise_http(error)
    _no_cache(response)
    return ServerTestResponse(**result)
