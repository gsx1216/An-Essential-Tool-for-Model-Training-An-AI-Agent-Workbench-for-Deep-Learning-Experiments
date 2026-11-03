"""真实聊天路由和身份依赖的HTTP回归；身份业务和聊天业务均为模拟对象。"""

import unittest
from unittest.mock import create_autospec
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
import httpx
from mysql.connector import Error as MySQLError
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.api import dependencies
from app.api.chat_routes import router
from app.api.error_handlers import request_validation_exception_handler
from app.api.history_routes import router as history_router
from app.api.request_logging import RequestLoggingMiddleware
from app.errors import InvalidTokenError, ModelCredentialDecryptionError, ModelCredentialNotConfiguredError, SessionNotFoundError
from app.errors import NoPendingReplyError, RequestIdConflictError, UnfinishedTurnError
from app.security.tokens import TokenService
from app.services.current_user_service import CurrentUserService
from app.services.user_chat_service import UserChatService


SESSION = 'b4c4e67554ee4c1981740a1e901ec614'
SEND = f'/sessions/{SESSION}/messages'
RETRY = f'/sessions/{SESSION}/retry'
PRIVATE = 'TEST_ONLY_PRIVATE_VALUE'


class ChatApiTests(unittest.TestCase):
    def setUp(self):
        self.token = TokenService().create_token()
        self.second_token = TokenService().create_token()
        self.identity = create_autospec(CurrentUserService, instance=True, spec_set=True)

        async def identify(token):
            if token == self.token:
                return 7, 'alice'
            if token == self.second_token:
                return 8, 'bob'
            raise InvalidTokenError(PRIVATE)

        self.identity.get_current_user.side_effect = identify
        self.service = create_autospec(UserChatService, instance=True, spec_set=True)
        self.service.send_message.return_value = '假回复'
        self.service.retry_message.return_value = '重试后的假回复'
        self.factory_calls = []

        def factory():
            self.factory_calls.append(True)
            return self.service

        self.application = FastAPI()
        self.application.add_middleware(RequestLoggingMiddleware)
        self.application.add_exception_handler(RequestValidationError, request_validation_exception_handler)
        self.application.include_router(history_router)
        self.application.include_router(router)
        self.application.dependency_overrides[dependencies.get_current_user_service] = lambda: self.identity
        self.application.dependency_overrides[dependencies.get_user_chat_service] = factory
        self.client = TestClient(self.application)
        self.addCleanup(self.client.close)
        self.addCleanup(self.application.dependency_overrides.clear)
        self.request_id = uuid4()
        self.body = {'content': '假问题', 'request_id': str(self.request_id)}

    def post(self, path=SEND, headers=None, **options):
        if headers is None:
            headers = {'Authorization': 'Bearer ' + self.token}
        if path == SEND and 'json' not in options and 'content' not in options:
            options['json'] = self.body
        return self.client.post(path, headers=headers, **options)

    def assert_private(self, response):
        self.assertEqual(response.headers.get('Cache-Control'), 'no-store')
        self.assertEqual(response.headers.get('Pragma'), 'no-cache')
        self.assertTrue(response.headers.get('X-Request-ID'))

    def test_send_awaits_business_with_authenticated_owner_and_uuid_hex(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'content': '假回复'})
        self.assert_private(response)
        self.service.send_message.assert_awaited_once_with(
            user_id=7, session_id=SESSION, content='假问题', request_id=self.request_id.hex,
        )
        self.service.retry_message.assert_not_awaited()

    def test_retry_awaits_retry_without_new_question_or_request_id(self):
        response = self.post(RETRY)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'content': '重试后的假回复'})
        self.assert_private(response)
        self.service.retry_message.assert_awaited_once_with(user_id=7, session_id=SESSION)
        self.service.send_message.assert_not_awaited()

    def test_anonymous_requests_stop_before_chat_factory(self):
        for path in (SEND, RETRY):
            response = self.post(path, headers={})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers.get('WWW-Authenticate'), 'Bearer')
        self.assertFalse(self.factory_calls)
        self.service.send_message.assert_not_awaited()
        self.service.retry_message.assert_not_awaited()

    def test_invalid_token_stops_before_chat_factory(self):
        response = self.post(headers={'Authorization': 'Bearer ' + TokenService().create_token()})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.factory_calls)

    def test_wrong_authentication_scheme_is_rejected(self):
        response = self.post(headers={'Authorization': 'Basic ' + PRIVATE})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.factory_calls)
        self.identity.get_current_user.assert_not_awaited()

    def test_each_required_request_field_is_validated_before_business(self):
        for body in ({}, {'content': '假问题'}, {'request_id': self.request_id.hex}):
            with self.subTest(fields=tuple(body)):
                self.assertEqual(self.post(json=body).status_code, 422)
        self.service.send_message.assert_not_awaited()

    def test_invalid_uuid_and_content_type_are_safe_422(self):
        for body in ({'content': '假问题', 'request_id': PRIVATE},
                     {'content': {'secret': PRIVATE}, 'request_id': self.request_id.hex}):
            response = self.post(json=body)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(PRIVATE, response.text)
            self.assertTrue(all(set(item) == {'loc', 'type', 'msg'} for item in response.json()['detail']))
        self.service.send_message.assert_not_awaited()

    def test_body_cannot_override_identity_key_or_context(self):
        for field in ('user_id', 'api_key', 'history', 'messages', 'session_id'):
            with self.subTest(field=field):
                response = self.post(json={**self.body, field: PRIVATE})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(PRIVATE, response.text)
        self.service.send_message.assert_not_awaited()

    def test_query_owner_does_not_override_token_identity(self):
        self.assertEqual(self.post(params={'user_id': 999}).status_code, 200)
        self.assertEqual(self.service.send_message.await_args.kwargs['user_id'], 7)

    def test_two_tokens_use_their_own_user_ids(self):
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post(headers={'Authorization': 'Bearer ' + self.second_token}).status_code, 200)
        self.assertEqual([call.kwargs['user_id'] for call in self.service.send_message.await_args_list], [7, 8])

    def test_identity_database_failure_is_503_before_chat_factory(self):
        self.identity.get_current_user.side_effect = MySQLError(PRIVATE, errno=2003)
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'detail': '请稍后重试。'})
        self.assertFalse(self.factory_calls)

    def test_factory_configuration_failure_stops_before_business(self):
        def unavailable():
            raise HTTPException(status_code=503, detail='请稍后重试。')
        self.application.dependency_overrides[dependencies.get_user_chat_service] = unavailable
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.service.send_message.assert_not_awaited()

    def assert_business_error(self, error, status, detail):
        for path, method in ((SEND, self.service.send_message), (RETRY, self.service.retry_message)):
            with self.subTest(path=path, error=type(error).__name__):
                method.side_effect = error
                response = self.post(path)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {'detail': detail})
                self.assertNotIn(PRIVATE, response.text)
                self.assertTrue(response.headers.get('X-Request-ID'))
                method.side_effect = None

    def test_session_ownership_errors_are_safe_404(self):
        self.assert_business_error(SessionNotFoundError(PRIVATE), 404, '会话不存在或不属于当前用户。')

    def test_missing_key_and_business_input_errors_are_400(self):
        self.assert_business_error(ModelCredentialNotConfiguredError(PRIVATE), 400, '请先配置个人模型 API Key。')
        self.assert_business_error(ValueError(PRIVATE), 400, '请求参数不符合要求。')

    def test_conflicting_question_has_fixed_409_without_echoing_error(self):
        self.service.send_message.side_effect = RequestIdConflictError(PRIVATE)
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {
            'detail': '该请求编号已用于其他问题，请为新问题生成新的 request_id。',
        })
        self.assertNotIn(PRIVATE, response.text)
        self.assertTrue(response.headers.get('X-Request-ID'))
        self.service.retry_message.assert_not_awaited()

    def test_new_question_with_unfinished_turn_has_fixed_409(self):
        self.service.send_message.side_effect = UnfinishedTurnError(PRIVATE)
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {'detail': '当前会话还有未完成的问题，请先重试或新建聊天。'})
        self.assertNotIn(PRIVATE, response.text)
        self.assertTrue(response.headers.get('X-Request-ID'))

    def test_retry_without_question_has_specific_safe_400(self):
        self.service.retry_message.side_effect = NoPendingReplyError(PRIVATE)
        response = self.post(RETRY)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': '当前会话没有等待回复的问题。'})
        self.assertNotIn(PRIVATE, response.text)
        self.assertTrue(response.headers.get('X-Request-ID'))
        self.service.send_message.assert_not_awaited()

    def test_database_and_decryption_errors_are_safe_503(self):
        for error in (MySQLError(PRIVATE, errno=1146), ModelCredentialDecryptionError(PRIVATE)):
            self.assert_business_error(error, 503, '请稍后重试。')

    def test_timeout_is_safe_504(self):
        error = APITimeoutError(httpx.Request('POST', 'https://example.invalid/chat'))
        self.assert_business_error(error, 504, '模型响应超时，请稍后重试。')

    def test_provider_connection_error_is_safe_502(self):
        error = APIConnectionError(message=PRIVATE, request=httpx.Request('POST', 'https://example.invalid/chat'))
        self.assert_business_error(error, 502, '模型服务暂时不可用，请稍后重试。')

    def test_provider_authentication_limit_and_server_errors_are_classified(self):
        for status, result, detail in (
            (401, 400, '个人模型配置不可用，请检查 API Key。'),
            (403, 400, '个人模型配置不可用，请检查 API Key。'),
            (429, 429, '模型请求过于频繁，请稍后重试。'),
            (500, 502, '模型服务暂时不可用，请稍后重试。'),
        ):
            response = httpx.Response(status, request=httpx.Request('POST', 'https://example.invalid/chat'))
            self.assert_business_error(APIStatusError(PRIVATE, response=response, body={'secret': PRIVATE}), result, detail)

    def test_unknown_errors_are_safe_500(self):
        self.assert_business_error(RuntimeError(PRIVATE), 500, '请稍后重试。')

    def test_openapi_has_bearer_security_and_history_and_send_can_coexist(self):
        paths = self.client.get('/openapi.json').json()['paths']
        self.assertIn('get', paths['/sessions/{session_id}/messages'])
        self.assertIn('post', paths['/sessions/{session_id}/messages'])
        for path in ('/sessions/{session_id}/messages', '/sessions/{session_id}/retry'):
            self.assertEqual(paths[path]['post']['security'], [{'BearerAuth': []}])
            self.assertIn('409', paths[path]['post']['responses'])
