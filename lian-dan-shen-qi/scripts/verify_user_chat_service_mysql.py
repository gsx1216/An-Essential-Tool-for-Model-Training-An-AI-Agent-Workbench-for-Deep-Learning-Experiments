"""真实临时MySQL验证个人聊天业务；模型网关为假对象，不调用供应商。"""

import asyncio
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from mysql.connector import Error as MySQLError

from app.api import dependencies
from app.config import get_mysql_password
from app.database import get_connection
from app.errors import ModelCredentialNotConfiguredError, SessionNotFoundError
from app.logging_config import DEFAULT_LOG_DIR, configure_logging
from app.repositories.message_repository import MessageRepository
from app.repositories.context_repository import ContextRepository
from app.repositories.model_credential_repository import ModelCredentialRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.security.api_key_cipher import ApiKeyCipher
from app.services.model_credential_service import ModelCredentialService
from app.services.user_chat_service import UserChatService


ROOT = Path(__file__).resolve().parents[1]


class FakeGateway:
    def __init__(self, api_key, error=None):
        self.api_key = api_key
        self.error = error
        self.messages = []
        self.closed = False

    async def invoke(self, messages):
        assert not self.closed
        self.messages.append(list(messages))
        if self.error is not None:
            raise self.error
        return {"content": "TEST_ONLY_REPLY"}

    async def aclose(self):
        self.closed = True


async def verify(users, sessions, messages, credentials, contexts):
    alice = users.create_user("user_chat_alice")
    bob = users.create_user("user_chat_bob")
    alice_session = sessions.create_session(alice, "Alice")
    bob_session = sessions.create_session(bob, "Bob")
    keys = ["sk-TEST_ONLY_CHAT_ALICE", "sk-TEST_ONLY_CHAT_ALICE_NEW", "sk-TEST_ONLY_CHAT_BOB"]
    await credentials.save_api_key(alice, keys[0])
    await credentials.save_api_key(bob, keys[2])
    gateways = []
    failures = []

    def build(*, api_key):
        gateway = FakeGateway(api_key, failures.pop(0) if failures else None)
        gateways.append(gateway)
        return gateway

    # 走用户实际编写的FastAPI依赖工厂；协作者是实际业务和临时库仓储。
    # 注入临时库仓储以隔离业务数据；仅模型构造器换成假对象。
    with patch.object(dependencies, 'get_mysql_password', return_value=credentials.repository.password):
        with patch.object(dependencies, 'get_model_credential_service', return_value=credentials):
            with patch.object(dependencies, 'MessageRepository', return_value=messages), \
                 patch.object(dependencies, 'ContextRepository', return_value=contexts):
                service = dependencies.get_user_chat_service()
                assert dependencies.get_user_chat_service() is service
    assert isinstance(service, UserChatService)
    assert service.repository is messages
    assert service.gateway_factory.credential_service is credentials
    service.gateway_factory.gateway_factory = build
    request_id = uuid4().hex
    reply = await service.send_message(alice, alice_session, "TEST_ONLY_QUESTION", request_id)
    assert reply == "TEST_ONLY_REPLY"
    assert messages.load_messages(alice, alice_session) == [
        ("human", "TEST_ONLY_QUESTION"), ("ai", "TEST_ONLY_REPLY"),
    ]
    assert gateways[-1].api_key == keys[0] and gateways[-1].closed
    assert gateways[-1].messages[0][-1] == ("human", "TEST_ONLY_QUESTION")

    # 相同请求返回落库回复，不再次调用模型、不重复写消息。
    assert await service.send_message(alice, alice_session, "TEST_ONLY_QUESTION", request_id) == reply
    assert gateways[-1].messages == [] and gateways[-1].closed
    assert len(messages.load_messages(alice, alice_session)) == 2

    await credentials.save_api_key(alice, keys[1])
    model_error = RuntimeError("TEST_ONLY_MODEL_FAILURE")
    failures.append(model_error)
    try:
        await service.send_message(alice, alice_session, "TEST_ONLY_RECOVERABLE")
    except RuntimeError as error:
        assert error is model_error
    else:
        raise AssertionError("Expected the fake model failure")
    assert gateways[-1].closed and gateways[-1].api_key == keys[1]
    assert messages.load_messages(alice, alice_session)[-1] == ("human", "TEST_ONLY_RECOVERABLE")
    turn = messages.get_unfinished_turn(alice, alice_session)
    assert turn.status == "failed" and turn.error_code == "MODEL_REQUEST_FAILED"

    assert await service.retry_message(alice, alice_session) == "TEST_ONLY_REPLY"
    alice_history = messages.load_messages(alice, alice_session)
    assert len(alice_history) == 4
    assert alice_history.count(("human", "TEST_ONLY_RECOVERABLE")) == 1
    assert alice_history[-1] == ("ai", "TEST_ONLY_REPLY")
    assert gateways[-1].api_key == keys[1] and gateways[-1].closed
    assert gateways[-1].messages[0][1:] == alice_history[:-1]

    await service.send_message(bob, bob_session, "TEST_ONLY_BOB")
    assert gateways[-1].api_key == keys[2] and gateways[-1].closed
    bob_history = messages.load_messages(bob, bob_session)

    try:
        await service.send_message(bob, alice_session, "TEST_ONLY_NOT_OWNED")
    except SessionNotFoundError:
        pass
    else:
        raise AssertionError("Another user's session must be rejected")
    assert gateways[-1].messages == [] and gateways[-1].closed
    assert messages.load_messages(alice, alice_session) == alice_history

    await credentials.delete_api_key(bob)
    count = len(gateways)
    try:
        await service.send_message(bob, bob_session, "TEST_ONLY_MISSING_KEY")
    except ModelCredentialNotConfiguredError:
        pass
    else:
        raise AssertionError("Missing personal key must be rejected")
    assert len(gateways) == count
    assert messages.load_messages(bob, bob_session) == bob_history

    # 实际数据库写回故障：问题保留，回复不落库，模型只调用一次，网关关闭。
    error_session = sessions.create_session(alice, "SQL failure")
    broken = messages.complete_turn_sql.replace(".chat_turns", "._missing_chat_turns")
    assert broken != messages.complete_turn_sql
    with patch.object(messages, "complete_turn_sql", broken):
        try:
            await service.send_message(alice, error_session, "TEST_ONLY_WRITEBACK_FAILURE")
        except MySQLError as error:
            assert error.errno == 1146
        else:
            raise AssertionError("Expected actual SQL failure")
    assert messages.load_messages(alice, error_session) == [("human", "TEST_ONLY_WRITEBACK_FAILURE")]
    assert len(gateways[-1].messages) == 1 and gateways[-1].closed
    assert all(gateway.closed for gateway in gateways)
    return keys


def main():
    password = get_mysql_password()
    dependencies.get_user_chat_service.cache_clear()
    name = "_codex_user_chat_" + uuid4().hex
    assert re.fullmatch(r"_codex_user_chat_[0-9a-f]{32}", name)
    created = False

    def target(repository):
        for attribute, value in vars(repository).copy().items():
            if attribute.endswith("_sql") and isinstance(value, str):
                setattr(repository, attribute, value.replace("experiment_assistant", name))
        return repository

    try:
        with get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                created = True
                for filename in ("init.sql", "create_chat_turns.sql", "create_model_credentials.sql", "create_context_summaries.sql"):
                    script = (ROOT / "sql" / filename).read_text(encoding="utf-8").replace("experiment_assistant", name)
                    cursor.execute(script)
                    while cursor.nextset():
                        pass
            connection.commit()
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            configure_logging(directory / "logs", console=False, force=True)
            master_key = Fernet.generate_key().decode("ascii")
            try:
                credentials = ModelCredentialService(
                    target(ModelCredentialRepository(password)), ApiKeyCipher(master_key),
                )
                keys = asyncio.run(verify(
                    target(UserRepository(password)), target(SessionRepository(password)),
                    target(MessageRepository(password)), credentials, target(ContextRepository(password)),
                ))
                contents = "\n".join(path.read_text(encoding="utf-8") for path in (directory / "logs").rglob("*.txt"))
                assert all(value not in contents for value in keys + [password, master_key])
            finally:
                configure_logging(DEFAULT_LOG_DIR, console=False, force=True)
        print("PASS: actual get_user_chat_service factory + personal chat services + Fernet + temporary MySQL; fake gateways only.")
        print("Verified: persistence, request deduplication, current key, failure retention/retry, fresh context, user isolation, deleted-key rejection, SQL writeback rollback, gateway cleanup and secret-free logs.")
        print("No model was called; production HTTP wiring and configuration were not changed.")
    finally:
        dependencies.get_user_chat_service.cache_clear()
        if created:
            assert re.fullmatch(r"_codex_user_chat_[0-9a-f]{32}", name)
            with get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{name}`")
            print("Temporary personal chat database removed.")


if __name__ == "__main__":
    main()
