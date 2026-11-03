"""服务器输入及安全响应；凭据不出现在公开详情或列表中。"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.models.server_connection import AuthMethod


class CreateServerRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    host: Annotated[str, Field(strict=True, min_length=1, max_length=45)]
    port: Annotated[int, Field(strict=True, ge=1, le=65535)] = 22
    username: Annotated[str, Field(strict=True, min_length=1, max_length=64)]
    auth_method: AuthMethod = 'private_key'
    host_fingerprint: Annotated[str, Field(strict=True, max_length=64)]
    work_dir: Annotated[str, Field(strict=True, max_length=1024)]
    password: SecretStr | None = Field(default=None, max_length=4096)
    private_key: SecretStr | None = Field(default=None, max_length=32768)
    passphrase: SecretStr | None = Field(default=None, max_length=4096)


class ServerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    server_id: str
    name: str
    host: str
    port: int
    username: str
    auth_method: AuthMethod
    host_fingerprint: str
    work_dir: str
    created_at: datetime


class ServerListResponse(BaseModel):
    items: list[ServerResponse]
    next_cursor: str | None


class ServerTestResponse(BaseModel):
    server_id: str
    connected: bool
    auth_method: AuthMethod
    fingerprint_verified: bool
    checked_at: datetime
    duration_ms: float
