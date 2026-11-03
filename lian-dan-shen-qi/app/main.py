"""应用入口：注册路由、请求日志和校验异常处理；具体登录校验由路由依赖触发。"""

from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.manual_analysis_routes import router as manual_analysis_router
from app.api.auth_routes import router as auth_router
from app.api.error_handlers import request_validation_exception_handler
from app.api.history_routes import router as history_router
from app.api.model_credential_routes import router as model_credential_router
from app.api.request_logging import RequestLoggingMiddleware
from app.api.session_routes import router as session_router
from app.api.chat_routes import router as chat_router
from app.api.experiment_routes import router as experiment_router
from app.api.experiment_run_routes import router as experiment_run_router
from app.api.server_connection_routes import router as server_router
from app.api.runner_routes import router as runner_router
from app.api.metric_routes import router as metric_router
from app.api.chat_tool_routes import router as chat_tool_router
from app.api.research_routes import router as research_router
from app.api.training_task_routes import router as training_router
from app.api.monitor_routes import router as monitor_router
from app.api.event_routes import router as event_router
from app.api.analysis_routes import router as analysis_router
from app.api.source_code_routes import router as source_code_router
from app.api.suggestion_routes import router as suggestion_router
from app.api.code_change_routes import router as code_change_router
from app.api.revision_run_routes import router as revision_router
from app.api.paper_routes import router as paper_router
from app.api.skill_routes import router as skill_router
from app.api.experiment_delete_routes import router as experiment_delete_router
from app.api.dependencies import get_chat_context_cache


@asynccontextmanager
async def lifespan(application):
    try:
        yield
    finally:
        if get_chat_context_cache.cache_info().currsize:
            await asyncio.to_thread(get_chat_context_cache().close)
            get_chat_context_cache.cache_clear()


app = FastAPI(title="“炼丹”神器", lifespan=lifespan)
app.add_middleware(RequestLoggingMiddleware)

app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
app.include_router(auth_router)
app.include_router(session_router)
app.include_router(history_router)
app.include_router(model_credential_router)
app.include_router(chat_router)
app.include_router(experiment_router)
app.include_router(experiment_run_router)
app.include_router(server_router)
app.include_router(runner_router)
app.include_router(metric_router)
app.include_router(chat_tool_router)
app.include_router(research_router)
app.include_router(training_router)
app.include_router(monitor_router)
app.include_router(event_router)
app.include_router(analysis_router)
app.include_router(source_code_router)
app.include_router(suggestion_router)
app.include_router(code_change_router)
app.include_router(revision_router)
app.include_router(paper_router)
app.include_router(skill_router)
app.include_router(experiment_delete_router)


@app.get("/health")
async def health():
    # TODO：返回一个字典，键为 "status"，值为 "ok"
    return {"status": "ok"}

app.include_router(manual_analysis_router)
