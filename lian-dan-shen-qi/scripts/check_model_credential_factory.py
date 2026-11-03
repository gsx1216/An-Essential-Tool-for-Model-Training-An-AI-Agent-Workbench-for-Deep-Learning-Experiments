"""运行个人Key工厂正式回归；使用临时密钥和模拟配置。"""

import unittest

from app.logging_config import configure_logging
from tests.test_model_credential_factory import ModelCredentialFactoryTests


if __name__ == "__main__":
    configure_logging(console=False)
    unittest.main(verbosity=2)
