"""只读查询本人会话的来源和建议；无需再次联网，也不要求有模型 Key。"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter,Depends,Query,Response

from app.api.auth_dependencies import require_current_user
from app.api.chat_routes import KNOWN_CHAT_ERRORS,_raise_chat_http_error
from app.api.dependencies import get_research_service
from app.api.schemas import UserResponse
from app.services.research_service import ResearchService

router=APIRouter(tags=['研究资料与建议'])


async def _saved(session_id,response,user,service,limit,advice):
    try:
        result=await service.list_saved(user.user_id,session_id.hex,limit=limit,advice=advice)
    except KNOWN_CHAT_ERRORS as error:
        _raise_chat_http_error(error,'research_routes.list_saved')
    response.headers['Cache-Control']='no-store'
    return result


@router.get('/sessions/{session_id}/references',summary='查看本人的会话资料来源快照元信息')
async def references(session_id:UUID,response:Response,
    limit:Annotated[int,Query(ge=1,le=50)]=20,
    user:UserResponse=Depends(require_current_user),service:ResearchService=Depends(get_research_service)):
    return await _saved(session_id,response,user,service,limit,False)


@router.get('/sessions/{session_id}/research-advice',summary='查看本人的已保存研究建议和引用')
async def advice(session_id:UUID,response:Response,
    limit:Annotated[int,Query(ge=1,le=50)]=20,
    user:UserResponse=Depends(require_current_user),service:ResearchService=Depends(get_research_service)):
    return await _saved(session_id,response,user,service,limit,True)
