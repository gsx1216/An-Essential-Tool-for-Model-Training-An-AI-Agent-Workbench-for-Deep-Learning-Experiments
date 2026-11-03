"""用户训练目录与计划查询；聊天中的实际启动由工具执行。"""
import logging
from uuid import UUID

from fastapi import APIRouter,Depends,HTTPException,Query,Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_training_task_service
from app.api.schemas import UserResponse
from app.errors import ExperimentNotFoundError,ServerNotFoundError,RunnerScriptNotAllowedError
from app.logging_config import log_failure
from app.models.training_task import TaskRequest,TrainingConflictError,TrainingNotFoundError
from app.services.training_task_service import TrainingTaskService,public_plan
from app.models.training_project import ProjectDirectoryRequest, ProjectFileRequest, ProjectEnvironmentRequest, ProjectEnvironmentResponse, ProjectResourceResponse
import asyncio

router=APIRouter(tags=['对话训练工具'])
logger=logging.getLogger(__name__)


@router.post('/training-projects/resources',response_model=ProjectResourceResponse,summary='只读查询本人服务器 nvidia-smi GPU 型号、显存与利用率')
async def project_resources(request:ProjectEnvironmentRequest,response:Response,
                            user:UserResponse=Depends(require_current_user),
                            service:TrainingTaskService=Depends(get_training_task_service)):
    response.headers['Cache-Control']='no-store'
    try:return await service.project_source(user.user_id,'resources',request.model_dump())
    except (ValueError,MySQLError,RuntimeError,TimeoutError) as error:fail(error)


@router.post('/training-projects/environments',response_model=ProjectEnvironmentResponse,
             summary='读取本人服务器可执行的系统/Conda/项目虚拟环境；不执行项目')
async def project_environments(request:ProjectEnvironmentRequest,response:Response,
                               user:UserResponse=Depends(require_current_user),
                               service:TrainingTaskService=Depends(get_training_task_service)):
    response.headers['Cache-Control']='no-store'
    try:return await service.project_source(user.user_id,'environments',request.model_dump())
    except (ValueError,MySQLError,RuntimeError,TimeoutError) as error:fail(error)


def fail(error):
    if isinstance(error,(TrainingNotFoundError,ExperimentNotFoundError,ServerNotFoundError)):status,text=404,'训练任务或关联记录不存在或不可访问。'
    elif isinstance(error,TrainingConflictError):status,text=409,'任务名称或配置冲突，请检查已有任务。'
    elif isinstance(error,RunnerScriptNotAllowedError):status,text=403,'训练入口尚未被部署者配置。'
    elif isinstance(error,ValueError):status,text=400,'请求参数不符合训练任务要求。'
    else:status,text=503,'请稍后重试。'
    log_failure(logger,error,operation='training_task_routes',level=logging.ERROR if status==503 else logging.WARNING)
    raise HTTPException(status,text) from error


@router.post('/training-projects/files',summary='训练前只读列出本人服务器项目目录；无需已有运行')
async def list_project_files(request:ProjectDirectoryRequest,response:Response,
                             user:UserResponse=Depends(require_current_user),
                             service:TrainingTaskService=Depends(get_training_task_service)):
    response.headers['Cache-Control']='no-store'
    try:return await service.project_source(user.user_id,'list',request.model_dump())
    except (ValueError,MySQLError,RuntimeError,TimeoutError) as error:fail(error)


@router.post('/training-projects/file',summary='训练前只读读取本人项目 README、入口或配置；不执行')
async def read_project_file(request:ProjectFileRequest,response:Response,
                            user:UserResponse=Depends(require_current_user),
                            service:TrainingTaskService=Depends(get_training_task_service)):
    response.headers['Cache-Control']='no-store'
    try:return await service.project_source(user.user_id,'read',request.model_dump())
    except (ValueError,MySQLError,RuntimeError,TimeoutError) as error:fail(error)


@router.post('/training-tasks',status_code=201,summary='配置自己的项目或选择已登记入口；保存配置，不开始训练')
async def create_task(request:TaskRequest,user:UserResponse=Depends(require_current_user),service:TrainingTaskService=Depends(get_training_task_service)):
    try:return await service.create(user.user_id,request)
    except (ValueError,MySQLError,RuntimeError,TimeoutError) as error:fail(error)


@router.get('/training-tasks',summary='按名称查询自己的训练任务')
async def list_tasks(query:str=Query('',max_length=80),offset:int=Query(0,ge=0,le=10000),limit:int=Query(5,ge=1,le=5),
                     user:UserResponse=Depends(require_current_user),service:TrainingTaskService=Depends(get_training_task_service)):
    try:return await service.list(user.user_id,query=query,offset=offset,limit=limit)
    except (ValueError,MySQLError,RuntimeError) as error:fail(error)


@router.get('/training-tasks/{task_id}',summary='读取自己的训练任务和参数规则')
async def get_task(task_id:UUID,user:UserResponse=Depends(require_current_user),service:TrainingTaskService=Depends(get_training_task_service)):
    try:return await service.get(user.user_id,task_id.hex)
    except (ValueError,MySQLError,RuntimeError) as error:fail(error)


@router.delete('/training-tasks/{task_id}',status_code=204,response_class=Response,
               summary='删除本人的训练配置，保留历史计划和运行，不停止已运行训练',
               responses={401:{'description':'请先登录'},404:{'description':'配置不存在或不可访问'},
                          503:{'description':'请稍后重试'}})
async def delete_task(task_id:UUID,user:UserResponse=Depends(require_current_user),
                      service:TrainingTaskService=Depends(get_training_task_service)):
    try:
        await service.delete(user.user_id,task_id.hex)
    except (ValueError,MySQLError,RuntimeError) as error:
        fail(error)
    return Response(status_code=204,headers={'Cache-Control':'no-store'})


@router.get('/sessions/{session_id}/training-plan',summary='读取本聊天最近的训练计划，不执行')
async def latest_plan(session_id:UUID,user:UserResponse=Depends(require_current_user),service:TrainingTaskService=Depends(get_training_task_service)):
    try:return public_plan(await asyncio.to_thread(service.repository.get_plan,user.user_id,session_id.hex))
    except (ValueError,MySQLError,RuntimeError) as error:fail(error)
