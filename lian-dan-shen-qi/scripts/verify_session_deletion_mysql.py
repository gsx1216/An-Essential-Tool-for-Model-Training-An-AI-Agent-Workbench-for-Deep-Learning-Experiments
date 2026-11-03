"""真实本机 MySQL 临时库验证会话服务、删除归属、回滚和迟到回复；不调用模型。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from dotenv import load_dotenv
from mysql.connector import Error as MySQLError

from app.config import get_mysql_password
from app.api.dependencies import get_chat_session_service
from app.database import get_connection
from app.errors import SessionNotFoundError
from app.repositories.login_session_repository import LoginSessionRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.security.tokens import TokenService
from app.services.chat_session_service import ChatSessionService


ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / "config" / ".env", override=False, interpolate=False)
    password = get_mysql_password()
    name = "_codex_session_delete_" + uuid4().hex
    assert re.fullmatch(r"_codex_session_delete_[0-9a-f]{32}", name)
    created = False

    def sql(filename):
        return (ROOT / "sql" / filename).read_text(encoding="utf-8").replace("experiment_assistant", name)

    def rewrite(repository):
        for attribute, value in vars(repository).copy().items():
            if attribute.endswith("_sql") and isinstance(value, str):
                setattr(repository, attribute, value.replace("experiment_assistant", name))
        return repository

    def snapshot():
        # 表名完全来自固定列表；返回真实行，检查回滚不只是检查记录数量。
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                result = []
                for table in ("users", "login_sessions", "chat_sessions", "chat_messages", "chat_turns"):
                    cursor.execute(f"SELECT * FROM `{name}`.`{table}` ORDER BY id")
                    result.append(cursor.fetchall())
                return result

    def assert_deleted(session_id):
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                for table, column in (("chat_sessions", "id"), ("chat_messages", "session_id"),
                                      ("chat_turns", "session_id")):
                    cursor.execute(f"SELECT COUNT(*) FROM `{name}`.`{table}` WHERE `{column}` = %s", (session_id,))
                    assert cursor.fetchone()[0] == 0

    def expect_value_error(function, *args):
        try:
            function(*args)
        except ValueError:
            return
        raise AssertionError("Deleted conversation accepted a stale write")

    try:
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                created = True
                cursor.execute(sql("init.sql"))
                while cursor.nextset():
                    pass
                cursor.execute(sql("create_chat_turns.sql"))
                cursor.execute(sql("create_login_sessions.sql"))
            connection.commit()

        users = rewrite(UserRepository(password))
        sessions = rewrite(SessionRepository(password))
        messages = rewrite(MessageRepository(password))
        logins = rewrite(LoginSessionRepository(password))
        tokens = TokenService()
        alice = users.create_user("delete_alice")
        bob = users.create_user("delete_bob")
        token_hash = tokens.hash_token(tokens.create_token())
        logins.save_session(alice, token_hash, tokens.get_expires_at())

        def conversation(owner, state):
            session_id = sessions.create_session(owner, "删除验证")
            turn_id = messages.start_turn(owner, session_id, "temporary question")
            attempt_id = None
            if state != "pending":
                attempt_id, _ = messages.claim_turn(owner, session_id, turn_id)
                if state == "completed":
                    messages.complete_turn(owner, session_id, turn_id, attempt_id, "temporary reply")
                elif state == "failed":
                    messages.fail_turn(owner, session_id, turn_id, attempt_id)
            return session_id, turn_id, attempt_id

        own_other, _, _ = conversation(alice, "completed")
        bob_session, _, _ = conversation(bob, "completed")
        target, _, _ = conversation(alice, "completed")
        messages.start_turn(alice, target, "second question without a reply")
        original = snapshot()
        assert sessions.delete_session(bob, target) is False
        assert sessions.delete_session(alice, bob_session) is False
        assert sessions.delete_session(2**63, target) is False
        assert sessions.delete_session(alice, uuid4().hex) is False
        assert snapshot() == original

        # 模拟删除了轮次后消息 SQL 出错、删除了子表后会话 SQL 出错。
        for attribute, table in (("delete_session_messages_sql", "chat_messages"),
                                 ("delete_session_sql", "chat_sessions")):
            original_sql = getattr(sessions, attribute)
            try:
                setattr(sessions, attribute, original_sql.replace("." + table, "._missing_" + table))
                try:
                    sessions.delete_session(alice, target)
                except MySQLError as error:
                    assert error.errno == 1146
                else:
                    raise AssertionError("Expected database failure")
            finally:
                setattr(sessions, attribute, original_sql)
            assert snapshot() == original

        assert sessions.delete_session(alice, target) is True
        assert sessions.delete_session(alice, target) is False
        assert_deleted(target)
        assert len(messages.load_messages(alice, own_other)) == 2
        assert len(messages.load_messages(bob, bob_session)) == 2

        # pending/running/failed/completed 均能删除，旧生成任务不能复活消息。
        for state in ("pending", "running", "failed", "completed"):
            session_id, turn_id, attempt_id = conversation(alice, state)
            assert sessions.delete_session(alice, session_id) is True
            expect_value_error(messages.start_turn, alice, session_id, "new late question")
            if attempt_id is not None:
                expect_value_error(messages.complete_turn, alice, session_id, turn_id, attempt_id, "late reply")
                expect_value_error(messages.fail_turn, alice, session_id, turn_id, attempt_id)
            assert_deleted(session_id)
        empty = sessions.create_session(alice)
        assert sessions.delete_session(alice, empty) is True
        assert_deleted(empty)

        # 删除和回复写回同时竞争同一会话行锁，无论谁先执行，最终都不留内容。
        racing, turn_id, attempt_id = conversation(alice, "running")
        barrier = Barrier(2)

        def delete_racing():
            barrier.wait(timeout=10)
            return sessions.delete_session(alice, racing)

        def complete_racing():
            barrier.wait(timeout=10)
            try:
                messages.complete_turn(alice, racing, turn_id, attempt_id, "racing reply")
                return "completed_before_delete"
            except ValueError:
                return "rejected_after_delete"

        with ThreadPoolExecutor(max_workers=2) as executor:
            deletion = executor.submit(delete_racing)
            completion = executor.submit(complete_racing)
            assert deletion.result(timeout=20) is True
            assert completion.result(timeout=20) in {"completed_before_delete", "rejected_after_delete"}
        assert_deleted(racing)
        assert logins.get_active_user(token_hash) == (alice, "delete_alice")
        assert users.get_user(alice) == (alice, "delete_alice")
        assert users.get_user(bob) == (bob, "delete_bob")

        async def verify_service():
            service = ChatSessionService(sessions)
            created_id = await service.create_session(alice, "  服务联调会话  ")
            default_id = await service.create_session(alice)
            assert re.fullmatch(r"[0-9a-f]{32}", created_id)
            own_sessions = dict(await service.list_sessions(alice))
            assert own_sessions[created_id] == "服务联调会话"
            assert own_sessions[default_id] == "新聊天"
            assert bob_session not in own_sessions
            assert created_id not in dict(await service.list_sessions(bob))

            for invalid_title in ("", "   ", "字" * 201):
                try:
                    await service.create_session(alice, invalid_title)
                except ValueError:
                    pass
                else:
                    raise AssertionError("Service accepted an invalid title")

            for owner, candidate in ((bob, created_id), (alice, uuid4().hex)):
                try:
                    await service.delete_session(owner, candidate)
                except SessionNotFoundError:
                    pass
                else:
                    raise AssertionError("Service accepted an unowned or absent conversation")

            messages.start_turn(alice, created_id, "service question")
            before_failure = snapshot()
            original_sql = sessions.delete_session_messages_sql
            try:
                sessions.delete_session_messages_sql = original_sql.replace(".chat_messages", "._missing_chat_messages")
                try:
                    await service.delete_session(alice, created_id)
                except MySQLError as error:
                    assert error.errno == 1146
                else:
                    raise AssertionError("Service swallowed a database failure")
            finally:
                sessions.delete_session_messages_sql = original_sql
            assert snapshot() == before_failure

            assert await service.delete_session(alice, created_id) is None
            assert_deleted(created_id)
            assert created_id not in dict(await service.list_sessions(alice))
            try:
                await service.delete_session(alice, created_id)
            except SessionNotFoundError:
                pass
            else:
                raise AssertionError("Repeated deletion must report missing conversation")
            assert await service.delete_session(alice, default_id) is None
            assert len(messages.load_messages(alice, own_other)) == 2
            assert len(messages.load_messages(bob, bob_session)) == 2
            assert logins.get_active_user(token_hash) == (alice, "delete_alice")

            # 使用用户完成的工厂组装真实服务，再把其 SQL 限定到本次临时库。
            get_chat_session_service.cache_clear()
            try:
                before_factory = snapshot()
                with patch("app.api.dependencies.get_mysql_password", return_value=password):
                    composed = get_chat_session_service()
                    assert get_chat_session_service() is composed
                assert snapshot() == before_factory
                rewrite(composed.repository)
                composed_id = await composed.create_session(alice, "工厂联调会话")
                assert dict(await composed.list_sessions(alice))[composed_id] == "工厂联调会话"
                assert composed_id not in dict(await composed.list_sessions(bob))
                assert await composed.delete_session(alice, composed_id) is None
                assert_deleted(composed_id)
            finally:
                get_chat_session_service.cache_clear()

        asyncio.run(verify_service())

        print("PASS: real MySQL deletes conversations/messages/turns, all turn states and empty chats; owner isolation and repeat deletion.")
        print("PASS: partial-delete rollback, rejected stale writes and concurrent completion; other chats, users and login credentials preserved.")
        print("PASS: student ChatSessionService create/list/delete, default/normalized titles, owner isolation, invalid titles, missing/repeated deletion and database failure propagation.")
        print("PASS: student service factory caches the correct collaborators without creating records; composed service performs real scoped CRUD.")
        print("NOTE: this script covers the service/repository; mounted HTTP endpoints are verified by verify_current_user_api_mysql.")
    finally:
        if created:
            assert re.fullmatch(r"_codex_session_delete_[0-9a-f]{32}", name)
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{name}`")
            print("Temporary session-deletion verification database removed.")


if __name__ == "__main__":
    main()
