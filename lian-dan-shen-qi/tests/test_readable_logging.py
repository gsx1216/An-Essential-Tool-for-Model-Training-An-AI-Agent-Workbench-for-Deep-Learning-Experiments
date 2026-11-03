"""参考项目布局的可读日志、准确源码定位、异常原因与敏感字段回归。"""

import asyncio
import io
import json
import logging
import os
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mysql.connector import Error as MySQLError

from app.logging_config import (
    DEFAULT_LOG_DIR, configure_logging, logged_operation, logging_context,
)
from app.security.tokens import TokenService


SECRET = "TEST_ONLY_SECRET_NEVER_LOG_THIS_VALUE"
logger = logging.getLogger("app.test_readable_logging")


class ReadableLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        configure_logging(self.directory, console=False, force=True, log_format="text")

    def tearDown(self):
        configure_logging(DEFAULT_LOG_DIR, console=False, force=True)
        self.temporary.cleanup()

    def contents(self):
        return "\n".join(path.read_text(encoding="utf-8") for path in self.directory.rglob("*.txt"))

    def test_default_configuration_writes_readable_text(self):
        with patch.dict(os.environ, {}, clear=True):
            configure_logging(self.directory, console=False, force=True)
        with logging_context():
            logger.info("operation_succeeded")
        text = self.contents()
        self.assertRegex(text, r"(?: UTC|[+-][0-9]{2}:[0-9]{2}) \| INFO")
        self.assertIn("执行成功", text)
        self.assertIn("MainThread", text)
        self.assertRegex(text, r"request_id=[0-9a-f]{32}")
        self.assertIn("tests/test_readable_logging.py", text)

    def test_decorated_success_points_to_original_function(self):
        @logged_operation("test.readable_success")
        def echo(value):
            return value

        self.assertEqual(echo(SECRET), SECRET)
        text = self.contents()
        self.assertIn(".echo:", text)
        self.assertIn("[tests/test_readable_logging.py]", text)
        self.assertIn("operation=test.readable_success", text)
        self.assertIn("duration_ms=", text)
        self.assertNotIn("logging_config.py", text)
        self.assertNotIn(SECRET, text)

    def test_async_success_keeps_async_call_contract_and_source(self):
        @logged_operation("test.readable_async")
        async def echo(value):
            await asyncio.sleep(0)
            return value

        self.assertEqual(asyncio.run(echo(SECRET)), SECRET)
        text = self.contents()
        self.assertIn(".echo:", text)
        self.assertIn("执行成功", text)
        self.assertNotIn(SECRET, text)

    def test_expected_rejection_points_to_actual_business_raise_line(self):
        @logged_operation("test.readable_rejection")
        def reject():
            TokenService().hash_token("")

        error_line = None
        try:
            reject()
        except ValueError as error:
            tb = error.__traceback__
            while tb is not None:
                if tb.tb_frame.f_code.co_name == "hash_token":
                    error_line = tb.tb_lineno
                tb = tb.tb_next
        else:
            self.fail("Invalid input must still raise ValueError")
        text = self.contents()
        self.assertIsNotNone(error_line)
        self.assertIn(f"app.security.tokens:hash_token:{error_line}", text)
        self.assertIn("[app/security/tokens.py]", text)
        self.assertIn("WARNING", text)
        self.assertIn("参数或业务校验未通过", text)
        self.assertNotIn("异常链[", text)

    def test_database_failure_explains_errno_without_raw_error_or_locals(self):
        @logged_operation("test.readable_database")
        def repository(value):
            raise MySQLError(value, errno=1146)

        with self.assertRaises(MySQLError):
            repository(SECRET)
        text = self.contents()
        self.assertIn("ERROR", text)
        self.assertIn("数据库表不存在", text)
        self.assertIn("errno=1146", text)
        self.assertIn("异常链[1]", text)
        self.assertIn(":repository:", text.splitlines()[0])
        self.assertNotIn(SECRET, text)

    def test_cause_chain_preserves_original_location_and_both_error_types(self):
        with logging_context():
            try:
                try:
                    raise MySQLError(SECRET, errno=2003)
                except MySQLError as error:
                    raise RuntimeError(SECRET) from error
            except RuntimeError:
                logger.exception("operation_failed")
        text = self.contents()
        self.assertIn("RuntimeError", text)
        self.assertIn("异常链[2]", text)
        self.assertIn("无法连接数据库服务", text)
        self.assertIn("tests/test_readable_logging.py:", text)
        self.assertNotIn(SECRET, text)

    def test_raw_messages_arguments_and_extra_are_not_rendered(self):
        with logging_context():
            logger.error(SECRET, SECRET, extra={"password": SECRET, "token": SECRET, "request": SECRET})
        text = self.contents()
        self.assertIn("应用事件", text)
        self.assertNotIn(SECRET, text)

    def test_threaded_failure_retains_request_id_and_is_logged_once(self):
        @logged_operation("test.thread_repository")
        def repository():
            raise MySQLError(SECRET, errno=2013)

        @logged_operation("test.thread_service")
        async def service():
            await asyncio.to_thread(repository)

        with logging_context(new=True) as context:
            request_id = context.request_id
            with self.assertRaises(MySQLError):
                asyncio.run(service())
        text = self.contents()
        self.assertIn("request_id=" + request_id, text)
        self.assertIn("asyncio_0", text)
        self.assertEqual(text.count("异常链[1]"), 1)
        self.assertNotIn(SECRET, text)

    def test_json_mode_remains_available_and_contains_source_and_thread(self):
        with patch.dict(os.environ, {"APP_LOG_FORMAT": "json"}):
            configure_logging(self.directory, console=False, force=True)
        with logging_context():
            logger.info("operation_succeeded")
        record = json.loads(self.contents())
        self.assertEqual(record["event"], "operation_succeeded")
        self.assertEqual(record["source"]["file"], "tests/test_readable_logging.py")
        self.assertGreater(record["source"]["line"], 0)
        self.assertEqual(record["thread_name"], "MainThread")

    def test_console_uses_same_readable_safe_layout(self):
        console = io.StringIO()
        with redirect_stderr(console):
            configure_logging(self.directory, console=True, force=True, log_format="text")
            with logging_context():
                logger.error(SECRET)
            # StreamHandler 保存创建时的流，在退出 redirect 前关闭它。
            configure_logging(DEFAULT_LOG_DIR, console=False, force=True)
        self.assertIn(" | ERROR", console.getvalue())
        self.assertIn("应用事件", console.getvalue())
        self.assertNotIn(SECRET, console.getvalue())
