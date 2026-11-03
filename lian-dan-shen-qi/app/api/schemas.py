"""HTTP 数据结构：校验请求字段类型、声明公开响应；SecretStr 遮蔽展示，不代替密码哈希。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr, Field
from app.models.skill import SkillId


class RegisterRequest(BaseModel):
    # TODO 1：声明 username 字段，类型为 str
    username: str
    # TODO 2：声明 password 字段，类型为 SecretStr
    password: SecretStr


class UserResponse(BaseModel):
    # TODO 3：声明 user_id 字段，类型为 int
    user_id: int
    # TODO 4：声明 username 字段，类型为 str
    username: str


class LoginRequest(BaseModel):
    username: str
    password: SecretStr


class LoginResponse(BaseModel):
    """登录成功后返回的数据。"""

    # TODO 1：access_token，字符串，保存登录凭证
    access_token: str
    # TODO 2：token_type，字符串，默认值为 "bearer"
    token_type: str = "bearer"
    # TODO 3：expires_in，整数，表示多少秒后过期
    expires_in: int
    # TODO 4：user，类型为已有的 UserResponse
    user: UserResponse


class CreateSessionRequest(BaseModel):
    """创建聊天会话的输入；所属用户由身份依赖决定。"""
    # 拒绝未声明的字段，例如请求正文中的 user_id。
    model_config = ConfigDict(extra="forbid")

    # TODO 4：声明 title，类型为 str，默认值为“新聊天”。
    # 标题的空白与长度由已完成的业务服务继续校验。
    title: str = "新聊天"


class SessionCreatedResponse(BaseModel):
    """创建成功时返回服务端生成的会话 ID。"""
    # TODO 5：声明 session_id，类型为 str。
    session_id: str


class SessionResponse(BaseModel):
    """会话列表中的一项，字段对应仓储返回的 (id, title)。"""
    # TODO 6：声明 session_id，类型为 str。
    session_id: str
    # TODO 7：声明 title，类型为 str。
    title: str


class HistoryMessageResponse(BaseModel):
    """历史消息列表中的一项；各字段来自数据库，不重新编号或改写正文。"""
    # TODO 4：声明 seq，类型为 int，表示会话内的消息序号。
    seq: int
    # TODO 5：声明 role，类型为 str，例如 human、ai、system。
    role: str
    # TODO 6：声明 content，类型为 str，保存原始消息内容。
    # 这些字段都必填，不设置默认值；不要把消息正文存到日志中。
    content: str


class ModelCredentialRequest(BaseModel):
    """保存当前用户个人Key；所属用户来自Token，供应商由接口路径固定。"""
    model_config = ConfigDict(extra="forbid")

    # TODO 1：声明 api_key，类型为 SecretStr，必填、不设置默认值。
    # SecretStr仅遮蔽显示；实际加密由业务层调用ApiKeyCipher完成。
    api_key: SecretStr


class ModelCredentialStatusResponse(BaseModel):
    """公开配置状态，只包含供应商和是否配置，不返回原始Key或密文。"""
    # TODO 2：声明 provider，类型str，默认值"deepseek"。
    # TODO 3：声明 configured，类型bool，必填、不设置默认值。
    provider: str = "deepseek"
    configured: bool


class SendMessageRequest(BaseModel):
    """发送新问题；用户身份来自Token，会话来自路径，不接收浏览器历史或Key。"""
    model_config = ConfigDict(extra="forbid")

    # TODO 1：声明content，类型str，必填，不设置默认值。
    # 内容长度与空白检查由现有业务校验；本模型先校验字段结构和类型。
    content:str
    # TODO 2：声明request_id，类型UUID，必填，不设置默认值。
    # 调用方在首次提交前生成一个请求UUID；同一问题因网络问题重发时复用它。
    # 不要在这里设置uuid4默认值，否则重发可能变成另一个新请求。
    request_id:UUID
    # 每轮手动选择；省略或null表示普通聊天，重试沿用原轮次快照。
    skill_id:SkillId | None=Field(default=None,description='可选，从GET /skills选择真实ID；本轮不选可省略或传null。同一request_id不能改变选择。',examples=['cod-experiment-analysis'])

class ChatReplyResponse(BaseModel):
    """发送和重试共用的公开结果，只返回已保存的回复内容。"""
    # TODO 3：声明content，类型str，必填，不设置默认值。
    content: str
