"""实验表的增量创建与选择性迁移，不运行聊天回填或数据清理。"""

from contextlib import ExitStack
import unittest
from unittest.mock import MagicMock, patch

from mysql.connector import Error as MySQLError

from app import database


class ExperimentMigrationTests(unittest.TestCase):
    def test_selective_migration_only_executes_experiment_ddl(self):
        connection = MagicMock()
        database._ensure_experiments(connection)
        sql = connection.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
        self.assertIn('CREATE TABLE IF NOT EXISTS experiment_assistant.experiments', sql)
        self.assertNotIn('DROP ', sql)
        self.assertNotIn('DELETE ', sql.replace('ON DELETE RESTRICT', ''))
        connection.commit.assert_called_once()

    def test_ddl_failure_never_reports_commit(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.execute.side_effect = MySQLError(errno=1142)
        with self.assertRaises(MySQLError):
            database._ensure_experiments(connection)
        connection.commit.assert_not_called()

    def test_default_initialization_includes_experiments(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value.nextset.return_value = None
        with ExitStack() as stack:
            stack.enter_context(patch.object(database, '_ensure_evaluation'))
            stack.enter_context(patch.object(database, '_ensure_chat_tools'))
            stack.enter_context(patch.object(database, '_ensure_research'))
            stack.enter_context(patch.object(database, '_ensure_training_tools'))
            stack.enter_context(patch.object(database, '_ensure_run_monitors'))
            stack.enter_context(patch.object(database, '_ensure_event_bus'))
            stack.enter_context(patch.object(database, '_ensure_analysis'))
            stack.enter_context(patch.object(database, '_ensure_papers'))
            stack.enter_context(patch.object(database, '_ensure_skills'))
            stack.enter_context(patch.object(database, 'get_connection', return_value=connection))
            for name in ('_ensure_session_user_fk', '_ensure_user_password_column', '_ensure_chat_turns',
                         '_ensure_login_sessions', '_ensure_model_credentials', '_ensure_context_summaries'):
                stack.enter_context(patch.object(database, name))
            ensure = stack.enter_context(patch.object(database, '_ensure_experiments'))
            database.initialize_database('unused')
        ensure.assert_called_once_with(connection)

    def test_experiment_cli_never_runs_full_database_initialization(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch('sys.argv', ['app.database', '--experiment-schema']), \
             patch.object(database, 'get_mysql_password', return_value='PRIVATE'), \
             patch.object(database, 'get_connection', return_value=connection), \
             patch.object(database, '_ensure_experiments') as ensure, \
             patch.object(database, 'initialize_database') as initialize, patch('builtins.print'):
            database.main()
        ensure.assert_called_once_with(connection)
        initialize.assert_not_called()


if __name__ == '__main__':
    unittest.main()
