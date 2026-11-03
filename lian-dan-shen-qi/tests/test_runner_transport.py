"""协议出站只使用固定命令；拒绝未校验的状态、退出码、日志范围及 stderr。"""

from contextlib import asynccontextmanager
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.errors import RunnerProtocolError
from app.services.runner_transport import RunnerTransport
from tests.test_runner_core import observation


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.process=MagicMock();self.process.stdin.drain=AsyncMock();self.process.wait_closed=AsyncMock()
        self.process.stdout.read=AsyncMock(return_value=json.dumps({'ok':True,'observation':observation()}).encode())
        self.process.stderr.read=AsyncMock(return_value=b'');self.process.exit_status=0
        self.connection=MagicMock();self.connection.create_process=AsyncMock(return_value=self.process)
        @asynccontextmanager
        async def connect(*args):
            yield self.connection
        connector=MagicMock();connector.connection=connect
        self.transport=RunnerTransport(connector,'python3')

    async def test_payload_is_stdin_data_not_a_shell_command(self):
        await self.transport.request(None,{}, {'action':'status','parameters':"'; echo PRIVATE"})
        command=self.connection.create_process.call_args.args[0]
        self.assertNotIn('PRIVATE',command)
        self.assertIn('_source',json.loads(self.process.stdin.write.call_args.args[0]))

    async def test_protocol_rejects_stderr_nonzero_invalid_json_and_false_success(self):
        for output,error,code in [(b'not json',b'',0),(b'{"ok":false}',b'',0),(b'{}',b'PRIVATE',0),(b'{}',b'',1),
                                  (json.dumps({'ok':True,'observation':observation('succeeded',exit_code=2)}).encode(),b'',0)]:
            self.process.stdout.read.return_value=output;self.process.stderr.read.return_value=error;self.process.exit_status=code
            with self.assertRaises(RunnerProtocolError):
                await self.transport.request(None,{}, {'action':'status'})

    async def test_unknown_observation_fields_are_not_reflected(self):
        self.process.stdout.read.return_value=json.dumps({'ok':True,'observation':observation(private_key='PRIVATE')}).encode()
        result=await self.transport.request(None,{}, {'action':'status'})
        self.assertNotIn('private_key',result)

    async def test_remote_log_cursor_cannot_exceed_requested_range(self):
        self.process.stdout.read.return_value=json.dumps({'ok':True,'observation':observation(content='hello',next_offset=100,has_more=False,truncated=False)}).encode()
        with self.assertRaises(RunnerProtocolError):
            await self.transport.request(None,{}, {'action':'logs','offset':0,'limit':8})
