"""公共/个人 Skill 和本人轮次快照；不执行提示词中的脚本。"""
import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_skill_catalog,get_skill_message_repository
from app.api.schemas import UserResponse
from app.logging_config import log_failure
from app.models.skill import (SkillId, SkillList, SkillSnapshot, PersonalSkillCreate, TurnSkillResponse, SkillNotFound,
                              SkillTurnNotFound, SkillConfigurationError, SkillStorageError)

router=APIRouter(tags=['动态Skill'])
logger=logging.getLogger(__name__)
ERRORS={401:{'description':'请重新登录。'},404:{'description':'Skill或本人轮次不存在。'},503:{'description':'请稍后重试。'}}


async def call(response,function,*args):
    try:result=await asyncio.to_thread(function,*args)
    except (SkillNotFound,SkillTurnNotFound) as error:
        log_failure(logger,error,operation='skill_routes',level=logging.WARNING)
        raise HTTPException(404,'Skill或轮次不存在或不可访问。') from error
    except (SkillConfigurationError,SkillStorageError,MySQLError) as error:
        log_failure(logger,error,operation='skill_routes')
        raise HTTPException(503,'请稍后重试。') from error
    response.headers['Cache-Control']='no-store'
    return result


@router.get('/skills',response_model=SkillList,responses=ERRORS,summary='列出公共和本人Skill；不调用模型')
async def skills(response:Response,user:UserResponse=Depends(require_current_user),catalog=Depends(get_skill_catalog)):
    return {'items':await call(response,catalog.list,user.user_id)}


@router.post('/skills',response_model=SkillSnapshot,status_code=201,responses=ERRORS,summary='创建本人的提示词 Skill；不调用模型或执行脚本')
async def create_skill(request:PersonalSkillCreate,response:Response,user:UserResponse=Depends(require_current_user),catalog=Depends(get_skill_catalog)):
    return await call(response,catalog.create,user.user_id,request)


@router.delete('/skills/{skill_id}',status_code=204,responses=ERRORS,summary='删除本人个人Skill及缓存；保留历史轮次已使用的快照')
async def delete_skill(skill_id:SkillId,response:Response,user:UserResponse=Depends(require_current_user),catalog=Depends(get_skill_catalog)):
    await call(response,catalog.delete,user.user_id,skill_id)
    response.status_code=204
    return response


@router.get('/skills/{skill_id}',response_model=SkillSnapshot,responses=ERRORS,summary='查看公共或本人Skill正文、版本和快照摘要值')
async def skill(skill_id:SkillId,response:Response,user:UserResponse=Depends(require_current_user),catalog=Depends(get_skill_catalog)):
    return await call(response,catalog.get,skill_id,user.user_id)


@router.get('/sessions/{session_id}/turns/{turn_id}/skill',response_model=TurnSkillResponse,responses=ERRORS,
            summary='查看本人这一轮实际使用的Skill冻结快照；不调用模型')
async def turn_skill(session_id:UUID,turn_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),repository=Depends(get_skill_message_repository)):
    snapshot=await call(response,repository.load_turn_skill,user.user_id,session_id.hex,turn_id.hex)
    return {'turn_id':turn_id.hex,'skill':snapshot}
