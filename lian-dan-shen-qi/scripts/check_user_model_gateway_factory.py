"""运行按用户创建模型网关回归；不访问数据库或模型。"""

import unittest

from tests.test_user_model_gateway_factory import UserModelGatewayFactoryChecks


if __name__ == "__main__":
    unittest.main(verbosity=2)
