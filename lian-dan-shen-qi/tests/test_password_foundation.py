import unittest
from unittest.mock import MagicMock, patch

from mysql.connector import IntegrityError, errorcode

from app import database
from app.repositories import user_repository
from app.security.passwords import PasswordService, validate_password_hash


PASSWORD = "example-password-for-tests"


def fake_database():
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value.__enter__.return_value
    return connection, cursor


class PasswordServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.passwords = PasswordService()

    def test_random_salts_and_correct_wrong_password_verification(self):
        first = self.passwords.hash_password(PASSWORD)
        second = self.passwords.hash_password(PASSWORD)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("$argon2id$"))
        self.assertNotIn(PASSWORD, first)
        self.assertEqual(validate_password_hash(first), first)
        self.assertTrue(self.passwords.verify_password(PASSWORD, first))
        self.assertFalse(self.passwords.verify_password("another-password", first))

    def test_unicode_and_leading_trailing_spaces_are_not_modified(self):
        password = "  这是一个足够长的测试密码，包含空格与中文  "
        encoded = self.passwords.hash_password(password)
        self.assertTrue(self.passwords.verify_password(password, encoded))
        self.assertFalse(self.passwords.verify_password(password.strip(), encoded))

    def test_hash_rejects_wrong_types_and_password_length_limits(self):
        for value in (None, 123, True, "", "x" * 7, "x" * 33, "x" * 8 + "\ud800"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.passwords.hash_password(value)
        for value in ("x" * 8, "x" * 14, "x" * 32):
            with self.subTest(length=len(value)):
                self.assertTrue(self.passwords.verify_password(value, self.passwords.hash_password(value)))

    def test_older_long_password_can_still_be_verified_but_not_registered(self):
        # 模拟旧规则下已经保存的哈希，修改注册长度不能让旧账号突然无法登录。
        legacy_password = "legacy-password-" * 3
        encoded = self.passwords._hasher.hash(legacy_password)
        with self.assertRaises(ValueError):
            self.passwords.hash_password(legacy_password)
        self.assertTrue(self.passwords.verify_password(legacy_password, encoded))

    def test_missing_or_invalid_stored_hash_cannot_authenticate(self):
        for stored in (None, "", "plaintext", "$argon2id$bad", b"bytes"):
            with self.subTest(stored=stored):
                self.assertFalse(self.passwords.verify_password(PASSWORD, stored))
                self.assertFalse(self.passwords.verify_password("dummy-account-placeholder", stored))

    def test_excessive_hash_parameters_are_rejected_without_expensive_verification(self):
        encoded = self.passwords.hash_password(PASSWORD)
        excessive = encoded.replace("m=19456", "m=999999999")
        with self.assertRaises(ValueError):
            validate_password_hash(excessive)
        self.assertFalse(self.passwords.verify_password(PASSWORD, excessive))


class RegisteredUserRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.encoded = PasswordService().hash_password(PASSWORD)

    def setUp(self):
        self.repository = user_repository.UserRepository("unused")

    def test_registration_inserts_hash_and_returns_generated_id(self):
        connection, cursor = fake_database()
        cursor.lastrowid = 7
        with patch.object(user_repository, "get_connection", return_value=connection):
            self.assertEqual(self.repository.create_registered_user("  张三  ", self.encoded), 7)
        cursor.execute.assert_called_once_with(self.repository.create_registered_user_sql, ("张三", self.encoded))
        self.assertNotIn(PASSWORD, repr(cursor.execute.call_args.args))
        connection.commit.assert_called_once_with()

    def test_registration_rejects_plaintext_or_invalid_username_before_connecting(self):
        with patch.object(user_repository, "get_connection") as connect:
            for username, encoded in (("", self.encoded), ("名" * 65, self.encoded), ("张三", PASSWORD), ("张三", None), ("张三", "$argon2id$bad")):
                with self.subTest(username=username, encoded=encoded), self.assertRaises(ValueError):
                    self.repository.create_registered_user(username, encoded)
            connect.assert_not_called()

    def test_duplicate_registered_username_rolls_back(self):
        connection, cursor = fake_database()
        cursor.execute.side_effect = IntegrityError("duplicate", errno=errorcode.ER_DUP_ENTRY)
        with patch.object(user_repository, "get_connection", return_value=connection):
            with self.assertRaisesRegex(ValueError, "用户名已存在"):
                self.repository.create_registered_user("张三", self.encoded)
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once_with()

    def test_auth_lookup_returns_internal_row_or_none_using_parameters(self):
        for row in (None, (1, "张三", None), (1, "张三", self.encoded)):
            with self.subTest(row=row):
                connection, cursor = fake_database()
                cursor.fetchone.return_value = row
                with patch.object(user_repository, "get_connection", return_value=connection):
                    self.assertEqual(self.repository.get_user_auth("  张三  "), row)
                cursor.execute.assert_called_once_with(self.repository.get_user_auth_sql, ("张三",))


class UserPasswordMigrationTests(unittest.TestCase):
    def test_old_table_adds_nullable_column_without_updating_users(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = None
        self.assertTrue(database._ensure_user_password_column(connection))
        sql = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("ADD COLUMN password_hash VARCHAR(255) NULL", sql[-1])
        self.assertFalse(any(statement.lstrip().upper().startswith(("UPDATE", "DELETE")) for statement in sql))
        connection.commit.assert_called_once_with()

    def test_existing_password_column_is_not_added_again(self):
        connection, cursor = fake_database()
        cursor.fetchone.return_value = ("varchar", 255, "YES")
        self.assertFalse(database._ensure_user_password_column(connection))
        cursor.execute.assert_called_once()

    def test_incompatible_column_stops_without_changing_data(self):
        for column in (("varchar", 64, "YES"), ("text", 65535, "YES"), ("varchar", 255, "NO")):
            with self.subTest(column=column):
                connection, cursor = fake_database()
                cursor.fetchone.return_value = column
                with self.assertRaises(ValueError):
                    database._ensure_user_password_column(connection)
                cursor.execute.assert_called_once()
                connection.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
