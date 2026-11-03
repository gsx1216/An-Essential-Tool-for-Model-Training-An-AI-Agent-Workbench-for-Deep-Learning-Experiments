"""Typed chat rejections: safe reasons, warning severity, source and cross-thread deduplication."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi import HTTPException

from app.api.chat_routes import _raise_chat_http_error
from app.errors import NoPendingReplyError, RequestIdConflictError, UnfinishedTurnError
from app.logging_config import DEFAULT_LOG_DIR, configure_logging, logged_operation, logging_context


SECRET = 'TEST_ONLY_NEVER_LOG_QUESTION_OR_KEY'
CASES = (
    (RequestIdConflictError, '同一请求编号被用于不同问题', 409),
    (UnfinishedTurnError, '会话存在尚未完成的提问', 409),
    (NoPendingReplyError, '会话没有等待回复的提问', 400),
)


class ChatBusinessLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        configure_logging(DEFAULT_LOG_DIR, console=False, force=True)
        self.temporary.cleanup()

    def exercise_rejections(self, log_format):
        configure_logging(self.directory, console=False, force=True, log_format=log_format)
        requests = []
        for error_class, reason, status in CASES:
            with self.subTest(error=error_class.__name__):
                @logged_operation('test.chat_rejection_repository')
                def repository():
                    raise error_class(SECRET)

                @logged_operation('test.chat_rejection_service')
                async def service():
                    await asyncio.to_thread(repository)

                with logging_context(new=True) as context:
                    requests.append(context.request_id)
                    try:
                        asyncio.run(service())
                    except error_class as error:
                        with self.assertRaises(HTTPException) as caught:
                            _raise_chat_http_error(error, 'chat_routes.test')
                        self.assertEqual(caught.exception.status_code, status)
                        self.assertNotIn(SECRET, caught.exception.detail)
                    else:
                        self.fail('The business rejection must reach the caller.')
        contents = '\n'.join(p.read_text(encoding='utf-8') for p in self.directory.rglob('*.txt'))
        self.assertNotIn(SECRET, contents)
        return contents, requests

    def test_text_reasons_are_specific_without_logging_raw_exception_or_secrets(self):
        contents, requests = self.exercise_rejections('text')
        self.assertEqual(contents.count('WARNING'), len(CASES))
        self.assertNotIn(' | ERROR', contents)
        for (_, reason, _), request_id in zip(CASES, requests):
            self.assertIn('原因：' + reason, contents)
            self.assertEqual(contents.count('request_id=' + request_id), 1)
        self.assertIn('tests/test_chat_business_logging.py', contents)
        self.assertIn('operation=test.chat_rejection_repository', contents)

    def test_json_reasons_preserve_error_type_and_deduplicate_across_layers(self):
        contents, requests = self.exercise_rejections('json')
        records = [json.loads(line) for line in contents.splitlines() if line]
        self.assertEqual(len(records), len(CASES))
        for record, (error_class, reason, _), request_id in zip(records, CASES, requests):
            self.assertEqual(record['level'], 'WARNING')
            self.assertEqual(record['error_type'], error_class.__name__)
            self.assertEqual(record['reason'], reason)
            self.assertEqual(record['request_id'], request_id)
            self.assertEqual(record['source']['file'], 'tests/test_chat_business_logging.py')
            self.assertGreater(record['source']['line'], 0)
            self.assertNotIn('exceptions', record)


if __name__ == '__main__':
    unittest.main()
