"""修改副本训练接口；登录身份来自 Token，启动独立于准备。"""
from uuid import UUID
from fastapi import APIRouter, Depends, Query, Response

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_revision_run_service
from app.api.schemas import UserResponse
from app.api.code_change_routes import operate, ERRORS
from app.models.revision_run import PrepareRevision, RevisionResponse, ComparisonResponse
from app.api.runner_routes import OperationResponse

router=APIRouter(tags=['修改后训练与原结果对比'])


@router.post('/code-changes/{change_id}/training-runs',response_model=RevisionResponse,responses=ERRORS,
    summary='预检修改副本并保存新运行；不启动训练')
async def prepare(change_id:UUID,request:PrepareRevision,response:Response,
    user:UserResponse=Depends(require_current_user),service=Depends(get_revision_run_service)):
    return await operate(response,service.prepare,user.user_id,change_id.hex,request.request_id.hex,request.parameters)


@router.get('/code-changes/{change_id}/training-runs',response_model=list[RevisionResponse],responses=ERRORS,summary='列出本人修改记录派生的训练')
async def records(change_id:UUID,response:Response,limit:int=Query(10,ge=1,le=20),
    user:UserResponse=Depends(require_current_user),service=Depends(get_revision_run_service)):
    return await operate(response,service.list,user.user_id,change_id.hex,limit)


@router.get('/runs/{run_id}/revision',response_model=RevisionResponse,responses=ERRORS,summary='读取新运行与原运行、修改记录和源码版本的绑定')
async def record(run_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),service=Depends(get_revision_run_service)):
    return await operate(response,service.get,user.user_id,run_id.hex)


@router.post('/runs/{run_id}/revision/start',status_code=202,response_model=OperationResponse,responses=ERRORS,
    summary='显式启动已准备的修改后运行；相同 run 不重复训练')
async def start(run_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),service=Depends(get_revision_run_service)):
    return await operate(response,service.start,user.user_id,run_id.hex)


@router.get('/runs/{run_id}/comparison',response_model=ComparisonResponse,responses=ERRORS,summary='读取原运行与此修改后运行的同协议报告对比；不重建报告')
async def comparison(run_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),service=Depends(get_revision_run_service)):
    return await operate(response,service.compare,user.user_id,run_id.hex)
