"""实验定义业务：异步接口调用同步仓储，实验登记不启动进程或模型。"""

import asyncio

from app.logging_config import logged_operation
from app.models.experiment import (ExperimentPage, ExperimentRecord, normalize_experiment_id,
                                   validate_experiment_fields, validate_experiment_limit)
from app.repositories.experiment_repository import ExperimentRepository
from app.validators import validate_positive_integer


class ExperimentService:
    def __init__(self, repository: ExperimentRepository):
        self.repository = repository

    @logged_operation('experiment_service.create_experiment')
    async def create_experiment(self, user_id: int, title: str, kind: str = 'script', description: str = '', *, evaluation=None) -> ExperimentRecord:
        user_id = validate_positive_integer(user_id, '用户 ID')
        title, kind, description = validate_experiment_fields(title, kind, description)
        options = {} if evaluation is None else {'evaluation': evaluation}
        return await asyncio.to_thread(self.repository.create_experiment, user_id, title, kind, description, **options)

    @logged_operation('experiment_service.get_experiment')
    async def get_experiment(self, user_id: int, experiment_id: str) -> ExperimentRecord:
        user_id = validate_positive_integer(user_id, '用户 ID')
        experiment_id = normalize_experiment_id(experiment_id)
        return await asyncio.to_thread(self.repository.get_experiment, user_id, experiment_id)

    @logged_operation('experiment_service.list_experiments')
    async def list_experiments(self, user_id: int, *, limit: int = 20, before_id: str | None = None) -> ExperimentPage:
        user_id = validate_positive_integer(user_id, '用户 ID')
        limit = validate_experiment_limit(limit)
        if before_id is not None:
            before_id = normalize_experiment_id(before_id)
        return await asyncio.to_thread(self.repository.list_experiments, user_id, limit=limit, before_id=before_id)
