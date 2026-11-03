"""HTTP 入口：注册、签发登录凭证，通过身份依赖保护 /auth/me。"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import get_bearer_token
from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_auth_service, get_login_service
from app.api.dependencies import get_logout_service
from app.api.schemas import LoginRequest, LoginResponse, RegisterRequest, UserResponse
from app.errors import InvalidCredentialsError, UsernameAlreadyExistsError
from app.errors import InvalidTokenError
from app.logging_config import log_failure
from app.services.auth_service import AuthService
from app.services.login_service import LoginService
from app.services.logout_service import LogoutService

router = APIRouter(
    prefix="/auth",
    tags=["用户认证"],
)
logger = logging.getLogger(__name__)


@router.post("/register", response_model=UserResponse, status_code=201)
async def register_user(
        request: RegisterRequest,
        service: AuthService = Depends(get_auth_service),
) -> UserResponse:
    # 提取密码交给业务服务；路由不保存或打印明文密码。
    password = request.password.get_secret_value()
    try:
        user_id, username = await service.register(request.username, password)

    except UsernameAlreadyExistsError as error:
        log_failure(logger, error, event="operation_rejected", operation="auth_routes.register", level=logging.WARNING)
        # 此异常继承 ValueError，必须在通用 ValueError 前单独处理。
        raise HTTPException(
            status_code=409,
            detail="用户名已存在，请使用其他用户名。",
        ) from error

    except ValueError as error:
        log_failure(logger, error, event="operation_rejected", operation="auth_routes.register", level=logging.WARNING)
        raise HTTPException(
            status_code=400,
            detail="用户名或密码不符合要求。",
        ) from error

    except MySQLError as error:
        log_failure(logger, error, event="database_failure", operation="auth_routes.register")
        # 对外使用固定提示；from error 保留内部异常关联，便于排查。
        raise HTTPException(
            status_code=503,
            detail="请稍后重试。",
        ) from error

    # 只有业务成功后才会运行到这里；不把密码或哈希放进响应。
    return UserResponse(user_id=user_id, username=username)


@router.post(
    "/login", response_model=LoginResponse, status_code=200, summary="用户登录并获取凭证",
    responses={401: {"description": "用户名或密码错误。"},
               503: {"description": "请稍后重试。"},
               500: {"description": "请稍后重试。"}},
)
async def login_user(
        request: LoginRequest,
        response: Response,
        service: LoginService = Depends(get_login_service),
) -> LoginResponse:
    # SecretStr 只负责隐藏展示，校验密码需要取出原始字符串。
    password = request.password.get_secret_value()
    try:
        result = await service.login(request.username, password)

    except InvalidCredentialsError as error:
        log_failure(logger, error, event="operation_rejected", operation="auth_routes.login", level=logging.WARNING)
        # 用户不存在或密码错误时使用同一个提示。
        raise HTTPException(
            status_code=401,
            detail="用户名或密码错误。",
        ) from error

    except MySQLError as error:
        log_failure(logger, error, event="database_failure", operation="auth_routes.login")
        raise HTTPException(
            status_code=503,
            detail="请稍后重试。",
        ) from error

    # 响应中含原始凭证，要求客户端和中间代理不缓存。
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return LoginResponse(
        access_token=result.access_token,
        token_type=result.token_type,
        expires_in=result.expires_in,
        user=UserResponse(user_id=result.user_id, username=result.username),
    )


@router.get(
    "/me",
    response_model=UserResponse,
    status_code=200,
    summary="查看当前登录用户",
    responses={
        401: {"description": "登录已失效，请重新登录。"},
        503: {"description": "请稍后重试。"},
    },
)
async def read_current_user(
        response: Response,
        current_user: UserResponse = Depends(require_current_user),
) -> UserResponse:
    # TODO 1：设置 response.headers["Cache-Control"] = "no-store"。
    # 同时设置 response.headers["Pragma"] = "no-cache"。
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    # TODO 2：返回 current_user。
    # 它已经是依赖校验成功后的用户信息，不需要再查数据库。
    return current_user


@router.post(
    "/logout",
    status_code=204,
    response_class=Response,
    response_model=None,
    summary="退出当前登录",
    responses={
        401: {"description": "登录已失效，请重新登录。"},
        503: {"description": "请稍后重试。"},
    },
)
async def logout_current_session(
        access_token: str = Depends(get_bearer_token),
        service: LogoutService = Depends(get_logout_service),
) -> Response:
    try:
        # TODO 1：await 调用 service.logout(access_token)。
        # 成功结果是 None，这里不需要拆包，也不需要再次套 to_thread。
        await service.logout(access_token)
    except InvalidTokenError as error:
        log_failure(
            logger, error, event="operation_rejected",
            operation="auth_routes.logout", level=logging.WARNING,
        )
        raise HTTPException(
            status_code=401,
            detail="登录已失效，请重新登录。",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    except MySQLError as error:
        log_failure(logger, error, event="database_failure", operation="auth_routes.logout")
        raise HTTPException(status_code=503, detail="请稍后重试。") from error

    # TODO 2：返回空的 Response 对象。
    # status_code=204；headers={"Cache-Control": "no-store", "Pragma": "no-cache"}。
    return Response(status_code=204,headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
