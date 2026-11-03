"""日志持久化、异常去重、并发关联、敏感信息隔离与轮转测试。"""

import asyncio
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
import logging
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import traceback
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app.api.dependencies import get_auth_service, get_login_service
from app.api.request_logging import RequestLoggingMiddleware
from app.logging_config import (
    DEFAULT_LOG_DIR, DailyFileHandler, SafeJsonFormatter,
    configure_logging, log_failure, logged_operation, logging_context,
)
from app.main import app
from app.errors import InvalidCredentialsError
from app.services.auth_service import AuthService
from app.services.login_service import LoginService


SECRET = "TEST_ONLY_PRIVATE_VALUE_MUST_NOT_BE_LOGGED_123"
logger = logging.getLogger("app.test_logging")


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        # 本文件检查结构化字段；可读文本格式由 test_readable_logging 单独验证。
        configure_logging(self.directory, console=False, force=True, log_format="json")
        self.original_overrides = dict(app.dependency_overrides)

    def tearDown(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.original_overrides)
        configure_logging(DEFAULT_LOG_DIR, console=False, force=True)
        self.temporary.cleanup()

    def contents(self):
        return "\n".join(path.read_text(encoding="utf-8") for path in self.directory.rglob("*.txt"))

    def records(self):
        return [json.loads(line) for line in self.contents().splitlines() if line]

    def service(self, error):
        service = Mock(spec=LoginService)
        service.login = AsyncMock(side_effect=error)
        app.dependency_overrides[get_login_service] = lambda: service

    def test_success_is_persisted_without_arguments_or_return_content(self):
        @logged_operation("test.echo")
        def echo(value):
            return value

        self.assertEqual(echo(SECRET), SECRET)
        self.assertTrue(list(self.directory.rglob("*.txt")))
        self.assertNotIn(SECRET, self.contents())
        record = self.records()[-1]
        self.assertEqual(record["event"], "operation_succeeded")
        self.assertEqual(record["operation"], "test.echo")
        self.assertRegex(record["request_id"], r"^[0-9a-f]{32}$")

    def test_sync_failure_inside_async_service_is_logged_once(self):
        @logged_operation("test.repository")
        def repository():
            raise MySQLError(SECRET, errno=2003)

        @logged_operation("test.service")
        async def service():
            await asyncio.to_thread(repository)

        with self.assertRaises(MySQLError):
            asyncio.run(service())
        errors = [record for record in self.records() if "exceptions" in record]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["exceptions"][0]["errno"], 2003)
        self.assertTrue(errors[0]["exceptions"][0]["frames"])
        self.assertNotIn(SECRET, self.contents())

    def test_exception_chain_has_locations_but_no_exception_text_or_locals(self):
        with logging_context():
            try:
                try:
                    raise ValueError(SECRET)
                except ValueError as error:
                    raise RuntimeError(SECRET) from error
            except RuntimeError:
                logger.exception("operation_failed")
        errors = self.records()[-1]["exceptions"]
        self.assertEqual([error["type"] for error in errors], ["RuntimeError", "ValueError"])
        self.assertTrue(all(error["frames"][0]["line"] > 0 for error in errors))
        self.assertNotIn(SECRET, self.contents())
        for error in errors:
            for frame in error["frames"]:
                self.assertEqual(set(frame), {"module", "file", "function", "line"})

    def test_arbitrary_messages_parameters_and_extra_are_not_serialized(self):
        with logging_context():
            logger.error(SECRET, SECRET, extra={"password": SECRET, "token": SECRET, "body": SECRET})
        self.assertNotIn(SECRET, self.contents())
        self.assertEqual(self.records()[-1]["event"], "application_event")

    def test_health_response_and_logs_share_server_generated_request_id(self):
        with TestClient(app) as client:
            response = client.get("/health?api_key=" + SECRET, headers={"X-Request-ID": SECRET, "Authorization": SECRET})
        self.assertEqual(response.status_code, 200)
        request_id = response.headers["X-Request-ID"]
        self.assertRegex(request_id, r"^[0-9a-f]{32}$")
        record = self.records()[-1]
        self.assertEqual(record["request_id"], request_id)
        self.assertEqual(record["route"], "/health")
        self.assertNotIn(SECRET, self.contents())

    def test_unmatched_raw_path_is_not_logged(self):
        with TestClient(app) as client:
            response = client.get("/" + SECRET)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.records()[-1]["route"], "<unmatched>")
        self.assertNotIn(SECRET, self.contents())

    def test_database_failure_still_returns_generic_503_and_writes_safe_trace(self):
        self.service(MySQLError(SECRET, errno=2003))
        with TestClient(app) as client:
            response = client.post("/auth/login", json={"username": "alice", "password": SECRET})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        errors = [record for record in self.records() if "exceptions" in record]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["request_id"], response.headers["X-Request-ID"])
        self.assertNotIn(SECRET, self.contents())

    def test_expected_login_rejection_is_warning_without_error_trace(self):
        self.service(InvalidCredentialsError(SECRET))
        with TestClient(app) as client:
            response = client.post("/auth/login", json={"username": "alice", "password": SECRET})
        self.assertEqual(response.status_code, 401)
        records = self.records()
        self.assertTrue(any(record["event"] == "operation_rejected" for record in records))
        self.assertFalse(any("exceptions" in record for record in records))
        self.assertNotIn(SECRET, self.contents())

    def test_invalid_request_does_not_log_password_body(self):
        self.service(ValueError("unused"))
        with TestClient(app) as client:
            response = client.post("/auth/login", json={"password": {"secret": SECRET}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(SECRET, response.text)
        self.assertNotIn(SECRET, self.contents())

    def test_unhandled_error_is_persisted_and_returns_generic_500(self):
        isolated = FastAPI()
        isolated.add_middleware(RequestLoggingMiddleware)

        @isolated.get("/broken")
        async def broken():
            raise RuntimeError(SECRET)

        with TestClient(isolated) as client:
            response = client.get("/broken")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertTrue(any(record["event"] == "unhandled_exception" for record in self.records()))
        self.assertNotIn(SECRET, self.contents())

    def test_dependency_configuration_failure_is_also_captured(self):
        get_auth_service.cache_clear()
        get_login_service.cache_clear()
        try:
            with patch("app.api.dependencies.get_mysql_password", side_effect=ValueError(SECRET)):
                with TestClient(app) as client:
                    response = client.post("/auth/login", json={"username": "alice", "password": SECRET})
        finally:
            get_auth_service.cache_clear()
            get_login_service.cache_clear()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertTrue(any("exceptions" in record for record in self.records()))
        self.assertNotIn(SECRET, self.contents())

    def test_concurrent_contexts_remain_distinct_across_threads(self):
        async def worker():
            with logging_context(new=True) as context:
                await asyncio.to_thread(logger.info, "operation_succeeded")
                await asyncio.sleep(0)
                logger.info("operation_succeeded")
                return context.request_id

        async def run():
            return await asyncio.gather(*(worker() for _ in range(6)))

        ids = asyncio.run(run())
        self.assertEqual(len(set(ids)), 6)
        records = self.records()
        for request_id in ids:
            self.assertEqual(sum(record["request_id"] == request_id for record in records), 2)

    def test_same_day_all_records_share_one_txt_without_size_rotation(self):
        handler = DailyFileHandler(self.directory)
        handler.setFormatter(SafeJsonFormatter())
        try:
            for _ in range(20):
                handler.handle(logging.LogRecord("app.test", logging.INFO, __file__, 1, "operation_succeeded", (), None))
        finally:
            handler.close()
        files = list(self.directory.rglob("*.txt"))
        self.assertEqual(len(files), 1)
        self.assertEqual(len(files[0].read_text(encoding="utf-8").splitlines()),20)
        self.assertTrue(all(json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines()))

    def test_app_worker_main_and_server_errors_share_same_daily_sink_once(self):
        with logging_context():
            for name in ('app.worker','__main__','uvicorn.error'):
                logging.getLogger(name).warning('operation_succeeded')
        self.assertEqual(len(self.records()),3)
        self.assertEqual({r['logger'] for r in self.records()},{'app.worker','__main__','uvicorn.error'})
        self.assertEqual(len(list(self.directory.rglob('app.txt'))),1)

    def test_local_day_boundary_creates_one_file_for_each_day(self):
        handler=DailyFileHandler(self.directory)
        handler.setFormatter(SafeJsonFormatter())
        try:
            for hour in (23,24):
                record=logging.LogRecord('app.test',logging.INFO,__file__,1,'operation_succeeded',(),None)
                record.created=(datetime.now().replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(hours=hour)).timestamp()
                handler.handle(record)
        finally:handler.close()
        files=list(self.directory.rglob('app.txt'))
        self.assertEqual(len(files),2)
        self.assertTrue(all(len(p.read_text(encoding='utf-8').splitlines())==1 for p in files))

    def test_response_diagnostics_explain_empty_text_without_saving_model_content(self):
        from app.models.analysis import ModelReplyError
        with logging_context():
            logger.info('model_response_received',extra={'content_kind':'text','content_chars':0,'content_bytes':0,
                'content_blank':True,'finish_reason':'length','reasoning_chars':1234,'output_tokens':4096,
                'reasoning_tokens':4096,'raw_reply':SECRET})
            try:raise ModelReplyError('EMPTY_MODEL_OUTPUT')
            except ModelReplyError as error:log_failure(logger,error,operation='fixture.reply')
        records=self.records()
        self.assertEqual(records[0]['finish_reason'],'length')
        self.assertEqual(records[0]['reasoning_chars'],1234)
        self.assertIn('正文为空',records[1]['reason'])
        self.assertNotIn(SECRET,self.contents())

    def test_retention_only_removes_old_owned_log_files(self):
        old = self.directory / (datetime.now(timezone.utc).date() - timedelta(days=9)).isoformat()
        old.mkdir()
        owned = old / "app-12345.log"
        owned.write_text("old", encoding="utf-8")
        unrelated = old / "notes.txt"
        unrelated.write_text("keep", encoding="utf-8")
        with logging_context():
            logger.info("operation_succeeded")
        self.assertFalse(owned.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_file_output_failure_does_not_break_business_or_expose_record(self):
        blocker = self.directory / "not-a-directory"
        blocker.write_text("file", encoding="utf-8")
        configure_logging(blocker, console=False, force=True)

        @logged_operation("test.echo")
        def echo():
            return SECRET

        console = io.StringIO()
        with redirect_stderr(console):
            self.assertEqual(echo(), SECRET)
        self.assertIn("log output failed", console.getvalue())
        self.assertNotIn(SECRET, console.getvalue())

    def test_cleanup_failure_does_not_discard_current_event(self):
        handler = DailyFileHandler(self.directory)
        handler.setFormatter(SafeJsonFormatter())
        console = io.StringIO()
        try:
            with patch.object(handler, "_cleanup", side_effect=PermissionError(SECRET)):
                with redirect_stderr(console):
                    handler.handle(logging.LogRecord("app.test", logging.INFO, __file__, 1, "operation_succeeded", (), None))
        finally:
            handler.close()
        self.assertEqual(self.records()[-1]["event"], "operation_succeeded")
        self.assertNotIn(SECRET, console.getvalue())

    def test_multiple_processes_write_one_daily_file(self):
        code = (
            "import logging,sys; from app.logging_config import configure_logging; "
            "configure_logging(sys.argv[1],console=False,log_format='json'); "
            "logging.getLogger('app.child').info('operation_succeeded')"
        )
        processes = [subprocess.Popen([sys.executable, "-B", "-c", code, str(self.directory)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for process in processes:
            output, error = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, (output, error))
        self.assertEqual(len(list(self.directory.rglob("*.txt"))), 1)
        self.assertEqual(len(self.records()),2)

    def test_failure_after_response_start_does_not_send_second_response(self):
        messages = []

        async def broken(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise RuntimeError(SECRET)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        middleware = RequestLoggingMiddleware(broken)
        try:
            asyncio.run(middleware({"type": "http", "method": "GET", "path": "/unused"}, receive, send))
        except RuntimeError as error:
            self.assertTrue(error.__suppress_context__)
            self.assertNotIn(SECRET, "".join(traceback.format_exception(error)))
        else:
            self.fail("Stream failure must terminate the response")
        self.assertEqual(len(messages), 1)
        self.assertNotIn(SECRET, self.contents())
