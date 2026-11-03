"""HTTP 身份校验：提取 Bearer Token，将业务身份结果或异常转换为 HTTP 结果。"""

import logging

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mysql.connector import Error as MySQLError

from app.api.dependencies import get_current_user_service
from app.api.schemas import UserResponse
from app.errors import InvalidTokenError
from app.logging_config import log_failure, logged_operation
from app.services.current_user_service import CurrentUserService


logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="输入登录接口返回的 access_token。",
)


@logged_operation("auth_dependencies.get_bearer_token", expected_errors=(HTTPException,))
def get_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    """只提取原始 Token；此处不查询数据库，也不确认凭证是否仍有效。"""
    # TODO 1：如果 credentials is None，抛出 HTTPException。
    # status_code=401；detail="登录已失效，请重新登录。"；
    # headers={"WWW-Authenticate": "Bearer"}。
    # HTTPBearer 已负责识别 Bearer；缺失、空凭证或其他认证方式都可能返回 None。
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="登录已失效，请重新登录。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # TODO 2：返回 credentials.credentials。
    # credentials.scheme 是 "Bearer"；credentials.credentials 才是原始 Token。
    # 不打印 Token，不读取 URL 参数，不做 strip 或大小写转换。
    return credentials.credentials


async def require_current_user(
    access_token: str = Depends(get_bearer_token),
    service: CurrentUserService = Depends(get_current_user_service),
) -> UserResponse:
    """鉴权成功返回公开用户信息；鉴权失败时接口处理函数不会继续执行。"""
    try:
        # TODO 3：await 调用 service.get_current_user(access_token)。
        # 用 user_id、username 接收返回的两个值。
        # 这个方法本来就是 async def，直接 await，不再套 to_thread。
        # 填好 TODO 3、TODO 4 后删除这一行。
        user_id, username = await service.get_current_user(access_token)

    except InvalidTokenError as error:
        log_failure(logger, error, event="operation_rejected",
                    operation="auth_dependencies.require_current_user", level=logging.WARNING)
        raise HTTPException(
            status_code=401,
            detail="登录已失效，请重新登录。",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    except MySQLError as error:
        log_failure(logger, error, event="database_failure",
                    operation="auth_dependencies.require_current_user")
        raise HTTPException(status_code=503, detail="请稍后重试。") from error

    # TODO 4：返回 UserResponse，用 user_id 和 username 填入对应字段。
    # 不返回原始 Token、哈希或登录记录内部编号。
    return UserResponse(user_id=user_id, username=username)
