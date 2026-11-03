"""账号业务：注册时规范用户名并生成密码哈希，登录时核对已有账号密码。"""

from app.logging_config import logged_operation

import asyncio

from app.repositories.user_repository import UserRepository
from app.security.passwords import PasswordService
from app.validators import normalize_required_text


class AuthService:
    @logged_operation("auth_service.__init__", log_success=False, expected_errors=())
    def __init__(self, repository: UserRepository, password_service: PasswordService):
        self.repository = repository
        self.password_service = password_service

    @logged_operation("auth_service.register")
    async def register(self, username: str, password: str) -> tuple[int, str]:
        """注册成功只返回用户 ID 和用户名，不返回密码或密码哈希。"""
        # TODO 1：校验并规范化用户名，最大长度 64。
        username = normalize_required_text(username, "用户名", 64)
        # TODO 2：用 to_thread 调用 hash_password，取得 password_hash。
        password_hash = await asyncio.to_thread(
            self.password_service.hash_password,
            password,
        )
        # TODO 3：用 to_thread 调用 create_registered_user，取得 user_id。
        user_id = await asyncio.to_thread(
            self.repository.create_registered_user,
            username,
            password_hash,
        )
        # TODO 4：返回 (user_id, username)。
        return user_id, username

    @logged_operation("auth_service.authenticate")
    async def authenticate(self, username: str, password: str) -> tuple[int, str]:
        """仅核对账号密码；凭证签发由 LoginService 负责，后续 Token 校验由 CurrentUserService 负责。"""
        # get_user_auth 返回 (id, username, password_hash) 或 None。
        user_record = await asyncio.to_thread(
            self.repository.get_user_auth,
            username,
        )
        # verify_password 接收明文输入和存储的哈希；不存在的用户传 None。
        if  user_record is None:
            password_hash = None
        else:
            password_hash = user_record[2]
        # 验证失败统一抛出“用户名或密码错误”，不要区分用户是否存在。
        is_valid = await asyncio.to_thread(
            self.password_service.verify_password,
            password,  # 用户输入的密码
            password_hash,  # 数据库中的哈希，或者 None
        )
        if not is_valid:
            raise ValueError("用户名或密码错误")
        # 成功只返回 (id, username)。
        return user_record[0], user_record[1]
