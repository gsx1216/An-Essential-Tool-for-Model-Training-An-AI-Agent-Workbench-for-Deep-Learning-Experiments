"""历史接口准备检查：工厂对象与响应模型，模拟配置，不连接数据库。"""

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.api.dependencies import get_chat_history_service
from app.api.schemas import HistoryMessageResponse
from app.repositories.message_repository import MessageRepository
from app.services.chat_history_service import ChatHistoryService


class HistoryFactoryTests(unittest.TestCase):
    def setUp(self):
        get_chat_history_service.cache_clear()
        self.addCleanup(get_chat_history_service.cache_clear)

    def test_factory_caches_correct_service_without_database_io(self):
        with patch("app.api.dependencies.get_mysql_password", return_value="TEST_ONLY_UNUSED") as config:
            with patch("app.repositories.message_repository.get_connection") as connect:
                first = get_chat_history_service()
                second = get_chat_history_service()
        self.assertIs(first, second)
        self.assertIsInstance(first, ChatHistoryService)
        self.assertIsInstance(first.repository, MessageRepository)
        self.assertEqual(first.repository.password, "TEST_ONLY_UNUSED")
        config.assert_called_once_with()
        connect.assert_not_called()

    def test_missing_server_configuration_propagates_and_is_not_cached(self):
        error = ValueError("TEST_ONLY_MISSING_SERVER_CONFIG")
        with patch("app.api.dependencies.get_mysql_password", side_effect=error) as config:
            for _ in range(2):
                with self.assertRaises(ValueError) as caught:
                    get_chat_history_service()
                self.assertIs(caught.exception, error)
        self.assertEqual(config.call_count, 2)


class HistoryMessageSchemaTests(unittest.TestCase):
    def test_message_contains_original_sequence_role_and_content(self):
        message = HistoryMessageResponse(seq=12, role="human", content="你好")
        self.assertEqual(message.model_dump(), {"seq": 12, "role": "human", "content": "你好"})

    def test_content_keeps_code_indentation_and_newline(self):
        content = "    print('hello')\n"
        message = HistoryMessageResponse(seq=12, role="ai", content=content)
        self.assertEqual(message.model_dump().get("content"), content)

    def test_exact_fields_exist_and_are_required(self):
        self.assertEqual(set(HistoryMessageResponse.model_fields), {"seq", "role", "content"})
        self.assertTrue(all(field.is_required() for field in HistoryMessageResponse.model_fields.values()))

    def test_missing_fields_are_rejected(self):
        for payload in ({}, {"seq": 1}, {"seq": 1, "role": "human"}):
            with self.subTest(fields=sorted(payload)):
                with self.assertRaises(ValidationError):
                    HistoryMessageResponse(**payload)

    def test_invalid_field_types_are_rejected(self):
        cases = (("seq", "not-an-integer"), ("seq", None), ("role", None), ("content", None))
        for name, value in cases:
            with self.subTest(field=name, input_type=type(value).__name__):
                payload = {"seq": 1, "role": "human", "content": "你好"}
                payload[name] = value
                with self.assertRaises(ValidationError):
                    HistoryMessageResponse(**payload)
