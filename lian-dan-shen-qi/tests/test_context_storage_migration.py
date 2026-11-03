"""摘要增量建表、外键清理和初始化挂接；只使用模拟数据库。"""

from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from mysql.connector import Error as MySQLError

from app import database


class ContextStorageMigrationTests(unittest.TestCase):
    def test_incremental_migration_only_creates_table_and_commits(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        database._ensure_context_summaries(connection)
        cursor.execute.assert_called_once()
        sql = cursor.execute.call_args.args[0]
        self.assertIn("CREATE TABLE IF NOT EXISTS", sql)
        self.assertIn("ON DELETE CASCADE", sql)
        self.assertIn("PRIMARY KEY (session_id)", sql)
        self.assertNotIn("DROP TABLE", sql)
        self.assertNotIn("UPDATE experiment_assistant.chat_messages", sql)
        connection.commit.assert_called_once_with()

    def test_migration_failure_propagates_without_commit(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.execute.side_effect = MySQLError("TEST_ONLY_FAILURE")
        with self.assertRaises(MySQLError):
            database._ensure_context_summaries(connection)
        connection.commit.assert_not_called()

    def test_full_initialization_includes_summary_migration(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value.nextset.return_value = False
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
            stack.enter_context(patch.object(database, "get_connection", return_value=connection))
            for name in ("_ensure_user_password_column", "_ensure_session_user_fk", "_ensure_chat_turns",
                         "_ensure_login_sessions", "_ensure_model_credentials"):
                stack.enter_context(patch.object(database, name))
            ensure = stack.enter_context(patch.object(database, "_ensure_context_summaries"))
            database.initialize_database("unused")
        ensure.assert_called_once_with(connection)

    def test_selective_command_runs_only_summary_migration(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch('sys.argv', ['app.database', '--context-summary-schema']), \
             patch.object(database, 'get_mysql_password', return_value='TEST_ONLY'), \
             patch.object(database, 'get_connection', return_value=connection), \
             patch.object(database, '_ensure_context_summaries') as ensure, \
             patch.object(database, 'initialize_database') as initialize, \
             patch('builtins.print'):
            database.main()
        ensure.assert_called_once_with(connection)
        initialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
