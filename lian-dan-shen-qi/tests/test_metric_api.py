"""接口类型、当前身份、安全异常、只读依赖与选择性迁移。"""

from datetime import datetime,timezone
import unittest
from unittest.mock import create_autospec,MagicMock,patch

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mysql.connector import Error as MySQLError

from app import database
from app.api import dependencies
from app.api.auth_dependencies import require_current_user
from app.api.error_handlers import request_validation_exception_handler
from app.api.metric_routes import router
from app.api.schemas import UserResponse
from app.errors import MetricSourceChangedError,MetricStorageError,MetricSyncConflictError,ReportNotFoundError,RunNotFoundError
from app.services.metric_service import MetricService


class MetricApiTests(unittest.TestCase):
    def setUp(self):
        self.service=create_autospec(MetricService,instance=True)
        self.service.sync.return_value={'run_id':'a'*32,'revision':1,'pages':1,'new_events':1,'stream':{'valid':1}}
        self.service.events.return_value={'items':[],'next_offset':None};self.service.latest.return_value={'items':[]}
        self.service.report.return_value={'version':1,'generated_at':datetime.now(timezone.utc),'is_stale':False,'data':{}}
        self.app=FastAPI();self.app.include_router(router);self.app.add_exception_handler(RequestValidationError,request_validation_exception_handler)
        self.app.dependency_overrides[require_current_user]=lambda:UserResponse(user_id=7,username='alice')
        self.app.dependency_overrides[dependencies.get_metric_service]=lambda:self.service
        self.client=TestClient(self.app);self.addCleanup(self.client.close)

    def test_current_owner_and_pagination_are_forwarded(self):
        result=self.client.get('/runs/'+'a'*32+'/metrics?user_id=99&limit=2&split=validation&metric_name=loss')
        self.assertEqual(result.status_code,200)
        self.service.events.assert_awaited_once_with(7,'a'*32,after=-1,limit=2,split='validation',metric_name='loss')
        self.assertEqual(result.headers['Cache-Control'],'no-store')

    def test_case_insensitive_parser_body_and_event_filters(self):
        rule={'pattern':r'Epoch: (?P<EPOCH>\d+) MAE: (?P<MAE>[0-9.]+)',
              'dataset':'Tiny_COD','split':'Validation','metrics':['MAE']}
        expected={**rule,'dataset':'tiny_cod','split':'validation','metrics':['mae']}
        self.service.set_parser.return_value={'rules':[expected]}
        result=self.client.put('/runs/'+'a'*32+'/metrics/parser',json={'rules':[rule]})
        self.assertEqual(result.status_code,200)
        self.service.set_parser.assert_awaited_once_with(7,'a'*32,[expected])
        result=self.client.get('/runs/'+'a'*32+'/metrics?split=Validation&metric_name=MAE&dataset=Tiny_COD')
        self.assertEqual(result.status_code,200)
        self.service.events.assert_awaited_once_with(7,'a'*32,after=-1,limit=100,split='validation',metric_name='mae',dataset='tiny_cod')

    def test_sync_and_report_generation_are_explicit(self):
        self.assertEqual(self.client.post('/runs/'+'a'*32+'/metrics/sync?max_pages=2').status_code,200)
        self.service.sync.assert_awaited_once_with(7,'a'*32,max_pages=2)
        self.assertEqual(self.client.post('/runs/'+'a'*32+'/report').status_code,200)
        self.service.report.assert_awaited_with(7,'a'*32,generate=True)
        self.assertEqual(self.client.get('/runs/'+'a'*32+'/report').status_code,200)
        self.service.report.assert_awaited_with(7,'a'*32)

    def test_bad_uuid_filter_and_budgets_are_422(self):
        for path in ('/runs/bad/metrics','/runs/'+'a'*32+'/metrics?limit=501','/runs/'+'a'*32+'/metrics?metric_name=secret%22',
                     '/runs/'+'a'*32+'/metrics?split=other'):
            self.assertEqual(self.client.get(path).status_code,422)
        self.assertEqual(self.client.post('/runs/'+'a'*32+'/metrics/sync?max_pages=0').status_code,422)
        self.service.sync.assert_not_awaited()

    def test_errors_do_not_expose_database_or_source_content(self):
        for error,status in [(RunNotFoundError('PRIVATE'),404),(ReportNotFoundError('PRIVATE'),404),(MetricStorageError('PRIVATE'),503),
                             (MetricSourceChangedError('PRIVATE'),409),(MetricSyncConflictError('PRIVATE'),409),(MySQLError('PRIVATE'),503)]:
            self.service.report.side_effect=error
            result=self.client.get('/runs/'+'a'*32+'/report')
            self.assertEqual(result.status_code,status);self.assertNotIn('PRIVATE',result.text)

    def test_formal_openapi_has_all_five_authenticated_operations(self):
        from app.main import app
        paths=app.openapi()['paths']
        for path,method in [('/runs/{run_id}/metrics/sync','post'),('/runs/{run_id}/metrics','get'),
                            ('/runs/{run_id}/metrics/latest','get'),('/runs/{run_id}/report','post'),('/runs/{run_id}/report','get')]:
            self.assertTrue(paths[path][method]['security'])

    def test_factory_is_lazy_and_does_not_require_ssh_key_configuration(self):
        dependencies.get_metric_service.cache_clear()
        try:
            with patch.object(dependencies,'get_mysql_password',return_value='PRIVATE'),patch.object(dependencies,'get_runner_service') as runner:
                service=dependencies.get_metric_service()
                self.assertIsInstance(service,MetricService);runner.assert_not_called()
        finally:dependencies.get_metric_service.cache_clear()

    def test_selective_migration_never_runs_chat_backfill_or_training(self):
        connection=MagicMock();connection.__enter__.return_value=connection
        with patch('sys.argv',['app.database','--metrics-schema']),patch.object(database,'get_mysql_password',return_value='PRIVATE'), \
             patch.object(database,'get_connection',return_value=connection),patch.object(database,'_ensure_metrics') as ensure, \
             patch.object(database,'initialize_database') as full,patch('builtins.print'):
            database.main()
        ensure.assert_called_once_with(connection);full.assert_not_called()
