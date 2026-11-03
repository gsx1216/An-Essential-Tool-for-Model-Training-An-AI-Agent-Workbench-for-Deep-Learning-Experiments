"""实验异步业务验证：提前校验、同步仓储线程调用和协作者缓存边界。"""

import asyncio
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.api import dependencies
from app.errors import ExperimentNotFoundError
from app.models.experiment import ExperimentPage, ExperimentRecord
from app.services.experiment_service import ExperimentService


ID = 'a' * 32


class ExperimentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_normalizes_title_and_preserves_original_description(self):
        repository = MagicMock()
        record = ExperimentRecord(ID, '实验', 'script', datetime.now(timezone.utc), datetime.now(timezone.utc), '  保留\n')
        repository.create_experiment.return_value = record
        result = await ExperimentService(repository).create_experiment(7, ' 实验 ', description='  保留\n')
        self.assertIs(result, record)
        repository.create_experiment.assert_called_once_with(7, '实验', 'script', '  保留\n')

    async def test_read_and_page_forward_normalized_identity_and_cursor(self):
        repository = MagicMock()
        repository.list_experiments.return_value = ExperimentPage((), None)
        service = ExperimentService(repository)
        await service.get_experiment(7, 'AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA')
        await service.list_experiments(7, limit=2, before_id='AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA')
        repository.get_experiment.assert_called_once_with(7, ID)
        repository.list_experiments.assert_called_once_with(7, limit=2, before_id=ID)

    async def test_invalid_arguments_do_not_schedule_repository_work(self):
        repository = MagicMock()
        service = ExperimentService(repository)
        for method, args, kw in [('create_experiment', (7, ' '), {}),
                                  ('get_experiment', (7, 'bad'), {}),
                                  ('list_experiments', (7,), dict(limit=True))]:
            with self.subTest(method=method), self.assertRaises(ValueError):
                await getattr(service, method)(*args, **kw)
        self.assertEqual(repository.mock_calls, [])

    async def test_repository_domain_error_is_not_swallowed(self):
        repository = MagicMock()
        repository.get_experiment.side_effect = ExperimentNotFoundError('not owned')
        with self.assertRaises(ExperimentNotFoundError):
            await ExperimentService(repository).get_experiment(7, ID)


class ExperimentDependencyTests(unittest.TestCase):
    def tearDown(self):
        dependencies.get_experiment_service.cache_clear()

    def test_factory_caches_collaborators_without_querying_database(self):
        dependencies.get_experiment_service.cache_clear()
        with patch.object(dependencies, 'get_mysql_password', return_value='PRIVATE_PASSWORD'), \
             patch('app.repositories.experiment_repository.get_connection') as connect:
            a = dependencies.get_experiment_service()
            b = dependencies.get_experiment_service()
        self.assertIs(a, b)
        connect.assert_not_called()

    def test_configuration_and_missing_sql_errors_become_safe_503(self):
        for error in (ValueError('PRIVATE_CONFIG'), FileNotFoundError('PRIVATE_PATH')):
            dependencies.get_experiment_service.cache_clear()
            with patch.object(dependencies, 'get_mysql_password', side_effect=error):
                with self.assertRaises(HTTPException) as result:
                    dependencies.get_experiment_service()
            self.assertEqual(result.exception.status_code, 503)
            self.assertEqual(result.exception.detail, '请稍后重试。')


if __name__ == '__main__':
    unittest.main()
