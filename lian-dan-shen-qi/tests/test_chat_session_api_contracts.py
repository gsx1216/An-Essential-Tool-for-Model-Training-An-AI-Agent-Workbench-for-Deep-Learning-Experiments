"""会话接口准备检查：验证工厂和数据模型，不连接 MySQL，不调用 HTTP 路由。"""

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.api.dependencies import get_chat_session_service
from app.api.schemas import CreateSessionRequest, SessionCreatedResponse, SessionResponse
from app.repositories.session_repository import SessionRepository
from app.services.chat_session_service import ChatSessionService


SESSION_ID = "b4c4e67554ee4c1981740a1e901ec614"


class ChatSessionFactoryTests(unittest.TestCase):
    def setUp(self):
        get_chat_session_service.cache_clear()
        self.addCleanup(get_chat_session_service.cache_clear)

    def test_factory_caches_service_with_chat_repository_without_database_io(self):
        with patch("app.api.dependencies.get_mysql_password", return_value="TEST_ONLY_UNUSED") as config:
            with patch("app.repositories.session_repository.get_connection") as connect:
                first = get_chat_session_service()
                second = get_chat_session_service()
        self.assertIs(first, second)
        self.assertIsInstance(first, ChatSessionService)
        self.assertIsInstance(first.repository, SessionRepository)
        self.assertEqual(first.repository.password, "TEST_ONLY_UNUSED")
        config.assert_called_once_with()
        connect.assert_not_called()

    def test_configuration_failure_is_propagated_and_not_cached(self):
        error = ValueError("TEST_ONLY_MISSING_SERVER_CONFIG")
        with patch("app.api.dependencies.get_mysql_password", side_effect=error) as config:
            for _ in range(2):
                with self.assertRaises(ValueError) as caught:
                    get_chat_session_service()
                self.assertIs(caught.exception, error)
        self.assertEqual(config.call_count, 2)


class ChatSessionSchemaTests(unittest.TestCase):
    def test_create_request_has_default_title(self):
        self.assertEqual(CreateSessionRequest().model_dump(), {"title": "新聊天"})

    def test_create_request_preserves_text_for_service_normalization(self):
        self.assertEqual(CreateSessionRequest(title="  实验一  ").title, "  实验一  ")

    def test_create_request_rejects_non_text_title(self):
        for value in (None, 123, True, {}, []):
            with self.subTest(input_type=type(value).__name__):
                with self.assertRaises(ValidationError):
                    CreateSessionRequest(title=value)

    def test_create_request_rejects_client_selected_owner_or_session(self):
        for payload in ({"user_id": 99}, {"session_id": SESSION_ID}, {"title": "测试", "unexpected": 1}):
            with self.subTest(fields=sorted(payload)):
                with self.assertRaises(ValidationError) as caught:
                    CreateSessionRequest(**payload)
                self.assertTrue(any(item["type"] == "extra_forbidden" for item in caught.exception.errors()))

    def test_created_response_contains_only_generated_id(self):
        self.assertEqual(SessionCreatedResponse(session_id=SESSION_ID).model_dump(), {"session_id": SESSION_ID})

    def test_session_summary_contains_id_and_title(self):
        self.assertEqual(SessionResponse(session_id=SESSION_ID, title="实验一").model_dump(),
                         {"session_id": SESSION_ID, "title": "实验一"})

    def test_response_fields_are_required_and_typed(self):
        cases = ((SessionCreatedResponse, {}), (SessionCreatedResponse, {"session_id": 123}),
                 (SessionResponse, {"session_id": SESSION_ID}),
                 (SessionResponse, {"session_id": SESSION_ID, "title": None}))
        for model, payload in cases:
            with self.subTest(model=model.__name__, fields=sorted(payload)):
                with self.assertRaises(ValidationError):
                    model(**payload)
