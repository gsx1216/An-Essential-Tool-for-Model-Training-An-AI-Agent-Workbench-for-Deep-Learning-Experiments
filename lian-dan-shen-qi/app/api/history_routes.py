"""历史查看接口练习：只读取展示数据；填写后再在 main.py 注册。"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_chat_history_service
from app.api.schemas import HistoryMessageResponse, UserResponse
from app.errors import SessionNotFoundError
from app.logging_config import log_failure
from app.services.chat_history_service import ChatHistoryService


router = APIRouter(prefix="/sessions", tags=["聊天会话"])
logger = logging.getLogger(__name__)


@router.get(
    "/{session_id}/messages",
    response_model=list[HistoryMessageResponse],
    status_code=200,
    summary="分页查看自己的会话历史",
    responses={
        400: {"description": "请求参数不符合要求。"},
        401: {"description": "登录已失效，请重新登录。"},
        404: {"description": "会话不存在或不属于当前用户。"},
        503: {"description": "请稍后重试。"},
    },
)
async def read_chat_history(
    session_id: str,
    response: Response,
    after_seq: int = Query(default=0, ge=0, le=2**64 - 1),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: UserResponse = Depends(require_current_user),
    service: ChatHistoryService = Depends(get_chat_history_service),
) -> list[HistoryMessageResponse]:
    try:
        # TODO 1：await 调用 service.load_history_page，用 messages 接收结果。
        # 四个参数：user_id=current_user.user_id、session_id=session_id、
        # after_seq=after_seq、limit=limit。
        # 这个业务方法本身是 async def，直接 await，不再套 to_thread。
        # 本接口只读取供页面展示的历史，不调用 ChatService 或 ModelGateway。
        messages = await service.load_history_page(user_id=current_user.user_id, after_seq=after_seq, limit=limit,session_id=session_id)
    except SessionNotFoundError as error:
        log_failure(logger, error, event="operation_rejected",
                    operation="history_routes.read", level=logging.WARNING)
        raise HTTPException(status_code=404, detail="会话不存在或不属于当前用户。") from error
    except ValueError as error:
        log_failure(logger, error, event="operation_rejected",
                    operation="history_routes.read", level=logging.WARNING)
        raise HTTPException(status_code=400, detail="请求参数不符合要求。") from error
    except MySQLError as error:
        log_failure(logger, error, event="database_failure", operation="history_routes.read")
        raise HTTPException(status_code=503, detail="请稍后重试。") from error

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    # TODO 2：遍历 messages 中的 (seq, role, content)。
    # 每项转换成 HistoryMessageResponse(seq=seq, role=role, content=content)，
    # 加入列表，循环结束后返回完整列表；空历史也返回 []。
    # seq 使用数据库原值，不按页面位置重新编号；正文不做 strip。
    result = []
    for seq,role,content in messages:
        result.append(
            HistoryMessageResponse(
                seq=seq, role=role, content=content
            )
        )
    return result