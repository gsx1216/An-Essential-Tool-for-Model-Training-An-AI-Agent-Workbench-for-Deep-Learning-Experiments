"""聊天接口已提供的异常转换回归；只构造本地错误，不发送HTTP请求。"""

import unittest

from fastapi import HTTPException
import httpx
from mysql.connector import Error as MySQLError
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError

from app.api.chat_routes import _raise_chat_http_error
from app.errors import (
    ModelCredentialDecryptionError, ModelCredentialNotConfiguredError,
    NoPendingReplyError, RequestIdConflictError, SessionNotFoundError, UnfinishedTurnError,
)


PRIVATE = 'TEST_ONLY_PRIVATE_PROVIDER_BODY'


class ChatHttpErrorTests(unittest.TestCase):
    def assert_conversion(self, error, status, detail):
        with self.assertRaises(HTTPException) as caught:
            _raise_chat_http_error(error, 'chat_routes.test')
        self.assertEqual(caught.exception.status_code, status)
        self.assertEqual(caught.exception.detail, detail)
        self.assertIs(caught.exception.__cause__, error)
        self.assertNotIn(PRIVATE, caught.exception.detail)

    def provider_error(self, status):
        request = httpx.Request('POST', 'https://example.invalid/chat')
        response = httpx.Response(status, request=request)
        return APIStatusError(PRIVATE, response=response, body={'error': PRIVATE})

    def test_session_not_found_is_404(self):
        self.assert_conversion(SessionNotFoundError(PRIVATE), 404, '会话不存在或不属于当前用户。')

    def test_missing_personal_key_is_400(self):
        self.assert_conversion(ModelCredentialNotConfiguredError(PRIVATE), 400, '请先配置个人模型 API Key。')

    def test_decryption_failure_is_generic_503(self):
        self.assert_conversion(ModelCredentialDecryptionError(PRIVATE), 503, '请稍后重试。')

    def test_database_failure_is_generic_503(self):
        self.assert_conversion(MySQLError(PRIVATE, errno=1146), 503, '请稍后重试。')

    def test_invalid_business_input_is_400(self):
        self.assert_conversion(ValueError(PRIVATE), 400, '请求参数不符合要求。')

    def test_request_id_conflict_has_fixed_safe_409(self):
        self.assert_conversion(RequestIdConflictError(PRIVATE), 409,
                               '该请求编号已用于其他问题，请为新问题生成新的 request_id。')

    def test_unfinished_turn_has_actionable_409(self):
        self.assert_conversion(UnfinishedTurnError(PRIVATE), 409,
                               '当前会话还有未完成的问题，请先重试或新建聊天。')

    def test_no_pending_reply_has_actionable_400(self):
        self.assert_conversion(NoPendingReplyError(PRIVATE), 400, '当前会话没有等待回复的问题。')

    def test_timeout_is_504_instead_of_generic_connection_failure(self):
        error = APITimeoutError(httpx.Request('POST', 'https://example.invalid/chat'))
        self.assert_conversion(error, 504, '模型响应超时，请稍后重试。')

    def test_connection_failure_is_502(self):
        error = APIConnectionError(message=PRIVATE, request=httpx.Request('POST', 'https://example.invalid/chat'))
        self.assert_conversion(error, 502, '模型服务暂时不可用，请稍后重试。')

    def test_provider_authentication_rejection_is_personal_configuration_error(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.assert_conversion(self.provider_error(status), 400, '个人模型配置不可用，请检查 API Key。')

    def test_provider_rate_limit_is_429(self):
        self.assert_conversion(self.provider_error(429), 429, '模型请求过于频繁，请稍后重试。')

    def test_other_provider_errors_are_safe_502(self):
        for error in (self.provider_error(400), self.provider_error(500), OpenAIError(PRIVATE)):
            with self.subTest(kind=type(error).__name__):
                self.assert_conversion(error, 502, '模型服务暂时不可用，请稍后重试。')
