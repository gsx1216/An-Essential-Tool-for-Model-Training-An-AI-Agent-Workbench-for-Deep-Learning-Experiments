"""单次运行分析：限定归属和领取范围；预览绝不入队或调用模型。"""
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from app import process_run_analysis as command
from app.models.analysis import AnalysisSettings
from app.repositories import analysis_repository
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.suggestion_repository import SuggestionRepository
from app.services.analysis_service import AnalysisService

RUN = 'a' * 32


class ClaimScopeTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = self.connection.cursor.return_value.__enter__.return_value

    def test_each_stage_filters_owner_and_run_without_global_reaping(self):
        for cls in (AnalysisRepository, SuggestionRepository):
            with self.subTest(stage=cls.agent_name):
                self.cursor.reset_mock()
                self.cursor.fetchone.side_effect = [{'run_id': RUN}, None]
                repo = cls('TEST_ONLY')
                with patch.object(analysis_repository, 'get_connection', return_value=self.connection):
                    self.assertIsNone(repo.claim(120, 2, user_id=7, run_id=RUN))
                calls = self.cursor.execute.call_args_list
                self.assertIn('t.user_id=%s AND t.run_id=%s', calls[-1].args[0])
                self.assertIn("t.agent_name='" + cls.agent_name + "'", calls[-1].args[0])
                self.assertEqual(calls[-1].args[1], (7, RUN, 2))
                self.assertNotIn(repo.reap_analysis_tasks_sql, [call.args[0] for call in calls])

    def test_wrong_owner_cannot_claim(self):
        from app.errors import RunNotFoundError
        repo = AnalysisRepository('TEST_ONLY')
        self.cursor.fetchone.return_value = None
        with patch.object(analysis_repository, 'get_connection', return_value=self.connection), self.assertRaises(RunNotFoundError):
            repo.claim(120, 2, user_id=8, run_id=RUN)
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.connection.rollback.assert_called_once()

    def test_incomplete_scope_never_falls_back_to_global_queue(self):
        repo = AnalysisRepository('TEST_ONLY')
        for scope in ({'user_id': 7}, {'run_id': RUN}):
            with self.subTest(scope=scope), patch.object(analysis_repository, 'get_connection') as connect:
                with self.assertRaises((ValueError, TypeError)):
                    repo.claim(120, 2, **scope)
                connect.assert_not_called()


class ScopedCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_neither_receives_event_nor_constructs_model_services(self):
        with patch.object(command, 'inspect_run', return_value={'user_id': 7, 'run_id': RUN}), \
             patch.object(command, 'EventRepository') as events, \
             patch.object(command, 'build_analysis') as analysis, \
             patch.object(command, 'build_suggestions') as suggestions:
            result = await command.process(7, RUN)
        self.assertFalse(result['model_called'])
        events.assert_not_called()
        analysis.assert_not_called()
        suggestions.assert_not_called()

    async def test_execute_passes_scope_to_both_stages_once(self):
        plan = {'user_id': 7, 'run_id': RUN, 'event_id': 'b'*32,
                'analysis_enabled': True, 'suggestions_enabled': True}
        analysis, suggestions = Mock(), Mock()
        analysis.tick, suggestions.tick = AsyncMock(return_value=True), AsyncMock(return_value=True)
        settings = AnalysisSettings()
        with patch.object(command, 'inspect_run', return_value=plan), \
             patch.object(command, 'get_mysql_password', return_value='TEST_ONLY'), \
             patch.object(command, 'EventRepository') as events, \
             patch.object(command, 'get_analysis_settings', return_value=settings), \
             patch.object(command, 'get_suggestion_settings', return_value=settings), \
             patch.object(command, 'build_analysis', return_value=analysis), \
             patch.object(command, 'build_suggestions', return_value=suggestions):
            events.return_value.list.return_value = []
            await command.process(7, RUN, execute=True)
        events.return_value.receive.assert_called_once_with(plan['event_id'])
        analysis.tick.assert_awaited_once_with(user_id=7, run_id=RUN)
        suggestions.tick.assert_awaited_once_with(user_id=7, run_id=RUN)
        self.assertFalse(settings.enabled)

    async def test_tick_forwards_scope_instead_of_unrestricted_claim(self):
        repo = Mock()
        repo.claim.return_value = None
        service = AnalysisService(repo, settings=replace(AnalysisSettings(), enabled=True))
        self.assertFalse(await service.tick(user_id=7, run_id=RUN))
        repo.claim.assert_called_once_with(service.settings.lease_seconds, service.settings.max_attempts,
                                           user_id=7, run_id=RUN)
