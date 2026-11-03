"""选择性迁移不触发旧回填；不接受错误父键定义。"""

import unittest
from unittest.mock import MagicMock, patch

from app import database


class RunnerMigrationTests(unittest.TestCase):
    def test_selective_cli_does_not_run_full_initialization(self):
        connection=MagicMock();connection.__enter__.return_value=connection
        with patch('sys.argv',['app.database','--runner-schema']),patch.object(database,'get_mysql_password',return_value='PRIVATE'), \
             patch.object(database,'get_connection',return_value=connection),patch.object(database,'_ensure_run_executions') as ensure, \
             patch.object(database,'initialize_database') as full,patch('builtins.print'):
            database.main()
        ensure.assert_called_once_with(connection);full.assert_not_called()

    def test_existing_correct_parent_indexes_are_not_altered(self):
        connection=MagicMock();cursor=connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value=[(table,index,seq,col,0) for table,index in [('experiment_runs','uq_runs_identity_owner'),('server_connections','uq_servers_identity_owner')]
                                     for seq,col in [(1,'id'),(2,'user_id')]]
        database._ensure_run_executions(connection)
        sqls=[c.args[0] for c in cursor.execute.call_args_list]
        self.assertFalse(any('ALTER TABLE' in s for s in sqls));self.assertIn('FOREIGN KEY (server_id, user_id)',sqls[-1])

    def test_invalid_parent_index_refuses_migration(self):
        connection=MagicMock();cursor=connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value=[('experiment_runs','uq_runs_identity_owner',1,'user_id',0)]
        with self.assertRaises(ValueError):
            database._ensure_run_executions(connection)
        connection.commit.assert_not_called()
