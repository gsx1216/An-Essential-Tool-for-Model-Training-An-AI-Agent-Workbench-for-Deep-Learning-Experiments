"""仅供本机验收：临时MySQL和假模型组成的独立应用，不接入正式main。"""

from contextlib import contextmanager
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app import config, database
from app.api import dependencies
from app.api.auth_routes import router as auth_router
from app.api.chat_routes import router as chat_router
from app.api.error_handlers import request_validation_exception_handler
from app.api.history_routes import router as history_router
from app.api.model_credential_routes import router as credential_router
from app.api.request_logging import RequestLoggingMiddleware
from app.api.session_routes import router as session_router
from app.api.experiment_routes import router as experiment_router
from app.api.experiment_run_routes import router as run_router
from app.api.server_connection_routes import router as server_router
from app.api.runner_routes import router as runner_router
from app.api.metric_routes import router as metric_router
from app.api.chat_tool_routes import router as chat_tool_router
from app.api.research_routes import router as research_router
from app.api.training_task_routes import router as training_router
from app.api.monitor_routes import router as monitor_router
from app.api.event_routes import router as event_router,get_event_repository
from app.api.analysis_routes import router as analysis_router
from app.repositories.analysis_repository import AnalysisRepository
from app.services.analysis_service import AnalysisService
from app.repositories.event_repository import EventRepository
from app.repositories.monitor_repository import MonitorRepository
from app.services.monitor_service import MonitorService
from app.repositories.training_repository import TrainingRepository
from app.services.training_task_service import TrainingTaskService
from app.repositories.research_repository import ResearchRepository
from app.services.research_service import ResearchService
from app.services.public_research_client import PublicResearchClient
from app.repositories.metric_repository import MetricRepository
from app.services.metric_service import MetricService
from app.models.runner import RunnerSettings
from app.repositories.runner_repository import RunnerRepository
from app.services.runner_service import RunnerService
from app.services.runner_transport import RunnerTransport
from app.logging_config import DEFAULT_LOG_DIR, configure_logging
from app.repositories.login_session_repository import LoginSessionRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.repositories.experiment_repository import ExperimentRepository
from app.repositories.experiment_run_repository import ExperimentRunRepository
from app.repositories.server_connection_repository import ServerConnectionRepository
from app.security.server_credentials import ServerCredentialCipher
from app.models.server_connection import ServerSettings
from app.services.server_connection_service import ServerConnectionService
from app.services.ssh_connector import SSHConnector
from app.security.passwords import PasswordService
from app.security.tokens import TokenService
from app.services.auth_service import AuthService
from app.services.chat_history_service import ChatHistoryService
from app.services.chat_session_service import ChatSessionService
from app.services.current_user_service import CurrentUserService
from app.services.login_service import LoginService
from app.services.logout_service import LogoutService
from app.services.experiment_service import ExperimentService
from app.services.experiment_run_service import ExperimentRunService
from app.models.context_settings import ContextSettings
from app.services.history_summarizer import HISTORY_SUMMARY_PROMPT
from app.services.input_compressor import COMPRESSION_PROMPT


ROOT = Path(__file__).resolve().parents[1]


class FakeGateway:
    def __init__(self, api_key, error=None, handler=None):
        self.api_key = api_key
        self.error = error
        self.calls = []
        self.closed = False
        self.handler = handler
        self.options = []

    async def invoke(self, messages, **options):
        assert not self.closed
        self.calls.append(list(messages))
        self.options.append(options)
        if self.error is not None:
            raise self.error
        if self.handler is not None:
            return await self.handler(messages, options)
        if messages[0][1].startswith(HISTORY_SUMMARY_PROMPT):
            return {'content': '历史摘要：讨论训练参数；保留限制和待办。'}
        if messages[0][1].startswith(COMPRESSION_PROMPT):
            return {'content': '当前问题：请说明学习率与训练结果。'}
        return {'content': '这是模拟回复，没有调用真实模型。'}

    async def aclose(self):
        self.closed = True


class FakeGatewayBuilder:
    def __init__(self):
        self.created = []
        self.next_errors = []
        self.handler = None

    def __call__(self, *, api_key):
        gateway = FakeGateway(api_key, self.next_errors.pop(0) if self.next_errors else None, self.handler)
        self.created.append(gateway)
        return gateway


@contextmanager
def temporary_chat_api(password, context_settings=None, *, runner_settings=None, chat_tools=False, research=False, server_settings=None, training=False, context_cache=None, analysis=False, source_code=False, suggestions=False, code_changes=False, revisions=False,papers=False,skills=False,skill_root=None):
    """创建随机临时库；上下文退出时关闭日志并仅删除本次创建的库。"""
    name = '_codex_chat_http_' + uuid4().hex
    assert re.fullmatch(r'_codex_chat_http_[0-9a-f]{32}', name)
    created = False
    application = None
    dependencies.get_user_chat_service.cache_clear()
    dependencies.get_model_credential_service.cache_clear()

    def target(repository):
        for attribute, value in vars(repository).copy().items():
            if attribute.endswith('_sql') and isinstance(value, str):
                setattr(repository, attribute, value.replace('experiment_assistant', name))
        return repository

    try:
        with database.get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
                created = True
                for filename in ('init.sql', 'create_chat_turns.sql', 'create_login_sessions.sql', 'create_model_credentials.sql', 'create_context_summaries.sql', 'create_experiments.sql', 'create_experiment_runs.sql', 'create_server_connections.sql', 'create_run_executions.sql', 'create_run_metric_streams.sql', 'create_run_network_sources.sql', 'create_run_metric_parsers.sql', 'create_run_metric_events.sql', 'create_run_reports.sql', 'create_experiment_evaluation_specs.sql', 'create_run_evaluation_specs.sql', 'add_chat_tool_session_index.sql', 'add_chat_tool_turn_index.sql', 'create_chat_run_bindings.sql', 'create_chat_tool_calls.sql', 'create_research_references.sql', 'create_research_advice.sql', 'create_research_rate_limits.sql', 'extend_research_call_limit.sql','create_training_tasks.sql','create_chat_training_plans.sql','create_training_launch_claims.sql','create_run_monitor_tasks.sql','create_event_outbox.sql','create_agent_tasks.sql','create_analysis_preferences.sql','create_analysis_results.sql','create_suggestion_preferences.sql','create_suggestion_results.sql','create_suggestion_evidence.sql','create_code_changes.sql','create_code_change_claims.sql','create_revision_runs.sql','create_revision_launch_claims.sql','paper_fulltext_session.sql','create_papers.sql','create_paper_files.sql','create_paper_chunks.sql'):
                    sql = (ROOT / 'sql' / filename).read_text(encoding='utf-8').replace('experiment_assistant', name)
                    cursor.execute(sql)
                    while cursor.nextset():
                        pass
            connection.commit()
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            configure_logging(directory / 'logs', console=False, force=True)
            try:
                master = Fernet.generate_key().decode('ascii')
                with patch.object(config, 'ENV_FILE', directory / '.env'):
                    with patch.dict(os.environ, {'MODEL_CREDENTIAL_ENCRYPTION_KEY': master}):
                        with patch.object(dependencies, 'get_mysql_password', return_value=password):
                            credentials = dependencies.get_model_credential_service()
                target(credentials.repository)
                users = target(UserRepository(password))
                logins = target(LoginSessionRepository(password))
                sessions = target(SessionRepository(password))
                messages = target(MessageRepository(password))
                experiments = target(ExperimentRepository(password))
                runs = target(ExperimentRunRepository(password))
                servers = target(ServerConnectionRepository(password))
                server_service = ServerConnectionService(servers, ServerCredentialCipher(master), SSHConnector(server_settings or ServerSettings(('127.0.0.1/32',))))
                executions = target(RunnerRepository(password))
                metric_repository = target(MetricRepository(password))
                runner_settings = runner_settings or RunnerSettings()
                runner = RunnerService(executions, runs, server_service,
                                       RunnerTransport(server_service.connector, runner_settings.python_command), runner_settings)
                from app.repositories.revision_run_repository import RevisionRunRepository
                from app.services.revision_run_service import RevisionRunService
                from app.services.revision_transport import RevisionTransport
                from app.services.chat_revision_service import ChatRevisionService
                from app.models.revision_run import RevisionSettings
                from app.api.revision_run_routes import router as revision_router
                revision_repository=target(RevisionRunRepository(password))
                target(revision_repository.runs)
                runner.revisions=revision_repository
                metrics = MetricService(metric_repository, runner)
                revision_service=RevisionRunService(revision_repository,executions,runs,runner_factory=lambda:runner,
                    servers_factory=lambda:server_service,transport_factory=lambda:RevisionTransport(server_service.connector,runner_settings.python_command),
                    metrics_factory=lambda:metrics,settings=RevisionSettings())
                revision_adapter=ChatRevisionService(revision_service)
                from app.repositories.paper_repository import PaperRepository
                from app.services.paper_service import PaperService
                from app.api.paper_routes import router as paper_router
                from app.api.skill_routes import router as skill_router
                from app.services.skill_catalog import SkillCatalog
                skill_catalog=SkillCatalog(skill_root or ROOT/'skills')
                paper_repository=target(PaperRepository(password))
                paper_service=PaperService(paper_repository)
                monitors=target(MonitorRepository(password))
                events=target(EventRepository(password))
                analysis_repository=target(AnalysisRepository(password))
                analysis_service=AnalysisService(analysis_repository)
                from app.repositories.suggestion_repository import SuggestionRepository
                from app.services.suggestion_service import SuggestionService
                from app.api.suggestion_routes import router as suggestion_router
                suggestion_repository=target(SuggestionRepository(password))
                suggestion_service=SuggestionService(suggestion_repository)
                monitor=MonitorService(monitors,executions,metric_repository,runner_factory=lambda:runner)
                auth = AuthService(users, PasswordService())
                tokens = TokenService()
                current = CurrentUserService(logins, tokens)
                login = LoginService(auth, logins, tokens)
                logout = LogoutService(logins, tokens)
                with patch.object(dependencies, 'get_mysql_password', return_value=password):
                    with patch.object(dependencies, 'get_model_credential_service', return_value=credentials):
                        with patch.object(dependencies, 'MessageRepository', return_value=messages):
                            with patch.object(dependencies, 'get_context_settings', return_value=context_settings or ContextSettings()):
                                # 旧验收默认完全隔离本机 Redis；缓存验收显式注入随机命名空间。
                                with patch.object(dependencies, 'get_chat_context_cache', return_value=context_cache):
                                    chat = dependencies.get_user_chat_service()
                                    chat.skill_catalog=skill_catalog if skills else None
                target(chat.context_repository)
                research_repository=target(ResearchRepository(password))
                research_service=ResearchService(research_repository,PublicResearchClient(research_repository.permit))
                training_repository=target(TrainingRepository(password))
                training_service=TrainingTaskService(training_repository,experiments,ExperimentRunService(runs),server_service,runner)
                from app.services.source_code_service import SourceCodeService
                from app.services.source_transport import SourceTransport
                from app.api.source_code_routes import router as source_router
                sources=SourceCodeService(executions,server_service,SourceTransport(server_service.connector,runner_settings.python_command))
                from app.repositories.code_change_repository import CodeChangeRepository
                from app.services.code_change_service import CodeChangeService
                from app.services.chat_code_change_service import ChatCodeChangeService
                from app.api.code_change_routes import router as change_router
                from app.services.user_model_gateway_factory import UserModelGatewayFactory
                change_repository=target(CodeChangeRepository(password))
                change_builder=FakeGatewayBuilder()
                change_service=CodeChangeService(change_repository,executions,source_factory=lambda:sources,
                    servers_factory=lambda:server_service, planner_factory=lambda:UserModelGatewayFactory(credentials,change_builder))
                change_adapter=ChatCodeChangeService(change_service)
                if chat_tools or research or training or analysis or source_code or suggestions or code_changes or revisions or papers:
                    target(chat.tool_service.repository)
                    chat.tool_service.runs = runs
                    chat.tool_service.metrics_factory = lambda: metrics
                    chat.tool_service.runner_factory = lambda: runner
                    chat.tool_service.research_factory = (lambda:research_service) if research else None
                    chat.tool_service.training_factory = (lambda:training_service) if training else None
                    chat.tool_service.analysis_factory = (lambda:analysis_service) if analysis else None
                    chat.tool_service.source_factory = (lambda:sources) if source_code else None
                    chat.tool_service.suggestion_factory = (lambda:suggestion_service) if suggestions else None
                    chat.tool_service.change_factory = (lambda:change_adapter) if code_changes else None
                    chat.tool_service.revision_factory = (lambda:revision_adapter) if revisions else None
                    chat.tool_service.paper_factory = (lambda:paper_service) if papers else None
                else:
                    chat.tool_service = None
                builder = FakeGatewayBuilder()
                # 明确替换网关构造器；测试应用从不构造真实模型客户端。
                chat.gateway_factory.gateway_factory = builder
                application = FastAPI(title='聊天接口练习：假模型与临时数据库')
                application.add_middleware(RequestLoggingMiddleware)
                application.add_exception_handler(RequestValidationError, request_validation_exception_handler)
                for router in (auth_router, session_router, history_router, credential_router, chat_router, experiment_router, run_router, server_router, runner_router, metric_router, chat_tool_router, research_router, training_router, monitor_router,event_router,analysis_router,source_router,suggestion_router,change_router,revision_router,paper_router,skill_router):
                    application.include_router(router)
                application.dependency_overrides.update({
                    dependencies.get_auth_service: lambda: auth,
                    dependencies.get_login_service: lambda: login,
                    dependencies.get_current_user_service: lambda: current,
                    dependencies.get_logout_service: lambda: logout,
                    dependencies.get_chat_session_service: lambda: ChatSessionService(sessions, context_cache=context_cache),
                    dependencies.get_chat_history_service: lambda: ChatHistoryService(messages),
                    dependencies.get_model_credential_service: lambda: credentials,
                    dependencies.get_user_chat_service: lambda: chat,
                    dependencies.get_experiment_service: lambda: ExperimentService(experiments),
                    dependencies.get_experiment_run_service: lambda: ExperimentRunService(runs),
                    dependencies.get_server_connection_service: lambda: server_service,
                    dependencies.get_runner_service: lambda: runner,
                    dependencies.get_metric_service: lambda: metrics,
                    dependencies.get_chat_tool_service: lambda: chat.tool_service,
                    dependencies.get_research_service: lambda: research_service,
                    dependencies.get_training_task_service: lambda: training_service,
                    dependencies.get_monitor_service: lambda: monitor,
                    get_event_repository:lambda:events,
                    dependencies.get_analysis_service:lambda:analysis_service,
                    dependencies.get_source_code_service:lambda:sources,
                    dependencies.get_suggestion_service:lambda:suggestion_service,
                    dependencies.get_code_change_service:lambda:change_service,
                    dependencies.get_revision_run_service:lambda:revision_service,
                    dependencies.get_paper_service:lambda:paper_service,
                    dependencies.get_skill_catalog:lambda:skill_catalog,
                    dependencies.get_skill_message_repository:lambda:messages,
                })
                with patch('app.services.model_gateway.ChatDeepSeek', side_effect=AssertionError('测试应用禁止创建真实模型客户端。')):
                    yield application, {'builder': builder, 'messages': messages, 'master': master,
                                        'contexts': chat.context_repository, 'sessions': sessions, 'chat': chat,
                                        'experiments': experiments,
                                        'runs': runs,
                                        'servers': servers, 'server_service': server_service,
                                        'executions': executions, 'runner': runner,
                                        'metrics': metrics, 'metric_repository': metric_repository,
                                        'research':research_service,'research_repository':research_repository,
                                        'training':training_service,'training_repository':training_repository,
                                        'monitor':monitor,'monitor_repository':monitors,
                                        'events':events,
                                        'sources':sources,
                                        'analysis':analysis_service,'analysis_repository':analysis_repository,
                                        'suggestions':suggestion_service,'suggestion_repository':suggestion_repository,
                                        'changes':change_service,'change_repository':change_repository,'change_builder':change_builder,
                                        'papers':paper_service,'paper_repository':paper_repository,'skill_catalog':skill_catalog,
                                        'revision_service':revision_service,'revision_repository':revision_repository,
                                        'database_name': name, 'log_dir': directory / 'logs'}
            finally:
                if application is not None:
                    application.dependency_overrides.clear()
                configure_logging(DEFAULT_LOG_DIR, console=False, force=True)
    finally:
        dependencies.get_user_chat_service.cache_clear()
        dependencies.get_model_credential_service.cache_clear()
        if created:
            assert re.fullmatch(r'_codex_chat_http_[0-9a-f]{32}', name)
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'DROP DATABASE `{name}`')
