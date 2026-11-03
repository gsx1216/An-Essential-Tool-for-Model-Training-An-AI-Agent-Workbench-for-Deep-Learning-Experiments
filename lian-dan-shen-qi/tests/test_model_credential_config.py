"""主密钥读取回归：只用临时.env和随机临时密钥，不读取真实配置。"""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from app import config
from app.errors import ModelCredentialConfigurationError
from app.logging_config import DEFAULT_LOG_DIR, configure_logging


NAME = "MODEL_CREDENTIAL_ENCRYPTION_KEY"


class ModelCredentialConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.env_file = self.directory / ".env"
        self.key = Fernet.generate_key().decode("ascii")
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        path = patch.object(config, "ENV_FILE", self.env_file)
        path.start()
        self.addCleanup(path.stop)

    def test_reads_valid_server_master_key(self):
        os.environ[NAME] = self.key
        self.assertEqual(config.get_model_encryption_key(), self.key)

    def test_reads_local_env_master_key(self):
        self.env_file.write_text(f"{NAME}='{self.key}'\n", encoding="utf-8")
        self.assertEqual(config.get_model_encryption_key(), self.key)

    def test_server_environment_has_priority(self):
        other = Fernet.generate_key().decode("ascii")
        self.env_file.write_text(f"{NAME}='{other}'\n", encoding="utf-8")
        os.environ[NAME] = self.key
        self.assertEqual(config.get_model_encryption_key(), self.key)

    def test_missing_or_empty_configuration_is_rejected(self):
        with self.assertRaises(ModelCredentialConfigurationError):
            config.get_model_encryption_key()
        self.env_file.write_text(f"{NAME}=\n", encoding="utf-8")
        with self.assertRaises(ModelCredentialConfigurationError):
            config.get_model_encryption_key()

    def test_malformed_keys_are_rejected_without_echo(self):
        for value in ("not-a-master-key", "A" * 44, "界" * 44, " " + self.key,
                      self.key + " ", " " + self.key[:-1]):
            with self.subTest(length=len(value)), patch.dict(os.environ, {NAME: value}, clear=True):
                with self.assertRaises(ModelCredentialConfigurationError) as caught:
                    config.get_model_encryption_key()
                self.assertNotIn(value, str(caught.exception))

    def test_empty_server_variable_does_not_fall_back_to_local_file(self):
        self.env_file.write_text(f"{NAME}='{self.key}'\n", encoding="utf-8")
        os.environ[NAME] = ""
        with self.assertRaises(ModelCredentialConfigurationError):
            config.get_model_encryption_key()

    def test_env_file_does_not_expand_other_variables_into_master_key(self):
        self.env_file.write_text(f"{NAME}='${{OTHER_TEST_KEY}}'\n", encoding="utf-8")
        os.environ["OTHER_TEST_KEY"] = self.key
        with self.assertRaises(ModelCredentialConfigurationError):
            config.get_model_encryption_key()

    def test_configuration_read_never_generates_a_replacement_key(self):
        with patch.object(Fernet, "generate_key") as generate:
            os.environ[NAME] = self.key
            self.assertEqual(config.get_model_encryption_key(), self.key)
            del os.environ[NAME]
            with self.assertRaises(ModelCredentialConfigurationError):
                config.get_model_encryption_key()
        generate.assert_not_called()

    def test_invalid_configuration_is_logged_without_secret_value(self):
        secret = "TEST_ONLY_PRIVATE_INVALID_MASTER_KEY"
        os.environ[NAME] = secret
        configure_logging(self.directory / "logs", console=False, force=True)
        try:
            with self.assertRaises(ModelCredentialConfigurationError):
                config.get_model_encryption_key()
            contents = "\n".join(path.read_text(encoding="utf-8") for path in (self.directory / "logs").rglob("*.txt"))
            self.assertIn("config.get_model_encryption_key", contents)
            self.assertNotIn(secret, contents)
        finally:
            configure_logging(DEFAULT_LOG_DIR, console=False, force=True)
