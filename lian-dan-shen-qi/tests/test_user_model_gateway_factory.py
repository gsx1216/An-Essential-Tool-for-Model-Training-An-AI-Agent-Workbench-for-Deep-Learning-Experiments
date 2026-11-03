"""按用户创建网关的回归：使用假Key、假凭据服务和假网关。"""

import threading
import unittest
from unittest.mock import Mock, create_autospec

from mysql.connector import Error as MySQLError

from app.errors import ModelCredentialDecryptionError, ModelCredentialNotConfiguredError
from app.services.model_credential_service import ModelCredentialService
from app.services.user_model_gateway_factory import UserModelGatewayFactory


class UserModelGatewayFactoryChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.credentials = create_autospec(ModelCredentialService, instance=True, spec_set=True)
        self.credentials.get_api_key.return_value = "sk-TEST_ONLY_ALICE"
        self.gateway = object()
        self.gateway_factory = Mock(return_value=self.gateway)
        self.factory = UserModelGatewayFactory(
            credential_service=self.credentials,
            gateway_factory=self.gateway_factory,
        )

    async def test_awaits_key_lookup_for_requested_user(self):
        await self.factory.create_for_user(7)
        self.credentials.get_api_key.assert_awaited_once_with(7)
        self.gateway_factory.assert_called_once_with(api_key="sk-TEST_ONLY_ALICE")

    async def test_returns_created_gateway_object(self):
        gateway = await self.factory.create_for_user(7)
        self.assertIs(gateway, self.gateway)

    async def test_two_users_receive_separate_gateways_and_keys(self):
        first, second = object(), object()
        self.gateway_factory.side_effect = [first, second]
        self.credentials.get_api_key.side_effect = lambda user_id: {
            7: "sk-TEST_ONLY_ALICE", 8: "sk-TEST_ONLY_BOB",
        }[user_id]
        self.assertIs(await self.factory.create_for_user(7), first)
        self.assertIs(await self.factory.create_for_user(8), second)
        self.assertEqual(
            [call.kwargs for call in self.gateway_factory.call_args_list],
            [{"api_key": "sk-TEST_ONLY_ALICE"}, {"api_key": "sk-TEST_ONLY_BOB"}],
        )

    async def test_updated_key_is_read_again_instead_of_reusing_old_gateway(self):
        self.credentials.get_api_key.side_effect = ["sk-TEST_ONLY_OLD", "sk-TEST_ONLY_NEW"]
        self.gateway_factory.side_effect = [object(), object()]
        first = await self.factory.create_for_user(7)
        second = await self.factory.create_for_user(7)
        self.assertIsNot(first, second)
        self.assertEqual(self.credentials.get_api_key.await_count, 2)
        self.assertEqual(
            [call.kwargs for call in self.gateway_factory.call_args_list],
            [{"api_key": "sk-TEST_ONLY_OLD"}, {"api_key": "sk-TEST_ONLY_NEW"}],
        )

    async def test_missing_key_propagates_without_creating_gateway(self):
        error = ModelCredentialNotConfiguredError("TEST_ONLY_NOT_CONFIGURED")
        self.credentials.get_api_key.side_effect = error
        with self.assertRaises(ModelCredentialNotConfiguredError) as caught:
            await self.factory.create_for_user(7)
        self.assertIs(caught.exception, error)
        self.gateway_factory.assert_not_called()

    async def test_decryption_error_does_not_fall_back_to_another_key(self):
        error = ModelCredentialDecryptionError("TEST_ONLY_DECRYPTION_FAILED")
        self.credentials.get_api_key.side_effect = error
        with self.assertRaises(ModelCredentialDecryptionError) as caught:
            await self.factory.create_for_user(7)
        self.assertIs(caught.exception, error)
        self.gateway_factory.assert_not_called()

    async def test_invalid_user_id_stops_before_lookup_and_construction(self):
        for user_id in (0, -1, True, False, "7", None):
            with self.subTest(kind=type(user_id).__name__, value=user_id):
                with self.assertRaises(ValueError):
                    await self.factory.create_for_user(user_id)
        self.credentials.get_api_key.assert_not_awaited()
        self.gateway_factory.assert_not_called()

    async def test_database_error_propagates_without_creating_gateway(self):
        error = MySQLError("TEST_ONLY_DATABASE_FAILURE", errno=2003)
        self.credentials.get_api_key.side_effect = error
        with self.assertRaises(MySQLError) as caught:
            await self.factory.create_for_user(7)
        self.assertIs(caught.exception, error)
        self.gateway_factory.assert_not_called()

    async def test_gateway_construction_error_propagates(self):
        error = RuntimeError("TEST_ONLY_GATEWAY_CONSTRUCTION_FAILURE")
        self.gateway_factory.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            await self.factory.create_for_user(7)
        self.assertIs(caught.exception, error)

    async def test_synchronous_gateway_construction_runs_in_worker_thread(self):
        loop_thread = threading.get_ident()
        construction_threads = []

        def build(*, api_key):
            construction_threads.append(threading.get_ident())
            return self.gateway

        self.gateway_factory.side_effect = build
        await self.factory.create_for_user(7)
        self.assertEqual(len(construction_threads), 1)
        self.assertNotEqual(construction_threads[0], loop_thread)

