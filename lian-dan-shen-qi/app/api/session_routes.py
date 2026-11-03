"""聊天会话 HTTP 练习：验证身份、调用业务、转换响应；填写后再注册到 main。"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_chat_session_service
from app.api.schemas import CreateSessionRequest, SessionCreatedResponse, SessionResponse, UserResponse
from app.errors import SessionNotFoundError
from app.logging_config import log_failure
from app.services.chat_session_service import ChatSessionService


router = APIRouter(prefix="/sessions", tags=["聊天会话"])
logger = logging.getLogger(__name__)


@router.post(
    "",
    response_model=SessionCreatedResponse,
    status_code=201,
    summary="创建聊天会话",
    responses={
        400: {"description": "请求参数不符合要求。"},
        401: {"description": "登录已失效，请重新登录。"},
        503: {"description": "请稍后重试。"},
    },
)
async def create_chat_session(
    request: CreateSessionRequest,
    response: Response,
    current_user: UserResponse = Depends(require_current_user),
    service: ChatSessionService = Depends(get_chat_session_service),
) -> SessionCreatedResponse:
    try:
        # TODO 1：await 调用 service.create_session。
        # 参数：user_id=current_user.user_id、title=request.title。
        # 用 session_id 接收返回值；这里先不 return。
        # 这是异步业务方法，不再套 asyncio.to_thread。
        session_id = await service.create_session(
            user_id=current_user.user_id,
            title=request.title,
        )
    except ValueError as error:
        log_failure(logger, error, event="operation_rejected",
                    operation="session_routes.create", level=logging.WARNING)
        raise HTTPException(status_code=400, detail="请求参数不符合要求。") from error
    except MySQLError as error:
        log_failure(logger, error, event="database_failure", operation="session_routes.create")
        raise HTTPException(status_code=503, detail="请稍后重试。") from error

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    # TODO 2：构造并返回 SessionCreatedResponse，传入 session_id=session_id。
    # 不返回 Token、密码或请求正文里的其他字段。
    return SessionCreatedResponse(session_id=session_id)

@router.get(
    "",
    response_model=list[SessionResponse],
    status_code=200,
    summary="查看自己的聊天会话",
    responses={
        400: {"description": "请求参数不符合要求。"},
        401: {"description": "登录已失效，请重新登录。"},
        503: {"description": "请稍后重试。"},
    },
)
async def list_chat_sessions(
    response: Response,
    current_user: UserResponse = Depends(require_current_user),
    service: ChatSessionService = Depends(get_chat_session_service),
) -> list[SessionResponse]:
    try:
        # TODO 3：await 调用 service.list_sessions，用 sessions 接收结果。
        # 参数只能取 current_user.user_id，不从请求参数中读取用户 ID。
        sessions = await service.list_sessions(current_user.user_id)
    except ValueError as error:
        log_failure(logger, error, event="operation_rejected",
                    operation="session_routes.list", level=logging.WARNING)
        raise HTTPException(status_code=400, detail="请求参数不符合要求。") from error
    except MySQLError as error:
        log_failure(logger, error, event="database_failure", operation="session_routes.list")
        raise HTTPException(status_code=503, detail="请稍后重试。") from error

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    # TODO 4：遍历 sessions 中的 (session_id, title)。
    # 每一项转换成 SessionResponse(session_id=session_id, title=title)。
    # 返回完整的模型列表，空列表也是成功；不要在 for 内直接 return 第一项。
    result = []
    for session_id, title in sessions:
        result.append(
            SessionResponse(session_id=session_id, title=title)
        )
    return result

@router.delete(
    "/{session_id}",
    response_class=Response,
    response_model=None,
    status_code=204,
    summary="删除自己的聊天会话及内容",
    responses={
        400: {"description": "请求参数不符合要求。"},
        401: {"description": "登录已失效，请重新登录。"},
        404: {"description": "会话不存在或不属于当前用户。"},
        503: {"description": "请稍后重试。"},
    },
)
async def delete_chat_session(
    session_id: str,
    current_user: UserResponse = Depends(require_current_user),
    service: ChatSessionService = Depends(get_chat_session_service),
) -> Response:
    try:
        # TODO 5：await 调用 service.delete_session。
        # 参数：current_user.user_id 和路径中的 session_id。
        # 成功返回 None；直接等待完成即可，不用 if 判断 None。
        await service.delete_session(
            current_user.user_id, session_id=session_id
        )
    except SessionNotFoundError as error:
        # 它继承 ValueError，必须放在通用 ValueError 前面。
        log_failure(logger, error, event="operation_rejected",
                    operation="session_routes.delete", level=logging.WARNING)
        raise HTTPException(status_code=404, detail="会话不存在或不属于当前用户。") from error
    except ValueError as error:
        log_failure(logger, error, event="operation_rejected",
                    operation="session_routes.delete", level=logging.WARNING)
        raise HTTPException(status_code=400, detail="请求参数不符合要求。") from error
    except MySQLError as error:
        log_failure(logger, error, event="database_failure", operation="session_routes.delete")
        raise HTTPException(status_code=503, detail="请稍后重试。") from error

    # TODO 6：返回空的 Response，明确传入 status_code=204。
    # headers={"Cache-Control": "no-store", "Pragma": "no-cache"}。
    # 手动返回 Response 时，实际状态码以这个对象为准，不能只依赖上方装饰器。
    return Response(
        status_code=204,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
