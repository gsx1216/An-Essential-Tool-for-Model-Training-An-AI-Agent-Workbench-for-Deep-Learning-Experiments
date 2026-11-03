"""已有凭证校验：检查格式、计算哈希、查询未过期且未撤销的登录记录，返回数据库确认的身份。"""

import asyncio
import re

from app.errors import InvalidTokenError
from app.logging_config import logged_operation
from app.repositories.login_session_repository import LoginSessionRepository
from app.security.tokens import TokenService


class CurrentUserService:
    @logged_operation("current_user_service.__init__", log_success=False, expected_errors=())
    def __init__(
        self,
        session_repository: LoginSessionRepository,
        token_service: TokenService,
    ):
        self.session_repository = session_repository
        self.token_service = token_service

    @logged_operation("current_user_service.get_current_user")
    async def get_current_user(self, access_token: str) -> tuple[int, str]:
        """验证已有凭证，返回数据库确认的 (用户 ID, 用户名)。"""
        # TODO 1：检查 access_token 是 str，且符合当前签发格式。
        # 当前 create_token() 生成 43 个字符，只包含字母、数字、下划线和短横线。
        # 提示：先 isinstance，再 re.fullmatch(r"[A-Za-z0-9_-]{43}", access_token)。
        # 不符合时 raise InvalidTokenError("登录已失效，请重新登录。")。
        # 不能 strip、转换大小写或接受前端另传的 user_id。
        if (
                not isinstance(access_token, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", access_token) is None
        ):
            raise InvalidTokenError("登录已失效，请重新登录")
        # TODO 2：调用 self.token_service.hash_token(...)，用 token_hash 接收。
        # 这是同步的短计算，直接调用，不加 await；不要创建新 Token。
        token_hash = self.token_service.hash_token(access_token)
        # TODO 3：await asyncio.to_thread(...)，调用仓储的 get_active_user。
        # 第一个参数是 self.session_repository.get_active_user（不加括号），
        # 第二个参数是 token_hash；使用 user_record 接收结果。
        user_record = await asyncio.to_thread(
            self.session_repository.get_active_user,
            token_hash,
        )
        # TODO 4：如果 user_record is None，抛出与 TODO 1 相同的异常。
        # SQL 已负责到期和撤销检查，不用再查用户或在这里重复比较时间。
        # 数据库异常继续抛出，不要把它捕获后当作 Token 无效。
        if user_record is None:
            raise InvalidTokenError("登录已失效，请重新登录")
        # TODO 5：返回 user_record，其中是 (用户 ID, 用户名)。
        # 不返回 Token 哈希，不重新签发 Token，也不延长原有有效期。
        return user_record
