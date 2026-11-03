"""运行仓储：参数化归属、原子性、唯一键重试、分页与最新训练选择。"""

from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

from mysql.connector import IntegrityError, Error as MySQLError

from app.errors import ExperimentNotFoundError, RunNotFoundError, RunRequestConflictError, RunStorageError
from app.models.experiment_run import canonical_parameters, run_input_digest
from app.repositories import experiment_run_repository as module
from app.repositories.experiment_run_repository import ExperimentRunRepository


RUN, EXP, REQ = 'a' * 32, 'b' * 32, 'c' * 32
STAMP = datetime(2026, 10, 25)


def row(identifier=RUN):
    params = canonical_parameters({'epochs': 2})
    return dict(run_id=identifier, experiment_id=EXP, experiment_title='training', experiment_kind='training',
                experiment_description='description', request_id=REQ, input_digest=run_input_digest(EXP, params),
                parameters_json=params, status='created', created_at=STAMP, updated_at=STAMP, started_at=None,
                finished_at=None, exit_code=None, error_code=None, log_ref=None, result_ref=None, result_version=0)


class RunRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = ExperimentRunRepository('PRIVATE_PASSWORD')
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.rowcount = 1

    def invoke(self, method, *args, **kwargs):
        with patch.object(module, 'get_connection', return_value=self.connection):
            return getattr(self.repository, method)(*args, **kwargs)

    def create(self, parameters=None):
        return self.invoke('create_run', 7, EXP, REQ, {'epochs': 2} if parameters is None else parameters)

    def test_constructor_only_reads_sql_without_connecting(self):
        with patch.object(module, 'get_connection') as connect:
            ExperimentRunRepository('unused')
        connect.assert_not_called()

    def test_create_locks_owner_copies_snapshot_and_commits_only_after_readback(self):
        self.cursor.fetchone.side_effect = [{'id': EXP}, row()]
        with patch.object(module, 'uuid4', return_value=MagicMock(hex=RUN)):
            result = self.create()
        self.assertTrue(result.created)
        self.assertEqual(result.run.status, 'created')
        self.assertEqual(result.run.created_at.tzinfo, timezone.utc)
        self.cursor.execute.assert_any_call(self.repository.lock_run_experiment_sql, (7, EXP))
        self.cursor.execute.assert_any_call(self.repository.get_experiment_run_sql, (7, RUN))
        self.connection.commit.assert_called_once()

    def test_missing_or_foreign_experiment_prevents_insert(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(ExperimentNotFoundError):
            self.create()
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.connection.rollback.assert_called_once()

    def arrange_duplicate(self, existing):
        other = MagicMock()
        other.__enter__.return_value = other
        other_cursor = other.cursor.return_value.__enter__.return_value
        other_cursor.fetchone.return_value = existing
        self.cursor.fetchone.return_value = {'id': EXP}
        def execute(sql, params):
            if sql == self.repository.create_experiment_run_sql:
                raise IntegrityError(msg='PRIVATE_DUPLICATE_TEXT', errno=1062)
        self.cursor.execute.side_effect = execute
        return other, other_cursor

    def test_duplicate_request_reads_new_transaction_and_reuses_existing_record(self):
        other, cursor = self.arrange_duplicate(row())
        with patch.object(module, 'get_connection', side_effect=[self.connection, other]) as connect:
            result = self.repository.create_run(7, EXP, REQ, {'epochs': 2})
        self.assertFalse(result.created)
        self.assertEqual(result.run.run_id, RUN)
        self.assertEqual(connect.call_count, 2)
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()
        cursor.execute.assert_called_once_with(self.repository.get_run_by_request_sql, (7, REQ))

    def test_duplicate_request_with_changed_parameters_is_conflict(self):
        other, _ = self.arrange_duplicate(row())
        with patch.object(module, 'get_connection', side_effect=[self.connection, other]):
            with self.assertRaises(RunRequestConflictError):
                self.repository.create_run(7, EXP, REQ, {'epochs': 3})

    def test_unrelated_unique_collision_is_not_treated_as_success(self):
        other, _ = self.arrange_duplicate(None)
        with patch.object(module, 'get_connection', side_effect=[self.connection, other]):
            with self.assertRaises(IntegrityError):
                self.repository.create_run(7, EXP, REQ, {'epochs': 2})

    def test_readback_failure_and_database_failure_roll_back_create(self):
        self.cursor.fetchone.side_effect = [{'id': EXP}, None]
        with self.assertRaises(RunStorageError):
            self.create()
        self.connection.commit.assert_not_called()
        self.cursor.execute.side_effect = MySQLError(msg='PRIVATE_SQL', errno=2013)
        with self.assertRaises(MySQLError):
            self.create()

    def test_detail_scopes_owner_and_missing_records_share_404_domain(self):
        self.cursor.fetchone.return_value = row()
        result = self.invoke('get_run', 7, RUN)
        self.assertEqual(result.parameters, {'epochs': 2})
        self.cursor.execute.assert_called_once_with(self.repository.get_experiment_run_sql, (7, RUN))
        self.cursor.fetchone.return_value = None
        with self.assertRaises(RunNotFoundError):
            self.invoke('get_run', 8, RUN)

    def test_corrupt_parameters_digest_and_status_are_storage_errors(self):
        for values in ({'parameters_json': 'not JSON'}, {'parameters_json': '[]'},
                       {'input_digest': 'wrong'}, {'status': 'not-a-status'}, {'status': None}):
            bad = row()
            bad.update(values)
            self.cursor.fetchone.return_value = bad
            with self.subTest(fields=list(values)), self.assertRaises(RunStorageError):
                self.invoke('get_run', 7, RUN)

    def test_first_page_is_bounded_and_uses_last_visible_record_as_cursor(self):
        self.cursor.fetchone.return_value = {'id': EXP}
        self.cursor.fetchall.return_value = [row('f' * 32), row('e' * 32), row('d' * 32)]
        result = self.invoke('list_runs', 7, EXP, limit=2)
        self.assertEqual(result.next_cursor, 'e' * 32)
        self.assertEqual(len(result.items), 2)
        self.assertFalse(hasattr(result.items[0], 'parameters_json'))
        self.cursor.execute.assert_any_call(self.repository.list_experiment_runs_sql, (7, EXP, 3))
        self.connection.rollback.assert_called_once()

    def test_before_cursor_requires_same_experiment_and_uses_stable_boundary(self):
        self.cursor.fetchone.side_effect = [{'id': EXP}, {'created_at': STAMP}]
        self.cursor.fetchall.return_value = []
        result = self.invoke('list_runs', 7, EXP, before_id=RUN)
        self.assertEqual(result.items, ())
        self.cursor.execute.assert_any_call(self.repository.get_run_cursor_sql, (7, EXP, RUN))
        self.cursor.execute.assert_any_call(self.repository.list_experiment_runs_before_sql, (7, EXP, STAMP, STAMP, RUN, 21))

    def test_foreign_or_other_experiment_cursor_stops_before_page_query(self):
        self.cursor.fetchone.side_effect = [{'id': EXP}, None]
        with self.assertRaises(RunNotFoundError):
            self.invoke('list_runs', 7, EXP, before_id=RUN)
        self.assertEqual(self.cursor.execute.call_count, 2)

    def test_latest_training_selects_explicitly_scoped_sql_without_silent_success_filter(self):
        for options, name, params in (({}, 'latest_training_run', (7,)),
                                     ({'status': 'failed'}, 'latest_training_run_status', (7, 'failed')),
                                     ({'experiment_id': EXP}, 'latest_training_run_experiment', (7, EXP)),
                                     ({'experiment_id': EXP, 'status': 'succeeded'}, 'latest_training_run_experiment_status', (7, EXP, 'succeeded'))):
            self.cursor.fetchone.side_effect = [{'id': EXP}, row()] if 'experiment_id' in options else [row()]
            result = self.invoke('latest_training_run', 7, **options)
            self.assertEqual(result.run_id, RUN)
            self.cursor.execute.assert_any_call(getattr(self.repository, name + '_sql'), params)

    def test_latest_without_data_returns_none_and_releases_snapshot_on_failure(self):
        self.cursor.fetchone.return_value = None
        self.assertIsNone(self.invoke('latest_training_run', 7))
        self.cursor.execute.side_effect = MySQLError(errno=2013)
        with self.assertRaises(MySQLError):
            self.invoke('latest_training_run', 7)
        self.assertEqual(self.connection.rollback.call_count, 2)

    def test_invalid_identity_ids_limits_and_parameters_never_connect(self):
        calls = [('create_run', (True, EXP, REQ, {}), {}), ('create_run', (7, EXP, 'bad', {}), {}),
                 ('create_run', (7, EXP, REQ, []), {}), ('get_run', (7, 'bad'), {}),
                 ('list_runs', (7, EXP), {'limit': True}), ('list_runs', (7, EXP), {'before_id': 'bad'}),
                 ('latest_training_run', (0,), {}), ('latest_training_run', (7,), {'status': 'bad'})]
        with patch.object(module, 'get_connection') as connect:
            for method, args, kwargs in calls:
                with self.subTest(method=method), self.assertRaises(ValueError):
                    getattr(self.repository, method)(*args, **kwargs)
        connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
