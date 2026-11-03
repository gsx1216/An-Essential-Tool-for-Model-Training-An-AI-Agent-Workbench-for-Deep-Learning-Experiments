"""运行个人聊天依赖工厂回归；不连接数据库或模型。"""

import unittest

from tests.test_user_chat_factory import UserChatFactoryChecks


if __name__ == "__main__":
    unittest.main(verbosity=2)
