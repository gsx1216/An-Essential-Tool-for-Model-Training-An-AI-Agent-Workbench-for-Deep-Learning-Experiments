"""运行个人凭据业务的正式回归测试；不连接真实数据库、不调用模型。"""

import unittest

from app.logging_config import configure_logging
from tests.test_model_credential_service import ModelCredentialServiceTests


if __name__ == "__main__":
    configure_logging(console=False)
    unittest.main(verbosity=2)
