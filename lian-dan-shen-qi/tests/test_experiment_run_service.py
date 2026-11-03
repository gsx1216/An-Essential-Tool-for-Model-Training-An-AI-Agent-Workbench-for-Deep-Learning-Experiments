"""运行异步业务的复制边界、提前拒绝、查询转发和依赖工厂。"""

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.api import dependencies
from app.services.experiment_run_service import ExperimentRunService


RUN, EXP, REQ = 'a' * 32, 'b' * 32, 'c' * 32


class RunServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_copies_parameters_before_scheduling_sync_repository(self):
        repository = MagicMock()
        source = {'epochs': [1, 2]}
        def create(user, experiment, request, parameters):
            parameters['epochs'].append(3)
            return 'done'
        repository.create_run.side_effect = create
        result = await ExperimentRunService(repository).create_run(7, EXP, REQ, source)
        self.assertEqual(result, 'done')
        self.assertEqual(source, {'epochs': [1, 2]})

    async def test_read_page_and_latest_forward_scoped_parameters(self):
        repository = MagicMock()
        service = ExperimentRunService(repository)
        await service.get_run(7, RUN)
        await service.list_runs(7, EXP, limit=2, before_id=RUN)
        await service.latest_training_run(7, experiment_id=EXP, status='failed')
        repository.get_run.assert_called_once_with(7, RUN)
        repository.list_runs.assert_called_once_with(7, EXP, limit=2, before_id=RUN)
        repository.latest_training_run.assert_called_once_with(7, experiment_id=EXP, status='failed')

    async def test_invalid_scope_or_parameters_does_not_schedule_work(self):
        repository = MagicMock()
        service = ExperimentRunService(repository)
        for name, args, kwargs in (('create_run', (7, EXP, REQ, {'api_key': 'private'}), {}),
                                   ('get_run', (7, 'bad'), {}), ('list_runs', (7, EXP), {'limit': 101}),
                                   ('latest_training_run', (True,), {})):
            with self.assertRaises(ValueError):
                await getattr(service, name)(*args, **kwargs)
        self.assertEqual(repository.mock_calls, [])


class RunDependencyTests(unittest.TestCase):
    def tearDown(self):
        dependencies.get_experiment_run_service.cache_clear()

    def test_dependency_caches_collaborators_without_querying_or_creating_models(self):
        dependencies.get_experiment_run_service.cache_clear()
        with patch.object(dependencies, 'get_mysql_password', return_value='PRIVATE'), \
             patch('app.repositories.experiment_run_repository.get_connection') as connect:
            a = dependencies.get_experiment_run_service()
            b = dependencies.get_experiment_run_service()
        self.assertIs(a, b)
        connect.assert_not_called()

    def test_dependency_configuration_failure_is_generic_503(self):
        for error in (ValueError('PRIVATE'), FileNotFoundError('PRIVATE')):
            dependencies.get_experiment_run_service.cache_clear()
            with patch.object(dependencies, 'get_mysql_password', side_effect=error), self.assertRaises(HTTPException) as reply:
                dependencies.get_experiment_run_service()
            self.assertEqual(reply.exception.status_code, 503)
            self.assertEqual(reply.exception.detail, '请稍后重试。')


if __name__ == '__main__':
    unittest.main()
