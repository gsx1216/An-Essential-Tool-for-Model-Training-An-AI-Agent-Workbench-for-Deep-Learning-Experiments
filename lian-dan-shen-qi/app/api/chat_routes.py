"""聊天消息HTTP练习：填写字段与核心调用、检查通过后再挂载到main。"""

import logging
import asyncio
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Response
from mysql.connector import Error as MySQLError
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_user_chat_service
from app.api.schemas import ChatReplyResponse, SendMessageRequest, UserResponse
from app.errors import (
    ChatToolProtocolError, ChatToolBudgetError, ChatToolStorageError,
    ModelCredentialDecryptionError,
    ModelCredentialNotConfiguredError,
    NoPendingReplyError,
    RequestIdConflictError,
    SessionNotFoundError,
    UnfinishedTurnError,
)
from app.logging_config import log_failure
from app.services.user_chat_service import UserChatService
from app.models.context_state import (
    ContextCompactionError, ContextConflictError, ContextStorageError, ContextTooLargeError, InputCompressionError,
)
from app.models.skill import SkillNotFound,SkillConfigurationError,SkillStorageError,SkillSelectionConflict
from app.api.chat_stream import stream_reply


router = APIRouter(prefix="/sessions", tags=["聊天消息"])
logger = logging.getLogger(__name__)
KNOWN_CHAT_ERRORS = (ValueError, ModelCredentialDecryptionError, MySQLError, OpenAIError,
                    ContextCompactionError, ContextStorageError, InputCompressionError, ChatToolProtocolError, ChatToolStorageError,
                    SkillConfigurationError,SkillStorageError)
ERROR_RESPONSES = {
    400: {"description": "请求参数或个人模型配置不符合要求。"},
    401: {"description": "登录已失效，请重新登录。"},
    404: {"description": "会话不存在或不属于当前用户。"},
    409: {"description": "请求编号已用于其他问题，或会话仍有未完成的问题。"},
    413: {"description": "上下文处理后仍过长，请拆分任务。"},
    429: {"description": "模型请求过于频繁，请稍后重试。"},
    502: {"description": "模型服务暂时不可用，请稍后重试。"},
    503: {"description": "请稍后重试。"},
    504: {"description": "模型响应超时，请稍后重试。"},
}


def _stream_error(error):
    if isinstance(error,KNOWN_CHAT_ERRORS):
        try:_raise_chat_http_error(error,'chat_routes.stream')
        except HTTPException as response:return response.status_code,response.detail
    log_failure(logger,error,operation='chat_routes.stream')
    return 503,'请稍后重试。'


@router.post('/{session_id}/messages/stream',summary='流式发送本人问题；done 表示完整回复已持久化',responses={**ERROR_RESPONSES,200:{'description':'SSE: status/reset/delta/done/error；done 表示已保存。','content':{'text/event-stream':{'schema':{'type':'string'}}}}},response_class=Response)
async def stream_chat_message(session_id:str,request:SendMessageRequest,
                              current_user:UserResponse=Depends(require_current_user),service:UserChatService=Depends(get_user_chat_service)):
    try:await asyncio.to_thread(service.repository.check_session,current_user.user_id,session_id)
    except KNOWN_CHAT_ERRORS as error:_raise_chat_http_error(error,'chat_routes.stream_preflight')
    return stream_reply(lambda delta:service.send_message(current_user.user_id,session_id,request.content,request.request_id.hex,
        skill_id=request.skill_id,on_delta=delta),_stream_error)


@router.post('/{session_id}/retry/stream',summary='流式重试本人已保存的未完成问题',responses={**ERROR_RESPONSES,200:{'description':'SSE: status/reset/delta/done/error；done 表示已保存。','content':{'text/event-stream':{'schema':{'type':'string'}}}}},response_class=Response)
async def stream_retry_chat_message(session_id:str,current_user:UserResponse=Depends(require_current_user),service:UserChatService=Depends(get_user_chat_service)):
    try:await asyncio.to_thread(service.repository.check_session,current_user.user_id,session_id)
    except KNOWN_CHAT_ERRORS as error:_raise_chat_http_error(error,'chat_routes.stream_preflight')
    return stream_reply(lambda delta:service.retry_message(current_user.user_id,session_id,on_delta=delta),_stream_error)


def _raise_chat_http_error(error: Exception, operation: str) -> NoReturn:
    """转换已知错误并记录安全日志；不回传驱动/供应商的原始错误正文。"""
    if isinstance(error, SkillNotFound):
        status, detail = 404, 'Skill不存在或未启用，请重新选择。'
    elif isinstance(error, SessionNotFoundError):
        status, detail = 404, "会话不存在或不属于当前用户。"
    elif isinstance(error, SkillSelectionConflict):
        status, detail = 409, '该请求编号已绑定其他Skill，请为新的选择生成新的 request_id。'
    elif isinstance(error, RequestIdConflictError):
        status, detail = 409, "该请求编号已用于其他问题，请为新问题生成新的 request_id。"
    elif isinstance(error, UnfinishedTurnError):
        status, detail = 409, "当前会话还有未完成的问题，请先重试或新建聊天。"
    elif isinstance(error, NoPendingReplyError):
        status, detail = 400, "当前会话没有等待回复的问题。"
    elif isinstance(error, ModelCredentialNotConfiguredError):
        status, detail = 400, "请先配置个人模型 API Key。"
    elif isinstance(error, ContextConflictError):
        status, detail = 409, "会话状态已更新，请刷新后重试。"
    elif isinstance(error, ChatToolBudgetError):
        status, detail = 413, "本次工具查询内容过多，请缩小查询范围。"
    elif isinstance(error, ContextTooLargeError):
        status, detail = 413, "上下文处理后仍然过长，请缩短材料或拆分任务。"
    elif isinstance(error, ChatToolProtocolError):
        status, detail = 502, "本次查询暂时未能完成，请稍后重试。"
    elif isinstance(error, (ContextCompactionError, InputCompressionError)):
        status, detail = 502, "上下文处理暂时不可用，请稍后重试。"
    elif isinstance(error, (ModelCredentialDecryptionError, MySQLError, ContextStorageError, ChatToolStorageError,SkillConfigurationError,SkillStorageError)):
        status, detail = 503, "请稍后重试。"
    elif isinstance(error, APITimeoutError):
        # 超时属于连接异常的子类，必须先判断。
        status, detail = 504, "模型响应超时，请稍后重试。"
    elif isinstance(error, APIConnectionError):
        status, detail = 502, "模型服务暂时不可用，请稍后重试。"
    elif isinstance(error, APIStatusError):
        if error.status_code in (401, 403):
            # 当前登录身份已通过验证；这里是个人供应商凭据被拒绝。
            status, detail = 400, "个人模型配置不可用，请检查 API Key。"
        elif error.status_code == 429:
            status, detail = 429, "模型请求过于频繁，请稍后重试。"
        else:
            status, detail = 502, "模型服务暂时不可用，请稍后重试。"
    elif isinstance(error, ValueError):
        status, detail = 400, "请求参数不符合要求。"
    else:
        # 剩余已知OpenAIError；本函数只由下面的已知异常分支调用。
        status, detail = 502, "模型服务暂时不可用，请稍后重试。"
    log_failure(logger, error, event="operation_rejected" if status < 500 else "operation_failed",
                operation=operation, level=logging.WARNING if status < 500 else logging.ERROR)
    raise HTTPException(status_code=status, detail=detail) from error


@router.post("/{session_id}/messages", response_model=ChatReplyResponse, status_code=200,
             summary="向自己的会话发送问题", responses=ERROR_RESPONSES)
async def send_chat_message(
    session_id: str,
    request: SendMessageRequest,
    response: Response,
    current_user: UserResponse = Depends(require_current_user),
    service: UserChatService = Depends(get_user_chat_service),
) -> ChatReplyResponse:
    try:
        # TODO 4：await调用service.send_message，用reply接收。
        # 参数user_id=current_user.user_id、session_id=session_id、
        # content=request.content、request_id=request.request_id.hex。
        # UUID对象的.hex才是仓储需要的32位字符串；不要重新生成UUID。
        reply = await service.send_message(user_id=current_user.user_id,session_id=session_id,content=request.content,request_id=request.request_id.hex,
            **({'skill_id':request.skill_id} if request.skill_id is not None else {}))
    except KNOWN_CHAT_ERRORS as error:
        _raise_chat_http_error(error, "chat_routes.send")

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    # TODO 5：返回ChatReplyResponse，字段content使用reply。
    return ChatReplyResponse(content=reply)


@router.post("/{session_id}/retry", response_model=ChatReplyResponse, status_code=200,
             summary="重试自己的会话中等待回复的问题", responses=ERROR_RESPONSES)
async def retry_chat_message(
    session_id: str,
    response: Response,
    current_user: UserResponse = Depends(require_current_user),
    service: UserChatService = Depends(get_user_chat_service),
) -> ChatReplyResponse:
    try:
        # TODO 6：await调用service.retry_message，用reply接收。
        # 参数user_id=current_user.user_id、session_id=session_id。
        # 不需要新的问题正文或request_id，不调用send_message。
        reply = await service.retry_message(user_id=current_user.user_id,session_id=session_id)
    except KNOWN_CHAT_ERRORS as error:
        _raise_chat_http_error(error, "chat_routes.retry")

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    # TODO 7：返回ChatReplyResponse，字段content使用reply。
    return ChatReplyResponse(content=reply)
