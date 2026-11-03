"""已有本机 MySQL 的隔离临时库联调；只使用假摘要模型，不触碰项目现有数据。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
from threading import Barrier
from uuid import uuid4

from dotenv import load_dotenv
from mysql.connector import Error as MySQLError

from app.config import get_mysql_password
from app.database import get_connection
from app.errors import SessionNotFoundError
from app.models.context_state import ContextConflictError, ContextSummary, HistoryGap
from app.repositories.context_repository import ContextRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.services.context_manager import ContextManager
from app.services.context_service import ContextService
from scripts.check_context_manager import FakeSummaryGenerator, FakeTokenCounter


ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / "config" / ".env", override=False, interpolate=False)
    password = get_mysql_password()
    name = "_codex_context_storage_" + uuid4().hex
    assert re.fullmatch(r"_codex_context_storage_[0-9a-f]{32}", name)
    created = False
    groups = []

    def sql(filename):
        return (ROOT / "sql" / filename).read_text(encoding="utf-8").replace("experiment_assistant", name)

    def rewrite(repository):
        for attribute, value in vars(repository).copy().items():
            if attribute.endswith("_sql") and isinstance(value, str):
                setattr(repository, attribute, value.replace("experiment_assistant", name))
        return repository

    def expect(error_type, function, *args, **kwargs):
        try:
            function(*args, **kwargs)
        except error_type:
            return
        raise AssertionError(f"Expected {error_type.__name__}")

    def count(table, session_id):
        assert table in {"chat_context_summaries", "chat_sessions", "chat_messages", "chat_turns"}
        column = "id" if table == "chat_sessions" else "session_id"
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM `{name}`.`{table}` WHERE `{column}` = %s", (session_id,))
                return cursor.fetchone()[0]

    try:
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                created = True
                cursor.execute(sql("init.sql"))
                while cursor.nextset():
                    pass
                cursor.execute(sql("create_chat_turns.sql"))
                cursor.execute(sql("create_context_summaries.sql"))
                cursor.execute(sql("create_context_summaries.sql"))
            connection.commit()

        users = rewrite(UserRepository(password))
        sessions = rewrite(SessionRepository(password))
        messages = rewrite(MessageRepository(password))
        contexts = rewrite(ContextRepository(password))
        alice = users.create_user("context_alice")
        bob = users.create_user("context_bob")

        def conversation(owner, turns=3, long=False):
            sid = sessions.create_session(owner, "temporary context verification")
            for number in range(turns):
                question = f"历史问题{number}：" + ("a" * 95 if long else "问题")
                reply = f"历史回复{number}：" + ("b" * 95 if long else "回复")
                turn_id = messages.start_turn(owner, sid, question)
                attempt_id, _ = messages.claim_turn(owner, sid, turn_id)
                messages.complete_turn(owner, sid, turn_id, attempt_id, reply)
            pending = messages.start_turn(owner, sid, "当前问题")
            return sid, pending

        target, _ = conversation(alice)
        own_other, _ = conversation(alice)
        bob_sid, _ = conversation(bob)
        original_raw = messages.load_messages(alice, target)
        first = contexts.load_context(alice, target)
        assert first.summary is None and first.version == 0 and first.tail_seq == 7
        assert [m.seq for m in first.messages] == list(range(1, 8))
        contexts.save_summary(alice, target, ContextSummary("first persisted summary", 2, 1),
                              expected_version=0, expected_tail_seq=7)
        restored = rewrite(ContextRepository(password)).load_context(alice, target)
        assert restored.summary == ContextSummary("first persisted summary", 2, 1)
        assert [m.seq for m in restored.messages] == [3, 4, 5, 6, 7]
        contexts.save_summary(alice, target, ContextSummary("incremental persisted summary", 4, 2),
                              expected_version=1, expected_tail_seq=7)
        assert contexts.load_context(alice, target).version == 2
        assert messages.load_messages(alice, target) == original_raw
        groups.append("first insert, restart restore, incremental update, source history preserved")

        expect(SessionNotFoundError, contexts.load_context, bob, target)
        expect(SessionNotFoundError, contexts.save_summary, bob, target, ContextSummary("forbidden", 4, 2),
               expected_version=1, expected_tail_seq=7)
        assert contexts.load_context(alice, target).summary.content == "incremental persisted summary"
        assert contexts.load_context(bob, bob_sid).version == 0
        groups.append("owner isolation and generic missing/unowned errors")

        expect(ContextConflictError, contexts.save_summary, alice, target, ContextSummary("stale", 4, 2),
               expected_version=1, expected_tail_seq=7)
        changed, pending = conversation(alice)
        snapshot = contexts.load_context(alice, changed)
        attempt_id, _ = messages.claim_turn(alice, changed, pending)
        messages.complete_turn(alice, changed, pending, attempt_id, "new reply changes tail")
        expect(ContextConflictError, contexts.save_summary, alice, changed, ContextSummary("stale tail", 2, 1),
               expected_version=snapshot.version, expected_tail_seq=snapshot.tail_seq)
        assert count("chat_context_summaries", changed) == 0
        groups.append("stale version and changed source-tail results rejected")

        gap_sid, _ = conversation(alice)
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM `{name}`.chat_messages WHERE session_id=%s AND seq=2", (gap_sid,))
            connection.commit()
        gap_summary = ContextSummary("source has a missing reply", 4, 1, (HistoryGap(2, 2),))
        contexts.save_summary(alice, gap_sid, gap_summary, expected_version=0, expected_tail_seq=7)
        assert contexts.load_context(alice, gap_sid).summary == gap_summary
        forged, _ = conversation(alice)
        expect(ValueError, contexts.save_summary, alice, forged,
               ContextSummary("forged gap", 4, 1, (HistoryGap(2, 2),)), expected_version=0, expected_tail_seq=7)
        assert count("chat_context_summaries", forged) == 0
        groups.append("JSON gap round trip and forged source coverage rejected")

        rollback_sid, _ = conversation(alice)
        contexts.save_summary(alice, rollback_sid, ContextSummary("before rollback", 2, 1),
                              expected_version=0, expected_tail_seq=7)
        before = contexts.load_context(alice, rollback_sid)
        original_sql = contexts.update_context_summary_sql
        try:
            contexts.update_context_summary_sql = original_sql.replace(".chat_context_summaries", "._missing_context_table")
            expect(MySQLError, contexts.save_summary, alice, rollback_sid, ContextSummary("never committed", 4, 2),
                   expected_version=1, expected_tail_seq=7)
        finally:
            contexts.update_context_summary_sql = original_sql
        assert contexts.load_context(alice, rollback_sid) == before
        groups.append("SQL failure rolls back without replacing previous summary")

        racing, _ = conversation(alice)
        barrier = Barrier(2)
        def save_racing(label):
            barrier.wait(timeout=10)
            try:
                contexts.save_summary(alice, racing, ContextSummary(label, 2, 1),
                                      expected_version=0, expected_tail_seq=7)
                return "saved"
            except ContextConflictError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(save_racing, label) for label in ("writer A", "writer B")]
            assert sorted(f.result(timeout=20) for f in futures) == ["conflict", "saved"]
        assert contexts.load_context(alice, racing).version == 1
        groups.append("concurrent first writes have exactly one winner")

        async def verify_service():
            service_sid, _ = conversation(alice, turns=5, long=True)
            original = messages.load_messages(alice, service_sid)
            generator = FakeSummaryGenerator()
            manager = ContextManager(FakeTokenCounter(), generator, system_prompt="S",
                max_input_tokens=1000, compact_at_tokens=700, max_summary_tokens=200, keep_recent_turns=2)
            result = await ContextService(contexts, manager).prepare(alice, service_sid)
            assert result.was_compacted and result.summary.summarized_until_seq == 6
            restarted_generator = FakeSummaryGenerator()
            restarted_manager = ContextManager(FakeTokenCounter(), restarted_generator, system_prompt="S",
                max_input_tokens=1000, compact_at_tokens=700, max_summary_tokens=200, keep_recent_turns=2)
            restarted = ContextService(rewrite(ContextRepository(password)), restarted_manager)
            result2 = await restarted.prepare(alice, service_sid)
            assert not result2.was_compacted and result2.summary == result.summary
            assert restarted_generator.calls == []
            assert messages.load_messages(alice, service_sid) == original
            failure_sid, _ = conversation(alice, turns=5, long=True)
            generator.error = RuntimeError("TEST_ONLY_SUMMARY_FAILURE")
            try:
                await ContextService(contexts, manager).prepare(alice, failure_sid)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Expected fake summary failure")
            assert count("chat_context_summaries", failure_sid) == 0
            assert len(messages.load_messages(alice, failure_sid)) == 11
        asyncio.run(verify_service())
        groups.append("async service saves and reuses summary after restart; failure preserves pending question")

        assert sessions.delete_session(alice, target)
        for table in ("chat_context_summaries", "chat_sessions", "chat_messages", "chat_turns"):
            assert count(table, target) == 0
        expect(SessionNotFoundError, contexts.save_summary, alice, target, ContextSummary("late result", 6, 3),
               expected_version=2, expected_tail_seq=7)
        assert len(messages.load_messages(alice, own_other)) == 7
        assert len(messages.load_messages(bob, bob_sid)) == 7
        groups.append("session deletion cascades to summary and rejects late resurrection; other chats preserved")

        deleting, _ = conversation(alice)
        barrier = Barrier(2)
        def delete_racing():
            barrier.wait(timeout=10)
            return sessions.delete_session(alice, deleting)
        def write_while_deleting():
            barrier.wait(timeout=10)
            try:
                contexts.save_summary(alice, deleting, ContextSummary("racing delete", 2, 1),
                                      expected_version=0, expected_tail_seq=7)
                return "saved_before_delete"
            except SessionNotFoundError:
                return "rejected_after_delete"
        with ThreadPoolExecutor(max_workers=2) as pool:
            delete_future = pool.submit(delete_racing)
            save_future = pool.submit(write_while_deleting)
            assert delete_future.result(timeout=20)
            assert save_future.result(timeout=20) in {"saved_before_delete", "rejected_after_delete"}
        assert count("chat_context_summaries", deleting) == 0
        groups.append("delete/save race leaves no summary or revived conversation")

        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql("create_context_summaries.sql"))
            connection.commit()
        assert contexts.load_context(alice, racing).version == 1
        groups.append("repeat migration preserves existing summaries")
        for group in groups:
            print("PASS:", group)
        print(f"PASS: {len(groups)} real MySQL verification groups; fake models only.")
    finally:
        if created:
            assert re.fullmatch(r"_codex_context_storage_[0-9a-f]{32}", name)
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{name}`")
            print("Temporary context verification database removed.")


if __name__ == "__main__":
    main()
