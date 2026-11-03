"""手动分析入口：只有明确 POST 才调用模型，GET 就绪状态不调用模型。"""
from uuid import UUID
from fastapi import APIRouter,BackgroundTasks,Depends,Response,HTTPException
from pydantic import BaseModel
from app.api.auth_dependencies import require_current_user
from app.api.schemas import UserResponse
from app.api.source_code_routes import KNOWN,raise_source_error
from app.models.analysis import AnalysisConflictError
from app.models.source_code import MainNetworkResponse
from app.services.manual_analysis_service import build_manual_service

router=APIRouter(tags=['手动分析与建议'])

class ManualAnalysisResponse(BaseModel):
    report_ready:bool
    main_network:MainNetworkResponse
    processing:bool
    can_start:bool
    message:str
    accepted:bool=False

@router.get('/runs/{run_id}/analysis-readiness',response_model=ManualAnalysisResponse,summary='检查手动分析是否就绪；不调用模型')
async def readiness(run_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),service=Depends(build_manual_service)):
    try:result=await service.state(user.user_id,run_id.hex)
    except KNOWN as error:raise_source_error(error)
    response.headers['Cache-Control']='no-store'
    return result

@router.post('/runs/{run_id}/analysis/start',response_model=ManualAnalysisResponse,status_code=202,
             summary='手动生成本次分析与建议；使用本人模型 Key，不启动训练')
async def start(run_id:UUID,response:Response,background:BackgroundTasks,user:UserResponse=Depends(require_current_user),service=Depends(build_manual_service)):
    try:result=await service.prepare(user.user_id,run_id.hex)
    except AnalysisConflictError as error:raise HTTPException(409,str(error)) from error
    except KNOWN as error:raise_source_error(error)
    if result['accepted']:background.add_task(service.execute,user.user_id,run_id.hex,result['event_id'])
    response.headers['Cache-Control']='no-store'
    return result
