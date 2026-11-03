"""环境发现只读、协议和当前用户隔离，不启动训练或安装依赖。"""
import unittest
import posixpath
from unittest.mock import patch, mock_open, MagicMock, AsyncMock
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.runner import source_reader
from app.services.source_transport import validate_response
from app.models.source_code import SourceProtocolError
from app.api.training_task_routes import router
from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_training_task_service
from app.api.schemas import UserResponse
from app.services.training_task_service import TrainingTaskService
from app.errors import ServerNotFoundError
from tests.test_server_connection_core import profile


class DiscoveryTests(unittest.TestCase):
    def test_gpu_query_is_fixed_readonly_and_parses_utilization(self):
        with patch.object(source_reader.shutil,'which',return_value='/usr/bin/nvidia-smi'), \
             patch.object(source_reader.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'0, RTX 4090, 24564, 1200, 5\n')) as run:
            result=source_reader.gpu_resources()
        self.assertEqual(result['gpus'][0]['memory_used_mb'],1200)
        self.assertIn('--query-gpu=index,name,memory.total,memory.used,utilization.gpu',run.call_args.args[0])
        self.assertNotIn('shell',run.call_args.kwargs)
        with patch.object(source_reader.shutil,'which',return_value=None):
            self.assertEqual(source_reader.gpu_resources()['gpu_status'],'not_installed')
    def test_conda_path_and_project_venv_discovered_without_running_python(self):
        available={'/usr/bin/python3','/home/test/miniconda3/bin/python','/home/test/miniconda3/envs/test/bin/python','/project/.venv/bin/python'}
        with patch.object(source_reader.os.path,'expanduser',return_value='/home/test'), \
             patch.object(source_reader.os.path,'join',side_effect=posixpath.join), \
             patch.object(source_reader.os.path,'normpath',side_effect=posixpath.normpath), \
             patch.object(source_reader.sys,'executable','/usr/bin/python3'), \
             patch.object(source_reader.shutil,'which',return_value='/usr/bin/python3'), \
             patch('builtins.open',mock_open(read_data='/home/test/miniconda3/envs/test\n/tmp/not-executable\n')), \
             patch.object(source_reader.os,'scandir',side_effect=FileNotFoundError()), \
             patch.object(source_reader.os.path,'isfile',side_effect=lambda path:path in available), \
             patch.object(source_reader.os,'access',return_value=True):
            result=source_reader.discover_environments('/project')
        self.assertEqual({row['python'] for row in result['items']},available)
        self.assertEqual(len(result['items']),4)
        self.assertFalse(result['truncated'])

    def test_transport_rejects_untrusted_environment_paths_and_duplicates(self):
        row={'name':'Python','python':'/usr/bin/python3','kind':'system'}
        payload={'action':'environments','work_dir':'/project'}
        self.assertEqual(validate_response({'ok':True,'data':{'items':[row],'truncated':False}},payload)['items'],[row])
        for items in ([row,row],[row|{'python':'python;run'}],[row|{'kind':'command'}]):
            with self.assertRaises(SourceProtocolError):
                validate_response({'ok':True,'data':{'items':items,'truncated':False}},payload)

    def test_http_identity_and_no_store(self):
        service=SimpleNamespace(project_source=AsyncMock(return_value={'server_id':profile().server_id,'work_dir':'/project','items':[],'truncated':False}))
        app=FastAPI();app.include_router(router)
        app.dependency_overrides[get_training_task_service]=lambda:service
        app.dependency_overrides[require_current_user]=lambda:UserResponse(user_id=7,username='test')
        with TestClient(app) as client:
            response=client.post('/training-projects/environments',json={'server_id':profile().server_id})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertEqual(service.project_source.call_args.args[:2],(7,'environments'))
            self.assertEqual(client.post('/training-projects/environments',json={'server_id':profile().server_id,'user_id':8}).status_code,422)


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_users_server_never_opens_ssh(self):
        servers=SimpleNamespace(get=AsyncMock(side_effect=ServerNotFoundError()))
        transport=SimpleNamespace(request=AsyncMock())
        service=TrainingTaskService(MagicMock(),MagicMock(),MagicMock(),servers,MagicMock(),source_transport=transport)
        with self.assertRaises(ServerNotFoundError):
            await service.project_source(7,'environments',{'server_id':profile().server_id})
        transport.request.assert_not_awaited()
