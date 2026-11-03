"""运行聊天接口数据模型回归；不连接数据库或模型。"""

import unittest

from tests.test_chat_api_models import ChatApiModelChecks


if __name__ == "__main__":
    unittest.main(verbosity=2)
