"""运行正式回归中的个人凭据仓储测试，不连接真实数据库。"""

import unittest

from app.logging_config import configure_logging
from tests.test_model_credential_repository import ModelCredentialRepositoryTests


if __name__ == "__main__":
    configure_logging(console=False)
    unittest.main(verbosity=2)
