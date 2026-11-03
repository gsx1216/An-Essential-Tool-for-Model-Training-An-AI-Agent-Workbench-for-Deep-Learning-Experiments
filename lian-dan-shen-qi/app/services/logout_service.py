"""退出登录业务：校验并哈希当前 Token，原子撤销其记录；HTTP 接入仍是下一阶段练习。"""

import asyncio
import re

from app.errors import InvalidTokenError
from app.logging_config import logged_operation
from app.repositories.login_session_repository import LoginSessionRepository
from app.security.tokens import TokenService


class LogoutService:
    @logged_operation("logout_service.__init__", log_success=False, expected_errors=())
    def __init__(
        self,
        session_repository: LoginSessionRepository,
        token_service: TokenService,
    ):
        self.session_repository = session_repository
        self.token_service = token_service

    @logged_operation("logout_service.logout")
    async def logout(self, access_token: str) -> None:
        """撤销当前 Token；格式错误或没有匹配到有效凭证时抛出 InvalidTokenError。"""
        # TODO 1：检查 access_token 是 str，且符合 r"[A-Za-z0-9_-]{43}"。
        # 不符合时 raise InvalidTokenError("登录已失效，请重新登录。")。
        # 参考 CurrentUserService 的写法，必须判断 fullmatch 的返回值。
        if (
                not isinstance(access_token, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", access_token) is None
        ):
            raise InvalidTokenError("登录已失效，请重新登录")
        # TODO 2：调用 self.token_service.hash_token(access_token)，用 token_hash 接收。
        # 普通短计算直接调用，不加 await。
        token_hash = self.token_service.hash_token(access_token)
        # TODO 3：await asyncio.to_thread(...) 调用仓储的 revoke_session。
        # 传函数本身 self.session_repository.revoke_session，其后传 token_hash。
        # 使用 revoked 接收返回的 bool。
        revoked = await asyncio.to_thread(
            self.session_repository.revoke_session,
            token_hash,
        )
        # TODO 4：如果 revoked 为 False，抛出与 TODO 1 相同的异常。
        # 仓储已在一条 UPDATE 中检查到期与撤销状态，不用再查询一次用户。
        # 不捕获数据库异常；失败时继续交给后续 HTTP 层转换为 503。
        if not revoked:
            raise InvalidTokenError("登录已失效，请重新登录")
        # TODO 5：成功后 return None，表示正常完成；不返回 Token 或哈希。
        # 不生成新 Token，不删除记录，不修改其他登录记录或用户信息。
        return None
