"""真实临时 MySQL 联调 main 已挂载的认证、会话与历史接口；不调用模型。"""

import asyncio
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app import database
from app.api.dependencies import get_auth_service, get_chat_history_service, get_chat_session_service, get_current_user_service, get_login_service, get_logout_service
from app.config import get_mysql_password
from app.errors import InvalidTokenError, SessionNotFoundError
from app.logging_config import DEFAULT_LOG_DIR, configure_logging
from app.main import app
from app.repositories.login_session_repository import LoginSessionRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.security.passwords import PasswordService
from app.security.tokens import TokenService
from app.services.auth_service import AuthService
from app.services.chat_session_service import ChatSessionService
from app.services.current_user_service import CurrentUserService
from app.services.login_service import LoginService
from app.services.logout_service import LogoutService


ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / "config" / ".env", override=False, interpolate=False)
    password = get_mysql_password()
    name = "_codex_me_api_check_" + uuid4().hex
    assert re.fullmatch(r"_codex_me_api_check_[0-9a-f]{32}", name)
    created = False
    original_overrides = dict(app.dependency_overrides)

    def sql(filename):
        return (ROOT / "sql" / filename).read_text(encoding="utf-8").replace("experiment_assistant", name)

    def rewrite_repository(repository):
        for attribute, value in vars(repository).copy().items():
            if attribute.endswith("_sql") and isinstance(value, str):
                setattr(repository, attribute, value.replace("experiment_assistant", name))
        return repository

    try:
        with database.get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                created = True
                cursor.execute(sql("init.sql"))
                while cursor.nextset():
                    pass
                cursor.execute(sql("create_login_sessions.sql"))
                cursor.execute(sql("create_chat_turns.sql"))
            connection.commit()

        users = rewrite_repository(UserRepository(password))
        sessions = rewrite_repository(LoginSessionRepository(password))
        tokens = TokenService()
        auth = AuthService(users, PasswordService())
        login = LoginService(auth, sessions, tokens)
        current = CurrentUserService(sessions, tokens)
        logout = LogoutService(sessions, tokens)
        chats = rewrite_repository(SessionRepository(password))
        messages = rewrite_repository(MessageRepository(password))
        chat_service = ChatSessionService(chats)
        get_chat_history_service.cache_clear()
        with patch("app.api.dependencies.get_mysql_password", return_value=password):
            history_service = get_chat_history_service()
            assert get_chat_history_service() is history_service
        rewrite_repository(history_service.repository)
        app.dependency_overrides[get_auth_service] = lambda: auth
        app.dependency_overrides[get_login_service] = lambda: login
        app.dependency_overrides[get_current_user_service] = lambda: current
        app.dependency_overrides[get_logout_service] = lambda: logout
        app.dependency_overrides[get_chat_session_service] = lambda: chat_service
        app.dependency_overrides[get_chat_history_service] = lambda: history_service

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            configure_logging(directory, console=False, force=True, log_format="text")
            try:
                with TestClient(app) as client:
                    identities = {}
                    for username in ("identity_alice", "identity_bob"):
                        response = client.post("/auth/register", json={"username": username, "password": "Test1234"})
                        assert response.status_code == 201
                        identities[username] = response.json()

                    issued = []
                    for username in ("identity_alice", "identity_alice", "identity_bob"):
                        response = client.post("/auth/login", json={"username": username, "password": "Test1234"})
                        assert response.status_code == 200
                        result = response.json()
                        assert result["user"] == identities[username]
                        assert result["expires_in"] == 259200
                        issued.append(result["access_token"])
                    alice_token, second_alice_token, bob_token = issued
                    assert len(set(issued)) == 3

                    def headers(token):
                        return {"Authorization": "Bearer " + token}

                    for token, username in ((alice_token, "identity_alice"), (bob_token, "identity_bob")):
                        response = client.get("/auth/me", headers=headers(token), params={"user_id": 999999})
                        assert response.status_code == 200
                        assert response.json() == identities[username]
                        assert response.headers["Cache-Control"] == "no-store"
                        assert response.headers["Pragma"] == "no-cache"
                        assert token not in response.text

                    # 使用正式 main 中注册的真实路由、真实鉴权、业务与临时库 SQL。
                    alice_id = identities["identity_alice"]["user_id"]
                    bob_id = identities["identity_bob"]["user_id"]
                    assert client.get("/sessions", headers=headers(alice_token)).json() == []
                    response = client.post("/sessions", headers=headers(alice_token), json={})
                    assert response.status_code == 201
                    alice_empty = response.json()["session_id"]
                    assert response.headers["Cache-Control"] == "no-store"
                    response = client.post("/sessions", headers=headers(alice_token), json={"title": "  实验历史  "})
                    assert response.status_code == 201
                    alice_chat = response.json()["session_id"]
                    response = client.post("/sessions", headers=headers(bob_token), json={"title": "Bob会话"})
                    assert response.status_code == 201
                    bob_chat = response.json()["session_id"]
                    assert len({alice_empty, alice_chat, bob_chat}) == 3
                    response = client.get("/sessions", headers=headers(alice_token), params={"user_id": bob_id})
                    assert response.status_code == 200
                    assert {row["session_id"]: row["title"] for row in response.json()} == {
                        alice_empty: "新聊天", alice_chat: "实验历史",
                    }
                    assert response.headers["Cache-Control"] == "no-store"
                    assert client.get("/sessions", headers=headers(bob_token)).json() == [
                        {"session_id": bob_chat, "title": "Bob会话"},
                    ]
                    assert client.post("/sessions", headers=headers(alice_token), json={"title": "   "}).status_code == 400
                    assert client.post("/sessions", headers=headers(alice_token), json={"title": "字" * 201}).status_code == 400
                    assert client.post("/sessions", headers=headers(alice_token), json={"user_id": bob_id}).status_code == 422
                    for method, path, options in (("POST", "/sessions", {"json": {}}),
                                                  ("GET", "/sessions", {}),
                                                  ("DELETE", "/sessions/" + alice_chat, {})):
                        response = client.request(method, path, **options)
                        assert response.status_code == 401

                    question = "TEST_ONLY_SESSION_PRIVATE_QUESTION"
                    reply = "TEST_ONLY_SESSION_PRIVATE_REPLY"
                    turn_id = messages.start_turn(alice_id, alice_chat, question)
                    attempt_id, _ = messages.claim_turn(alice_id, alice_chat, turn_id)
                    messages.complete_turn(alice_id, alice_chat, turn_id, attempt_id, reply)
                    assert messages.load_history_page(alice_id, alice_empty) == []
                    assert messages.load_history_page(alice_id, alice_chat, limit=1) == [(1, "human", question)]
                    assert messages.load_history_page(alice_id, alice_chat, after_seq=1, limit=1) == [(2, "ai", reply)]
                    assert messages.load_history_page(alice_id, alice_chat, after_seq=2, limit=1) == []
                    for owner, candidate in ((bob_id, alice_chat), (alice_id, uuid4().hex)):
                        try:
                            messages.load_history_page(owner, candidate)
                        except SessionNotFoundError:
                            pass
                        else:
                            raise AssertionError("History accepted a missing or unowned conversation")

                    async def verify_history_service():
                        assert await history_service.load_history_page(alice_id, alice_empty) == []
                        assert await history_service.load_history_page(alice_id, alice_chat, limit=1) == [(1, "human", question)]
                        assert await history_service.load_history_page(alice_id, alice_chat, after_seq=1, limit=1) == [(2, "ai", reply)]
                        assert await history_service.load_history_page(alice_id, alice_chat, after_seq=2) == []
                        formatted_id = " " + str(UUID(alice_chat)).upper() + " "
                        assert await history_service.load_history_page(alice_id, formatted_id) == [
                            (1, "human", question), (2, "ai", reply),
                        ]
                        for owner, candidate in ((bob_id, alice_chat), (alice_id, uuid4().hex)):
                            try:
                                await history_service.load_history_page(owner, candidate)
                            except SessionNotFoundError:
                                pass
                            else:
                                raise AssertionError("History service accepted a missing or unowned conversation")
                        for options in ({"after_seq": -1}, {"limit": 0}, {"limit": 101}):
                            try:
                                await history_service.load_history_page(alice_id, alice_chat, **options)
                            except ValueError:
                                pass
                            else:
                                raise AssertionError("History service accepted invalid pagination")
                        broken_query = history_service.repository.load_history_page_sql.replace(".chat_messages", "._missing_history_messages")
                        with patch.object(history_service.repository, "load_history_page_sql", broken_query):
                            try:
                                await history_service.load_history_page(alice_id, alice_chat)
                            except MySQLError as error:
                                assert error.errno == 1146
                            else:
                                raise AssertionError("History service swallowed a database failure")

                    asyncio.run(verify_history_service())

                    # 正式 main 已挂载历史路由；真实身份、业务、仓储和 SQL 指向临时库。
                    history_path = "/sessions/" + alice_chat + "/messages"
                    with TestClient(app) as history_client:
                        response = history_client.get(history_path, headers=headers(alice_token))
                        assert response.status_code == 200
                        assert response.json() == [
                            {"seq": 1, "role": "human", "content": question},
                            {"seq": 2, "role": "ai", "content": reply},
                        ]
                        assert response.headers["Cache-Control"] == "no-store"
                        assert response.headers["Pragma"] == "no-cache"
                        assert response.headers["X-Request-ID"]
                        response = history_client.get(history_path, headers=headers(alice_token), params={"after_seq": 1, "limit": 1})
                        assert response.status_code == 200
                        assert response.json() == [{"seq": 2, "role": "ai", "content": reply}]
                        assert history_client.get(history_path, headers=headers(alice_token), params={"after_seq": 2}).json() == []
                        response = history_client.get("/sessions/" + alice_empty + "/messages", headers=headers(alice_token))
                        assert response.status_code == 200 and response.json() == []
                        response = history_client.get(history_path, headers=headers(bob_token))
                        assert response.status_code == 404
                        assert response.json() == {"detail": "会话不存在或不属于当前用户。"}
                        assert history_client.get("/sessions/" + uuid4().hex + "/messages", headers=headers(alice_token)).status_code == 404
                        assert history_client.get("/sessions/bad-id/messages", headers=headers(alice_token)).status_code == 400
                        assert history_client.get(history_path).status_code == 401
                        for options in ({"after_seq": -1}, {"limit": 0}, {"limit": 101}):
                            assert history_client.get(history_path, headers=headers(alice_token), params=options).status_code == 422
                        response = history_client.get(history_path, headers=headers(bob_token), params={"user_id": alice_id})
                        assert response.status_code == 404
                        broken_query = history_service.repository.load_history_page_sql.replace(".chat_messages", "._missing_history_messages")
                        with patch.object(history_service.repository, "load_history_page_sql", broken_query):
                            response = history_client.get(history_path, headers=headers(alice_token))
                        assert response.status_code == 503
                        assert response.json() == {"detail": "请稍后重试。"}
                        assert "_missing_history_messages" not in response.text

                    response = client.delete("/sessions/" + alice_chat, headers=headers(bob_token))
                    assert response.status_code == 404
                    assert response.json() == {"detail": "会话不存在或不属于当前用户。"}
                    assert len(messages.load_messages(alice_id, alice_chat)) == 2
                    response = client.delete("/sessions/" + alice_chat, headers=headers(alice_token))
                    assert response.status_code == 204 and response.content == b""
                    assert response.headers["Cache-Control"] == "no-store"
                    assert response.headers["Pragma"] == "no-cache"
                    try:
                        asyncio.run(history_service.load_history_page(alice_id, alice_chat))
                    except SessionNotFoundError:
                        pass
                    else:
                        raise AssertionError("Deleted history remained readable")
                    with TestClient(app) as history_client:
                        assert history_client.get(history_path, headers=headers(alice_token)).status_code == 404
                    assert client.delete("/sessions/" + alice_chat, headers=headers(alice_token)).status_code == 404
                    assert client.delete("/sessions/" + uuid4().hex, headers=headers(alice_token)).status_code == 404
                    assert client.delete("/sessions/bad-id", headers=headers(alice_token)).status_code == 400
                    with database.get_connection(password) as connection:
                        with connection.cursor() as cursor:
                            for table, column in (("chat_sessions", "id"), ("chat_messages", "session_id"),
                                                  ("chat_turns", "session_id")):
                                cursor.execute(f"SELECT COUNT(*) FROM `{name}`.`{table}` WHERE `{column}` = %s", (alice_chat,))
                                assert cursor.fetchone()[0] == 0
                    assert client.get("/sessions", headers=headers(alice_token)).json() == [
                        {"session_id": alice_empty, "title": "新聊天"},
                    ]
                    assert client.get("/sessions", headers=headers(bob_token)).json() == [
                        {"session_id": bob_chat, "title": "Bob会话"},
                    ]

                    # 真实 SQL 故障检查：创建/列表503，删除子表失败必须回滚。
                    messages.start_turn(alice_id, alice_empty, question)
                    cases = (("sql_session_sql", ".chat_sessions", "POST", "/sessions", {"json": {}}),
                             ("list_sessions_sql", ".chat_sessions", "GET", "/sessions", {}),
                             ("delete_session_messages_sql", ".chat_messages", "DELETE", "/sessions/" + alice_empty, {}))
                    for attribute, table, method, path, options in cases:
                        original_sql = getattr(chats, attribute)
                        try:
                            setattr(chats, attribute, original_sql.replace(table, "._missing_session_table"))
                            response = client.request(method, path, headers=headers(alice_token), **options)
                            assert response.status_code == 503
                            assert response.json() == {"detail": "请稍后重试。"}
                            chat_failed_request_id = response.headers["X-Request-ID"]
                        finally:
                            setattr(chats, attribute, original_sql)
                    assert messages.load_messages(alice_id, alice_empty) == [("human", question)]
                    assert messages.get_unfinished_turn(alice_id, alice_empty).status == "pending"
                    with patch.object(chat_service, "list_sessions", side_effect=RuntimeError("TEST_ONLY_SESSION_INTERNAL_FAILURE")):
                        response = client.get("/sessions", headers=headers(alice_token))
                    assert response.status_code == 500
                    assert response.json() == {"detail": "请稍后重试。"}

                    for candidate_headers in ({}, {"Authorization": "Basic dGVzdDp0ZXN0"},
                                              {"Authorization": "Bearer"}, headers("bad"),
                                              headers(tokens.create_token())):
                        response = client.get("/auth/me", headers=candidate_headers)
                        assert response.status_code == 401
                        assert response.headers["WWW-Authenticate"] == "Bearer"
                        assert response.json() == {"detail": "登录已失效，请重新登录。"}

                    expired_token = tokens.create_token()
                    with database.get_connection(password) as connection:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                f"INSERT INTO `{name}`.login_sessions (user_id, token_hash, created_at, expires_at) "
                                "VALUES (%s, %s, UTC_TIMESTAMP(6) - INTERVAL 2 DAY, UTC_TIMESTAMP(6) - INTERVAL 1 DAY)",
                                (identities["identity_alice"]["user_id"], tokens.hash_token(expired_token)),
                            )
                            cursor.execute(f"SELECT expires_at FROM `{name}`.login_sessions WHERE token_hash = %s",
                                           (tokens.hash_token(alice_token),))
                            original_expiry = cursor.fetchone()[0]
                        connection.commit()
                    assert client.get("/auth/me", headers=headers(expired_token)).status_code == 401

                    # 真实 HTTP 退出只撤销当前 Token；数据库连接和业务均为真实实现。
                    response = client.post("/auth/logout", headers=headers(alice_token))
                    assert response.status_code == 204
                    assert response.content == b""
                    assert response.headers["Cache-Control"] == "no-store"
                    assert response.headers["Pragma"] == "no-cache"
                    assert response.headers["X-Request-ID"]
                    for candidate_headers in ({}, {"Authorization": "Basic dGVzdDp0ZXN0"},
                                              {"Authorization": "Bearer"}, headers("bad"),
                                              headers(alice_token), headers(expired_token),
                                              headers(tokens.create_token())):
                        response = client.post("/auth/logout", headers=candidate_headers)
                        assert response.status_code == 401
                        assert response.headers["WWW-Authenticate"] == "Bearer"
                        assert response.json() == {"detail": "登录已失效，请重新登录。"}
                    assert sessions.revoke_session(tokens.hash_token(alice_token)) is False
                    assert sessions.revoke_session(tokens.hash_token(expired_token)) is False
                    assert sessions.revoke_session(tokens.hash_token(tokens.create_token())) is False
                    for candidate in (alice_token, expired_token, tokens.create_token(), "bad", None, 123):
                        try:
                            asyncio.run(logout.logout(candidate))
                        except InvalidTokenError:
                            pass
                        else:
                            raise AssertionError("LogoutService accepted an invalid credential")
                    assert client.get("/auth/me", headers=headers(alice_token)).status_code == 401
                    assert client.get("/auth/me", headers=headers(second_alice_token)).json() == identities["identity_alice"]
                    assert client.get("/auth/me", headers=headers(bob_token)).json() == identities["identity_bob"]

                    original_update = sessions.revoke_session_sql
                    try:
                        sessions.revoke_session_sql = original_update.replace(".login_sessions", "._missing_login_sessions")
                        response = client.post("/auth/logout", headers=headers(bob_token))
                        assert response.status_code == 503
                        assert response.json() == {"detail": "请稍后重试。"}
                        logout_failed_request_id = response.headers["X-Request-ID"]
                        try:
                            asyncio.run(logout.logout(bob_token))
                        except MySQLError as error:
                            assert error.errno == 1146
                        else:
                            raise AssertionError("LogoutService swallowed a real database failure")
                    finally:
                        sessions.revoke_session_sql = original_update
                    assert client.get("/auth/me", headers=headers(bob_token)).status_code == 200

                    original_query = sessions.get_active_user_sql
                    try:
                        sessions.get_active_user_sql = original_query.replace(".login_sessions", "._missing_login_sessions")
                        response = client.get("/auth/me", headers=headers(bob_token))
                        assert response.status_code == 503
                        assert response.json() == {"detail": "请稍后重试。"}
                        failed_request_id = response.headers["X-Request-ID"]
                    finally:
                        sessions.get_active_user_sql = original_query

                    with patch.object(tokens, "hash_token", side_effect=RuntimeError("TEST_ONLY_INTERNAL_FAILURE")):
                        response = client.get("/auth/me", headers=headers(bob_token))
                        logout_response = client.post("/auth/logout", headers=headers(bob_token))
                    assert response.status_code == 500
                    assert response.json() == {"detail": "请稍后重试。"}
                    assert logout_response.status_code == 500
                    assert logout_response.json() == {"detail": "请稍后重试。"}
                    assert client.get("/auth/me", headers=headers(second_alice_token)).status_code == 200
                    assert client.get("/auth/me", headers=headers(bob_token)).status_code == 200

                    with database.get_connection(password) as connection:
                        with connection.cursor() as cursor:
                            cursor.execute(f"SELECT COUNT(*) FROM `{name}`.login_sessions")
                            assert cursor.fetchone()[0] == 4
                            cursor.execute(f"SELECT expires_at, revoked_at FROM `{name}`.login_sessions WHERE token_hash = %s",
                                           (tokens.hash_token(alice_token),))
                            expires_at, revoked_at = cursor.fetchone()
                            assert expires_at == original_expiry and revoked_at is not None

                    text = "\n".join(path.read_text(encoding="utf-8") for path in directory.rglob("*.txt"))
                    for private in issued + [expired_token, "Test1234", "TEST_ONLY_INTERNAL_FAILURE",
                                            question, reply, "TEST_ONLY_SESSION_INTERNAL_FAILURE"]:
                        assert private not in text
                    for token in issued:
                        assert tokens.hash_token(token) not in text
                    assert failed_request_id in text and logout_failed_request_id in text and chat_failed_request_id in text and "errno=1146" in text
            finally:
                configure_logging(DEFAULT_LOG_DIR, console=False, force=True)

        print("PASS: real HTTP registration/login/me, user separation, bearer errors, expired/revoked rejection, generic 503/500, safe logs, unchanged expiry and retained records.")
        print("PASS: real HTTP logout 204 with empty body and cache headers; repeat/expired/unknown/missing rejection; generic 503/500; current token revoked, other logins remain valid.")
        print("PASS: mounted session HTTP create/list/delete, owner isolation, input rejection, full deletion, real SQL 503 and rollback, generic 500 and safe logs.")
        print("PASS: student history factory/service and real repository pagination/order/empty history, UUID normalization, owner/missing/deleted rejection, invalid pagination and database failure propagation.")
        print("PASS: main-mounted history HTTP route with real authentication and temporary MySQL: full/paged/empty rows, owner and spoof rejection, missing/deleted 404, invalid UUID 400, missing token 401, paging 422, real SQL 503 and cache/request-id headers.")
    finally:
        get_chat_history_service.cache_clear()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
        if created:
            assert re.fullmatch(r"_codex_me_api_check_[0-9a-f]{32}", name)
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{name}`")
            print("Temporary current-user API verification database removed.")


if __name__ == "__main__":
    main()
