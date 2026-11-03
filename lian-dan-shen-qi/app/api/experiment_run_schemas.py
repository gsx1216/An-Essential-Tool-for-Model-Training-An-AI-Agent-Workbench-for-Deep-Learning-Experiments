"""运行记录 HTTP 数据结构；状态和结果只能由未来的后端执行器写入。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.experiment import ExperimentKind
from app.models.evaluation import EvaluationSpec
from app.models.experiment_run import RunStatus


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    parameters: dict[str, Any] = Field(default_factory=dict)


class RunListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    run_id: str
    experiment_id: str
    experiment_title: str
    experiment_kind: ExperimentKind
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    error_code: str | None
    has_logs: bool
    has_result: bool
    result_version: int


class RunResponse(RunListItem):
    request_id: str
    experiment_description: str
    parameters: dict[str, Any]
    evaluation: EvaluationSpec | None = None


class RunCreationResponse(BaseModel):
    run: RunResponse
    replayed: bool


class RunListResponse(BaseModel):
    items: list[RunListItem]
    next_cursor: str | None


class LatestTrainingRunResponse(BaseModel):
    run: RunResponse | None
