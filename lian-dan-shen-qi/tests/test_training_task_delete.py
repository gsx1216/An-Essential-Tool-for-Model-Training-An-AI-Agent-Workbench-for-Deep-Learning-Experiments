"""删除接口的身份、参数和返回契约。实际外键保留由隔离 MySQL 脚本验证。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_training_task_service
from app.api.schemas import UserResponse
from app.api.training_task_routes import router
from app.models.training_task import TrainingNotFoundError


class DeleteTaskTests(unittest.TestCase):
    def setUp(self):
        self.service=SimpleNamespace(delete=AsyncMock())
        self.app=FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[get_training_task_service]=lambda:self.service
        self.app.dependency_overrides[require_current_user]=lambda:UserResponse(user_id=7,username='test')

    def test_delete_returns_empty_204_and_uses_authenticated_owner(self):
        with TestClient(self.app) as client:
            response=client.delete('/training-tasks/12345678-1234-4234-8234-123456789abc')
        self.assertEqual(response.status_code,204)
        self.assertEqual(response.content,b'')
        self.service.delete.assert_awaited_once_with(7,'12345678123442348234123456789abc')

    def test_inaccessible_task_is_404(self):
        self.service.delete.side_effect=TrainingNotFoundError()
        with TestClient(self.app) as client:
            response=client.delete('/training-tasks/'+'a'*32)
        self.assertEqual(response.status_code,404)

    def test_invalid_id_never_calls_service(self):
        with TestClient(self.app) as client:
            response=client.delete('/training-tasks/not-a-uuid')
        self.assertEqual(response.status_code,422)
        self.service.delete.assert_not_awaited()

    def test_no_login_is_rejected(self):
        self.app.dependency_overrides.pop(require_current_user)
        with TestClient(self.app) as client:
            response=client.delete('/training-tasks/'+'a'*32)
        self.assertEqual(response.status_code,401)
        self.service.delete.assert_not_awaited()
