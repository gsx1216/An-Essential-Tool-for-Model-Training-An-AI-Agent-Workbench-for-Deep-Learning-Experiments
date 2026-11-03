"""Skill 的用户隔离、永久缓存和删除；不访问真实模型或服务器。"""
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_skill_catalog
from app.api.schemas import UserResponse
from app.api.skill_routes import router
from app.models.skill import SkillMetadata, SkillSnapshot, SkillNotFound
from app.repositories.personal_skill_repository import PersonalSkillRepository
from app.services.redis_skill_cache import RedisSkillCache
from app.services.skill_catalog import SkillCatalog

ID = 'personal-' + 'a'*32


def snapshot():
    return SkillSnapshot.freeze(SkillMetadata(name=ID, title='个人流程', description='检查来源', version='1'), '先读取真实代码。')


class CacheTests(unittest.TestCase):
    def test_database_is_one_even_when_url_query_selects_another_database(self):
        cache = RedisSkillCache('redis://localhost:6379/7?db=9')
        self.addCleanup(cache.client.close)
        self.assertEqual(cache.client.connection_pool.connection_kwargs['db'], 1)

    def test_per_user_keys_and_no_expiration(self):
        client = MagicMock()
        cache = RedisSkillCache('redis://localhost', client=client)
        item = snapshot()
        cache.store(7, item)
        client.set.assert_called_once_with(cache.key(7, ID), item.model_dump_json())
        self.assertNotEqual(cache.key(7, ID), cache.key(8, ID))
        self.assertEqual(client.set.call_args.kwargs, {})
        cache.delete(7, ID)
        client.delete.assert_called_once_with(cache.key(7, ID))

    def test_corrupt_or_different_skill_cache_is_not_used(self):
        client = MagicMock()
        cache = RedisSkillCache('redis://localhost', client=client)
        client.get.return_value = snapshot().model_dump_json()
        self.assertIsNone(cache.load(7, ID, '0'*64))
        client.get.return_value = '{broken'
        self.assertIsNone(cache.load(7, ID, snapshot().sha256))

    def test_redis_failure_falls_back_without_exposing_credentials(self):
        client = MagicMock()
        client.get.side_effect = ConnectionError('PRIVATE_PASSWORD')
        cache = RedisSkillCache('redis://localhost', client=client)
        with self.assertLogs('app.services.redis_skill_cache', level='WARNING') as captured:
            self.assertIsNone(cache.load(7, ID, snapshot().sha256))
        self.assertNotIn('PRIVATE_PASSWORD', '\n'.join(captured.output))


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cache = MagicMock()
        self.repo = PersonalSkillRepository('TEST_ONLY', self.cache)

    def test_cache_hit_still_checks_current_ownership_without_loading_body(self):
        item = snapshot()
        self.cursor.fetchone.return_value = (item.sha256,)
        self.cache.load.return_value = item
        with patch('app.repositories.personal_skill_repository.get_connection', return_value=self.connection):
            self.assertEqual(self.repo.get(7, ID), item)
        self.cursor.execute.assert_called_once_with(self.repo.get_personal_skill_identity_sql, (7, ID))
        self.assertIn('FOR SHARE', self.repo.get_personal_skill_identity_sql)
        self.connection.commit.assert_called_once()

    def test_missing_record_never_uses_even_an_existing_cache(self):
        self.cursor.fetchone.return_value = None
        with patch('app.repositories.personal_skill_repository.get_connection', return_value=self.connection), self.assertRaises(SkillNotFound):
            self.repo.get(8, ID)
        self.cache.load.assert_not_called()

    def test_cache_miss_loads_snapshot_and_refills_before_unlock(self):
        item = snapshot()
        self.cursor.fetchone.side_effect = [(item.sha256,), (item.model_dump_json(),)]
        self.cache.load.return_value = None
        self.cache.store.side_effect = lambda *_: self.connection.commit.assert_not_called()
        with patch('app.repositories.personal_skill_repository.get_connection', return_value=self.connection):
            self.assertEqual(self.repo.get(7, ID), item)
        self.cache.store.assert_called_once_with(7, item)
        self.connection.commit.assert_called_once()

    def test_delete_commits_before_removing_only_this_users_cache(self):
        self.cursor.rowcount = 1
        self.cache.delete.side_effect = lambda *_: self.connection.commit.assert_called_once()
        with patch('app.repositories.personal_skill_repository.get_connection', return_value=self.connection):
            self.repo.delete(7, ID)
        self.cursor.execute.assert_called_once_with(self.repo.delete_personal_skill_sql, (7, ID))
        self.cache.delete.assert_called_once_with(7, ID)

    def test_failed_delete_does_not_remove_cache(self):
        self.cursor.execute.side_effect = RuntimeError('DB failure')
        with patch('app.repositories.personal_skill_repository.get_connection', return_value=self.connection), self.assertRaises(RuntimeError):
            self.repo.delete(7, ID)
        self.connection.rollback.assert_called_once()
        self.cache.delete.assert_not_called()


class DeleteApiTests(unittest.TestCase):
    def test_login_required_identity_from_token_and_public_templates_not_deleted(self):
        repo = MagicMock()
        with TemporaryDirectory() as root:
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_skill_catalog] = lambda: SkillCatalog(root, repo)
            with TestClient(app) as client:
                self.assertEqual(client.delete('/skills/' + ID).status_code, 401)
                app.dependency_overrides[require_current_user] = lambda: UserResponse(user_id=7, username='owner')
                result = client.delete('/skills/' + ID)
                self.assertEqual(result.status_code, 204)
                self.assertEqual(result.content, b'')
                repo.delete.assert_called_once_with(7, ID)
                self.assertEqual(client.delete('/skills/public-analysis').status_code, 404)
                self.assertEqual(repo.delete.call_count, 1)
