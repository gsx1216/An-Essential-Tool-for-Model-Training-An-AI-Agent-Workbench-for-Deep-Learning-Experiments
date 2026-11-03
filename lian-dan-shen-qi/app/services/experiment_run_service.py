"""运行记录异步入口；创建只持久化 created 状态，不启动执行器。"""

import asyncio
import json

from app.logging_config import logged_operation
from app.models.experiment import normalize_experiment_id, validate_experiment_limit
from app.models.experiment_run import canonical_parameters, normalize_run_uuid, validate_run_status
from app.repositories.experiment_run_repository import ExperimentRunRepository
from app.validators import validate_positive_integer


class ExperimentRunService:
    def __init__(self, repository: ExperimentRunRepository):
        self.repository = repository

    @logged_operation('experiment_run_service.create_run')
    async def create_run(self, user_id, experiment_id, request_id, parameters):
        user_id = validate_positive_integer(user_id, '用户 ID')
        experiment_id = normalize_experiment_id(experiment_id)
        request_id = normalize_run_uuid(request_id, '运行请求编号')
        # 先校验并复制，避免异步等待期间调用方改动原始参数对象。
        snapshot = json.loads(canonical_parameters(parameters))
        return await asyncio.to_thread(self.repository.create_run, user_id, experiment_id, request_id, snapshot)

    @logged_operation('experiment_run_service.get_run')
    async def get_run(self, user_id, run_id):
        user_id = validate_positive_integer(user_id, '用户 ID')
        run_id = normalize_run_uuid(run_id)
        return await asyncio.to_thread(self.repository.get_run, user_id, run_id)

    @logged_operation('experiment_run_service.list_runs')
    async def list_runs(self, user_id, experiment_id, *, limit=20, before_id=None):
        user_id = validate_positive_integer(user_id, '用户 ID')
        experiment_id = normalize_experiment_id(experiment_id)
        limit = validate_experiment_limit(limit)
        before_id = normalize_run_uuid(before_id) if before_id is not None else None
        return await asyncio.to_thread(self.repository.list_runs, user_id, experiment_id, limit=limit, before_id=before_id)

    @logged_operation('experiment_run_service.latest_training_run')
    async def latest_training_run(self, user_id, *, experiment_id=None, status=None):
        user_id = validate_positive_integer(user_id, '用户 ID')
        experiment_id = normalize_experiment_id(experiment_id) if experiment_id is not None else None
        status = validate_run_status(status)
        return await asyncio.to_thread(self.repository.latest_training_run, user_id, experiment_id=experiment_id, status=status)
