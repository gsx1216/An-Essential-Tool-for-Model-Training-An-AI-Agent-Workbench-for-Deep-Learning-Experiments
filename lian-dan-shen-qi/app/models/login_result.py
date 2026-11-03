"""登录业务结果对象：由路由转换为 LoginResponse；承载数据，不承担身份校验。"""

from dataclasses import dataclass, field


@dataclass(frozen=True, kw_only=True)
class LoginResult:
    # repr=False 避免调试时打印对象就显示原始凭证；它不负责加密。
    access_token: str = field(repr=False)
    expires_in: int
    user_id: int
    username: str
    token_type: str = "bearer"
