"""真实临时 MySQL 验证凭据SQL；--repository验证仓储，--service同时验证业务。"""

import argparse
import asyncio
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from mysql.connector import Error as MySQLError, IntegrityError

from app import config, database
from app.api import dependencies
from app.config import get_mysql_password
from app.errors import ModelCredentialDecryptionError, ModelCredentialNotConfiguredError
from app.logging_config import configure_logging
from app.repositories import model_credential_repository
from app.repositories.model_credential_repository import ModelCredentialRepository
from app.security.api_key_cipher import ApiKeyCipher
from app.services.model_credential_service import ModelCredentialService


ROOT = Path(__file__).resolve().parents[1]


def verify_repository(password, name, alice, bob, cipher, encrypted_bob):
    repository = ModelCredentialRepository(password)
    for attribute, value in vars(repository).copy().items():
        if attribute.endswith("_sql") and isinstance(value, str):
            setattr(repository, attribute, value.replace("experiment_assistant", name))
    assert repository.get_key(alice) is None
    assert repository.get_key(bob) == encrypted_bob
    first_key = cipher.encrypt(alice, "sk-TEST_ONLY_REPOSITORY_FIRST")
    replacement = cipher.encrypt(alice, "sk-TEST_ONLY_REPOSITORY_REPLACEMENT")
    assert repository.save_key(alice, first_key) is None
    assert repository.get_key(alice) == first_key
    repository.save_key(alice, replacement)
    assert repository.get_key(alice) == replacement
    assert repository.get_key(bob) == encrypted_bob

    # SQL实际写入/删除之后模拟提交失败，验证仓储确实回滚并保留旧配置。
    class FailedCommitConnection:
        def __init__(self):
            self.connection = database.get_connection(password)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.connection.close()

        def cursor(self):
            return self.connection.cursor()

        def commit(self):
            raise MySQLError("TEST_ONLY_COMMIT_FAILURE", errno=2013)

        def rollback(self):
            self.connection.rollback()

    for operation, arguments in ((repository.save_key, (alice, first_key)),
                                 (repository.delete_key, (alice,))):
        with patch.object(model_credential_repository, "get_connection", side_effect=lambda _password: FailedCommitConnection()):
            try:
                operation(*arguments)
            except MySQLError as error:
                assert error.errno == 2013
            else:
                raise AssertionError("Repository accepted failed commit")
        assert repository.get_key(alice) == replacement

    broken = repository.get_key_sql.replace(".model_credentials", "._missing_model_credentials")
    with patch.object(repository, "get_key_sql", broken):
        try:
            repository.get_key(alice)
        except MySQLError as error:
            assert error.errno == 1146
        else:
            raise AssertionError("Repository swallowed SQL read failure")

    for operation, arguments in ((repository.save_key, (2**64 - 1, first_key)),
                                 (repository.get_key, (2**64 - 1,)),
                                 (repository.delete_key, (2**64 - 1,))):
        try:
            operation(*arguments)
        except ValueError:
            pass
        else:
            raise AssertionError("Repository accepted a missing user")
    assert repository.delete_key(alice) is True
    assert repository.get_key(alice) is None
    assert repository.delete_key(alice) is False
    assert repository.get_key(bob) == encrypted_bob
    print("PASS: real repository save/update/get/delete and repeated delete, owner separation, missing-user rejection, SQL failure propagation and save/delete commit-failure rollback.")


async def verify_service(password, name, alice, bob, master_key):
    # 使用用户写好的实际工厂；主密钥临时注入进程环境，配置读取走真实函数。
    factory = dependencies.get_model_credential_service
    factory.cache_clear()
    with TemporaryDirectory() as temporary:
        with patch.object(config, "ENV_FILE", Path(temporary) / ".env"):
            with patch.dict(os.environ, {"MODEL_CREDENTIAL_ENCRYPTION_KEY": master_key}):
                with patch.object(dependencies, "get_mysql_password", return_value=password):
                    try:
                        service = factory()
                        assert factory() is service
                    finally:
                        factory.cache_clear()
    repository = service.repository
    for attribute, value in vars(repository).copy().items():
        if attribute.endswith("_sql") and isinstance(value, str):
            setattr(repository, attribute, value.replace("experiment_assistant", name))
    assert await service.is_configured(alice) is False
    assert await service.is_configured(bob) is True
    assert await service.get_api_key(bob) == "sk-TEST_ONLY_BOB_KEY"
    try:
        await service.get_api_key(alice)
    except ModelCredentialNotConfiguredError:
        pass
    else:
        raise AssertionError("Unconfigured service supplied a key")

    first = "sk-TEST_ONLY_SERVICE_FIRST"
    replacement = "sk-TEST_ONLY_SERVICE_REPLACEMENT"
    assert await service.save_api_key(alice, first) is None
    assert await service.is_configured(alice) is True
    encrypted = await asyncio.to_thread(repository.get_key, alice)
    assert isinstance(encrypted, str) and encrypted != first and first not in encrypted
    assert await service.get_api_key(alice) == first
    await service.save_api_key(alice, replacement)
    assert await service.get_api_key(alice) == replacement
    assert await service.get_api_key(bob) == "sk-TEST_ONLY_BOB_KEY"

    try:
        await service.save_api_key(alice, " invalid key ")
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid API key reached storage")
    assert await service.get_api_key(alice) == replacement

    # 整段合法密文被移到另一用户，解密时仍必须拒绝归属不符。
    bob_ciphertext = await asyncio.to_thread(repository.get_key, bob)
    await asyncio.to_thread(repository.save_key, alice, bob_ciphertext)
    try:
        await service.get_api_key(alice)
    except ModelCredentialDecryptionError:
        pass
    else:
        raise AssertionError("Service accepted another user's ciphertext")
    await service.save_api_key(alice, replacement)

    wrong_master_service = ModelCredentialService(
        repository, ApiKeyCipher(Fernet.generate_key().decode("ascii")),
    )
    try:
        await wrong_master_service.get_api_key(alice)
    except ModelCredentialDecryptionError:
        pass
    else:
        raise AssertionError("Wrong master key was accepted")

    cases = (("save_key_sql", service.save_api_key, (alice, first)),
             ("get_key_sql", service.is_configured, (alice,)),
             ("get_key_sql", service.get_api_key, (alice,)),
             ("delete_key_sql", service.delete_api_key, (alice,)))
    for attribute, operation, arguments in cases:
        broken = getattr(repository, attribute).replace(".model_credentials", "._missing_model_credentials")
        with patch.object(repository, attribute, broken):
            try:
                await operation(*arguments)
            except MySQLError as error:
                assert error.errno == 1146
            else:
                raise AssertionError("Service swallowed a database failure")
        assert await service.get_api_key(alice) == replacement

    assert await service.delete_api_key(alice) is True
    assert await service.delete_api_key(alice) is False
    assert await service.is_configured(alice) is False
    try:
        await service.get_api_key(alice)
    except ModelCredentialNotConfiguredError:
        pass
    else:
        raise AssertionError("Deleted credential remained readable")
    assert await service.get_api_key(bob) == "sk-TEST_ONLY_BOB_KEY"
    print("PASS: actual student factory + real master-key loader + service + Fernet + repository + temporary MySQL: encrypted save/update, status, fresh internal reads, deletion/repeat deletion, user isolation, wrong-owner/master rejection and database error propagation. Master key was temporary; no real configuration or model was changed.")


def main(check_repository=False, check_service=False):
    configure_logging(console=False)
    password = get_mysql_password()
    name = "_codex_credential_check_" + uuid4().hex
    assert re.fullmatch(r"_codex_credential_check_[0-9a-f]{32}", name)
    created = False

    def sql(filename):
        return (ROOT / "sql" / filename).read_text(encoding="utf-8").replace("experiment_assistant", name)

    try:
        with database.get_connection(password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                created = True
                cursor.execute(sql("init.sql"))
                while cursor.nextset():
                    pass
            # 使用真实建表基础设施，把 SQL 目标改成刚创建的独立临时库。
            with patch.object(database, "_read_sql", side_effect=sql):
                database._ensure_model_credentials(connection)
                database._ensure_model_credentials(connection)

            with connection.cursor() as cursor:
                owners = []
                for username in ("credential_alice", "credential_bob"):
                    cursor.execute(f"INSERT INTO `{name}`.users (username) VALUES (%s)", (username,))
                    owners.append(cursor.lastrowid)
            connection.commit()
            alice, bob = owners
            master_key = Fernet.generate_key().decode("ascii")
            cipher = ApiKeyCipher(master_key)
            alice_key = "sk-TEST_ONLY_ALICE_KEY"
            replacement_key = "sk-TEST_ONLY_ALICE_REPLACEMENT"
            bob_key = "sk-TEST_ONLY_BOB_KEY"
            encrypted_alice = cipher.encrypt(alice, alice_key)
            encrypted_replacement = cipher.encrypt(alice, replacement_key)
            encrypted_bob = cipher.encrypt(bob, bob_key)

            def get_key(owner):
                with connection.cursor() as cursor:
                    cursor.execute(sql("get_model_credential.sql"), (owner,))
                    row = cursor.fetchone()
                return None if row is None else row[0]

            assert get_key(alice) is None
            with connection.cursor() as cursor:
                cursor.execute(sql("save_model_credential.sql"), (alice, encrypted_alice))
                cursor.execute(sql("save_model_credential.sql"), (bob, encrypted_bob))
            connection.commit()
            assert get_key(alice) == encrypted_alice
            assert get_key(bob) == encrypted_bob
            assert cipher.decrypt(alice, get_key(alice)) == alice_key

            with connection.cursor() as cursor:
                cursor.execute(sql("save_model_credential.sql"), (alice, encrypted_replacement))
            connection.commit()
            assert cipher.decrypt(alice, get_key(alice)) == replacement_key
            assert get_key(bob) == encrypted_bob
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM `{name}`.model_credentials")
                assert cursor.fetchone()[0] == 2
            connection.commit()

            with patch.object(database, "_read_sql", side_effect=sql):
                database._ensure_model_credentials(connection)
            assert get_key(alice) == encrypted_replacement
            assert get_key(bob) == encrypted_bob

            with connection.cursor() as cursor:
                cursor.execute(sql("save_model_credential.sql"), (bob, cipher.encrypt(bob, "sk-TEST_ONLY_ROLLBACK")))
            connection.rollback()
            assert get_key(bob) == encrypted_bob

            with connection.cursor() as cursor:
                try:
                    cursor.execute(sql("save_model_credential.sql"), (2**64 - 1, encrypted_alice))
                except IntegrityError as error:
                    assert error.errno == 1452
                    connection.rollback()
                else:
                    raise AssertionError("Foreign key accepted a missing user")
                try:
                    cursor.execute(sql("save_model_credential.sql"), (alice, "sk-TEST_ONLY_PLAINTEXT"))
                except MySQLError as error:
                    assert error.errno == 3819
                    connection.rollback()
                else:
                    raise AssertionError("Length constraint accepted a short plaintext key")

            with connection.cursor() as cursor:
                cursor.execute(sql("delete_model_credential.sql"), (bob,))
                assert cursor.rowcount == 1
            connection.rollback()
            assert get_key(bob) == encrypted_bob
            with connection.cursor() as cursor:
                cursor.execute(sql("delete_model_credential.sql"), (alice,))
                assert cursor.rowcount == 1
            connection.commit()
            assert get_key(alice) is None
            assert get_key(bob) == encrypted_bob
            with connection.cursor() as cursor:
                cursor.execute(sql("delete_model_credential.sql"), (alice,))
                assert cursor.rowcount == 0
                cursor.execute(f"SELECT COUNT(*) FROM `{name}`.users")
                assert cursor.fetchone()[0] == 2
            connection.commit()

        print("PASS: real SQL create/repeat-create, insert/update without duplicate rows, owner-scoped reads/deletes, encrypted round-trip, foreign-key and length constraints, save/delete rollback and retained users.")
        if check_repository or check_service:
            verify_repository(password, name, alice, bob, cipher, encrypted_bob)
        else:
            print("SQL-only mode: repository save/get/delete were not called; pass --repository to verify them.")
        if check_service:
            asyncio.run(verify_service(password, name, alice, bob, master_key))
    finally:
        if created:
            assert re.fullmatch(r"_codex_credential_check_[0-9a-f]{32}", name)
            with database.get_connection(password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{name}`")
            print("Temporary credential SQL verification database removed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", action="store_true", help="同时验证实际仓储方法")
    parser.add_argument("--service", action="store_true", help="同时验证实际业务、仓储和加解密")
    arguments = parser.parse_args()
    main(check_repository=arguments.repository, check_service=arguments.service)
