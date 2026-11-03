"""服务器 HTTP 的鉴权、秘密字段隔离、异常状态和依赖组装。"""

from datetime import datetime, timezone
import unittest
from unittest.mock import create_autospec, patch

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app.api import dependencies
from app.api.error_handlers import request_validation_exception_handler
from app.api.request_logging import RequestLoggingMiddleware
from app.api.server_connection_routes import router
from app.errors import (InvalidTokenError, ServerAddressNotAllowedError, ServerAuthenticationError, ServerBusyError,
                        ServerConnectionError, ServerConnectionTimeoutError, ServerCredentialDecryptionError,
                        ServerHostKeyError, ServerNotFoundError)
from app.models.server_connection import ServerPage, ServerRecord
from app.security.tokens import TokenService
from app.services.current_user_service import CurrentUserService
from app.services.server_connection_service import ServerConnectionService
from tests.test_server_connection_core import ID, PIN, profile


class ServerApiTests(unittest.TestCase):
    def setUp(self):
        self.token = TokenService().create_token()
        identity = create_autospec(CurrentUserService, instance=True, spec_set=True)
        async def identify(token):
            if token != self.token:
                raise InvalidTokenError('PRIVATE_TOKEN')
            return 7, 'alice'
        identity.get_current_user.side_effect = identify
        self.service = create_autospec(ServerConnectionService, instance=True, spec_set=True)
        self.item = ServerRecord(**vars(profile()), encrypted_credentials='PRIVATE_CIPHERTEXT')
        self.service.create.return_value = self.item
        self.service.get.return_value = self.item
        self.service.list.return_value = ServerPage((self.item,), None)
        self.service.test_connection.return_value = dict(server_id=ID, connected=True, auth_method='password',
                                                        fingerprint_verified=True, checked_at=datetime.now(timezone.utc), duration_ms=1.0)
        self.application = FastAPI()
        self.application.add_middleware(RequestLoggingMiddleware)
        self.application.add_exception_handler(RequestValidationError, request_validation_exception_handler)
        self.application.include_router(router)
        self.application.dependency_overrides[dependencies.get_current_user_service] = lambda: identity
        self.application.dependency_overrides[dependencies.get_server_connection_service] = lambda: self.service
        self.client = TestClient(self.application)
        self.addCleanup(self.client.close)
        self.addCleanup(self.application.dependency_overrides.clear)
        self.headers = {'Authorization': 'Bearer ' + self.token}
        self.body = dict(name='test', host='127.0.0.1', username='trainer', auth_method='password', password='PRIVATE_PASSWORD',
                          host_fingerprint=PIN, work_dir='/tmp/training')

    def test_create_passes_authenticated_owner_and_plaintext_only_to_service(self):
        reply = self.client.post('/servers?user_id=8', headers=self.headers, json=self.body)
        self.assertEqual(reply.status_code, 201)
        args, kwargs = self.service.create.call_args
        self.assertEqual(args, (7,))
        self.assertEqual(kwargs['password'], 'PRIVATE_PASSWORD')
        self.assertEqual(kwargs['port'], 22)
        self.assertEqual(reply.headers['Location'], '/servers/' + ID)
        self.assertNotIn('PRIVATE', reply.text)

    def test_list_detail_and_test_never_expose_cipher_or_credentials(self):
        for method, path in (('get', '/servers'), ('get', '/servers/' + ID), ('post', '/servers/' + ID + '/test')):
            reply = getattr(self.client, method)(path, headers=self.headers)
            self.assertEqual(reply.status_code, 200)
            self.assertNotIn('PRIVATE_CIPHERTEXT', reply.text)
            self.assertNotIn('encrypted_credentials', reply.text)
            self.assertEqual(reply.headers['Cache-Control'], 'no-store')
        self.service.test_connection.assert_awaited_once_with(7, ID)

    def test_pagination_and_delete_use_current_owner(self):
        reply = self.client.get('/servers', headers=self.headers, params={'limit': 2, 'before_id': ID})
        self.assertEqual(reply.status_code, 200)
        self.service.list.assert_awaited_once_with(7, limit=2, before_id=ID)
        reply = self.client.delete('/servers/' + ID, headers=self.headers)
        self.assertEqual(reply.status_code, 204)
        self.assertEqual(reply.content, b'')
        self.service.delete.assert_awaited_once_with(7, ID)

    def test_all_operations_reject_missing_or_invalid_bearer(self):
        for headers in ({}, {'Authorization': 'Bearer wrong'}):
            for method, path, options in (('post', '/servers', {'json': self.body}), ('get', '/servers', {}),
                                          ('get', '/servers/' + ID, {}), ('delete', '/servers/' + ID, {}),
                                          ('post', '/servers/' + ID + '/test', {})):
                reply = getattr(self.client, method)(path, headers=headers, **options)
                self.assertEqual(reply.status_code, 401)
        self.service.create.assert_not_awaited()
        self.service.test_connection.assert_not_awaited()

    def test_invalid_structure_port_uuid_and_extra_command_are_422(self):
        for body in ({**self.body, 'user_id': 8}, {**self.body, 'command': 'train'}, {**self.body, 'port': True},
                     {**self.body, 'port': 65536}, {**self.body, 'auth_method': 'none'}, {**self.body, 'username': ''}):
            reply = self.client.post('/servers', headers=self.headers, json=body)
            self.assertEqual(reply.status_code, 422)
            self.assertNotIn('PRIVATE_PASSWORD', reply.text)
        self.assertEqual(self.client.get('/servers/bad-id', headers=self.headers).status_code, 422)
        self.assertEqual(self.client.get('/servers?limit=101', headers=self.headers).status_code, 422)
        self.service.create.assert_not_awaited()

    def test_known_ssh_errors_have_safe_specific_statuses(self):
        for error, status in ((ServerNotFoundError('PRIVATE'), 404), (ServerAddressNotAllowedError('PRIVATE'), 403),
                              (ServerAuthenticationError('PRIVATE'), 400), (ServerHostKeyError('PRIVATE'), 409),
                              (ServerBusyError('PRIVATE'), 429), (ServerConnectionError('PRIVATE'), 502),
                              (ServerConnectionTimeoutError('PRIVATE'), 504), (ServerCredentialDecryptionError('PRIVATE'), 503)):
            self.service.test_connection.side_effect = error
            reply = self.client.post('/servers/' + ID + '/test', headers=self.headers)
            self.assertEqual(reply.status_code, status)
            self.assertNotIn('PRIVATE', reply.text)

    def test_database_failure_is_generic_503(self):
        self.service.create.side_effect = MySQLError(msg='PRIVATE_SQL', errno=1146)
        reply = self.client.post('/servers', headers=self.headers, json=self.body)
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(reply.json(), {'detail': '请稍后重试。'})

    def test_formal_entrypoint_mounts_all_operations_with_bearer(self):
        from app.main import app
        paths = app.openapi()['paths']
        for path, method in (('/servers', 'get'), ('/servers', 'post'), ('/servers/{server_id}', 'get'),
                              ('/servers/{server_id}', 'delete'), ('/servers/{server_id}/test', 'post')):
            self.assertEqual(paths[path][method]['security'], [{'BearerAuth': []}])


class ServerDependencyTests(unittest.TestCase):
    def tearDown(self):
        dependencies.get_server_connection_service.cache_clear()

    def test_configuration_failure_is_safe_503(self):
        dependencies.get_server_connection_service.cache_clear()
        with patch.object(dependencies, 'get_mysql_password', side_effect=ValueError('PRIVATE')):
            with self.assertRaises(HTTPException) as result:
                dependencies.get_server_connection_service()
        self.assertEqual(result.exception.status_code, 503)
        self.assertEqual(result.exception.detail, '请稍后重试。')


if __name__ == '__main__':
    unittest.main()
