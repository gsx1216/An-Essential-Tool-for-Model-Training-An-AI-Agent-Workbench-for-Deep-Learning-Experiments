"""实验仓储的原子创建、归属过滤、稳定分页与输入保护。"""

from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

from mysql.connector import IntegrityError, Error as MySQLError

from app.errors import ExperimentNotFoundError, ExperimentStorageError, InvalidTokenError
from app.repositories import experiment_repository as module
from app.repositories.experiment_repository import ExperimentRepository


ID = 'a' * 32
STAMP = datetime(2026, 10, 25, 1, 2, 3, 123456)


def row(identifier=ID, *, full=True):
    result = dict(experiment_id=identifier, title='测试', kind='training', created_at=STAMP, updated_at=STAMP)
    if full:
        result['description'] = '  描述和缩进保留\n'
    return result


class ExperimentRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = ExperimentRepository('PRIVATE_PASSWORD')
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.rowcount = 1
        self.cursor.fetchone.return_value = row()

    def invoke(self, method, *args, **kwargs):
        with patch.object(module, 'get_connection', return_value=self.connection):
            return getattr(self.repository, method)(*args, **kwargs)

    def test_constructor_does_not_open_database_connection(self):
        with patch.object(module, 'get_connection') as connect:
            ExperimentRepository('unused')
        connect.assert_not_called()

    def test_atomic_create_generates_uuid_preserves_description_and_returns_utc_record(self):
        with patch.object(module, 'uuid4', return_value=MagicMock(hex=ID)):
            record = self.invoke('create_experiment', 7, ' 测试 ', 'training', '  描述和缩进保留\n')
        self.cursor.execute.assert_any_call(self.repository.create_experiment_sql, (ID, '测试', 'training', '  描述和缩进保留\n', 7))
        self.cursor.execute.assert_any_call(self.repository.get_experiment_sql, (7, ID))
        self.assertEqual(record.description, '  描述和缩进保留\n')
        self.assertEqual(record.created_at.tzinfo, timezone.utc)
        self.connection.commit.assert_called_once()

    def test_missing_owner_cannot_create_experiment_and_rolls_back(self):
        self.cursor.rowcount = 0
        with self.assertRaises(InvalidTokenError):
            self.invoke('create_experiment', 999, 'test')
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_foreign_key_race_is_converted_to_expired_identity(self):
        self.cursor.execute.side_effect = IntegrityError(msg='PRIVATE_SQL', errno=1452)
        with self.assertRaises(InvalidTokenError):
            self.invoke('create_experiment', 7, 'test')
        self.connection.rollback.assert_called_once()

    def test_other_integrity_failure_is_not_misreported_as_identity(self):
        self.cursor.execute.side_effect = IntegrityError(msg='PRIVATE_SQL', errno=1062)
        with self.assertRaises(IntegrityError):
            self.invoke('create_experiment', 7, 'test')
        self.connection.rollback.assert_called_once()

    def test_missing_readback_or_select_failure_rolls_back_instead_of_reporting_created(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(ExperimentStorageError):
            self.invoke('create_experiment', 7, 'test')
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once()
        self.cursor.execute.side_effect = [None, MySQLError(msg='PRIVATE_DETAIL', errno=2013)]
        with self.assertRaises(MySQLError):
            self.invoke('create_experiment', 7, 'test')

    def test_detail_always_scopes_owner_and_normalizes_uuid(self):
        self.invoke('get_experiment', 7, 'AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA')
        self.cursor.execute.assert_called_once_with(self.repository.get_experiment_sql, (7, ID))

    def test_missing_and_other_owner_detail_have_same_domain_error(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(ExperimentNotFoundError):
            self.invoke('get_experiment', 8, ID)

    def test_first_page_uses_limit_plus_one_and_returns_cursor_for_last_visible_item(self):
        self.cursor.fetchall.return_value = [row('c' * 32, full=False), row('b' * 32, full=False), row(ID, full=False)]
        page = self.invoke('list_experiments', 7, limit=2)
        self.assertEqual([item.experiment_id for item in page.items], ['c' * 32, 'b' * 32])
        self.assertEqual(page.next_cursor, 'b' * 32)
        self.assertFalse(hasattr(page.items[0], 'description'))
        self.cursor.execute.assert_called_once_with(self.repository.list_experiments_sql, (7, 3))
        self.connection.start_transaction.assert_called_once_with(readonly=True, isolation_level='REPEATABLE READ', consistent_snapshot=True)
        self.connection.rollback.assert_called_once()

    def test_before_cursor_checks_owner_and_uses_timestamp_plus_id_boundary(self):
        self.cursor.fetchone.return_value = dict(created_at=STAMP)
        self.cursor.fetchall.return_value = [row(full=False)]
        page = self.invoke('list_experiments', 7, limit=2, before_id=ID)
        self.cursor.execute.assert_any_call(self.repository.get_experiment_cursor_sql, (7, ID))
        self.cursor.execute.assert_any_call(self.repository.list_experiments_before_sql, (7, STAMP, STAMP, ID, 3))
        self.assertIsNone(page.next_cursor)

    def test_unowned_cursor_is_rejected_before_list_query(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(ExperimentNotFoundError):
            self.invoke('list_experiments', 8, before_id=ID)
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.connection.rollback.assert_called_once()

    def test_empty_page_has_no_cursor(self):
        self.cursor.fetchall.return_value = []
        page = self.invoke('list_experiments', 7)
        self.assertEqual(page.items, ())
        self.assertIsNone(page.next_cursor)

    def test_invalid_input_never_opens_connection(self):
        calls = [('create_experiment', (True, 'test'), {}), ('create_experiment', (0, 'test'), {}),
                 ('create_experiment', (7, '  '), {}), ('create_experiment', (7, 'x' * 201), {}),
                 ('create_experiment', (7, 'test', 'shell'), {}), ('create_experiment', (7, 'test', [], ''), {}),
                 ('create_experiment', (7, 'test', 'script', 'x' * 10001), {}),
                 ('create_experiment', (7, 'test', 'script', '\ud800'), {}),
                 ('get_experiment', (7, 'bad UUID'), {}), ('get_experiment', (7, None), {}),
                 ('list_experiments', (7,), dict(limit=0)), ('list_experiments', (7,), dict(limit=True)),
                 ('list_experiments', (7,), dict(limit=101)), ('list_experiments', (7,), dict(before_id='bad'))]
        with patch.object(module, 'get_connection') as connect:
            for name, args, kwargs in calls:
                with self.subTest(method=name, argtypes=[type(v).__name__ for v in args]), self.assertRaises(ValueError):
                    getattr(self.repository, name)(*args, **kwargs)
        connect.assert_not_called()

    def test_list_failure_always_ends_read_snapshot(self):
        self.cursor.execute.side_effect = MySQLError(msg='PRIVATE', errno=2013)
        with self.assertRaises(MySQLError):
            self.invoke('list_experiments', 7)
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
