"""个人Key工厂回归：临时密钥、模拟配置，无真实数据库连接。"""

import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import dependencies
from app.errors import ModelCredentialConfigurationError
from app.repositories.model_credential_repository import ModelCredentialRepository
from app.security.api_key_cipher import ApiKeyCipher
from app.services.model_credential_service import ModelCredentialService


class ModelCredentialFactoryTests(unittest.TestCase):
    def setUp(self):
        self.factory = dependencies.get_model_credential_service
        self.factory.cache_clear()
        self.addCleanup(self.factory.cache_clear)
        self.master_key = Fernet.generate_key().decode("ascii")

    def assert_generic_unavailable(self, operation):
        with self.assertRaises(HTTPException) as caught:
            operation()
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail, "请稍后重试。")

    def test_assembles_correct_objects_and_reuses_service_without_database_io(self):
        with patch.object(dependencies, "get_mysql_password", return_value="TEST_ONLY_UNUSED_PASSWORD") as password:
            with patch.object(dependencies, "get_model_encryption_key", return_value=self.master_key) as master:
                with patch("app.repositories.model_credential_repository.get_connection") as connect:
                    first = self.factory()
                    second = self.factory()
        self.assertIs(first, second)
        self.assertIsInstance(first, ModelCredentialService)
        self.assertIsInstance(first.repository, ModelCredentialRepository)
        self.assertIsInstance(first.cipher, ApiKeyCipher)
        self.assertEqual(first.repository.password, "TEST_ONLY_UNUSED_PASSWORD")
        value = first.cipher.encrypt(7, "sk-TEST_ONLY_FAKE_KEY")
        self.assertEqual(ApiKeyCipher(self.master_key).decrypt(7, value), "sk-TEST_ONLY_FAKE_KEY")
        password.assert_called_once_with()
        master.assert_called_once_with()
        connect.assert_not_called()

    def test_missing_master_key_becomes_generic_503(self):
        with patch.object(dependencies, "get_mysql_password", return_value="unused"):
            with patch.object(dependencies, "get_model_encryption_key", side_effect=ModelCredentialConfigurationError("TEST_ONLY_PRIVATE_CONFIG")):
                self.assert_generic_unavailable(self.factory)

    def test_missing_mysql_password_becomes_generic_503(self):
        with patch.object(dependencies, "get_mysql_password", side_effect=ValueError("TEST_ONLY_PRIVATE_MYSQL_CONFIG")):
            self.assert_generic_unavailable(self.factory)

    def test_cipher_initialization_error_becomes_generic_503(self):
        with patch.object(dependencies, "get_mysql_password", return_value="unused"):
            with patch.object(dependencies, "get_model_encryption_key", return_value="TEST_ONLY_INVALID_MASTER"):
                self.assert_generic_unavailable(self.factory)

    def test_failed_construction_is_not_cached_and_can_recover(self):
        with patch.object(dependencies, "get_mysql_password", return_value="unused"):
            with patch.object(dependencies, "get_model_encryption_key", side_effect=[
                ModelCredentialConfigurationError("TEST_ONLY_MISSING_MASTER"), self.master_key,
            ]) as master:
                self.assert_generic_unavailable(self.factory)
                service = self.factory()
                self.assertIs(self.factory(), service)
        self.assertEqual(master.call_count, 2)

    def test_factory_does_not_require_a_global_deepseek_api_key(self):
        with patch.object(dependencies, "get_mysql_password", return_value="unused"):
            with patch.object(dependencies, "get_model_encryption_key", return_value=self.master_key):
                with patch("app.config.get_deepseek_api_key", side_effect=AssertionError("Must use personal API keys")) as shared:
                    self.assertIsInstance(self.factory(), ModelCredentialService)
        shared.assert_not_called()

    def test_dependency_failure_is_generic_503_before_route_executes(self):
        application = FastAPI()
        entered = []

        @application.get("/_factory_check")
        async def endpoint(service: ModelCredentialService = Depends(self.factory)):
            entered.append(True)
            return {"unexpected": True}

        with patch.object(dependencies, "get_mysql_password", return_value="unused"):
            with patch.object(dependencies, "get_model_encryption_key", side_effect=ModelCredentialConfigurationError("TEST_ONLY_PRIVATE_CONFIG")):
                with TestClient(application) as client:
                    response = client.get("/_factory_check")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "请稍后重试。"})
        self.assertFalse(entered)
        self.assertNotIn("TEST_ONLY_PRIVATE_CONFIG", response.text)

