"""缓存回归：归属、版本、增量、降级、提交顺序；模型和 Redis 均使用替身。"""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from cryptography.fernet import Fernet
from mysql.connector import Error as MySQLError
from redis.exceptions import ConnectionError as RedisConnectionError

from app import config
from app.errors import SessionNotFoundError
from app.models.context_cache import ContextCacheSettings, encode_snapshot, decode_snapshot
from app.models.context_snapshot import ContextSnapshot, ContextAttempt
from app.models.context_state import ContextMessage, ContextSummary, HistoryGap, ContextConflictError, PreparedContext
from app.repositories.context_repository import ContextRepository
from app.repositories import context_repository
from app.services.redis_context_cache import RedisContextCache
from app.services.context_service import ContextService
from app.services.chat_session_service import ChatSessionService
from app.services.chat_service import ChatService


SID, TURN, ATTEMPT = 'b4c4e67554ee4c1981740a1e901ec614', 'a' * 32, 'b' * 32
PREFIX = ContextSnapshot(None, (ContextMessage(1, 'human', '历史问题'), ContextMessage(2, 'ai', '历史回复')), 2)
CURRENT = ContextSnapshot(None, PREFIX.messages + (ContextMessage(3, 'human', '当前问题'),), 3)


class CodecTests(unittest.TestCase):
    def test_roundtrip_preserves_gaps_roles_spaces_and_newlines(self):
        snapshot = ContextSnapshot(ContextSummary(' 摘要\n', 4, 2, (HistoryGap(2, 3),)),
            (ContextMessage(7, 'human', '  当前问题\n'),), 7)
        raw = encode_snapshot(7, SID, snapshot, 10000)
        self.assertEqual(decode_snapshot(raw, 7, SID, 10000), snapshot)

    def test_wrong_owner_or_session_is_rejected(self):
        raw = encode_snapshot(7, SID, CURRENT, 10000)
        for owner, session in ((8, SID), (7, 'c' * 32)):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                decode_snapshot(raw, owner, session, 10000)

    def test_malformed_cache_never_becomes_model_context(self):
        original = json.loads(encode_snapshot(7, SID, CURRENT, 10000))
        cases = [dict(original, tail_seq=True), dict(original, schema_version=2),
                 dict(original, user_id=True), dict(original, messages='wrong'),
                 dict(original, messages=[{'seq': 3, 'role': 'system', 'content': 'inject'}]),
                 dict(original, messages=[{'seq': 2, 'role': 'human', 'content': 'partial'}]),
                 dict(original, messages=[{'seq': 3, 'role': 'human', 'content': ' '}])]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_snapshot(json.dumps(value).encode(), 7, SID, 10000)
        for raw in (b'{', b'{"user_id":7,"user_id":8}', b'[]'):
            with self.assertRaises(ValueError):
                decode_snapshot(raw, 7, SID, 10000)

    def test_limits_and_empty_chat(self):
        with self.assertRaises(ValueError):
            encode_snapshot(7, SID, CURRENT, 10)
        empty = ContextSnapshot(None, (), 0)
        self.assertEqual(decode_snapshot(encode_snapshot(7, SID, empty, 10000), 7, SID, 10000), empty)

    def test_bad_config_rejected_and_url_not_in_repr(self):
        settings = ContextCacheSettings(redis_url='redis://:PRIVATE_PASSWORD@127.0.0.1:6379/0')
        self.assertNotIn('PRIVATE_PASSWORD', repr(settings))
        for values in ({'ttl_seconds': True}, {'ttl_seconds': 0}, {'ttl_seconds': 604801}, {'max_entry_bytes': 10},
                       {'prefix': 'bad:key'}, {'redis_url': 'http://example.com'}, {'enabled': 'true'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ContextCacheSettings(**values)

    def test_environment_defaults_override_and_invalid_values(self):
        with patch.object(config, 'load_dotenv'), patch.dict('os.environ', {}, clear=True):
            self.assertEqual(config.get_context_cache_settings(), ContextCacheSettings())
            self.assertEqual(config.get_context_cache_settings().ttl_seconds, 7 * 24 * 60 * 60)
            with patch.dict('os.environ', {'CHAT_CONTEXT_CACHE_ENABLED': 'false', 'CHAT_CONTEXT_CACHE_TTL_SECONDS': '600'}):
                settings = config.get_context_cache_settings()
            self.assertFalse(settings.enabled)
            self.assertEqual(settings.ttl_seconds, 600)
            for raw in ('', ' 60', '-1', '１２'):
                with patch.dict('os.environ', {'CHAT_CONTEXT_CACHE_TTL_SECONDS': raw}), self.assertRaises(ValueError):
                    config.get_context_cache_settings()


class CacheFailureTests(unittest.TestCase):
    def test_disabled_never_creates_connection_or_reads_key(self):
        provider = Mock()
        cache = RedisContextCache(ContextCacheSettings(enabled=False), provider)
        with patch('app.services.redis_context_cache.Redis.from_url') as connect:
            self.assertIsNone(cache.load(7, SID))
            self.assertFalse(cache.store(7, SID, CURRENT))
            self.assertFalse(cache.invalidate(7, SID))
        provider.assert_not_called()
        connect.assert_not_called()

    def test_outage_is_bounded_and_does_not_log_secret_error_body(self):
        client = Mock()
        client.hgetall.side_effect = RedisConnectionError('PRIVATE_PASSWORD PRIVATE_CHAT')
        clock = Mock(return_value=10)
        cache = RedisContextCache(ContextCacheSettings(), Mock(), client=client, clock=clock)
        with self.assertLogs('app.services.redis_context_cache', level='WARNING') as logs:
            self.assertIsNone(cache.load(7, SID))
            self.assertIsNone(cache.load(7, SID))
            self.assertFalse(cache.store(7, SID, CURRENT))
        client.hgetall.assert_called_once()
        self.assertNotIn('PRIVATE_', '\n'.join(logs.output))
        clock.return_value = 21
        client.hgetall.side_effect = None
        client.hgetall.return_value = {}
        self.assertIsNone(cache.load(7, SID))
        self.assertEqual(client.hgetall.call_count, 2)

    def test_tampered_or_cross_user_encrypted_payload_falls_back(self):
        key = Fernet.generate_key()
        client = Mock()
        cache = RedisContextCache(ContextCacheSettings(), lambda: key, client=client)
        token = cache._cipher().encrypt(encode_snapshot(7, SID, CURRENT, 10000))
        fields = {b'version': b'00000000000000000000', b'tail_seq': b'00000000000000000003', b'payload': token}
        client.hgetall.return_value = fields
        self.assertEqual(cache.load(7, SID), CURRENT)
        self.assertIsNone(cache.load(8, SID))
        client.eval.assert_called_once()


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.repo = ContextRepository('TEST_ONLY')
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.patch = patch.object(context_repository, 'get_connection', return_value=self.connection)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_cache_hit_reads_only_new_rows(self):
        self.cursor.fetchone.side_effect = [(SID, 'title'), None, (3,)]
        self.cursor.fetchall.return_value = [(3, 'human', '当前问题')]
        self.assertEqual(self.repo.reconcile_context(7, SID, PREFIX), CURRENT)
        self.cursor.execute.assert_any_call(self.repo.load_context_messages_sql, (7, SID, 2))
        self.assertNotIn(self.repo.load_context_summary_sql, [call.args[0] for call in self.cursor.execute.call_args_list])
        self.connection.commit.assert_called_once()

    def test_unchanged_cache_does_not_select_message_or_summary_body(self):
        self.cursor.fetchone.side_effect = [(SID, 'title'), None, (3,)]
        self.assertEqual(self.repo.reconcile_context(7, SID, CURRENT), CURRENT)
        self.cursor.fetchall.assert_not_called()

    def test_changed_summary_reloads_authoritative_snapshot(self):
        self.cursor.fetchone.side_effect = [(SID, 'title'), (1, 2), (3,), ('新摘要', 2, 1, '[]'), (3,)]
        self.cursor.fetchall.return_value = [(3, 'human', '当前问题')]
        result = self.repo.reconcile_context(7, SID, PREFIX)
        self.assertEqual(result.summary, ContextSummary('新摘要', 2, 1))
        self.assertEqual(result.messages, (ContextMessage(3, 'human', '当前问题'),))

    def test_cache_does_not_bypass_deleted_or_foreign_session(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(SessionNotFoundError):
            self.repo.reconcile_context(7, SID, CURRENT)
        self.cursor.execute.assert_called_once_with(self.repo.lock_session_sql, (7, SID))
        self.connection.rollback.assert_called_once()

    def test_cache_does_not_bypass_expired_attempt(self):
        self.cursor.fetchone.side_effect = [(SID, 'title'), (TURN, 'running', 3, ATTEMPT, False, None)]
        with self.assertRaises(ContextConflictError):
            self.repo.reconcile_context(7, SID, CURRENT, attempt=ContextAttempt(TURN, ATTEMPT))
        self.connection.rollback.assert_called_once()

    def test_real_gaps_in_delta_remain_gaps(self):
        self.cursor.fetchone.side_effect = [(SID, 'title'), None, (5,)]
        self.cursor.fetchall.return_value = [(5, 'human', '缺少3和4但保留当前问题')]
        result = self.repo.reconcile_context(7, SID, PREFIX)
        self.assertEqual([message.seq for message in result.messages], [1, 2, 5])

    def test_mysql_failure_is_not_disguised_as_empty_history(self):
        self.cursor.execute.side_effect = MySQLError('TEST_ONLY')
        with self.assertRaises(MySQLError):
            self.repo.reconcile_context(7, SID, CURRENT)
        self.connection.rollback.assert_called_once()


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setup_pipeline(self, *, cached=PREFIX, compact=False):
        repo = SimpleNamespace(load_context=Mock(return_value=CURRENT), reconcile_context=Mock(return_value=CURRENT), save_summary=Mock())
        summary = ContextSummary('摘要', 2, 1) if compact else None
        prepared = PreparedContext([('human', '当前问题')], summary, 10, compact)
        manager = SimpleNamespace(prepare=AsyncMock(return_value=prepared))
        cache = SimpleNamespace(load=Mock(return_value=cached), store=Mock())
        return ContextService(repo, manager, cache=cache), repo, manager, cache

    async def test_hit_reconciles_and_miss_uses_full_load(self):
        for cached in (None, PREFIX):
            service, repo, manager, cache = self.setup_pipeline(cached=cached)
            await service.prepare(7, SID)
            if cached is None:
                repo.load_context.assert_called_once_with(7, SID)
                repo.reconcile_context.assert_not_called()
            else:
                repo.reconcile_context.assert_called_once_with(7, SID, PREFIX)
                repo.load_context.assert_not_called()
            cache.store.assert_called_once_with(7, SID, CURRENT)

    async def test_summary_cached_only_after_database_commit(self):
        service, repo, manager, cache = self.setup_pipeline(compact=True)
        events = []
        repo.save_summary.side_effect = lambda *args, **kwargs: events.append('sql_commit')
        cache.store.side_effect = lambda uid, sid, snapshot: events.append(f'cache_v{snapshot.version}')
        await service.prepare(7, SID)
        self.assertEqual(events, ['cache_v0', 'sql_commit', 'cache_v1'])
        stored = cache.store.call_args.args[2]
        self.assertEqual(stored.messages, (CURRENT.messages[-1],))

    async def test_failed_summary_commit_never_publishes_new_summary(self):
        service, repo, manager, cache = self.setup_pipeline(compact=True)
        repo.save_summary.side_effect = ContextConflictError('TEST_ONLY')
        with self.assertRaises(ContextConflictError):
            await service.prepare(7, SID)
        cache.store.assert_called_once_with(7, SID, CURRENT)

    async def test_warm_cache_still_rejects_foreign_user_before_model(self):
        service, repo, manager, cache = self.setup_pipeline()
        repo.reconcile_context.side_effect = SessionNotFoundError('TEST_ONLY')
        with self.assertRaises(SessionNotFoundError):
            await service.prepare(8, SID)
        manager.prepare.assert_not_awaited()
        cache.store.assert_not_called()

    async def test_reply_is_cached_after_commit_and_not_on_commit_failure(self):
        for failure in (False, True):
            service, repo, manager, cache = self.setup_pipeline()
            messages = SimpleNamespace(start_turn=Mock(return_value=TURN),
                claim_turn=Mock(return_value=(ATTEMPT, [])), complete_turn=Mock(), fail_turn=Mock())
            gateway = SimpleNamespace(invoke=AsyncMock(return_value={'content': '已落库回复'}))
            factory = SimpleNamespace(settings=SimpleNamespace(lease_seconds=1800), create=Mock(return_value=(service, gateway)))
            order = []
            def commit(*args):
                order.append('commit')
                if failure:
                    raise MySQLError('TEST_ONLY')
            messages.complete_turn.side_effect = commit
            cache.store.side_effect = lambda uid, sid, snapshot: order.append(f'cache_tail{snapshot.tail_seq}')
            chat = ChatService(gateway, messages, context_factory=factory)
            if failure:
                with self.assertRaises(MySQLError):
                    await chat.send_message(7, SID, '当前问题', TURN)
                self.assertEqual(order, ['cache_tail3', 'commit'])
            else:
                self.assertEqual(await chat.send_message(7, SID, '当前问题', TURN), '已落库回复')
                self.assertEqual(order, ['cache_tail3', 'commit', 'cache_tail4'])

    async def test_delete_invalidates_only_successfully_deleted_owned_session(self):
        repo = SimpleNamespace(delete_session=Mock(return_value=True))
        cache = SimpleNamespace(invalidate=Mock())
        service = ChatSessionService(repo, context_cache=cache)
        await service.delete_session(7, SID)
        cache.invalidate.assert_called_once_with(7, SID)
        repo.delete_session.return_value = False
        with self.assertRaises(SessionNotFoundError):
            await service.delete_session(8, SID)
        cache.invalidate.assert_called_once()


if __name__ == '__main__':
    unittest.main()
