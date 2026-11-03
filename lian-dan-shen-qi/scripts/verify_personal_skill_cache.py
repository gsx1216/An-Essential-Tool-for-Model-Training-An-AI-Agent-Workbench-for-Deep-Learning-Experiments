"""临时 MySQL + 真实 Redis 1 号库验证个人 Skill；仅清理本脚本创建的随机键。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import get_mysql_password, get_skill_cache_url
from app.database import get_connection
from app.models.skill import SkillNotFound
from app.repositories.personal_skill_repository import PersonalSkillRepository
from app.services.redis_skill_cache import RedisSkillCache
from scripts._chat_api_test_app import temporary_chat_api


def verify():
    password = get_mysql_password()
    cache = RedisSkillCache(get_skill_cache_url())
    keys = []
    try:
        assert cache.client.ping()
        assert cache.client.connection_pool.connection_kwargs['db'] == 1
        with temporary_chat_api(password, skills=True) as (app, state), TestClient(app) as client:
            name = state['database_name']
            root = Path(__file__).resolve().parents[1]
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute((root/'sql/create_personal_skills.sql').read_text().replace('experiment_assistant', name))
                connection.commit()
            repo = PersonalSkillRepository(password, cache)
            for attr, value in vars(repo).copy().items():
                if attr.endswith('_sql'):
                    setattr(repo, attr, value.replace('experiment_assistant', name))
            state['skill_catalog'].personal_repository = repo

            def account(prefix):
                credentials = {'username': prefix+uuid4().hex[:8], 'password': 'TestPass123!'}
                assert client.post('/auth/register', json=credentials).status_code == 201
                data = client.post('/auth/login', json=credentials).json()
                return data['user']['user_id'], {'Authorization': 'Bearer '+data['access_token']}

            uid, owner = account('skill_owner_')
            other_uid, other = account('skill_other_')
            response = client.post('/skills', headers=owner, json={'title': '个人验证流程', 'instructions': '先核对真实证据。'})
            assert response.status_code == 201, response.text
            item = response.json()
            sid = item['skill_id']
            key = cache.key(uid, sid)
            keys.append(key)
            assert client.get('/skills/'+sid, headers=owner).status_code == 200
            assert cache.client.exists(key) == 1 and cache.client.ttl(key) == -1
            assert client.get('/skills/'+sid, headers=other).status_code == 404
            assert not cache.client.exists(cache.key(other_uid, sid))
            assert client.delete('/skills/'+sid, headers=other).status_code == 404
            assert cache.client.exists(key) == 1
            assert all('instructions' not in row for row in client.get('/skills', headers=owner).json()['items'])
            assert all(row['skill_id'] != sid for row in client.get('/skills', headers=other).json()['items'])
            print('PASS: Redis DB1, no TTL, owner-only reads/deletes, metadata-only listing', flush=True)

            session_id = client.post('/sessions', headers=owner, json={'title': 'Skill 快照验证'}).json()['session_id']
            chosen = repo.get(uid, sid)
            turn_id = state['messages'].start_turn(uid, session_id, '待回答的问题', uuid4().hex, skill_snapshot=chosen)
            # 在共享读锁内暂停回填；并发删除必须等读事务结束再删除缓存。
            cache.client.delete(key)
            entered, release = Event(), Event()
            original_store = cache.store

            def paused_store(*args):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('verification barrier')
                original_store(*args)

            cache.store = paused_store
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    reading = pool.submit(repo.get, uid, sid)
                    assert entered.wait(5)
                    deleting = pool.submit(repo.delete, uid, sid)
                    release.set()
                    assert reading.result(timeout=10) == chosen
                    deleting.result(timeout=10)
            finally:
                release.set()
                cache.store = original_store
            assert not cache.client.exists(key)
            assert client.get('/skills/'+sid, headers=owner).status_code == 404
            saved = client.get(f'/sessions/{session_id}/turns/{turn_id}/skill', headers=owner)
            assert saved.status_code == 200 and saved.json()['skill']['sha256'] == chosen.sha256
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'SELECT COUNT(*) FROM `{name}`.personal_skills WHERE user_id=%s AND skill_id=%s', (uid, sid))
                    assert cursor.fetchone()[0] == 0
            print('PASS: concurrent delete cannot resurrect permanent cache; DB record removed; old turn snapshot preserved', flush=True)

            # 即使存在陈旧正文，已删除记录也不能被缓存放行。
            original_store(uid, chosen)
            try:
                repo.get(uid, sid)
                raise AssertionError('deleted Skill was returned')
            except SkillNotFound:
                pass
            assert client.delete('/skills/'+sid, headers=owner).status_code == 404
            assert not cache.client.exists(key)
            print('PASS: stale cache cannot bypass deletion; repeated delete cleans only own key', flush=True)
        print('ALL PASS: temporary database removed; no real model, SSH or training calls', flush=True)
    finally:
        if keys:
            cache.client.delete(*keys)
        cache.client.close()


if __name__ == '__main__':
    verify()
