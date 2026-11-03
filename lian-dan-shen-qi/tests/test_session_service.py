import unittest
from unittest.mock import patch

from mysql.connector import Error

from app.services.session_service import prepare_session


SESSION_ID = "b4c4e67554ee4c1981740a1e901ec614"


class SessionServiceTests(unittest.TestCase):
    def setUp(self):
        session_patch = patch("app.services.session_service.SessionRepository")
        message_patch = patch("app.services.session_service.MessageRepository")
        self.session_factory = session_patch.start()
        self.message_factory = message_patch.start()
        self.addCleanup(session_patch.stop)
        self.addCleanup(message_patch.stop)
        self.sessions = self.session_factory.return_value
        self.messages = self.message_factory.return_value
        self.sessions.create_session.return_value = SESSION_ID
        self.sessions.get_session.return_value = (SESSION_ID, "旧会话")
        self.messages.load_messages.return_value = []

    def test_empty_input_creates_session_without_loading_history(self):
        self.assertEqual(prepare_session("unused", 1), (SESSION_ID, []))
        self.sessions.create_session.assert_called_once_with(user_id=1)
        self.sessions.get_session.assert_not_called()
        self.messages.load_messages.assert_not_called()

    def test_whitespace_input_is_treated_as_new_session(self):
        self.assertEqual(prepare_session("unused", 1, "  \t "), (SESSION_ID, []))
        self.sessions.create_session.assert_called_once_with(user_id=1)
        self.sessions.get_session.assert_not_called()
        self.messages.load_messages.assert_not_called()

    def test_existing_owned_session_uses_database_id_and_returns_history(self):
        entered_id = " B4C4E675-54EE-4C19-8174-0A1E901EC614 "
        history = [("human", "你好"), ("ai", "你好，有什么需要帮助？")]
        self.messages.load_messages.return_value = history
        self.assertEqual(prepare_session("unused", 1, entered_id), (SESSION_ID, history))
        self.sessions.get_session.assert_called_once_with(1, entered_id.strip())
        self.sessions.create_session.assert_not_called()
        self.messages.load_messages.assert_called_once_with(1, SESSION_ID)

    def test_missing_or_unowned_session_stops_without_creating_or_loading(self):
        self.sessions.get_session.return_value = None
        with self.assertRaisesRegex(ValueError, "会话不存在或不属于当前用户"):
            prepare_session("unused", 1, SESSION_ID)
        self.sessions.create_session.assert_not_called()
        self.messages.load_messages.assert_not_called()

    def test_non_string_input_is_rejected_before_repositories_are_created(self):
        for value in (None, 2, True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "必须是字符串"):
                prepare_session("unused", 1, value)
        self.session_factory.assert_not_called()
        self.message_factory.assert_not_called()

    def test_missing_user_error_propagates_without_loading_messages(self):
        self.sessions.create_session.side_effect = ValueError("用户 ID 2 不存在")
        with self.assertRaisesRegex(ValueError, "用户 ID 2 不存在"):
            prepare_session("unused", 2)
        self.messages.load_messages.assert_not_called()

    def test_invalid_uuid_error_does_not_create_new_session(self):
        self.sessions.get_session.side_effect = ValueError("会话 ID 格式不正确")
        with self.assertRaisesRegex(ValueError, "格式不正确"):
            prepare_session("unused", 1, "invalid-uuid")
        self.sessions.create_session.assert_not_called()
        self.messages.load_messages.assert_not_called()

    def test_database_failure_is_not_reported_as_empty_history(self):
        self.messages.load_messages.side_effect = Error("database unavailable")
        with self.assertRaises(Error):
            prepare_session("unused", 1, SESSION_ID)
        self.sessions.create_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
