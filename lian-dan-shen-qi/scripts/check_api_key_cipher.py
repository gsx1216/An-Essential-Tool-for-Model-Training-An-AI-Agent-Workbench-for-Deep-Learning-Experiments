"""运行正式回归中的加解密测试；保留练习阶段的命令入口。"""

import unittest

from app.logging_config import configure_logging
from tests.test_api_key_cipher import ApiKeyCipherTests


if __name__ == "__main__":
    configure_logging(console=False)
    unittest.main(verbosity=2)
