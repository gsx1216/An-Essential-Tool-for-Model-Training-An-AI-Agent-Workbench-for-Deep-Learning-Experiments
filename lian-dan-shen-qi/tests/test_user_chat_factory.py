"""个人聊天依赖工厂回归：模拟配置，不连接数据库或模型。"""

import inspect
import unittest
from unittest.mock import create_autospec, patch

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import dependencies
from app.repositories.message_repository import MessageRepository
from app.services.model_credential_service import ModelCredentialService
from app.services.user_chat_service import UserChatService
from app.services.user_model_gateway_factory import UserModelGatewayFactory


class UserChatFactoryChecks(unittest.TestCase):
    def setUp(self):
        self.factory = dependencies.get_user_chat_service
        self.factory.cache_clear()
        self.addCleanup(self.factory.cache_clear)
        self.credentials = create_autospec(ModelCredentialService, instance=True, spec_set=True)
        password_patch = patch.object(dependencies, 'get_mysql_password', return_value='TEST_ONLY_UNUSED_PASSWORD')
        self.password = password_patch.start()
        self.addCleanup(password_patch.stop)
        credential_patch = patch.object(dependencies, 'get_model_credential_service', return_value=self.credentials)
        self.credential_provider = credential_patch.start()
        self.addCleanup(credential_patch.stop)

    def assert_unavailable(self):
        with self.assertRaises(HTTPException) as caught:
            self.factory()
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail, '请稍后重试。')

    def test_assembles_actual_collaborators_without_connecting_database(self):
        with patch('app.repositories.message_repository.get_connection') as connect:
            service = self.factory()
        self.assertIsInstance(service, UserChatService)
        self.assertIsInstance(service.gateway_factory, UserModelGatewayFactory)
        self.assertIs(service.gateway_factory.credential_service, self.credentials)
        self.assertIsInstance(service.repository, MessageRepository)
        self.assertEqual(service.repository.password, 'TEST_ONLY_UNUSED_PASSWORD')
        connect.assert_not_called()

    def test_successful_assembly_is_cached(self):
        first = self.factory()
        self.assertIs(self.factory(), first)
        self.password.assert_called_once_with()
        self.credential_provider.assert_called_once_with()

    def test_factory_has_no_user_or_request_parameters(self):
        self.assertEqual(len(inspect.signature(self.factory).parameters), 0)
        self.assertIsInstance(self.factory(), UserChatService)

    def test_assembly_never_reads_personal_keys_or_constructs_model_sdk(self):
        with patch('app.services.model_gateway.ChatDeepSeek') as sdk:
            self.factory()
        self.credentials.get_api_key.assert_not_called()
        self.credentials.is_configured.assert_not_called()
        sdk.assert_not_called()

    def test_mysql_configuration_failure_is_generic_503(self):
        self.password.side_effect = ValueError('TEST_ONLY_PRIVATE_MYSQL_CONFIG')
        self.assert_unavailable()
        self.credential_provider.assert_not_called()

    def test_existing_credential_factory_http_failure_propagates(self):
        error = HTTPException(status_code=503, detail='请稍后重试。')
        self.credential_provider.side_effect = error
        with self.assertRaises(HTTPException) as caught:
            self.factory()
        self.assertIs(caught.exception, error)

    def test_collaborator_value_error_is_generic_503(self):
        with patch.object(dependencies, 'MessageRepository', side_effect=ValueError('TEST_ONLY_PRIVATE_ASSEMBLY')):
            self.assert_unavailable()

    def test_failed_assembly_is_not_cached_and_can_recover(self):
        self.password.side_effect = [ValueError('TEST_ONLY_MISSING_CONFIG'), 'TEST_ONLY_UNUSED_PASSWORD']
        self.assert_unavailable()
        service = self.factory()
        self.assertIs(self.factory(), service)
        self.assertEqual(self.password.call_count, 2)
        self.credential_provider.assert_called_once_with()

    def test_depends_injects_the_assembled_service(self):
        application = FastAPI()
        services = []

        @application.get('/_factory_check')
        async def endpoint(service: UserChatService = Depends(self.factory)):
            services.append(service)
            return {'assembled': isinstance(service, UserChatService)}

        with TestClient(application) as client:
            response = client.get('/_factory_check')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'assembled': True})
        self.assertIs(services[0], self.factory())
        self.credentials.get_api_key.assert_not_called()

    def test_dependency_configuration_failure_stops_before_endpoint(self):
        application = FastAPI()
        entered = []

        @application.get('/_factory_check')
        async def endpoint(service: UserChatService = Depends(self.factory)):
            entered.append(True)
            return {'unexpected': True}

        self.password.side_effect = ValueError('TEST_ONLY_PRIVATE_SERVER_CONFIGURATION')
        with TestClient(application) as client:
            response = client.get('/_factory_check')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'detail': '请稍后重试。'})
        self.assertFalse(entered)

