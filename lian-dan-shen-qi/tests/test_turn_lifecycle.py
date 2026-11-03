"""模拟数据库边界故障；真实 MySQL 的状态与并发另做集成验证。"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mysql.connector import Error

from app import database
from app.errors import RequestIdConflictError, UnfinishedTurnError
from app.repositories import message_repository


SESSION_ID = "b4c4e67554ee4c1981740a1e901ec614"
TURN_ID = "430390d71ece4e63ae202043b057e9c2"
ATTEMPT_ID = "a" * 32
OLD_ATTEMPT = "b" * 32
USER = (1, "测试用户")
SESSION = (SESSION_ID, "测试会话")
QUESTION = "    print('hello')\n"
HISTORY = [("human", QUESTION)]


def turn(status="pending", attempt=None, active=False):
    return (TURN_ID, status, 1, attempt, active, "MODEL_REQUEST_FAILED" if status == "failed" else None)


class TurnLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.repository = message_repository.MessageRepository("unused")

    def fake_database(self, rows, history=HISTORY):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [USER, SESSION, *rows]
        cursor.fetchall.return_value = history
        cursor.rowcount = 1
        return connection, cursor

    def writes(self, cursor):
        return [call.args for call in cursor.execute.call_args_list
                if call.args[0].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]

    def assert_rollback(self, connection):
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once_with()

    def start(self, **overrides):
        arguments = dict(user_id=1, session_id=SESSION_ID, content=QUESTION, request_id=TURN_ID)
        return self.repository.start_turn(**(arguments | overrides))

    def claim(self, **overrides):
        arguments = dict(user_id=1, session_id=SESSION_ID, turn_id=TURN_ID)
        return self.repository.claim_turn(**(arguments | overrides))

    def complete(self, **overrides):
        arguments = dict(user_id=1, session_id=SESSION_ID, turn_id=TURN_ID,
                         attempt_id=ATTEMPT_ID, assistant_content="回复")
        return self.repository.complete_turn(**(arguments | overrides))

    def fail(self, **overrides):
        arguments = dict(user_id=1, session_id=SESSION_ID, turn_id=TURN_ID, attempt_id=ATTEMPT_ID)
        return self.repository.fail_turn(**(arguments | overrides))

    def test_start_commits_question_and_pending_record_without_ai(self):
        for previous, seq in ((None, 1), ((4,), 5)):
            with self.subTest(seq=seq):
                connection, cursor = self.fake_database([None, None, previous])
                with patch.object(message_repository, "get_connection", return_value=connection):
                    self.assertEqual(self.start(), TURN_ID)
                self.assertEqual(self.writes(cursor), [
                    (self.repository.save_message_sql, (SESSION_ID, seq, "human", QUESTION)),
                    (self.repository.create_turn_sql, (TURN_ID, SESSION_ID, seq)),
                    (self.repository.update_session_time_sql, (SESSION_ID,)),
                ])
                connection.commit.assert_called_once_with()
                connection.rollback.assert_not_called()

    def test_start_can_generate_request_id_on_backend(self):
        connection, cursor = self.fake_database([None, None, None])
        with patch.object(message_repository, "get_connection", return_value=connection):
            with patch.object(message_repository, "uuid4", return_value=SimpleNamespace(hex=TURN_ID)):
                self.assertEqual(self.start(request_id=None), TURN_ID)

    def test_same_request_and_content_reuse_existing_turn(self):
        for status in ("pending", "running", "failed", "completed"):
            with self.subTest(status=status):
                connection, cursor = self.fake_database([turn(status, ATTEMPT_ID), ("human", QUESTION)])
                with patch.object(message_repository, "get_connection", return_value=connection):
                    self.assertEqual(self.start(), TURN_ID)
                self.assertEqual(self.writes(cursor), [])

    def test_same_request_id_cannot_be_used_for_a_different_question(self):
        connection, cursor = self.fake_database([turn(), ("human", "另一个问题")])
        with patch.object(message_repository, "get_connection", return_value=connection):
            with self.assertRaisesRegex(RequestIdConflictError, "不同的问题"):
                self.start()
        self.assertEqual(self.writes(cursor), [])
        self.assert_rollback(connection)

    def test_unfinished_question_blocks_a_second_new_turn(self):
        for status in ("pending", "running", "failed"):
            with self.subTest(status=status):
                connection, cursor = self.fake_database([None, turn(status)])
                with patch.object(message_repository, "get_connection", return_value=connection):
                    with self.assertRaisesRegex(UnfinishedTurnError, "未完成的问题"):
                        self.start()
                self.assertEqual(self.writes(cursor), [])
                self.assert_rollback(connection)

    def test_invalid_inputs_never_connect(self):
        cases = (
            (self.start, {"user_id": 0}), (self.start, {"user_id": True}),
            (self.start, {"session_id": "bad"}), (self.start, {"content": "  "}),
            (self.start, {"request_id": "bad"}),
            (self.claim, {"turn_id": "bad"}), (self.claim, {"lease_seconds": 0}),
            (self.claim, {"lease_seconds": True}), (self.claim, {"lease_seconds": 3601}),
            (self.complete, {"assistant_content": " \n"}),
            (self.complete, {"attempt_id": "bad"}), (self.fail, {"error_code": "x" * 65}),
        )
        with patch.object(message_repository, "get_connection") as connect:
            for method, overrides in cases:
                with self.subTest(method=method.__name__, overrides=overrides), self.assertRaises(ValueError):
                    method(**overrides)
            connect.assert_not_called()

    def test_sequence_overflow_rejected_before_question_insert(self):
        connection, cursor = self.fake_database([None, None, (2**64 - 2,)])
        with patch.object(message_repository, "get_connection", return_value=connection):
            with self.assertRaises(ValueError):
                self.start()
        self.assertEqual(self.writes(cursor), [])
        self.assert_rollback(connection)

    def test_missing_user_or_wrong_owner_cannot_access_any_turn_operation(self):
        methods = (self.start, self.claim, self.complete, self.fail,
                   lambda: self.repository.get_unfinished_turn(1, SESSION_ID),
                   lambda: self.repository.get_completed_reply(1, SESSION_ID, TURN_ID))
        for method in methods:
            for rows in ([None], [USER, None]):
                with self.subTest(method=method, rows=rows):
                    connection, cursor = self.fake_database([])
                    cursor.fetchone.side_effect = rows
                    with patch.object(message_repository, "get_connection", return_value=connection):
                        with self.assertRaises(ValueError):
                            method()
                    self.assertEqual(self.writes(cursor), [])
                    connection.commit.assert_not_called()

    def test_claim_retries_pending_failed_or_expired_tasks_without_another_question(self):
        states = (turn(), turn("failed", OLD_ATTEMPT), turn("running", OLD_ATTEMPT, False))
        for state in states:
            with self.subTest(state=state):
                connection, cursor = self.fake_database([state, (1,)])
                with patch.object(message_repository, "get_connection", return_value=connection):
                    with patch.object(message_repository, "uuid4", return_value=SimpleNamespace(hex=ATTEMPT_ID)):
                        self.assertEqual(self.claim(), (ATTEMPT_ID, HISTORY))
                self.assertEqual(self.writes(cursor), [
                    (self.repository.claim_turn_sql, (ATTEMPT_ID, 600, SESSION_ID, TURN_ID)),
                ])
                self.assertIn((self.repository.lock_messages_sql, (1, SESSION_ID)),
                              [call.args for call in cursor.execute.call_args_list])
                connection.commit.assert_called_once_with()

    def test_claim_rejects_active_or_completed_tasks(self):
        for state in (turn("running", ATTEMPT_ID, True), turn("completed", ATTEMPT_ID)):
            with self.subTest(state=state):
                connection, cursor = self.fake_database([state])
                with patch.object(message_repository, "get_connection", return_value=connection):
                    with self.assertRaises(ValueError):
                        self.claim()
                self.assertEqual(self.writes(cursor), [])
                self.assert_rollback(connection)

    def test_claim_requires_saved_question_at_current_tail(self):
        for last, history in (((2,), HISTORY), ((1,), []), ((1,), [("ai", "意外回复")])):
            with self.subTest(last=last, history=history):
                connection, cursor = self.fake_database([turn(), last], history)
                with patch.object(message_repository, "get_connection", return_value=connection):
                    with self.assertRaises(ValueError):
                        self.claim()
                self.assertEqual(self.writes(cursor), [])

    def test_complete_only_inserts_ai_and_commits_status_together(self):
        connection, cursor = self.fake_database([turn("running", ATTEMPT_ID, True), (1,)])
        with patch.object(message_repository, "get_connection", return_value=connection):
            self.complete()
        self.assertEqual(self.writes(cursor), [
            (self.repository.save_message_sql, (SESSION_ID, 2, "ai", "回复")),
            (self.repository.complete_turn_sql, (SESSION_ID, TURN_ID, ATTEMPT_ID)),
            (self.repository.update_session_time_sql, (SESSION_ID,)),
        ])
        connection.commit.assert_called_once_with()

    def test_repeated_completion_is_idempotent_but_cannot_change_reply(self):
        for stored_reply in ("回复", "不同回复"):
            with self.subTest(stored_reply=stored_reply):
                connection, cursor = self.fake_database([
                    turn("completed", ATTEMPT_ID), ("ai", stored_reply),
                ])
                with patch.object(message_repository, "get_connection", return_value=connection):
                    if stored_reply == "回复":
                        self.complete()
                        connection.commit.assert_called_once_with()
                    else:
                        with self.assertRaises(ValueError):
                            self.complete()
                        self.assert_rollback(connection)
                self.assertEqual(self.writes(cursor), [])

    def test_old_generation_cannot_complete_or_fail_after_retry(self):
        for method in (self.complete, self.fail):
            with self.subTest(method=method):
                connection, cursor = self.fake_database([turn("running", OLD_ATTEMPT, True)])
                with patch.object(message_repository, "get_connection", return_value=connection):
                    with self.assertRaisesRegex(ValueError, "已失效"):
                        method()
                self.assertEqual(self.writes(cursor), [])
                self.assert_rollback(connection)

    def test_expired_generation_cannot_complete(self):
        connection, cursor = self.fake_database([turn("running", ATTEMPT_ID, False)])
        with patch.object(message_repository, "get_connection", return_value=connection):
            with self.assertRaisesRegex(ValueError, "已过期"):
                self.complete()
        self.assertEqual(self.writes(cursor), [])

    def test_fail_only_updates_state_and_never_deletes_saved_question(self):
        connection, cursor = self.fake_database([turn("running", ATTEMPT_ID, True)])
        with patch.object(message_repository, "get_connection", return_value=connection):
            self.fail()
        self.assertEqual(self.writes(cursor), [
            (self.repository.fail_turn_sql, ("MODEL_REQUEST_FAILED", SESSION_ID, TURN_ID, ATTEMPT_ID)),
            (self.repository.update_session_time_sql, (SESSION_ID,)),
        ])
        connection.commit.assert_called_once_with()

    def test_repeated_failure_is_idempotent_and_completed_reply_cannot_be_failed(self):
        for status in ("failed", "completed"):
            with self.subTest(status=status):
                connection, cursor = self.fake_database([turn(status, ATTEMPT_ID)])
                with patch.object(message_repository, "get_connection", return_value=connection):
                    if status == "failed":
                        self.fail()
                    else:
                        with self.assertRaises(ValueError):
                            self.fail()
                        self.assert_rollback(connection)
                self.assertEqual(self.writes(cursor), [])

    def test_write_failures_rollback_only_the_current_transaction(self):
        cases = (
            (self.start, [None, None, None], self.repository.create_turn_sql),
            (self.start, [None, None, None], self.repository.update_session_time_sql),
            (self.complete, [turn("running", ATTEMPT_ID, True), (1,)], self.repository.complete_turn_sql),
            (self.complete, [turn("running", ATTEMPT_ID, True), (1,)], self.repository.update_session_time_sql),
            (self.fail, [turn("running", ATTEMPT_ID, True)], self.repository.fail_turn_sql),
        )
        for method, rows, failed_sql in cases:
            with self.subTest(method=method, failed_sql=failed_sql):
                connection, cursor = self.fake_database(rows)
                failure = Error("simulated write failure")

                def execute(sql, params):
                    if sql == failed_sql:
                        raise failure

                cursor.execute.side_effect = execute
                with patch.object(message_repository, "get_connection", return_value=connection):
                    with self.assertRaises(Error) as caught:
                        method()
                self.assertIs(caught.exception, failure)
                self.assert_rollback(connection)

    def test_conditional_state_update_must_affect_one_row(self):
        for method, rows in (
            (self.claim, [turn(), (1,)]),
            (self.complete, [turn("running", ATTEMPT_ID, True), (1,)]),
            (self.fail, [turn("running", ATTEMPT_ID, True)]),
        ):
            with self.subTest(method=method):
                connection, cursor = self.fake_database(rows)
                cursor.rowcount = 0
                with patch.object(message_repository, "get_connection", return_value=connection):
                    with self.assertRaisesRegex(ValueError, "状态已变化"):
                        method()
                self.assert_rollback(connection)

    def test_missing_turn_cannot_be_claimed_or_modified(self):
        for method in (self.claim, self.complete, self.fail):
            with self.subTest(method=method):
                connection, cursor = self.fake_database([None])
                with patch.object(message_repository, "get_connection", return_value=connection):
                    with self.assertRaisesRegex(ValueError, "轮次不存在"):
                        method()
                self.assertEqual(self.writes(cursor), [])

    def test_completed_reply_read_is_scoped_to_owned_session_and_turn(self):
        for row in (None, ("已保存的回复",)):
            with self.subTest(row=row):
                connection, cursor = self.fake_database([row])
                with patch.object(message_repository, "get_connection", return_value=connection):
                    reply = self.repository.get_completed_reply(1, SESSION_ID, TURN_ID)
                self.assertEqual(reply, None if row is None else row[0])
                self.assertEqual(cursor.execute.call_args.args,
                                 (self.repository.get_completed_reply_sql, (1, SESSION_ID, TURN_ID)))
                self.assertEqual(self.writes(cursor), [])

    def test_unfinished_turn_exposes_status_and_server_lease_result(self):
        for row in (None, turn("failed", ATTEMPT_ID), turn("running", ATTEMPT_ID, True)):
            with self.subTest(row=row):
                connection, cursor = self.fake_database([row])
                with patch.object(message_repository, "get_connection", return_value=connection):
                    result = self.repository.get_unfinished_turn(1, SESSION_ID)
                if row is None:
                    self.assertIsNone(result)
                else:
                    self.assertEqual((result.turn_id, result.status, result.lease_active),
                                     (TURN_ID, row[1], bool(row[4])))
                self.assertEqual(self.writes(cursor), [])


class TurnInitializationTests(unittest.TestCase):
    def test_initialization_adds_table_and_backfills_without_updating_messages(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        database._ensure_chat_turns(connection)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("CREATE TABLE IF NOT EXISTS", statements[0])
        self.assertIn("INSERT INTO experiment_assistant.chat_turns", statements[1])
        self.assertNotIn("UPDATE experiment_assistant.chat_messages", "\n".join(statements))
        connection.commit.assert_called_once_with()

    def test_initialize_database_keeps_previous_backup_result_and_adds_turn_schema(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.nextset.return_value = None
        with patch.object(database, "_ensure_skills"), patch.object(database, "_ensure_papers"), patch.object(database, "_ensure_revision_runs"), patch.object(database, "_ensure_code_changes"), patch.object(database, "_ensure_analysis"), patch.object(database, "_ensure_event_bus"), patch.object(database, "_ensure_run_monitors"), patch.object(database, "_ensure_training_tools"), patch.object(database, "_ensure_research"), patch.object(database, "_ensure_chat_tools"), patch.object(database, "_ensure_evaluation"), patch.object(database, "get_connection", return_value=connection):
            with patch.object(database, "_ensure_session_user_fk", return_value="backup.json") as fk:
                with patch.object(database, "_ensure_chat_turns") as turns:
                    with patch.object(database, "_ensure_user_password_column") as passwords:
                        self.assertEqual(database.initialize_database("unused"), "backup.json")
        passwords.assert_called_once_with(connection)
        fk.assert_called_once_with(connection, False)
        turns.assert_called_once_with(connection)


if __name__ == "__main__":
    unittest.main()
