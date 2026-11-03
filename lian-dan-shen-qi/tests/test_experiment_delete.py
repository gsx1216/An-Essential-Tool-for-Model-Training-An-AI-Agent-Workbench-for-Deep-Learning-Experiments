"""永久删除的归属、事务失败、活动任务及 HTTP 契约，不访问真实模型。"""
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth_dependencies import require_current_user
from app.api.schemas import UserResponse
from app.api.experiment_delete_routes import router,get_purge_repository,clear_events
from app.errors import ExperimentNotFoundError,RunNotFoundError
from app.repositories.experiment_purge_repository import ExperimentPurgeRepository,PurgeConflictError

ID='a'*32


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repo=ExperimentPurgeRepository('TEST_ONLY')
        self.connection=MagicMock()
        self.connection.__enter__.return_value=self.connection
        self.cursor=self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.side_effect=[{'id':ID},None,None]
        self.cursor.fetchall.side_effect=[[{'id':'b'*32,'status':'succeeded'}],[]]
        self.connect=patch('app.repositories.experiment_purge_repository.get_connection',return_value=self.connection)
        self.connect.start()
        self.addCleanup(self.connect.stop)

    def test_delete_is_real_sql_scoped_to_owner_and_committed(self):
        self.assertEqual(self.repo.delete(7,ID,experiment=True),[])
        deletes=[call.args for call in self.cursor.execute.call_args_list if call.args[0].lstrip().startswith('DELETE')]
        self.assertGreater(len(deletes),20)
        self.assertTrue(all(args[1]==(7,ID) for args in deletes))
        self.assertIn('DELETE FROM experiment_assistant.experiments',deletes[-1][0])
        self.connection.commit.assert_called_once()

    def test_missing_owner_never_deletes(self):
        self.cursor.fetchone.side_effect=[None]
        with self.assertRaises(ExperimentNotFoundError):self.repo.delete(8,ID,experiment=True)
        self.assertFalse(any(c.args[0].lstrip().startswith('DELETE') for c in self.cursor.execute.call_args_list))
        self.connection.rollback.assert_called_once()

    def test_running_training_is_rejected_before_mutation(self):
        self.cursor.fetchall.side_effect=[[{'id':'b'*32,'status':'running'}]]
        with self.assertRaises(PurgeConflictError):self.repo.delete(7,ID,experiment=True)
        self.connection.commit.assert_not_called()

    def test_derived_run_outside_scope_is_not_silently_deleted(self):
        self.cursor.fetchone.side_effect=[{'id':ID},{'run_id':'c'*32}]
        with self.assertRaises(PurgeConflictError):self.repo.delete(7,ID,experiment=True)
        self.connection.rollback.assert_called_once()

    def test_midway_error_rolls_back_instead_of_partial_success(self):
        def execute(sql,*args):
            if 'DELETE c FROM experiment_assistant.run_reports' in sql:raise RuntimeError('TEST_ONLY')
        self.cursor.execute.side_effect=execute
        with self.assertRaises(RuntimeError):self.repo.delete(7,ID,experiment=True)
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()


class ApiTests(unittest.TestCase):
    def test_login_scope_no_body_and_conflicts(self):
        repo=MagicMock()
        repo.delete.return_value=[]
        app=FastAPI();app.include_router(router)
        app.dependency_overrides[get_purge_repository]=lambda:repo
        with TestClient(app) as client:
            self.assertEqual(client.delete('/runs/'+ID).status_code,401)
            repo.delete.assert_not_called()
            app.dependency_overrides[require_current_user]=lambda:UserResponse(user_id=7,username='owner')
            for path,experiment in (('/runs/'+ID,False),('/experiments/'+ID,True)):
                result=client.delete(path)
                self.assertEqual(result.status_code,204)
                self.assertEqual(result.content,b'')
                repo.delete.assert_called_with(7,ID,experiment=experiment)
            repo.delete.side_effect=PurgeConflictError('请先停止训练。')
            self.assertEqual(client.delete('/runs/'+ID).status_code,409)
            repo.delete.side_effect=RunNotFoundError('private')
            self.assertEqual(client.delete('/runs/'+ID).status_code,404)
            self.assertEqual(client.delete('/runs/not-a-uuid').status_code,422)


class RedisCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_selected_ids_are_removed_and_acknowledged(self):
        bus=MagicMock()
        bus.client.xdel=AsyncMock();bus.client.xack=AsyncMock();bus.aclose=AsyncMock()
        with patch('app.api.experiment_delete_routes.RedisEventBus',return_value=bus):
            await clear_events(['1-0','2-0'])
        self.assertEqual(bus.client.xdel.await_args.args[1:],('1-0','2-0'))
        self.assertEqual(bus.client.xack.await_args.args[2:],('1-0','2-0'))
        bus.aclose.assert_awaited_once()
