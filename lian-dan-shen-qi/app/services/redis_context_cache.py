"""可丢弃的加密上下文缓存；同步连接池通过 to_thread 使用，故障不阻断聊天。"""

import logging
import base64
import hashlib
import hmac
from threading import Lock
import time

from cryptography.fernet import Fernet, InvalidToken
from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from redis.exceptions import RedisError

from app.models.context_cache import encode_snapshot, decode_snapshot, CacheEntryTooLarge
from app.errors import ModelCredentialConfigurationError
from app.validators import normalize_session_id, validate_positive_integer


logger = logging.getLogger(__name__)

# 20 位十进制字符串比较避免 Lua double 对 BIGINT 超过 2**53 的精度损失。
# 版本和末尾都不能倒退；删除墓碑阻止并发旧请求重新填入已删除的聊天。
STORE_SCRIPT = """
if redis.call('HGET', KEYS[1], 'deleted') == '1' then return 0 end
local version = redis.call('HGET', KEYS[1], 'version')
local tail = redis.call('HGET', KEYS[1], 'tail_seq')
if version and tail and (version > ARGV[1] or tail > ARGV[2]) then return 0 end
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[1], 'version', ARGV[1], 'tail_seq', ARGV[2], 'payload', ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""

INVALIDATE_SCRIPT = """
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[1], 'deleted', '1')
redis.call('EXPIRE', KEYS[1], ARGV[1])
return 1
"""

REMOVE_INVALID_SCRIPT = """
if redis.call('HGET', KEYS[1], 'payload') == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisContextCache:
    def __init__(self, settings, key_provider, *, client=None, clock=time.monotonic):
        self.settings = settings
        self.key_provider = key_provider
        self._client = client
        self._clock = clock
        self._blocked_until = 0.0
        self._lock = Lock()

    def _key(self, user_id, session_id):
        user_id = validate_positive_integer(user_id, "用户 ID")
        session_id = normalize_session_id(session_id)
        return f"{self.settings.prefix}:chat-context:v1:{user_id}:{session_id}"

    def _ready(self):
        with self._lock:
            return self.settings.enabled and self._clock() >= self._blocked_until

    def _connection(self):
        with self._lock:
            if self._client is None:
                self._client = Redis.from_url(
                    self.settings.redis_url, decode_responses=False,
                    socket_connect_timeout=0.3, socket_timeout=0.3, max_connections=16,
                    retry=Retry(NoBackoff(), 0), health_check_interval=30,
                )
            return self._client

    def _failure(self, operation, error):
        with self._lock:
            first = self._clock() >= self._blocked_until
            self._blocked_until = self._clock() + self.settings.failure_backoff_seconds
        if first:
            # 不记录异常正文/连接地址/聊天内容/Token/Key；认证异常也统一降级。
            logger.warning("cache_bypassed", extra={"operation": "context_cache." + operation,
                           "error_type": type(error).__name__, "fallback": "mysql"})

    def _cipher(self):
        key = self.key_provider()
        raw = key.encode("ascii") if isinstance(key, str) else key
        # 与凭据加密分离用途；不新增或自动写入任何部署密钥。
        derived = hmac.new(base64.urlsafe_b64decode(raw), b"chat-context-cache:v1", hashlib.sha256).digest()
        return Fernet(base64.urlsafe_b64encode(derived))

    def load(self, user_id, session_id):
        key = self._key(user_id, session_id)
        if not self._ready():
            return None
        try:
            fields = self._connection().hgetall(key)
            if not fields or fields == {b"deleted": b"1"}:
                logger.info("cache_miss", extra={"user_id": user_id, "session_id": normalize_session_id(session_id)})
                return None
            token = fields.get(b"payload", b"")
            if set(fields) != {b"version", b"tail_seq", b"payload"} or len(token) > self.settings.max_entry_bytes:
                raise ValueError("缓存结构或大小异常。")
            raw = self._cipher().decrypt(token, ttl=self.settings.ttl_seconds)
            snapshot = decode_snapshot(raw, user_id, session_id, self.settings.max_entry_bytes)
            if (fields[b"version"] != f"{snapshot.version:020d}".encode("ascii")
                or fields[b"tail_seq"] != f"{snapshot.tail_seq:020d}".encode("ascii")):
                raise ValueError("缓存元数据与正文不一致。")
            # 这里只表示格式命中；权限和是否最新仍须在 MySQL 短事务中验证。
            logger.info("cache_hit", extra={"user_id": user_id, "session_id": normalize_session_id(session_id),
                        "tail_seq": snapshot.tail_seq, "summary_version": snapshot.version})
            return snapshot
        except (InvalidToken, ValueError, TypeError, UnicodeError, RecursionError, ModelCredentialConfigurationError) as error:
            try:
                self._connection().eval(REMOVE_INVALID_SCRIPT, 1, key, locals().get("token", b""))
            except RedisError:
                pass
            self._failure("read_invalid", error)
        except RedisError as error:
            self._failure("read", error)
        return None

    def store(self, user_id, session_id, snapshot):
        key = self._key(user_id, session_id)
        if not self._ready():
            return False
        try:
            raw = encode_snapshot(user_id, session_id, snapshot, self.settings.max_entry_bytes)
            token = self._cipher().encrypt(raw)
            if len(token) > self.settings.max_entry_bytes:
                logger.info("cache_skipped", extra={"user_id": user_id, "session_id": normalize_session_id(session_id)})
                return False
            return bool(self._connection().eval(STORE_SCRIPT, 1, key,
                f"{snapshot.version:020d}", f"{snapshot.tail_seq:020d}", token, self.settings.ttl_seconds))
        except CacheEntryTooLarge:
            logger.info("cache_skipped", extra={"user_id": user_id, "session_id": normalize_session_id(session_id)})
        except (InvalidToken, ValueError, TypeError, UnicodeError, RecursionError, ModelCredentialConfigurationError) as error:
            self._failure("write_invalid", error)
        except RedisError as error:
            self._failure("write", error)
        return False

    def invalidate(self, user_id, session_id):
        key = self._key(user_id, session_id)
        if not self.settings.enabled:
            return False
        # 删除即使处于熔断期也尝试一次，尽快清理正文；失败仍由 TTL 和 MySQL 归属拦截兜底。
        try:
            return bool(self._connection().eval(INVALIDATE_SCRIPT, 1, key, self.settings.ttl_seconds))
        except RedisError as error:
            self._failure("delete", error)
            return False

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
