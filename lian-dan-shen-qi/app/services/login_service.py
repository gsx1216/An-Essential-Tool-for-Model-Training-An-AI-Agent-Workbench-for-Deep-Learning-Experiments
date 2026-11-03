"""登录业务：验证账号密码，保存登录记录，返回新签发的凭证。"""

import asyncio

from app.logging_config import logged_operation
from app.errors import InvalidCredentialsError
from app.models.login_result import LoginResult
from app.repositories.login_session_repository import LoginSessionRepository
from app.security.tokens import TokenService
from app.services.auth_service import AuthService


class LoginService:
    @logged_operation("login_service.__init__", log_success=False, expected_errors=())
    def __init__(
            self,
            auth_service: AuthService,
            session_repository: LoginSessionRepository,
            token_service: TokenService,
    ):
        self.auth_service = auth_service
        self.session_repository = session_repository
        self.token_service = token_service

    @logged_operation("login_service.login")
    async def login(self, username: str, password: str) -> LoginResult:
        """验证身份、签发并保存凭证，保存成功后才返回登录结果。"""
        # TODO 1：await 调用 self.auth_service.authenticate(username, password)。
        # 用 user_id、normalized_username 接收返回的两个值。
        try:
            authenticated_user = await self.auth_service.authenticate(username, password)
        except ValueError as error:
            # 只把账号密码核对阶段的拒绝标为 401；后面的凭证计算和保存错误继续抛出。
            raise InvalidCredentialsError("用户名或密码错误。") from error
        user_id, normalized_username = authenticated_user
        # TODO 2：调用 self.token_service.create_token()，用 access_token 接收。
        # 这是普通同步方法，直接调用即可。
        access_token = self.token_service.create_token()
        # TODO 3：调用 self.token_service.hash_token(...)。
        # 参数为刚生成的 access_token，用 token_hash 接收结果。
        token_hash = self.token_service.hash_token(access_token)
        # TODO 4：调用 self.token_service.get_expires_at()，用 expires_at 接收。
        expires_at = self.token_service.get_expires_at()
        # TODO 5：await asyncio.to_thread(...) 调用 self.session_repository.save_session。
        # 传函数本身，不加括号；其后依次传 user_id、token_hash、expires_at。
        # 仓储提交成功后，这一步才会返回；异常继续交给上层处理。
        login_session_id = await asyncio.to_thread(
            self.session_repository.save_session,
            user_id,
            token_hash,
            expires_at,
        )
        # TODO 6：返回 LoginResult(...)，全部使用关键字参数。
        # access_token = 原始凭证；expires_in = self.token_service.ttl_seconds；
        # user_id = 已验证的用户 ID；username = normalized_username。
        # token_type 已有默认值 "bearer"，可以不传。
        return LoginResult(
            access_token=access_token,
            expires_in=self.token_service.ttl_seconds,
            user_id=user_id,
            username=normalized_username,
        )
        # 每轮的用户和凭证用局部变量保存，避免缓存复用服务时串到其他用户。
