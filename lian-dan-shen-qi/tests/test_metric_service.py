"""采集预算、真实来源边界、惰性 Runner、CAS 重试和离线读取。"""

import base64
from copy import deepcopy
import hashlib
from unittest.mock import AsyncMock, MagicMock
import unittest

from app.errors import MetricSourceChangedError, MetricSourceUnavailableError, MetricSyncConflictError, RunNotFoundError
from app.models.training_metrics import initial_stream
from app.services.metric_service import MetricService


class MetricServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.snapshot={'revision':0,'state':initial_stream(),'updated_at':None}
        self.repo=MagicMock()
        self.repo.snapshot.side_effect=lambda *args:deepcopy(self.snapshot)
        def commit(user,run,revision,state,events):
            if revision!=self.snapshot['revision']:return False
            if state!=self.snapshot['state']:
                self.snapshot={'revision':revision+1,'state':deepcopy(state),'updated_at':None}
            return True
        self.repo.commit_page.side_effect=commit
        self.runner=MagicMock();self.runner.operate=AsyncMock()
        self.raw=b'{"type":"metric","step":0,"loss":0.5}\n'
        async def fetch(user,run,action,*,offset,limit):
            data=self.raw[offset:offset+limit]
            return {'observation':{'status':'succeeded','stale':False,'truncated':False,'source_changed':False,
                'raw_base64':base64.b64encode(data).decode(),'anchor_sha256':hashlib.sha256(self.raw[max(0,offset-128):offset]).hexdigest(),
                'log_size':len(self.raw)}}
        self.runner.operate.side_effect=fetch
        self.service=MetricService(self.repo,self.runner)

    async def test_sync_persists_once_then_replay_reuses_revision(self):
        first=await self.service.sync(1,'a'*32);second=await self.service.sync(1,'a'*32)
        self.assertEqual(first['revision'],second['revision']);self.assertEqual(second['stream']['valid'],1)
        self.assertNotIn('pending',second['stream']);self.assertNotIn('tail',second['stream'])

    async def test_max_pages_bounds_work_and_leaves_explicit_more_data(self):
        self.raw=b'normal\n'*10000
        result=await self.service.sync(1,'a'*32,max_pages=2)
        self.assertEqual(self.runner.operate.await_count,2);self.assertEqual(result['stream']['offset'],32768)
        self.assertFalse(result['stream']['caught_up'])

    async def test_invalid_page_budget_rejected_before_repository(self):
        for value in (0,9,True):
            with self.assertRaises(ValueError):await self.service.sync(1,'a'*32,max_pages=value)
        self.repo.snapshot.assert_not_called()

    async def test_owner_check_precedes_lazy_runner_factory(self):
        self.repo.snapshot.side_effect=RunNotFoundError('private')
        factory=MagicMock();service=MetricService(self.repo,runner_factory=factory)
        with self.assertRaises(RunNotFoundError):await service.sync(1,'a'*32)
        factory.assert_not_called()

    async def test_changed_source_invalidates_report_source_without_resetting_events(self):
        self.runner.operate.side_effect=None
        self.runner.operate.return_value={'observation':{'status':'running','source_changed':True}}
        with self.assertRaises(MetricSourceChangedError):await self.service.sync(1,'a'*32)
        self.repo.invalidate_source.assert_called_once_with(1,'a'*32);self.repo.commit_page.assert_not_called()

    async def test_missing_source_is_not_empty_success(self):
        self.runner.operate.side_effect=None;self.runner.operate.return_value={'observation':{'status':'missing'}}
        with self.assertRaises(MetricSourceUnavailableError):await self.service.sync(1,'a'*32)
        self.repo.invalidate_source.assert_called_once()

    async def test_concurrent_version_changes_are_bounded(self):
        self.repo.commit_page.side_effect=None;self.repo.commit_page.return_value=False
        with self.assertRaises(MetricSyncConflictError):await self.service.sync(1,'a'*32)
        self.assertEqual(self.runner.operate.await_count,3)

    async def test_read_and_report_do_not_construct_runner(self):
        factory=MagicMock();service=MetricService(self.repo,runner_factory=factory)
        await service.events(1,'a'*32);await service.latest(1,'a'*32);await service.report(1,'a'*32,generate=True)
        factory.assert_not_called()
