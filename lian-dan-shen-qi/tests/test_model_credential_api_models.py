"""个人Key请求与公开状态模型回归；只用假Key。"""

import unittest

from pydantic import SecretStr, ValidationError

from app.api.schemas import ModelCredentialRequest, ModelCredentialStatusResponse


class ModelCredentialApiModelTests(unittest.TestCase):
    def test_request_declares_required_secret(self):
        self.assertEqual(set(ModelCredentialRequest.model_fields), {"api_key"})
        field = ModelCredentialRequest.model_fields["api_key"]
        self.assertIs(field.annotation, SecretStr)
        self.assertTrue(field.is_required())

    def test_missing_or_wrong_type_key_is_rejected(self):
        for payload in ({}, {"api_key": None}, {"api_key": 123}, {"api_key": {"secret": "TEST_ONLY"}}):
            with self.subTest(fields=tuple(payload)):
                with self.assertRaises(ValidationError):
                    ModelCredentialRequest(**payload)

    def test_secret_is_masked_but_explicitly_extractable(self):
        secret = "sk-TEST_ONLY_FAKE_KEY"
        request = ModelCredentialRequest(api_key=secret)
        self.assertEqual(request.api_key.get_secret_value(), secret)
        self.assertNotIn(secret, repr(request))
        self.assertNotIn(secret, request.model_dump_json())

    def test_client_cannot_supply_owner_or_server_configuration(self):
        for field in ("user_id", "provider", "master_key", "mysql_password"):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    ModelCredentialRequest(api_key="TEST_ONLY", **{field: "TEST_ONLY"})

    def test_status_has_provider_and_boolean_state(self):
        for configured in (True, False):
            with self.subTest(configured=configured):
                status = ModelCredentialStatusResponse(configured=configured)
                self.assertEqual(status.model_dump(), {"provider": "deepseek", "configured": configured})

    def test_status_requires_configured_field(self):
        with self.assertRaises(ValidationError):
            ModelCredentialStatusResponse()

    def test_status_declares_only_public_fields(self):
        self.assertEqual(set(ModelCredentialStatusResponse.model_fields), {"provider", "configured"})
        self.assertTrue(ModelCredentialStatusResponse.model_fields["configured"].is_required())

