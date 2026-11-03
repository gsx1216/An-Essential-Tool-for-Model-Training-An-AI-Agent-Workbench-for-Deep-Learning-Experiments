"""实验 HTTP 输入输出；归属由 Token 决定，禁止额外输入用户或运行命令。"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models.experiment import ExperimentKind
from app.models.evaluation import EvaluationSpec


class CreateExperimentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    kind: ExperimentKind = 'script'
    description: Annotated[str, Field(strict=True, max_length=10000)] = ''
    evaluation: EvaluationSpec | None = None


class ExperimentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    experiment_id: str
    title: str
    kind: ExperimentKind
    description: str
    evaluation: EvaluationSpec | None = None
    created_at: datetime
    updated_at: datetime


class ExperimentListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    experiment_id: str
    title: str
    kind: ExperimentKind
    created_at: datetime
    updated_at: datetime


class ExperimentListResponse(BaseModel):
    items: list[ExperimentListItem]
    next_cursor: str | None
