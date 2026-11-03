"""个人 Skill 正文缓存：Redis 1 号库、用户隔离、不设过期；MySQL 保留归属和持久记录。"""
import logging
import re

from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from redis.exceptions import RedisError

from app.models.skill import SKILL_ID_PATTERN, SkillStorageError, saved_snapshot
from app.validators import validate_positive_integer

logger = logging.getLogger(__name__)


class RedisSkillCache:
    def __init__(self, redis_url, *, client=None):
        from redis.connection import parse_url
        options = parse_url(redis_url)
        # URL 路径/query 可能指定其他库；Skill 明确覆盖为 1，认证和 TLS 配置沿用。
        options.update(db=1, decode_responses=True, socket_connect_timeout=0.3,
                       socket_timeout=0.3, max_connections=16, retry=Retry(NoBackoff(), 0))
        self.client = client if client is not None else Redis(**options)

    @staticmethod
    def key(user_id, skill_id):
        validate_positive_integer(user_id, '用户 ID')
        if not isinstance(skill_id, str) or not re.fullmatch(SKILL_ID_PATTERN, skill_id) or not skill_id.startswith('personal-'):
            raise ValueError('个人 Skill 标识不符合要求。')
        return f'experiment_assistant:skill:v1:{user_id}:{skill_id}'

    @staticmethod
    def _failure(operation, error):
        # 不记录连接 URL、密码或 Skill 正文。
        logger.warning('skill_cache_bypassed', extra={'operation': 'skill_cache.' + operation,
                       'error_type': type(error).__name__, 'fallback': 'mysql'})

    def load(self, user_id, skill_id, sha256):
        key = self.key(user_id, skill_id)
        try:
            raw = self.client.get(key)
            if raw is None:
                return None
            snapshot = saved_snapshot(raw)
            if snapshot.skill_id != skill_id or snapshot.sha256 != sha256:
                raise SkillStorageError('Skill 缓存与本人持久记录不一致。')
            return snapshot
        except (RedisError, SkillStorageError) as error:
            self._failure('load', error)
            return None

    def store(self, user_id, snapshot):
        key = self.key(user_id, snapshot.skill_id)
        try:
            # SET 不带 EX/PX，且去除该键可能存在的旧 TTL。
            self.client.set(key, snapshot.model_dump_json())
        except RedisError as error:
            self._failure('store', error)

    def delete(self, user_id, skill_id):
        key = self.key(user_id, skill_id)
        try:
            self.client.delete(key)
        except RedisError as error:
            self._failure('delete', error)
