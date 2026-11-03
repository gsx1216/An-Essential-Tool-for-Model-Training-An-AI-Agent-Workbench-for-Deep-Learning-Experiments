"""在本次创建的独立临时库验证真实 SQL，绝不操作用户的业务库。"""

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from dotenv import load_dotenv
from mysql.connector import Error, IntegrityError

from app.config import get_mysql_password
from app.database import get_connection
from app.repositories.message_repository import MessageRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.services.chat_service import ChatService


ROOT = Path(__file__).resolve().parents[1]


def expect_value_error(function, *args):
    try:
        function(*args)
    except ValueError:
        return
    raise AssertionError("Expected ValueError")


def main():
    load_dotenv(ROOT / "config" / ".env", override=False, interpolate=False)
    password = get_mysql_password()
    test_database = "_codex_turn_check_" + uuid4().hex
    assert re.fullmatch(r"_codex_turn_check_[0-9a-f]{32}", test_database)
    created = False
    try:
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{test_database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                created = True
                script = (ROOT / "sql" / "init.sql").read_text(encoding="utf-8")
                cursor.execute(script.replace("experiment_assistant", test_database))
                while cursor.nextset():
                    pass
                create_turns = (ROOT / "sql" / "create_chat_turns.sql").read_text(encoding="utf-8")
                cursor.execute(create_turns.replace("experiment_assistant", test_database))
                cursor.execute(create_turns.replace("experiment_assistant", test_database))
            connection.commit()

        users = UserRepository(password)
        sessions = SessionRepository(password)
        repository = MessageRepository(password)
        for instance in (users, sessions, repository):
            for name, value in vars(instance).copy().items():
                if name.endswith("_sql") and isinstance(value, str):
                    setattr(instance, name, value.replace("experiment_assistant", test_database))

        alice = users.create_user("alice")
        bob = users.create_user("bob")
        session_id = sessions.create_session(alice)
        request_id = uuid4().hex
        turn_id = repository.start_turn(alice, session_id, "original question", request_id)
        assert repository.load_messages(alice, session_id) == [("human", "original question")]
        assert repository.get_unfinished_turn(alice, session_id).status == "pending"
        assert repository.start_turn(alice, session_id, "original question", request_id) == turn_id
        expect_value_error(repository.start_turn, alice, session_id, "different", request_id)
        expect_value_error(repository.start_turn, alice, session_id, "second question")

        # 即使绕过仓储，数据库唯一约束也拒绝同一会话两个未完成轮次。
        with get_connection(password) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(repository.save_message_sql, (session_id, 2, "human", "illegal overlapping question"))
                    cursor.execute(repository.create_turn_sql, (uuid4().hex, session_id, 2))
            except IntegrityError as error:
                assert error.errno == 1062
                connection.rollback()
            else:
                connection.rollback()
                raise AssertionError("Open-turn uniqueness was not enforced")
        assert len(repository.load_messages(alice, session_id)) == 1

        # 两个并发领取者中只能有一个真正进入生成状态。
        barrier = Barrier(2)

        def claim():
            barrier.wait(timeout=10)
            try:
                return repository.claim_turn(alice, session_id, turn_id)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: claim(), range(2)))
        winners = [value for value in results if value is not None]
        assert len(winners) == 1
        attempt_1, messages = winners[0]
        assert messages == [("human", "original question")]
        expect_value_error(repository.claim_turn, bob, session_id, turn_id)
        expect_value_error(repository.complete_turn, bob, session_id, turn_id, attempt_1, "reply")
        expect_value_error(repository.fail_turn, bob, session_id, turn_id, attempt_1)
        repository.fail_turn(alice, session_id, turn_id, attempt_1)
        assert repository.load_messages(alice, session_id) == [("human", "original question")]
        assert repository.get_unfinished_turn(alice, session_id).status == "failed"
        attempt_2, messages = repository.claim_turn(alice, session_id, turn_id)
        assert attempt_2 != attempt_1 and len(messages) == 1
        expect_value_error(repository.complete_turn, alice, session_id, turn_id, attempt_1, "stale reply")
        repository.complete_turn(alice, session_id, turn_id, attempt_2, "valid reply")
        repository.complete_turn(alice, session_id, turn_id, attempt_2, "valid reply")
        assert repository.get_completed_reply(alice, session_id, turn_id) == "valid reply"
        expect_value_error(repository.get_completed_reply, bob, session_id, turn_id)
        assert repository.load_messages(alice, session_id) == [("human", "original question"), ("ai", "valid reply")]
        assert repository.get_unfinished_turn(alice, session_id) is None
        expect_value_error(repository.fail_turn, alice, session_id, turn_id, attempt_2)

        # 模拟程序中断、租约到期：新任务可接手，旧任务不能再写回。
        next_turn = repository.start_turn(alice, session_id, "recover after crash")
        old_attempt, _ = repository.claim_turn(alice, session_id, next_turn)
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE `{test_database}`.chat_turns SET lease_until = CURRENT_TIMESTAMP(6) - INTERVAL 1 SECOND WHERE id = %s",
                    (next_turn,),
                )
            connection.commit()
        new_attempt, messages = repository.claim_turn(alice, session_id, next_turn)
        assert len(messages) == 3 and new_attempt != old_attempt
        expect_value_error(repository.complete_turn, alice, session_id, next_turn, old_attempt, "stale")
        expect_value_error(repository.fail_turn, alice, session_id, next_turn, old_attempt)

        # 回复已经 INSERT，但状态更新报错：仅撤销回复，先前提交的问题必须保留。
        with patch.object(repository, "complete_turn_sql", f"UPDATE `{test_database}`.missing_table SET id = 1"):
            try:
                repository.complete_turn(alice, session_id, next_turn, new_attempt, "recoverable reply")
            except Error:
                pass
            else:
                raise AssertionError("Expected a simulated database failure")
        assert len(repository.load_messages(alice, session_id)) == 3
        assert repository.get_unfinished_turn(alice, session_id).status == "running"
        repository.complete_turn(alice, session_id, next_turn, new_attempt, "recoverable reply")
        assert len(repository.load_messages(alice, session_id)) == 4

        # 旧系统遗留的单条提问能补上 pending；重复迁移不会重复创建。
        legacy_session = sessions.create_session(alice, "legacy")
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(repository.save_message_sql, (legacy_session, 1, "human", "legacy question"))
            connection.commit()
        backfill = (ROOT / "sql" / "backfill_pending_turns.sql").read_text(encoding="utf-8")
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(backfill.replace("experiment_assistant", test_database))
                assert cursor.rowcount == 1
                cursor.execute(backfill.replace("experiment_assistant", test_database))
                assert cursor.rowcount == 0
            connection.commit()
        legacy_turn = repository.get_unfinished_turn(alice, legacy_session)
        assert legacy_turn.status == "pending"
        legacy_attempt, history = repository.claim_turn(alice, legacy_session, legacy_turn.turn_id)
        assert history == [("human", "legacy question")]
        repository.complete_turn(alice, legacy_session, legacy_turn.turn_id, legacy_attempt, "legacy reply")

        # 服务层也使用真实仓储，模型用假回复，验证失败后可恢复且不重复调用已完成请求。
        service_session = sessions.create_session(alice, "service")
        service_request = uuid4().hex
        gateway = SimpleNamespace(invoke=AsyncMock(side_effect=[RuntimeError("simulated model failure"), {"content": "retried reply"}]))
        service = ChatService(gateway, repository)

        async def verify_service():
            try:
                await service.send_message(alice, service_session, "service question", service_request)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Expected model failure")
            assert repository.load_messages(alice, service_session) == [("human", "service question")]
            assert repository.get_unfinished_turn(alice, service_session).status == "failed"
            assert await service.retry_message(alice, service_session) == "retried reply"
            assert await service.send_message(alice, service_session, "service question", service_request) == "retried reply"
            assert gateway.invoke.await_count == 2
            assert repository.load_messages(alice, service_session) == [("human", "service question"), ("ai", "retried reply")]

        asyncio.run(verify_service())
        print("PASS: real MySQL schema, durable questions, retries, idempotency, ownership, concurrent claims, expired attempts, rollback, repeatable migration and ChatService recovery.")
    finally:
        # 仅清理此脚本刚创建的随机数据库，不接受外部输入的库名。
        if created:
            assert re.fullmatch(r"_codex_turn_check_[0-9a-f]{32}", test_database)
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{test_database}`")
            print("Temporary integration database removed.")


if __name__ == "__main__":
    main()
