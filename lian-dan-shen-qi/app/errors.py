"""业务异常：供仓储抛出、接口识别，不依赖 FastAPI。"""


class UsernameAlreadyExistsError(ValueError):
    """用户名违反唯一约束；接口可以单独转换为 409 响应。"""


class InvalidCredentialsError(ValueError):
    """仅表示账号密码核对失败；接口转换为 401，不代表凭证保存故障。"""


class InvalidTokenError(ValueError):
    """请求的登录凭证无效；后续鉴权入口将它转换为统一的 401 响应。"""


class SessionNotFoundError(ValueError):
    """会话不存在或不属于当前用户；后续接口统一转换为 404，不透露归属。"""


class RequestIdConflictError(ValueError):
    """同一会话的请求编号被用于不同问题；接口转换为 409，不回传问题内容。"""


class UnfinishedTurnError(ValueError):
    """会话仍有未完成提问，不能追加新问题；接口转换为 409。"""


class NoPendingReplyError(ValueError):
    """没有等待回复的提问却请求重试；接口转换为带明确操作提示的 400。"""


class ModelCredentialNotConfiguredError(ValueError):
    """当前用户未配置模型Key；调用方提示先配置，不能使用平台共享Key。"""


class ModelCredentialDecryptionError(RuntimeError):
    """已存凭据无法解密；属于服务端配置或数据问题，不能当成未配置。"""


class ModelCredentialConfigurationError(RuntimeError):
    """后端加密主密钥缺失或格式错误，不向普通用户暴露具体配置。"""


class ExperimentNotFoundError(ValueError):
    """实验或分页游标不存在或不属于当前用户，统一转换为 404。"""


class ExperimentStorageError(RuntimeError):
    """实验持久化状态异常；接口只提示稍后重试，具体位置保存在日志。"""


class RunNotFoundError(ValueError):
    """运行或分页游标不存在或不属于当前用户，统一返回 404。"""


class RunRequestConflictError(ValueError):
    """本人同一创建请求编号已被不同实验或参数使用，返回 409。"""


class RunStorageError(RuntimeError):
    """运行快照或存储状态异常，前端返回通用 503。"""


class ServerNotFoundError(ValueError):
    """服务器配置不存在或属于别人。"""


class ServerAddressNotAllowedError(ValueError):
    """目标 IP 不符合部署允许网段。"""


class ServerStorageError(RuntimeError):
    """服务器配置持久化异常。"""


class ServerCredentialDecryptionError(RuntimeError):
    """密文损坏、主密钥错误或凭据归属不匹配。"""


class ServerAuthenticationError(RuntimeError):
    """SSH 凭据或认证失败。"""


class ServerHostKeyError(RuntimeError):
    """SSH 主机密钥与固定指纹不匹配。"""


class ServerConnectionError(RuntimeError):
    """SSH 网络或协议连接失败。"""


class ServerConnectionTimeoutError(TimeoutError):
    """SSH 连接或认证超过预算。"""


class ServerBusyError(RuntimeError):
    """本进程 SSH 连接名额已满。"""


class RunnerConflictError(ValueError):
    """运行已有不同目标，或状态不允许首次启动。"""


class RunnerNotStartedError(ValueError):
    """本人运行还没有执行绑定。"""


class RunnerScriptNotAllowedError(ValueError):
    """脚本不在部署者维护的注册表中。"""


class RunnerProtocolError(RuntimeError):
    """远端协议或持久化启动快照不完整，不能推测训练结果。"""


class ServerInUseError(ValueError):
    """服务器已有运行绑定，不能删除并破坏恢复凭据。"""


class MetricStorageError(RuntimeError):
    """指标游标、聚合或报告完整性校验失败。"""


class MetricSourceChangedError(ValueError):
    """日志缩短或边界变化，不得自动清空指标。"""


class MetricSourceUnavailableError(RuntimeError):
    """远端日志来源暂不可确认。"""


class MetricSyncConflictError(ValueError):
    """并发采集连续推进版本，可稍后重试。"""


class ReportNotFoundError(ValueError):
    """本人运行尚未生成结果报告。"""


class ChatToolProtocolError(RuntimeError):
    """模型工具协议、轮数或总超时异常；保留当前提问用于重试。"""


class ChatToolBudgetError(ValueError):
    """完整工具请求超过输入预算。"""


class ChatToolStorageError(RuntimeError):
    """工具审计或会话关联持久化异常。"""
