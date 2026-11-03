"""已存事件/游标完整性错误必须安全失败，不能返回伪造零值。"""

from datetime import datetime
import unittest
from unittest.mock import MagicMock

from app.errors import MetricStorageError
from app.models.training_metrics import initial_stream
from app.repositories.metric_repository import MetricRepository, encode


class MetricStorageTests(unittest.TestCase):
    def setUp(self):
        self.repo=MetricRepository('PRIVATE');self.cursor=MagicMock()

    def test_corrupt_stream_digest_is_storage_failure(self):
        text,_=encode(initial_stream())
        self.cursor.fetchone.return_value={'state_json':text,'state_digest':'wrong','source_offset':0,'revision':1,'updated_at':datetime.now()}
        with self.assertRaises(MetricStorageError):self.repo._stream(self.cursor,1,'a'*32)

    def test_inconsistent_cursor_and_json_position_rejected(self):
        text,digest=encode(initial_stream())
        self.cursor.fetchone.return_value={'state_json':text,'state_digest':digest,'source_offset':1,'revision':1,'updated_at':datetime.now()}
        with self.assertRaises(MetricStorageError):self.repo._stream(self.cursor,1,'a'*32)

    def test_stored_metric_values_require_the_same_schema_as_ingestion(self):
        row={'source_offset':0,'step':0,'epoch':None,'split':'train','values_json':'{"loss":null}'}
        with self.assertRaises(MetricStorageError):self.repo._event_row(row)
        row['values_json']='{"loss":0.5}'
        self.assertEqual(self.repo._event_row(row)['values']['loss'],.5)

    def test_stored_report_digest_and_identity_checked(self):
        row={'version':1,'report_json':'{"run_id":"b"}','report_digest':'wrong','generated_at':datetime.now()}
        with self.assertRaises(MetricStorageError):self.repo._report_row(row,'a'*32)

    def test_invalid_aggregate_is_not_returned_as_number(self):
        self.cursor.fetchall.return_value=[{'first_value':None,'last_value':1,'min_value':0,'max_value':1}]
        with self.assertRaises(MetricStorageError):self.repo._summaries(self.cursor,1,'a'*32)
