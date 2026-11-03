"""注册表、远端协议、业务归属与网络不确定性测试。"""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.errors import RunnerConflictError, RunnerProtocolError, RunnerScriptNotAllowedError, ServerConnectionError
from app.models.runner import RunnerSettings, validate_observation
from app.services.runner_service import RunnerService
from tests.test_server_connection_core import profile


SPEC = {'path': 'train.py', 'sha256': 'a' * 64, 'parameters': {'epochs': {'type': 'int', 'min': 1, 'max': 10, 'required': True}},
        'max_runtime_seconds': 10, 'max_log_bytes': 1024}


def observation(status='running', **options):
    return {'status': status, 'exit_code': 0 if status == 'succeeded' else None,
            'started': status in {'running', 'succeeded'}, 'error_code': None, 'log_bytes': 0, 'stale': False, **options}


class SettingsTests(unittest.TestCase):
    def test_default_denies_unregistered_script(self):
        with self.assertRaises(RunnerScriptNotAllowedError):
            RunnerSettings().resolve('train', {})

    def test_validated_parameter_copy_cannot_mutate_registry(self):
        settings = RunnerSettings(json.dumps({'train': SPEC}))
        spec, params = settings.resolve('train', {'epochs': 2})
        params['epochs'] = 5; spec['path'] = 'changed.py'
        self.assertEqual(settings.registry()['train']['path'], 'train.py')

    def test_rejects_types_unknown_missing_and_out_of_range(self):
        settings = RunnerSettings(json.dumps({'train': SPEC}))
        for params in ({}, {'epochs': True}, {'epochs': '2'}, {'epochs': 0}, {'epochs': 11}, {'epochs': 2, 'command': 'shell'}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                settings.resolve('train', params)

    def test_rejects_unsafe_registry_and_interpreter(self):
        for changes in ({'path': '../train.py'}, {'path': '/train.py'}, {'sha256': 'bad'}, {'max_runtime_seconds': True},
                        {'max_log_bytes': 10485761}, {'parameters': {'epochs': {'type': 'shell'}}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RunnerSettings(json.dumps({'train': {**SPEC, **changes}}))
        with self.assertRaises(ValueError):
            RunnerSettings('{}', 'python3;rm')

    def test_observation_rejects_fake_success_unknown_state_and_fields(self):
        for value in (None, observation('succeeded', exit_code=1), observation('not-real'), observation(stale='no'), observation(log_bytes=-1), observation(error_code='RAW_SECRET')):
            with self.subTest(value=value), self.assertRaises(RunnerProtocolError):
                validate_observation(value)
        self.assertEqual(validate_observation(observation('missing', stale=True))['status'], 'missing')


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo, self.runs = MagicMock(), MagicMock()
        self.servers, self.transport = MagicMock(), MagicMock()
        self.servers.get = AsyncMock(return_value=profile())
        self.transport.request = AsyncMock(return_value=observation())
        self.servers.cipher.decrypt.return_value = {'password': 'SECRET'}
        self.record = {'run_id': 'b' * 32, 'server_id': profile().server_id, 'script_id': 'train', 'job_id': 'c' * 32,
                       'launch': {'server_binding': profile().binding()}, 'launch_digest': 'd' * 64, 'status': 'queued',
                       'exit_code': None, 'error_code': None, 'created_at': None, 'checked_at': None}
        self.repo.get.return_value = self.record
        self.service = RunnerService(self.repo, self.runs, self.servers, self.transport, RunnerSettings(json.dumps({'train': SPEC})))

    async def test_replay_reuses_durable_payload_and_does_not_resolve_new_parameters(self):
        result = await self.service.start(1, 'b' * 32, profile().server_id, 'train')
        self.runs.get_run.assert_not_called(); self.repo.claim.assert_not_called()
        self.assertEqual(self.transport.request.call_args.args[2]['job_id'], 'c' * 32)
        self.assertNotIn('launch', result['execution'])
        self.assertEqual(self.servers.cipher.decrypt.return_value, {})

    async def test_network_error_keeps_queued_binding_and_clears_credentials(self):
        self.transport.request.side_effect = ServerConnectionError('PRIVATE')
        with self.assertRaises(ServerConnectionError):
            await self.service.start(1, 'b' * 32, profile().server_id, 'train')
        self.repo.observe.assert_not_called()
        self.assertEqual(self.servers.cipher.decrypt.return_value, {})

    async def test_other_binding_rejected_before_network(self):
        with self.assertRaises(RunnerConflictError):
            await self.service.start(1, 'b' * 32, 'e' * 32, 'train')
        self.transport.request.assert_not_awaited()

    async def test_terminal_replay_never_restarts(self):
        self.record['status'] = 'succeeded'
        result = await self.service.start(1, 'b' * 32, profile().server_id, 'train')
        self.assertIsNone(result['observation']); self.transport.request.assert_not_awaited()

    async def test_new_run_claims_before_any_remote_request(self):
        self.repo.get.side_effect = [None, self.record]
        self.runs.get_run.return_value = SimpleNamespace(parameters={'epochs': 2})
        self.repo.claim.return_value = self.record
        await self.service.start(1, 'b' * 32, profile().server_id, 'train')
        self.repo.claim.assert_called_once()
        self.assertEqual(self.repo.claim.call_args.args[4]['parameters'], {'epochs': 2})
        self.assertEqual(self.repo.claim.call_args.args[4]['output_directory'], 'training_outputs')

    async def test_custom_output_directory_is_frozen_in_new_run(self):
        self.service.settings=RunnerSettings(json.dumps({'train':SPEC}),output_directory='results/PRNet')
        self.repo.get.side_effect=[None,self.record]
        self.runs.get_run.return_value=SimpleNamespace(parameters={'epochs':2})
        self.repo.claim.return_value=self.record
        await self.service.start(1,'b'*32,profile().server_id,'train')
        self.assertEqual(self.repo.claim.call_args.args[4]['output_directory'],'results/PRNet')

    async def test_config_binding_change_never_connects(self):
        self.record['launch']['server_binding'] = 'changed'
        with self.assertRaises(RunnerConflictError):
            await self.service.operate(1, 'b' * 32, 'status')
        self.transport.request.assert_not_awaited()

    async def test_invalid_action_and_log_range_rejected_before_repository(self):
        for action, options in [('shell', {}), ('logs', {'offset': -1}), ('logs', {'limit': True})]:
            with self.assertRaises(ValueError):
                await self.service.operate(1, 'b' * 32, action, **options)
        self.repo.get.assert_not_called()

    async def test_removed_script_blocks_new_start_but_does_not_block_status_or_stop(self):
        self.service.settings = RunnerSettings()
        with self.assertRaises(RunnerScriptNotAllowedError):
            await self.service.start(1, 'b' * 32, profile().server_id, 'train')
        await self.service.operate(1, 'b' * 32, 'stop')
        self.assertEqual(self.transport.request.call_args.args[2]['action'], 'stop')
