"""不连接真实 MySQL，验证无效输入不会执行写入，以及事务失败会回滚。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mysql.connector import IntegrityError, errorcode

from app import database, validators
from app.repositories import message_repository, session_repository, user_repository


SESSION_ID = "b4c4e67554ee4c1981740a1e901ec614"
USER = (1, "测试用户")
SESSION = (SESSION_ID, "测试会话")


def fake_database():
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value.__enter__.return_value
    return connection, cursor


def has_write(cursor):
    return any(
        call.args[0].lstrip().upper().startswith(("INSERT", "UPDATE", "ALTER", "DELETE"))
        for call in cursor.execute.call_args_list
    )


class InputValidationTests(unittest.TestCase):
    def test_user_ids_and_sequences_reject_invalid_types_and_ranges(self):
        for value in (None, True, False, "2", 1.5, 0, -1, 2**64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validators.validate_positive_integer(value, "编号")

    def test_uuid_is_normalized(self):
        self.assertEqual(
            validators.normalize_session_id("B4C4E675-54EE-4C19-8174-0A1E901EC614"),
            SESSION_ID,
        )

    def test_invalid_uuid_is_rejected(self):
        for value in (None, 2, "", "2", "not-a-uuid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validators.normalize_session_id(value)

    def test_message_whitespace_is_preserved(self):
        content = "    print('hello')\n"
        self.assertEqual(validators.validate_message_content(content), content)

    def test_invalid_username_rejected_before_connecting(self):
        repository = user_repository.UserRepository("unused")
        with patch.object(user_repository, "get_connection") as connect:
            for username in (None, "", "  ", "名" * 65):
                with self.subTest(username=username), self.assertRaises(ValueError):
                    repository.create_user(username)
            connect.assert_not_called()

    def test_invalid_title_rejected_before_connecting(self):
        repository = session_repository.SessionRepository("unused")
        with patch.object(session_repository, "get_connection") as connect:
            for title in (None, "", "  ", "题" * 201):
                with self.subTest(title=title), self.assertRaises(ValueError):
                    repository.create_session(1, title)
            connect.assert_not_called()


class UserRepositoryTests(unittest.TestCase):
    def test_valid_user_returns_generated_id(self):
        repository = user_repository.UserRepository("unused")
        connection, cursor = fake_database()
        cursor.lastrowid = 7
        with patch.object(user_repository, "get_connection", return_value=connection):
            self.assertEqual(repository.create_user("  张三  "), 7)
        cursor.execute.assert_called_once_with(repository.create_user_sql, ("张三",))
        connection.commit.assert_called_once_with()

    def test_duplicate_username_rolls_back_and_has_readable_error(self):
        repository = user_repository.UserRepository("unused")
        connection, cursor = fake_database()
        cursor.execute.side_effect = IntegrityError("duplicate", errno=errorcode.ER_DUP_ENTRY)
        with patch.object(user_repository, "get_connection", return_value=connection):
            with self.assertRaisesRegex(ValueError, "用户名已存在"):
                repository.create_user("张三")
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once_with()


class SessionRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = session_repository.SessionRepository("unused")

    def test_missing_user_cannot_create_session_or_generate_success_uuid(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = None
        with patch.object(session_repository, "get_connection", return_value=connection):
            with patch.object(session_repository, "uuid4") as new_uuid:
                with self.assertRaisesRegex(ValueError, "用户 ID 2 不存在"):
                    self.repository.create_session(2)
                new_uuid.assert_not_called()
        self.assertFalse(has_write(cursor))
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once_with()

    def test_session_reads_use_one_owned_query_without_user_precheck(self):
        for method, arguments, sql, expected in (
            (self.repository.list_sessions, (2,), self.repository.list_sessions_sql, []),
            (self.repository.get_session, (2, SESSION_ID), self.repository.get_session_sql, None),
        ):
            with self.subTest(method=method.__name__):
                connection, cursor = fake_database()
                cursor.fetchone.return_value = None
                cursor.fetchall.return_value = []
                with patch.object(session_repository, "get_connection", return_value=connection):
                    self.assertEqual(method(*arguments), expected)
                cursor.execute.assert_called_once_with(sql, arguments)
                self.assertFalse(has_write(cursor))

    def test_valid_user_creates_owned_session(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = USER
        with patch.object(session_repository, "get_connection", return_value=connection):
            with patch.object(session_repository, "uuid4", return_value=SimpleNamespace(hex=SESSION_ID)):
                self.assertEqual(self.repository.create_session(1, "  测试会话  "), SESSION_ID)
        self.assertIn(
            (self.repository.sql_session_sql, (SESSION_ID, 1, "测试会话")),
            [call.args for call in cursor.execute.call_args_list],
        )
        connection.commit.assert_called_once_with()

    def test_database_fk_failure_is_reported_and_rolled_back(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = USER
        cursor.execute.side_effect = [None, IntegrityError("missing parent", errno=errorcode.ER_NO_REFERENCED_ROW_2)]
        with patch.object(session_repository, "get_connection", return_value=connection):
            with self.assertRaisesRegex(ValueError, "关联用户不存在"):
                self.repository.create_session(1)
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once_with()

    def test_other_users_session_returns_none(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = None
        with patch.object(session_repository, "get_connection", return_value=connection):
            self.assertIsNone(self.repository.get_session(1, SESSION_ID))

    def test_existing_user_without_sessions_gets_empty_list(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = USER
        cursor.fetchall.return_value = []
        with patch.object(session_repository, "get_connection", return_value=connection):
            self.assertEqual(self.repository.list_sessions(1), [])


class MessageRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = message_repository.MessageRepository("unused")

    def test_invalid_history_inputs_never_connect(self):
        valid = dict(user_id=1, session_id=SESSION_ID)
        cases = ({"user_id": 0}, {"user_id": True}, {"user_id": "1"}, {"session_id": "2"}, {"session_id": None})
        with patch.object(message_repository, "get_connection") as connect:
            for overrides in cases:
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    self.repository.load_messages(**(valid | overrides))
            connect.assert_not_called()

    def test_missing_user_cannot_read_messages(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = None
        with patch.object(message_repository, "get_connection", return_value=connection):
            with self.assertRaisesRegex(ValueError, "用户 ID 2 不存在"):
                self.repository.load_messages(2, SESSION_ID)
        self.assertFalse(has_write(cursor))
        cursor.fetchall.assert_not_called()
        connection.commit.assert_not_called()

    def test_wrong_owner_cannot_read_messages(self):
        connection, cursor = fake_database()
        cursor.fetchone.side_effect = [USER, None]
        with patch.object(message_repository, "get_connection", return_value=connection):
            with self.assertRaisesRegex(ValueError, "不属于该用户"):
                self.repository.load_messages(1, SESSION_ID)
        self.assertFalse(has_write(cursor))
        cursor.fetchall.assert_not_called()
        connection.commit.assert_not_called()

    def test_owner_can_load_history(self):
        connection, cursor = fake_database()
        cursor.fetchone.side_effect = [USER, SESSION]
        cursor.fetchall.return_value = [("human", "你好")]
        with patch.object(message_repository, "get_connection", return_value=connection):
            self.assertEqual(self.repository.load_messages(1, SESSION_ID), [("human", "你好")])
        self.assertEqual(cursor.execute.call_args.args, (self.repository.load_messages_sql, (1, SESSION_ID)))


class DatabaseMigrationTests(unittest.TestCase):
    def test_existing_fk_is_not_added_twice(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = (1,)
        self.assertIsNone(database._ensure_session_user_fk(connection))
        self.assertFalse(has_write(cursor))

    def test_clean_old_database_gets_foreign_key(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        database._ensure_session_user_fk(connection)
        self.assertTrue(any(call.args[0].startswith("ALTER TABLE") for call in cursor.execute.call_args_list))

    def test_orphans_are_not_changed_without_repair_flag(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = [(SESSION_ID, 2, "旧记录")]
        with patch.object(database, "_backup_orphan_owners") as backup:
            with self.assertRaisesRegex(ValueError, "暂未修改旧记录"):
                database._ensure_session_user_fk(connection)
            backup.assert_not_called()
        self.assertFalse(has_write(cursor))
        connection.commit.assert_not_called()

    def test_backup_failure_prevents_repair(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = [(SESSION_ID, 2, "旧记录")]
        with patch.object(database, "_backup_orphan_owners", side_effect=OSError("backup failed")):
            with self.assertRaises(OSError):
                database._ensure_session_user_fk(connection, repair_orphan_owners=True)
        self.assertFalse(has_write(cursor))
        connection.commit.assert_not_called()

    def test_explicit_repair_backs_up_before_targeted_update(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = None
        orphans = [(SESSION_ID, 2, "旧记录")]
        cursor.fetchall.return_value = orphans
        events = []
        def backup(rows):
            self.assertEqual(rows, orphans)
            events.append("backup")
            return Path("backup.json")
        def execute(sql, params=None):
            if sql.startswith("UPDATE"):
                events.append("update")
                self.assertEqual(params, (SESSION_ID, 2))
        cursor.execute.side_effect = execute
        with patch.object(database, "_backup_orphan_owners", side_effect=backup):
            self.assertEqual(database._ensure_session_user_fk(connection, True), Path("backup.json"))
        self.assertEqual(events, ["backup", "update"])

    def test_backup_preserves_original_owner_and_title(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(database, "PROJECT_ROOT", Path(directory)):
                path = database._backup_orphan_owners([(SESSION_ID, 2, "旧记录")])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["sessions"], [{"session_id": SESSION_ID, "old_user_id": 2, "title": "旧记录"}])

    def test_user_audit_is_read_only(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = [(SESSION_ID, 2, "旧记录")]
        with patch.object(database, "get_connection", return_value=connection):
            user, orphans = database.check_user_and_orphans("unused", 2)
        self.assertIsNone(user)
        self.assertEqual(len(orphans), 1)
        self.assertFalse(has_write(cursor))
        connection.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
