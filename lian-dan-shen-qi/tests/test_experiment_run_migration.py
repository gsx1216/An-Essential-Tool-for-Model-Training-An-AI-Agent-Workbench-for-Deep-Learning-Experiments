"""运行增量迁移：旧父键补建、重复执行、错误结构与选择性 CLI。"""

from contextlib import ExitStack
import unittest
from unittest.mock import MagicMock, patch

from app import database


class RunMigrationTests(unittest.TestCase):
    def test_old_parent_index_is_added_before_run_table(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        database._ensure_experiment_runs(connection)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn('ALTER TABLE experiment_assistant.experiments', statements[1])
        self.assertIn('CREATE TABLE IF NOT EXISTS experiment_assistant.experiment_runs', statements[2])
        connection.commit.assert_called_once()

    def test_existing_correct_index_is_not_altered(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(1, 'id', 0), (2, 'user_id', 0)]
        database._ensure_experiment_runs(connection)
        self.assertEqual(cursor.execute.call_count, 2)
        self.assertFalse(any('ALTER TABLE' in call.args[0] for call in cursor.execute.call_args_list))

    def test_wrong_index_shape_is_rejected_before_creating_child_table(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(1, 'id', 1), (2, 'user_id', 1)]
        with self.assertRaises(ValueError):
            database._ensure_experiment_runs(connection)
        self.assertEqual(cursor.execute.call_count, 1)
        connection.commit.assert_not_called()

    def test_selective_cli_does_not_initialize_other_tables_or_backfill_chats(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch('sys.argv', ['app.database', '--experiment-run-schema']), \
             patch.object(database, 'get_mysql_password', return_value='PRIVATE'), \
             patch.object(database, 'get_connection', return_value=connection), \
             patch.object(database, '_ensure_experiments') as parent, \
             patch.object(database, '_ensure_experiment_runs') as child, \
             patch.object(database, 'initialize_database') as full, patch('builtins.print'):
            database.main()
        parent.assert_called_once_with(connection)
        child.assert_called_once_with(connection)
        full.assert_not_called()

    def test_full_initialization_includes_run_table(self):
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
            stack.enter_context(patch.object(database, '_ensure_code_changes'))
            stack.enter_context(patch.object(database, '_ensure_revision_runs'))
            stack.enter_context(patch.object(database, '_ensure_papers'))
            stack.enter_context(patch.object(database, '_ensure_skills'))
            stack.enter_context(patch.object(database, 'get_connection', return_value=connection))
            for name in ('_ensure_session_user_fk', '_ensure_user_password_column', '_ensure_chat_turns',
                         '_ensure_login_sessions', '_ensure_model_credentials', '_ensure_context_summaries', '_ensure_experiments'):
                stack.enter_context(patch.object(database, name))
            ensure = stack.enter_context(patch.object(database, '_ensure_experiment_runs'))
            database.initialize_database('unused')
        ensure.assert_called_once_with(connection)


if __name__ == '__main__':
    unittest.main()
