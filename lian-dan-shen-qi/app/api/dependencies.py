"""对象组装层：按进程复用业务服务；不缓存请求 Token、当前用户或查询结果。"""

from functools import lru_cache
import logging

from fastapi import HTTPException

from app.config import get_research_enabled, get_context_settings, get_model_encryption_key, get_mysql_password, get_server_settings, get_runner_settings
from app.repositories.runner_repository import RunnerRepository
from app.services.runner_service import RunnerService
from app.services.runner_transport import RunnerTransport
from app.repositories.metric_repository import MetricRepository
from app.repositories.chat_tool_repository import ChatToolRepository
from app.repositories.research_repository import ResearchRepository
from app.services.research_service import ResearchService
from app.services.public_research_client import PublicResearchClient
from app.services.read_only_chat_service import ReadOnlyChatService
from app.repositories.training_repository import TrainingRepository
from app.services.training_task_service import TrainingTaskService
from app.services.metric_service import MetricService
from app.repositories.monitor_repository import MonitorRepository
from app.services.monitor_service import MonitorService
from app.config import get_monitor_settings
from app.errors import ModelCredentialConfigurationError
from app.logging_config import log_failure
from app.repositories.login_session_repository import LoginSessionRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.context_repository import ContextRepository
from app.repositories.model_credential_repository import ModelCredentialRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.repositories.experiment_repository import ExperimentRepository
from app.repositories.experiment_run_repository import ExperimentRunRepository
from app.repositories.server_connection_repository import ServerConnectionRepository
from app.security.server_credentials import ServerCredentialCipher
from app.services.server_connection_service import ServerConnectionService
from app.services.ssh_connector import SSHConnector
from app.security.passwords import PasswordService
from app.security.api_key_cipher import ApiKeyCipher
from app.security.tokens import TokenService
from app.services.auth_service import AuthService
from app.services.chat_session_service import ChatSessionService
from app.services.chat_history_service import ChatHistoryService
from app.services.current_user_service import CurrentUserService
from app.services.login_service import LoginService
from app.services.logout_service import LogoutService
from app.services.model_credential_service import ModelCredentialService
from app.services.user_chat_service import UserChatService
from app.services.user_model_gateway_factory import UserModelGatewayFactory
from app.services.redis_context_cache import RedisContextCache
from app.config import get_context_cache_settings
from app.services.experiment_service import ExperimentService
from app.services.experiment_run_service import ExperimentRunService


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_chat_code_change_service():
    from app.services.chat_code_change_service import ChatCodeChangeService
    return ChatCodeChangeService(get_code_change_service())


@lru_cache(maxsize=1)
def get_code_change_service():
    from functools import partial
    from app.config import get_code_change_settings
    from app.repositories.code_change_repository import CodeChangeRepository
    from app.services.code_change_service import CodeChangeService
    from app.services.code_change_transport import CodeChangeTransport
    from app.services.model_gateway import ModelGateway
    try:
        settings = get_code_change_settings()
        return CodeChangeService(CodeChangeRepository(get_mysql_password()), RunnerRepository(get_mysql_password()),
            source_factory=get_source_code_service, servers_factory=get_server_connection_service,
            transport_factory=lambda: CodeChangeTransport(get_server_connection_service().connector, get_runner_settings().python_command),
            planner_factory=lambda: UserModelGatewayFactory(get_model_credential_service(),
                partial(ModelGateway, model_name=settings.model_name, max_retries=0, timeout_seconds=settings.model_timeout_seconds)),
            settings=settings)
    except (ValueError, OSError) as error:
        log_failure(logger, error, operation='dependencies.get_code_change_service')
        raise HTTPException(503, '请稍后重试。') from error


@lru_cache(maxsize=1)
def get_suggestion_service():
    from app.repositories.suggestion_repository import SuggestionRepository
    from app.services.suggestion_service import SuggestionService
    try:
        return SuggestionService(SuggestionRepository(get_mysql_password()))
    except (ValueError, OSError) as error:
        log_failure(logger, error, operation='dependencies.get_suggestion_service')
        raise HTTPException(503, '请稍后重试。') from error


@lru_cache(maxsize=1)
def get_source_code_service():
    from app.services.source_code_service import SourceCodeService
    from app.services.source_transport import SourceTransport
    runner = get_runner_service()
    from app.repositories.run_network_repository import RunNetworkRepository
    return SourceCodeService(runner.repository, runner.servers,
        SourceTransport(runner.servers.connector, runner.settings.python_command), RunNetworkRepository(get_mysql_password()))


@lru_cache(maxsize=1)
def get_analysis_service():
    from app.repositories.analysis_repository import AnalysisRepository
    from app.services.analysis_service import AnalysisService
    try:
        # 读取/重排只组装仓储；无需模型 Key、主密钥、Redis 或 SSH。
        return AnalysisService(AnalysisRepository(get_mysql_password()))
    except (ValueError, OSError) as error:
        log_failure(logger, error, operation="dependencies.get_analysis_service")
        raise HTTPException(503, "请稍后重试。") from error


@lru_cache(maxsize=1)
def get_chat_context_cache() -> RedisContextCache:
    # 只缓存线程安全连接池与配置；此处不连接 Redis、不读取聊天、不解密个人 Key。
    return RedisContextCache(get_context_cache_settings(), get_model_encryption_key)


@lru_cache(maxsize=1)
def get_metric_service() -> MetricService:
    try:
        return MetricService(MetricRepository(get_mysql_password()), runner_factory=get_runner_service)
    except (ValueError, OSError) as error:
        log_failure(logger, error, operation='dependencies.get_metric_service')
        raise HTTPException(503, '请稍后重试。') from error


@lru_cache(maxsize=1)
def get_revision_run_service():
    from app.config import get_revision_settings
    from app.repositories.revision_run_repository import RevisionRunRepository
    from app.services.revision_run_service import RevisionRunService
    from app.services.revision_transport import RevisionTransport
    try:
        password=get_mysql_password()
        def transport():
            servers=get_server_connection_service()
            return RevisionTransport(servers.connector,get_runner_settings().python_command)
        return RevisionRunService(RevisionRunRepository(password),RunnerRepository(password),ExperimentRunRepository(password),
            runner_factory=get_runner_service,servers_factory=get_server_connection_service,
            transport_factory=transport,metrics_factory=get_metric_service,settings=get_revision_settings())
    except (ValueError,OSError) as error:
        log_failure(logger,error,operation='dependencies.get_revision_run_service')
        raise HTTPException(503,'请稍后重试。') from error


@lru_cache(maxsize=1)
def get_chat_revision_service():
    from app.services.chat_revision_service import ChatRevisionService
    return ChatRevisionService(get_revision_run_service())


@lru_cache(maxsize=1)
def get_runner_service() -> RunnerService:
    try:
        password = get_mysql_password()
        servers = get_server_connection_service()
        settings = get_runner_settings()
        from app.repositories.revision_run_repository import RevisionRunRepository
        return RunnerService(RunnerRepository(password), ExperimentRunRepository(password), servers,
                             RunnerTransport(servers.connector, settings.python_command), settings,
                             revisions=RevisionRunRepository(password), tasks=TrainingRepository(password))
    except (ValueError, OSError) as error:
        log_failure(logger, error, operation='dependencies.get_runner_service')
        raise HTTPException(503, '请稍后重试。') from error


@lru_cache(maxsize=1)
def get_server_connection_service() -> ServerConnectionService:
    try:
        return ServerConnectionService(ServerConnectionRepository(get_mysql_password()),
                                       ServerCredentialCipher(get_model_encryption_key()), SSHConnector(get_server_settings()))
    except (ValueError, OSError, ModelCredentialConfigurationError) as error:
        log_failure(logger, error, operation='dependencies.get_server_connection_service')
        raise HTTPException(503, '请稍后重试。') from error


@lru_cache(maxsize=1)
def get_experiment_run_service() -> ExperimentRunService:
    """缓存固定运行协作者，不缓存用户、连接、运行列表或参数快照。"""
    try:
        return ExperimentRunService(ExperimentRunRepository(get_mysql_password()))
    except (ValueError, OSError) as error:
        log_failure(logger, error, operation='dependencies.get_experiment_run_service')
        raise HTTPException(503, '请稍后重试。') from error


@lru_cache(maxsize=1)
def get_experiment_service() -> ExperimentService:
    """只缓存仓储与业务对象；不缓存某个用户、实验记录或数据库连接。"""
    try:
        return ExperimentService(ExperimentRepository(get_mysql_password()))
    except (ValueError, OSError) as error:
        log_failure(logger, error, operation='dependencies.get_experiment_service')
        raise HTTPException(status_code=503, detail='请稍后重试。') from error


@lru_cache(maxsize=1)
def get_auth_service() -> AuthService:
    # TODO 1：调用 get_mysql_password()，取得数据库密码。
    password = get_mysql_password()
    # TODO 2：创建 UserRepository 对象，传入数据库密码。
    userrepository = UserRepository(password)
    # TODO 3：创建 PasswordService 对象，不需要传参数。
    passwordsercive = PasswordService()
    # TODO 4：创建并返回 AuthService 对象，
    return AuthService(userrepository, passwordsercive)
    #         传入上面的仓储对象和密码工具对象。


@lru_cache(maxsize=1)
def get_login_service() -> LoginService:
    """每个进程复用协作者；每次登录的用户名和 Token 仍由方法局部变量保存。"""
    password = get_mysql_password()
    return LoginService(
        auth_service=get_auth_service(),
        session_repository=LoginSessionRepository(password),
        token_service=TokenService(),
    )


@lru_cache(maxsize=1)
def get_current_user_service() -> CurrentUserService:
    """组装当前用户校验服务；只缓存协作者，不缓存用户或 Token。"""
    # TODO 1：调用 get_mysql_password()，使用 password 接收数据库密码。
    password = get_mysql_password()
    # TODO 2：创建 LoginSessionRepository，传入 password，使用 repository 接收。
    repository = LoginSessionRepository(password)
    # TODO 3：创建 TokenService，使用 token_service 接收。
    token_service = TokenService()
    # TODO 4：创建并返回 CurrentUserService。
    # 关键字参数：session_repository=repository、token_service=token_service。
    return CurrentUserService(
        session_repository=repository,
        token_service=token_service,
    )


@lru_cache(maxsize=1)
def get_logout_service() -> LogoutService:
    """组装退出服务；复用仓储与工具，不保存本次请求的 Token。"""
    # TODO 1：调用 get_mysql_password()，用 password 接收。
    password = get_mysql_password()
    # TODO 2：创建 LoginSessionRepository(password)，用 repository 接收。
    repository = LoginSessionRepository(password)
    # TODO 3：创建 TokenService()，用 token_service 接收。
    token_service = TokenService()
    # TODO 4：创建并返回 LogoutService。
    # 参数：session_repository=repository、token_service=token_service。
    return LogoutService(
        session_repository=repository,
        token_service=token_service,
    )


@lru_cache(maxsize=1)
def get_chat_session_service() -> ChatSessionService:
    """组装聊天会话服务；填写后再供会话路由使用，不缓存用户或查询结果。"""
    # TODO 1：调用 get_mysql_password()，用 password 接收后端数据库密码。
    password = get_mysql_password()
    # TODO 2：创建 SessionRepository(password)，用 repository 接收。
    # 这里是聊天会话仓储，不是保存登录 Token 的 LoginSessionRepository。
    repository = SessionRepository(password)
    # TODO 3：创建并返回 ChatSessionService(repository)。
    # 返回服务对象，不要在这里调用 create_session/list_sessions/delete_session。
    return ChatSessionService(repository, context_cache=get_chat_context_cache())


@lru_cache(maxsize=1)
def get_chat_history_service() -> ChatHistoryService:
    """组装历史消息服务；填写后供历史路由使用，缓存协作者对象。"""
    # TODO 1：调用 get_mysql_password()，使用 password 接收后端数据库密码。
    password = get_mysql_password()
    # TODO 2：创建 MessageRepository(password)，使用 repository 接收。
    # 它提供 load_history_page；不要使用 SessionRepository 或 LoginSessionRepository。
    repository = MessageRepository(password)
    # TODO 3：创建并返回 ChatHistoryService(repository)。
    # 工厂只组装对象，不在这里读取某个用户的历史，也不接收 Token 或用户 ID。
    return ChatHistoryService(repository)


@lru_cache(maxsize=1)
def get_model_credential_service() -> ModelCredentialService:
    """个人Key服务工厂练习；只组装协作者，不读取用户Key或连接数据库。"""
    try:
        # TODO 1：调用 get_mysql_password()，用 password 接收后端数据库密码。'
        password = get_mysql_password()
        # TODO 2：调用 get_model_encryption_key()，用 master_key 接收加密主密钥。
        master_password = get_model_encryption_key()
        # TODO 3：创建 ModelCredentialRepository(password)，用 repository 接收。
        repository = ModelCredentialRepository(password)
        # TODO 4：创建 ApiKeyCipher(master_key)，用 cipher 接收。
        cipher = ApiKeyCipher(master_password)
        # TODO 5：返回 ModelCredentialService(repository=repository, cipher=cipher)。
        # 这里返回服务对象，不调用save_api_key/get_api_key等业务方法。
        return ModelCredentialService(repository=repository, cipher=cipher)
    except (ModelCredentialConfigurationError, ValueError) as error:
        # 依赖解析先于路由函数执行，因此配置错误在工厂这里转换。
        log_failure(logger, error, event="configuration_failure", operation="dependencies.get_model_credential_service")
        raise HTTPException(status_code=503, detail="请稍后重试。") from error


@lru_cache(maxsize=1)
def get_user_chat_service() -> UserChatService:
    """个人聊天依赖工厂练习：只组装协作者，不读取个人Key或发送消息。"""
    try:
        # TODO 1：调用get_mysql_password()，用password接收数据库密码。
        password = get_mysql_password()
        # TODO 2：调用已有get_model_credential_service()，用credential_service接收。
        # 复用已经完成的凭据业务工厂，不要在这里读取主密钥或重复组装加密器。
        credential = get_model_credential_service()
        # TODO 3：创建UserModelGatewayFactory，用gateway_factory接收。
        # 参数credential_service=credential_service；只创建工厂，不调用create_for_user。
        gateway_factory = UserModelGatewayFactory(credential)
        # TODO 4：创建MessageRepository(password)，用repository接收。
        # 这是消息仓储，构造时只加载SQL；没有用户ID，不执行数据库查询。
        repository = MessageRepository(password)
        # TODO 5：创建并返回UserChatService。
        # 参数gateway_factory=gateway_factory、repository=repository。
        # 填好后删除下面的占位异常；这里是同步组装函数，不使用await。
        return UserChatService(
            gateway_factory=gateway_factory, repository=repository,
              skill_catalog=get_skill_catalog(),
            context_repository=ContextRepository(password), context_settings=get_context_settings(),
            context_cache=get_chat_context_cache(),
            tool_service=ReadOnlyChatService(ChatToolRepository(password), ExperimentRunRepository(password),
                                            metrics_factory=get_metric_service, runner_factory=get_runner_service,
                                            research_factory=get_research_service if get_research_enabled() else None,
                                            analysis_factory=get_analysis_service,
                                            source_factory=get_source_code_service,
                                            suggestion_factory=get_suggestion_service,
                                            change_factory=get_chat_code_change_service,
                                            revision_factory=get_chat_revision_service,
                                            paper_factory=get_paper_service,
                                            training_factory=get_training_task_service),
        )
    except ValueError as error:
        # 既有凭据工厂抛出的HTTPException直接传递；本层配置ValueError安全转换。
        log_failure(logger, error, event="configuration_failure",
                    operation="dependencies.get_user_chat_service")
        raise HTTPException(status_code=503, detail="请稍后重试。") from error



@lru_cache(maxsize=1)
def get_chat_tool_service() -> ReadOnlyChatService:
    """审计查询独立组装，不读取模型 Key 或加密主密钥，不建立 SSH。"""
    try:
        password = get_mysql_password()
        return ReadOnlyChatService(ChatToolRepository(password), ExperimentRunRepository(password),
                                   metrics_factory=get_metric_service, runner_factory=get_runner_service)
    except (ValueError, OSError) as error:
        log_failure(logger, error, operation='dependencies.get_chat_tool_service')
        raise HTTPException(503, '请稍后重试。') from error


@lru_cache(maxsize=1)
def get_research_service() -> ResearchService:
    """公开资料协作者；查询结果按请求保存，不在缓存对象中存储用户上下文。"""
    try:
        repository=ResearchRepository(get_mysql_password())
        return ResearchService(repository,PublicResearchClient(repository.permit))
    except (ValueError,OSError) as error:
        log_failure(logger,error,operation='dependencies.get_research_service')
        raise HTTPException(503,'请稍后重试。') from error


@lru_cache(maxsize=1)
def get_training_task_service() -> TrainingTaskService:
    try:
        password=get_mysql_password()
        from app.services.source_transport import SourceTransport
        runner = get_runner_service()
        return TrainingTaskService(TrainingRepository(password),ExperimentRepository(password),
            get_experiment_run_service(),get_server_connection_service(),runner,
            source_transport=SourceTransport(runner.servers.connector, runner.settings.python_command))
    except (ValueError,OSError) as error:
        log_failure(logger,error,operation='dependencies.get_training_task_service')
        raise HTTPException(503,'请稍后重试。') from error


@lru_cache(maxsize=1)
def get_monitor_service() -> MonitorService:
    """元数据查询不要求解密凭据；仅 Worker 实际执行时才组装 Runner。"""
    try:
        password=get_mysql_password()
        return MonitorService(MonitorRepository(password),RunnerRepository(password),MetricRepository(password),
            runner_factory=get_runner_service,settings=get_monitor_settings())
    except (ValueError,OSError) as error:
        log_failure(logger,error,operation='dependencies.get_monitor_service')
        raise HTTPException(503,'请稍后重试。') from error


@lru_cache(maxsize=1)
def get_paper_service():
    from app.repositories.paper_repository import PaperRepository
    from app.services.paper_service import PaperService
    try:return PaperService(PaperRepository(get_mysql_password()))
    except (ValueError,OSError) as error:
        log_failure(logger,error,operation='dependencies.get_paper_service')
        raise HTTPException(503,'请稍后重试。') from error


@lru_cache(maxsize=1)
def get_skill_catalog():
    from pathlib import Path
    from app.services.skill_catalog import SkillCatalog
    from app.repositories.personal_skill_repository import PersonalSkillRepository
    from app.services.redis_skill_cache import RedisSkillCache
    from app.config import get_skill_cache_url
    # 仅复用连接配置；Skill 始终使用 1 号库，事件/聊天所在库保持原样。
    try:
        cache=RedisSkillCache(get_skill_cache_url())
        return SkillCatalog(Path(__file__).resolve().parents[2]/'skills',PersonalSkillRepository(get_mysql_password(),cache))
    except (ValueError,OSError) as error:
        log_failure(logger,error,operation='dependencies.get_skill_catalog')
        raise HTTPException(503,'请稍后重试。') from error


@lru_cache(maxsize=1)
def get_skill_message_repository():
    # 读取轮次快照不需要个人模型Key或模型凭据主密钥。
    try:return MessageRepository(get_mysql_password())
    except (ValueError,OSError) as error:
        log_failure(logger,error,operation='dependencies.get_skill_message_repository')
        raise HTTPException(503,'请稍后重试。') from error
