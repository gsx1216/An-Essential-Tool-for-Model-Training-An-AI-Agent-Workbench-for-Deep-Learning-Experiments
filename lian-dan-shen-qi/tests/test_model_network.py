"""失效代理不能污染默认模型连接；测试不连接真实模型。"""
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock
from app import config
from app.services.model_gateway import ModelGateway

class ModelNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_inherited_broken_proxy_is_ignored_by_both_sdk_clients(self):
        with patch.dict(os.environ,{'HTTP_PROXY':'http://127.0.0.1:9','HTTPS_PROXY':'http://127.0.0.1:9','MODEL_TRUST_ENV':'false'}),patch('app.services.model_gateway.ChatDeepSeek',return_value=SimpleNamespace()) as constructor:
            ModelGateway('sk-TEST_ONLY_NETWORK')
        sync=constructor.call_args.kwargs['http_client'];asynchronous=constructor.call_args.kwargs['http_async_client']
        try:
            self.assertFalse(sync._trust_env);self.assertFalse(asynchronous._trust_env)
            self.assertFalse(sync._mounts);self.assertFalse(asynchronous._mounts)
        finally:sync.close();await asynchronous.aclose()

    async def test_proxy_use_requires_explicit_opt_in(self):
        with patch.dict(os.environ,{'HTTP_PROXY':'http://127.0.0.1:9','HTTPS_PROXY':'http://127.0.0.1:9'}),patch('app.services.model_gateway.ChatDeepSeek',return_value=SimpleNamespace()) as constructor:
            ModelGateway('sk-TEST_ONLY_NETWORK',trust_env=True)
        sync=constructor.call_args.kwargs['http_client'];asynchronous=constructor.call_args.kwargs['http_async_client']
        try:
            self.assertTrue(sync._trust_env);self.assertTrue(asynchronous._trust_env)
            self.assertTrue(sync._mounts);self.assertTrue(asynchronous._mounts)
        finally:sync.close();await asynchronous.aclose()

    async def test_bad_config_or_non_boolean_override_fails_before_sdk(self):
        with patch('app.services.model_gateway.ChatDeepSeek') as constructor:
            with self.assertRaises(ValueError):ModelGateway('sk-TEST_ONLY_NETWORK',trust_env='false')
            with patch.dict(os.environ,{'MODEL_TRUST_ENV':'maybe'}),self.assertRaises(ValueError):ModelGateway('sk-TEST_ONLY_NETWORK')
            constructor.assert_not_called()

    def test_default_config_does_not_enable_environment_proxy(self):
        with patch.dict(os.environ,{},clear=True),patch.object(config,'ENV_FILE',Path('nonexistent-network-test.env')):
            self.assertFalse(config.get_model_trust_env())
