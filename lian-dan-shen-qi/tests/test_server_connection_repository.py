"""服务器 SQL 的归属、短事务、密文边界和选择性建表。"""

from dataclasses import replace
import unittest
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from mysql.connector import Error as MySQLError

from app import database
from app.errors import InvalidTokenError, ServerNotFoundError, ServerStorageError
from app.repositories import server_connection_repository as module
from app.repositories.server_connection_repository import ServerConnectionRepository
from app.security.server_credentials import ServerCredentialCipher
from tests.test_server_connection_core import ID, profile, record


class ServerRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = ServerConnectionRepository('PRIVATE_PASSWORD')
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.rowcount = 1
        self.item = record(ServerCredentialCipher(Fernet.generate_key().decode()))
        self.cursor.fetchone.return_value = vars(self.item)

    def invoke(self, name, *args, **kwargs):
        with patch.object(module, 'get_connection', return_value=self.connection):
            return getattr(self.repository, name)(*args, **kwargs)

    def test_constructor_does_not_open_connection(self):
        with patch.object(module, 'get_connection') as connect:
            ServerConnectionRepository('unused')
        connect.assert_not_called()

    def test_create_inserts_only_cipher_and_reads_back_before_commit(self):
        result = self.invoke('create', 7, profile(), self.item.encrypted_credentials)
        self.assertEqual(result.server_id, ID)
        self.assertEqual(self.cursor.execute.call_args_list[0].args[1][-2:], (self.item.encrypted_credentials, 7))
        self.connection.commit.assert_called_once()

    def test_nonexistent_owner_and_readback_failure_roll_back(self):
        self.cursor.rowcount = 0
        with self.assertRaises(InvalidTokenError):
            self.invoke('create', 7, profile(), self.item.encrypted_credentials)
        self.cursor.rowcount = 1
        self.cursor.fetchone.return_value = None
        with self.assertRaises(ServerStorageError):
            self.invoke('create', 7, profile(), self.item.encrypted_credentials)
        self.connection.commit.assert_not_called()

    def test_get_delete_are_owner_scoped_and_missing_delete_rolls_back(self):
        self.invoke('get', 7, ID)
        self.cursor.execute.assert_called_with(self.repository.get_server_connection_sql, (7, ID))
        self.cursor.rowcount = 0
        self.cursor.fetchone.side_effect=[vars(self.item),None]
        with self.assertRaises(ServerNotFoundError):
            self.invoke('delete', 8, ID)
        self.cursor.execute.assert_called_with(self.repository.delete_server_connection_sql, (8, ID))
        self.connection.rollback.assert_called_once()

    def test_delete_clears_ciphertext_and_keeps_historical_identity(self):
        self.cursor.fetchone.side_effect=[vars(self.item),None]
        self.invoke('delete',7,ID)
        self.assertIn("encrypted_credentials=''",self.repository.delete_server_connection_sql)
        self.assertIn('deleted_at',self.repository.delete_server_connection_sql)
        self.connection.commit.assert_called_once()

    def test_active_training_blocks_credential_erasure(self):
        from app.errors import ServerInUseError
        self.cursor.fetchone.side_effect=[vars(self.item),('running-id',)]
        with self.assertRaises(ServerInUseError):self.invoke('delete',7,ID)
        self.assertNotIn(self.repository.delete_server_connection_sql,[call.args[0] for call in self.cursor.execute.call_args_list])
        self.connection.rollback.assert_called_once()

    def test_page_is_bounded_and_removes_ciphertext_from_returned_objects(self):
        self.cursor.fetchall.return_value = [vars(self.item), vars(replace(self.item, server_id='b' * 32))]
        result = self.invoke('list', 7, limit=1)
        self.assertEqual(result.next_cursor, ID)
        self.assertFalse(hasattr(result.items[0], 'encrypted_credentials'))
        self.cursor.execute.assert_called_once_with(self.repository.list_server_connections_sql, (7, 2))
        self.connection.rollback.assert_called_once()

    def test_foreign_cursor_stops_before_list_query(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(ServerNotFoundError):
            self.invoke('list', 7, before_id=ID)
        self.assertEqual(self.cursor.execute.call_count, 1)

    def test_plaintext_and_bad_identity_are_rejected_before_connect(self):
        with patch.object(module, 'get_connection') as connect:
            for args in ((7, profile(), 'PRIVATE_PASSWORD'), (True, profile(), self.item.encrypted_credentials)):
                with self.assertRaises(ValueError):
                    self.repository.create(*args)
        connect.assert_not_called()

    def test_write_failure_rolls_back_without_reporting_success(self):
        self.cursor.execute.side_effect = MySQLError(msg='PRIVATE_SQL', errno=2013)
        with self.assertRaises(MySQLError):
            self.invoke('create', 7, profile(), self.item.encrypted_credentials)
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()


class ServerMigrationTests(unittest.TestCase):
    def test_selective_cli_only_creates_server_table(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch('sys.argv', ['app.database', '--server-schema']), \
             patch.object(database, 'get_mysql_password', return_value='PRIVATE'), \
             patch.object(database, 'get_connection', return_value=connection), \
             patch.object(database, '_ensure_server_connections') as ensure, \
             patch.object(database, 'initialize_database') as full, patch('builtins.print'):
            database.main()
        ensure.assert_called_once_with(connection)
        full.assert_not_called()

    def test_create_table_is_idempotent_and_has_owner_constraint(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.fetchone.side_effect=[(1,),('deleted_at IS NOT NULL',)]
        database._ensure_server_connections(connection)
        sql = connection.cursor.return_value.__enter__.return_value.execute.call_args_list[0].args[0]
        self.assertIn('CREATE TABLE IF NOT EXISTS experiment_assistant.server_connections', sql)
        self.assertIn('FOREIGN KEY (user_id)', sql)
        connection.commit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
