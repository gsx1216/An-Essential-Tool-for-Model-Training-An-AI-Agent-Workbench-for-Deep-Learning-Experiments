"""有界同步真实远端日志；无客户端指标上传入口，不调用模型。"""

import asyncio
import base64

from app.errors import MetricSourceChangedError, MetricSourceUnavailableError, MetricSyncConflictError
from app.logging_config import logged_operation
from app.models.experiment_run import normalize_run_uuid
from app.models.training_metrics import anchor_for, parse_page
from app.validators import validate_positive_integer


class MetricService:
    def __init__(self, repository, runner=None, *, runner_factory=None):
        self.repository, self.runner, self.runner_factory = repository, runner, runner_factory

    @logged_operation('metric_service.sync')
    async def sync(self, user_id, run_id, *, max_pages=4):
        user_id = validate_positive_integer(user_id, '用户 ID'); run_id = normalize_run_uuid(run_id)
        if type(max_pages) is not int or not 1 <= max_pages <= 8:
            raise ValueError('采集页数范围不正确。')
        committed, conflicts = 0, 0
        initial_valid = None
        runner = None
        while committed < max_pages:
            before = await asyncio.to_thread(self.repository.snapshot, user_id, run_id)
            state = before['state']
            if initial_valid is None:initial_valid=state['valid']
            if runner is None:
                runner = self.runner if self.runner is not None else self.runner_factory()
            result = await runner.operate(user_id, run_id, 'logs', offset=state['offset'], limit=16384)
            observation = result['observation']
            if observation['status'] == 'missing':
                await asyncio.to_thread(self.repository.invalidate_source, user_id, run_id)
                raise MetricSourceUnavailableError('暂未找到远端日志来源。')
            if observation['source_changed'] or observation['anchor_sha256'] != anchor_for(state):
                await asyncio.to_thread(self.repository.invalidate_source, user_id, run_id)
                raise MetricSourceChangedError('远端日志缩短或游标边界已变化。')
            data = base64.b64decode(observation['raw_base64'], validate=True)
            after, events = parse_page(state, data, observation,rules=before.get('metric_rules'))
            saved = await asyncio.to_thread(self.repository.commit_page, user_id, run_id, before['revision'], after, events)
            if not saved:
                conflicts += 1
                if conflicts >= 3:
                    raise MetricSyncConflictError('其他采集请求正在推进游标。')
                continue
            committed += 1
            if after['caught_up']:
                break
        snapshot = await asyncio.to_thread(self.repository.snapshot, user_id, run_id)
        public = {key: value for key, value in snapshot['state'].items() if key not in {'pending', 'tail', 'last_steps'}}
        return {'run_id': run_id, 'revision': snapshot['revision'], 'pages': committed,
                'new_events':max(0,public['valid']-(initial_valid or 0)), 'stream': public}

    @logged_operation('metric_service.get_parser')
    async def get_parser(self,user_id,run_id):
        return await asyncio.to_thread(self.repository.get_parser,user_id,run_id)

    @logged_operation('metric_service.set_parser')
    async def set_parser(self,user_id,run_id,rules):
        return await asyncio.to_thread(self.repository.set_parser,user_id,run_id,rules)

    @logged_operation('metric_service.events')
    async def events(self, user_id, run_id, **options):
        return await asyncio.to_thread(self.repository.list_events, user_id, run_id, **options)

    @logged_operation('metric_service.latest')
    async def latest(self, user_id, run_id):
        return await asyncio.to_thread(self.repository.latest, user_id, run_id)

    @logged_operation('metric_service.report')
    async def report(self, user_id, run_id, *, generate=False):
        return await asyncio.to_thread(self.repository.report, user_id, run_id, generate=generate)
