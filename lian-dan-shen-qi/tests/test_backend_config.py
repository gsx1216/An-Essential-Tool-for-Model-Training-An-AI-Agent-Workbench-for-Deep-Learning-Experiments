"""使用临时配置及模拟数据库检查后端配置，不读取真实密码、不连接 MySQL。"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mysql.connector import Error

from app import chat_cli, config


SESSION_ID = "b4c4e67554ee4c1981740a1e901ec614"


class BackendConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env_file = Path(directory.name) / ".env"
        environment_patch = patch.dict(os.environ, {}, clear=True)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        path_patch = patch.object(config, "ENV_FILE", self.env_file)
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def test_local_env_file_supplies_password(self):
        self.env_file.write_text("MYSQL_PASSWORD='fake-local-password'\n", encoding="utf-8")
        self.assertEqual(config.get_mysql_password(), "fake-local-password")

    def test_server_environment_takes_priority_over_env_file(self):
        self.env_file.write_text("MYSQL_PASSWORD='fake-local-password'\n", encoding="utf-8")
        os.environ["MYSQL_PASSWORD"] = "fake-server-password"
        self.assertEqual(config.get_mysql_password(), "fake-server-password")

    def test_missing_or_empty_password_has_actionable_error(self):
        for contents in (None, "", "MYSQL_PASSWORD=\n"):
            with self.subTest(contents=contents), patch.dict(os.environ, {}, clear=True):
                if contents is not None:
                    self.env_file.write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "后端尚未配置 MYSQL_PASSWORD"):
                    config.get_mysql_password()

    def test_password_whitespace_and_special_characters_are_preserved(self):
        self.env_file.write_text("MYSQL_PASSWORD='  fake#pass${literal}  '\n", encoding="utf-8")
        self.assertEqual(config.get_mysql_password(), "  fake#pass${literal}  ")


class ChatCliConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_entry_uses_backend_password_without_printing_it(self):
        output = io.StringIO()
        history = [("human", "你好"), ("ai", "你好，有什么需要帮助？")]
        with patch.object(chat_cli, "get_mysql_password", return_value="fake-server-password"):
            with patch.object(chat_cli, "SessionRepository") as session_factory:
                session_factory.return_value.list_sessions.return_value = [
                    ("a" * 32, "第一个会话"), (SESSION_ID, "第二个会话"),
                ]
                with patch("builtins.input", side_effect=["1", "2"]) as read_input:
                    with patch.object(chat_cli, "prepare_session", return_value=(SESSION_ID, history)) as prepare:
                        with redirect_stdout(output):
                            await chat_cli.main()
        session_factory.assert_called_once_with("fake-server-password")
        session_factory.return_value.list_sessions.assert_called_once_with(1)
        prepare.assert_called_once_with(
            password="fake-server-password", user_id=1, entered_id=SESSION_ID,
        )
        self.assertEqual(len(read_input.call_args_list), 2)
        self.assertIn(SESSION_ID, output.getvalue())
        self.assertIn("历史消息数量： 2", output.getvalue())
        self.assertNotIn("fake-server-password", output.getvalue())

    async def test_new_chat_asks_backend_to_generate_id_without_uuid_input(self):
        for selection, sessions in (("", []), ("0", [("a" * 32, "已有会话")])):
            with self.subTest(selection=selection):
                output = io.StringIO()
                with patch.object(chat_cli, "get_mysql_password", return_value="fake-server-password"):
                    with patch.object(chat_cli, "SessionRepository") as session_factory:
                        session_factory.return_value.list_sessions.return_value = sessions
                        with patch("builtins.input", side_effect=["1", selection]) as read_input:
                            with patch.object(chat_cli, "prepare_session", return_value=(SESSION_ID, [])) as prepare:
                                with redirect_stdout(output):
                                    await chat_cli.main()
                prepare.assert_called_once_with(
                    password="fake-server-password", user_id=1, entered_id="",
                )
                self.assertEqual(len(read_input.call_args_list), 2)
                self.assertIn("0. 新聊天", output.getvalue())
                self.assertIn("历史消息数量： 0", output.getvalue())

    async def test_invalid_selection_never_creates_or_opens_a_session(self):
        for selection in ("not-a-number", "-1", "2", SESSION_ID):
            with self.subTest(selection=selection):
                output = io.StringIO()
                with patch.object(chat_cli, "get_mysql_password", return_value="fake-server-password"):
                    with patch.object(chat_cli, "SessionRepository") as session_factory:
                        session_factory.return_value.list_sessions.return_value = [(SESSION_ID, "已有会话")]
                        with patch("builtins.input", side_effect=["1", selection]):
                            with patch.object(chat_cli, "prepare_session") as prepare:
                                with redirect_stdout(output):
                                    await chat_cli.main()
                prepare.assert_not_called()
                self.assertIn("进入会话失败", output.getvalue())

    async def test_failed_session_listing_never_creates_a_session(self):
        for failure in (ValueError("用户不存在"), Error("simulated database error")):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(chat_cli, "get_mysql_password", return_value="fake-server-password"):
                    with patch.object(chat_cli, "SessionRepository") as session_factory:
                        session_factory.return_value.list_sessions.side_effect = failure
                        with patch("builtins.input", side_effect=["1"]) as read_input:
                            with patch.object(chat_cli, "prepare_session") as prepare:
                                with redirect_stdout(io.StringIO()):
                                    await chat_cli.main()
                read_input.assert_called_once()
                prepare.assert_not_called()

    async def test_missing_backend_config_stops_before_user_input_or_database_access(self):
        output = io.StringIO()
        with patch.object(chat_cli, "get_mysql_password", side_effect=ValueError("后端尚未配置 MYSQL_PASSWORD")):
            with patch("builtins.input") as read_input:
                with patch.object(chat_cli, "prepare_session") as prepare:
                    with redirect_stdout(output):
                        await chat_cli.main()
        read_input.assert_not_called()
        prepare.assert_not_called()
        self.assertIn("后端尚未配置 MYSQL_PASSWORD", output.getvalue())

    async def test_invalid_user_id_stops_before_database_access(self):
        with patch.object(chat_cli, "get_mysql_password", return_value="fake-server-password"):
            with patch("builtins.input", return_value="invalid"):
                with patch.object(chat_cli, "prepare_session") as prepare:
                    with redirect_stdout(io.StringIO()):
                        await chat_cli.main()
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
