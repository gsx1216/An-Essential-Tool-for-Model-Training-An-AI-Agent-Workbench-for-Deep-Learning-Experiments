"""运行个人聊天业务回归；不连接数据库或模型。"""

import unittest

from tests.test_user_chat_service import UserChatServiceChecks


if __name__ == "__main__":
    unittest.main(verbosity=2)
