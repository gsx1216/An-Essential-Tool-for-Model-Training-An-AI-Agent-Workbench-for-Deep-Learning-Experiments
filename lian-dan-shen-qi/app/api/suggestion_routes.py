"""建议阶段元数据 API：当前用户来自 Token；查询/重排不调用模型、SSH 或训练。"""
from uuid import UUID
from fastapi import APIRouter, Depends, Query, Response

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_suggestion_service
from app.api.schemas import UserResponse
from app.api.analysis_routes import operate, ERRORS
from app.models.analysis import AnalysisPreference, AnalysisTaskResponse, AnalysisRetryResponse
from app.models.suggestion import SuggestionResponse, SuggestionToolRecord

router = APIRouter(tags=['代码与论文修改建议'])


@router.get('/me/suggestion-settings', response_model=AnalysisPreference, responses=ERRORS,
            summary='查看本人建议 Agent 开关；默认关闭')
async def preference(response: Response, user: UserResponse = Depends(require_current_user), service=Depends(get_suggestion_service)):
    return await operate(response, service.preference, user.user_id)


@router.put('/me/suggestion-settings', response_model=AnalysisPreference, responses=ERRORS,
            summary='设置本人建议开关；启用允许使用本人 Key、只读 SSH 和公开资料处理待建议任务')
async def set_preference(request: AnalysisPreference, response: Response,
                         user: UserResponse = Depends(require_current_user), service=Depends(get_suggestion_service)):
    return await operate(response, service.preference, user.user_id, enabled=request.enabled)


@router.get('/runs/{run_id}/suggestions', response_model=list[SuggestionResponse], responses=ERRORS,
            summary='查看已保存修改建议及代码/正文证据；不会生成新建议')
async def results(run_id: UUID, response: Response, limit: int = Query(10, ge=1, le=20), offset: int = Query(0, ge=0, le=10000),
                  user: UserResponse = Depends(require_current_user), service=Depends(get_suggestion_service)):
    return await operate(response, service.results, user.user_id, run_id.hex, limit=limit, offset=offset)


@router.get('/runs/{run_id}/suggestion-tasks/{task_id}', response_model=AnalysisTaskResponse, responses=ERRORS,
            summary='查看本人建议任务状态与安全错误分类')
async def task(run_id: UUID, task_id: UUID, response: Response,
               user: UserResponse = Depends(require_current_user), service=Depends(get_suggestion_service)):
    return await operate(response, service.task, user.user_id, run_id.hex, task_id.hex)


@router.post('/runs/{run_id}/suggestion-tasks/{task_id}/retry', response_model=AnalysisRetryResponse, status_code=202, responses=ERRORS,
             summary='重排失败或证据不足的建议；保留工具证据，不覆盖具体建议或重新训练')
async def retry(run_id: UUID, task_id: UUID, response: Response,
                user: UserResponse = Depends(require_current_user), service=Depends(get_suggestion_service)):
    return await operate(response, service.task, user.user_id, run_id.hex, task_id.hex, retry=True)


@router.get('/runs/{run_id}/suggestion-tasks/{task_id}/evidence', response_model=list[SuggestionToolRecord], responses=ERRORS,
            summary='查看建议任务已记录工具证据；包含失败读取，不返回租约或凭据')
async def evidence(run_id: UUID, task_id: UUID, response: Response,
                   user: UserResponse = Depends(require_current_user), service=Depends(get_suggestion_service)):
    return await operate(response, service.evidence, user.user_id, run_id.hex, task_id.hex)
