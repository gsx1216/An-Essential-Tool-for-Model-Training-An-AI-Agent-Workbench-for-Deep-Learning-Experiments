"""容量诊断的只读边界、范围约束、归属、计划脱敏和 CLI 失败路径。"""

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from mysql.connector import Error as MySQLError

from app.errors import SessionNotFoundError
from app.repositories import capacity_repository as module
from app.repositories.capacity_repository import CapacityRepository, safe_plan
from app.services.database_capacity_service import render_capacity_report
from scripts import check_database_capacity as cli
from scripts import verify_database_capacity_mysql as verification


SID = 'b4c4e67554ee4c1981740a1e901ec614'


class CapacityRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = CapacityRepository('PRIVATE_PASSWORD')
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.setup_cursor = MagicMock()
        self.cursor = MagicMock()
        self.connection.cursor.side_effect = lambda **kw: self._context(self.cursor if kw.get('dictionary') else self.setup_cursor)

    @staticmethod
    def _context(cursor):
        context = MagicMock()
        context.__enter__.return_value = cursor
        return context

    def arrange(self, *, owned=True, summary=None, count=2, has_more=True):
        self.cursor.fetchall.side_effect = [
            [dict(table_name='chat_messages', engine='InnoDB', estimated_rows=None,
                  data_bytes=16384, index_bytes=16384)],
            [dict(table_name='chat_messages', index_name='uq_chat_messages_session_seq',
                  position=i, column_name=name, non_unique=0, is_visible='YES')
             for i, name in enumerate(['session_id', 'seq'], 1)],
            [dict(table='s', type='const', key='PRIMARY', rows=1, Extra=None)],
            [dict(table='m', type='range', key='uq_chat_messages_session_seq', rows=2,
                  Extra='Using index condition; Using filesort')],
        ]
        self.cursor.fetchone.side_effect = [
            {'found': 1} if owned else None, summary,
            dict(inspected_messages=count, content_bytes=count * 3,
                 first_seq=1 if count else None, last_seq=2 if count else None),
            {'seq': 5} if has_more else None,
        ]

    def inspect(self, **kwargs):
        with patch.object(module, 'get_connection', return_value=self.connection):
            return self.repository.inspect(**kwargs)

    def test_constructor_never_connects_and_restricts_schema_identifiers(self):
        with patch.object(module, 'get_connection') as connect:
            for name in ('other', 'mysql', 'x`; DROP DATABASE x;--', None):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    CapacityRepository('unused', schema=name)
            CapacityRepository('unused', schema='_codex_capacity_' + 'a' * 32)
        connect.assert_not_called()

    def test_default_only_reads_metadata_in_readonly_transaction(self):
        self.arrange()
        result = self.inspect()
        self.assertIsNone(result['session'])
        self.assertIsNone(result['tables'][0]['estimated_rows'])
        self.assertEqual(self.cursor.execute.call_count, 2)
        self.assertIn('users', result['missing_tables'])
        self.setup_cursor.execute.assert_any_call('SET SESSION max_execution_time = %s', (2000,))
        self.connection.start_transaction.assert_called_once_with(
            isolation_level='REPEATABLE READ', consistent_snapshot=True, readonly=True)
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()
        self.assertNotIn('PRIVATE_PASSWORD', str(result))

    def test_owned_chat_is_bounded_and_explains_original_sql_with_parameters(self):
        self.arrange(summary=dict(summarized_until_seq=2, version=1))
        result = self.inspect(user_id=7, session_id=SID, sample_limit=2, after_seq=0)
        session = result['session']
        self.assertEqual(session['content_bytes'], 6)
        self.assertTrue(session['has_more'])
        self.assertEqual(session['summarized_until_seq'], 2)
        self.cursor.execute.assert_any_call(self.repository.sql['capacity_message_sample'], (7, SID, 0, 2))
        self.cursor.execute.assert_any_call('EXPLAIN FORMAT=TRADITIONAL ' + self.repository.sql['load_context_messages'], (7, SID, 2))
        self.cursor.execute.assert_any_call('EXPLAIN FORMAT=TRADITIONAL ' + self.repository.sql['load_history_page'], (7, SID, 0, 50))
        actual = [call.args[0] for call in self.cursor.execute.call_args_list]
        self.assertFalse(any('ANALYZE' in query for query in actual))
        self.assertNotIn(SID, str(result))
        self.connection.commit.assert_not_called()

    def test_wrong_owner_or_missing_session_stops_before_message_queries(self):
        self.arrange(owned=False)
        with self.assertRaises(SessionNotFoundError):
            self.inspect(user_id=8, session_id=SID)
        self.assertEqual(self.cursor.execute.call_count, 3)
        self.connection.rollback.assert_called_once()

    def test_no_summary_and_empty_range_are_explicit(self):
        self.arrange(count=0, has_more=False)
        session = self.inspect(user_id=7, session_id=SID, after_seq=99)['session']
        self.assertEqual(session['summarized_until_seq'], 0)
        self.assertIsNone(session['summary_version'])
        self.assertIsNone(session['last_seq'])
        self.assertFalse(session['has_more'])
        self.cursor.execute.assert_any_call(self.repository.sql['capacity_has_more'], (7, SID, 99))

    def test_invalid_scope_limits_and_bool_rejected_before_connect(self):
        cases = [dict(user_id=1), dict(session_id=SID), dict(user_id=True, session_id=SID),
                 dict(user_id=1, session_id='bad'), dict(sample_limit=0), dict(sample_limit=10001),
                 dict(sample_limit=True), dict(page_size=101), dict(page_size=False),
                 dict(after_seq=-1), dict(after_seq=2**64), dict(max_execution_ms=0),
                 dict(max_execution_ms=30001)]
        with patch.object(module, 'get_connection') as connect:
            for case in cases:
                with self.subTest(case=case), self.assertRaises(ValueError):
                    self.repository.inspect(**case)
        connect.assert_not_called()

    def test_uppercase_hyphenated_uuid_normalized(self):
        self.arrange()
        self.inspect(user_id=7, session_id='B4C4E675-54EE-4C19-8174-0A1E901EC614')
        self.cursor.execute.assert_any_call(self.repository.sql['capacity_owned_session'], (7, SID))

    def test_timeout_and_database_failures_rollback_and_propagate(self):
        self.cursor.execute.side_effect = MySQLError(msg='PRIVATE_QUERY_VALUE', errno=3024)
        with self.assertRaises(MySQLError):
            self.inspect()
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_plan_projection_never_exports_ref_extra_or_arbitrary_columns(self):
        result = safe_plan([dict(table='m', type='range', key='uq_chat_messages_session_seq',
                                 rows=13, Extra='Using filesort PRIVATE_BODY',
                                 ref='PRIVATE_ID', arbitrary='PRIVATE_KEY')])
        self.assertTrue(result[0]['filesort'])
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual(result[0]['estimated_rows'], 13)

    def test_invalid_plan_identifier_is_not_rendered(self):
        result = safe_plan([dict(table='<script>secret</script>', key='x|y\nsecret', type=None)])
        self.assertEqual(result[0]['table'], '未提供')
        self.assertNotIn('secret', str(result))


class ReportAndCliTests(unittest.TestCase):
    def report(self):
        return dict(generated_at_utc='2026-10-25T00:00:00+00:00', elapsed_ms=1.2,
                    tables=[], indexes=[], missing_tables=['users'], session=None,
                    settings=dict(after_seq=0, sample_limit=2, max_execution_ms=2000, page_size=50))

    def test_metadata_only_report_labels_estimates_missing_tables_and_scope(self):
        text = render_capacity_report(self.report())
        for expected in ('估计', '不可见', '未指定会话', '不是并发压测', '不会自动改表'):
            self.assertIn(expected, text)

    def test_partial_and_complete_ranges_never_claim_total_for_truncated_window(self):
        report = self.report()
        report['session'] = dict(inspected_messages=2, content_bytes=6, first_seq=1, last_seq=3,
                                 has_more=True, summarized_until_seq=1, summary_version=1, plans={})
        self.assertIn('不是完整会话总量', render_capacity_report(report))
        report['session']['has_more'] = False
        self.assertIn('此起点后的全部现存消息', render_capacity_report(report))

    def call_cli(self, arguments, repository=None, password_error=None):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
             patch.object(cli, 'get_mysql_password', return_value='PRIVATE_PASSWORD', side_effect=password_error), \
             patch.object(cli, 'CapacityRepository', return_value=repository):
            result = cli.main(arguments)
        return result, out.getvalue(), err.getvalue()

    def test_cli_writes_new_utf8_report_and_never_echoes_password(self):
        repo = MagicMock()
        repo.inspect.return_value = self.report()
        with TemporaryDirectory() as directory:
            target = Path(directory) / 'report.md'
            code, out, err = self.call_cli(['--output', str(target)], repo)
            self.assertEqual(code, 0)
            self.assertTrue(target.read_text(encoding='utf-8').startswith('# 数据库'))
            self.assertIn('报告已保存', out)
            self.assertNotIn('PRIVATE_PASSWORD', out + err)

    def test_cli_never_overwrites_existing_file(self):
        repo = MagicMock()
        repo.inspect.return_value = self.report()
        with TemporaryDirectory() as directory:
            target = Path(directory) / 'report.md'
            target.write_text('original', encoding='utf-8')
            code, _, err = self.call_cli(['--output', str(target)], repo)
            self.assertEqual(code, 1)
            self.assertIn('已存在', err)
            self.assertEqual(target.read_text(encoding='utf-8'), 'original')

    def test_cli_hides_raw_database_timeout_and_configuration_details(self):
        for error in (MySQLError(msg='PRIVATE_HOST_PASSWORD', errno=3024),
                      MySQLError(msg='PRIVATE_HOST_PASSWORD', errno=1045),
                      SessionNotFoundError('PRIVATE_OWNER')):
            repo = MagicMock()
            repo.inspect.side_effect = error
            code, out, err = self.call_cli([], repo)
            self.assertEqual(code, 1)
            self.assertNotIn('PRIVATE', out + err)
        code, _, err = self.call_cli([], password_error=ValueError('PRIVATE_CONFIG'))
        self.assertEqual(code, 1)
        self.assertNotIn('PRIVATE', err)

    def test_cli_requires_owner_and_session_together_without_reading_config(self):
        with redirect_stderr(StringIO()), patch.object(cli, 'get_mysql_password') as password:
            with self.assertRaises(SystemExit) as result:
                cli.main(['--user-id', '7'])
        self.assertEqual(result.exception.code, 2)
        password.assert_not_called()


class VerificationSafetyTests(unittest.TestCase):
    def test_cleanup_guard_refuses_project_database_and_untrusted_names(self):
        for name in ('experiment_assistant', 'mysql', '_codex_capacity_',
                     '_codex_capacity_' + 'a' * 32 + '; DROP DATABASE mysql', None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                verification.validate_temporary_name(name)
        verification.validate_temporary_name('_codex_capacity_' + 'a' * 32)

    def test_default_verification_does_not_enable_benchmark(self):
        with patch.object(verification, 'get_mysql_password', return_value='unused'), \
             patch.object(verification, 'run') as run:
            self.assertEqual(verification.main([]), 0)
        self.assertFalse(run.call_args.args[0].benchmark)

    def test_oversized_synthetic_fixture_refused_before_database_access(self):
        with redirect_stderr(StringIO()), patch.object(verification, 'get_mysql_password') as password, \
             patch.object(verification, 'run') as run:
            self.assertEqual(verification.main(['--benchmark', '--sessions', '100', '--messages-per-session', '2000']), 1)
        password.assert_not_called()
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
