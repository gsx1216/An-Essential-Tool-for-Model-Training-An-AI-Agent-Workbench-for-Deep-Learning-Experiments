"""本人会话的工具调用审计；不返回原始日志、凭据或模型推理。"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.api.auth_dependencies import require_current_user
from app.api.chat_routes import KNOWN_CHAT_ERRORS, _raise_chat_http_error
from app.api.dependencies import get_chat_tool_service
from app.api.schemas import UserResponse
from app.services.read_only_chat_service import ReadOnlyChatService

router=APIRouter(tags=['聊天工具记录'])


@router.get('/sessions/{session_id}/tool-calls',summary='查看本人会话的运行关联和工具调用记录')
async def tool_calls(session_id:UUID,response:Response,
    after_id:Annotated[int,Query(ge=0,le=18446744073709551615)]=0,
    limit:Annotated[int,Query(ge=1,le=100)]=20,
    user:UserResponse=Depends(require_current_user),service:ReadOnlyChatService=Depends(get_chat_tool_service)):
    try:
        result=await service.list_calls(user.user_id,session_id.hex,after_id=after_id,limit=limit)
    except KNOWN_CHAT_ERRORS as error:
        _raise_chat_http_error(error,'chat_tool_routes.list_calls')
    response.headers['Cache-Control']='no-store'
    return result
