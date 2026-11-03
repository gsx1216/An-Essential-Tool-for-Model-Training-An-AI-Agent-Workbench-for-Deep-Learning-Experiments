"""固定远端 Python bootstrap；业务 JSON 走 stdin，不作为 shell 命令拼接。"""

import asyncio
import base64
import re
import json
from pathlib import Path, PurePosixPath
import shlex

from app.errors import RunnerProtocolError
from app.models.runner import validate_observation


BOOTSTRAP = 'import json,sys; request=json.load(sys.stdin); source=request.pop("_source"); exec(source)'


class RunnerTransport:
    def __init__(self, connector, python_command):
        self.connector = connector
        self.command = shlex.join([python_command, '-c', BOOTSTRAP])
        self.source = (Path(__file__).resolve().parents[1] / 'runner' / 'remote_agent.py').read_text(encoding='utf-8')

    async def request(self, profile, credentials, payload):
        async with self.connector.connection(profile, credentials) as connection:
            process = await connection.create_process(self.command, encoding=None)
            process.stdin.write(json.dumps({**payload, '_source': self.source}, ensure_ascii=True).encode())
            await process.stdin.drain()
            process.stdin.write_eof()
            output, error = await asyncio.gather(process.stdout.read(131073), process.stderr.read(4097))
            await process.wait_closed()
            if len(output) > 131072 or error or process.exit_status != 0:
                raise RunnerProtocolError('远端执行协议失败。')
            try:
                result = json.loads(output)
                if type(result) is not dict or result.get('ok') is not True:
                    raise ValueError('remote rejected request')
                observation = validate_observation(result['observation'])
                if 'outputs' in observation:
                    launch = payload['launch']
                    expected = str(PurePosixPath(launch['work_dir']) / launch.get('output_directory', 'training_outputs') / launch['run_id'])
                    if observation['outputs']['directory'] != expected:
                        raise ValueError('output directory binding mismatch')
                if payload['action'] == 'logs':
                    if (not isinstance(observation.get('content'), str) or len(observation['content']) > 16384
                            or type(observation.get('next_offset')) is not int
                            or not payload['offset'] <= observation['next_offset'] <= payload['offset'] + payload['limit']
                            or type(observation.get('has_more')) is not bool or type(observation.get('truncated')) is not bool):
                        raise ValueError('invalid log response')
                    raw = base64.b64decode(observation['raw_base64'], validate=True)
                    if (len(raw) != observation['next_offset'] - payload['offset']
                            or raw.decode('utf-8', errors='replace') != observation['content']
                            or type(observation.get('log_size')) is not int or not 0 <= observation['log_size'] <= 10485760
                            or type(observation.get('source_changed')) is not bool
                            or not isinstance(observation.get('anchor_sha256'), str)
                            or not re.fullmatch('[0-9a-f]{64}', observation['anchor_sha256'])):
                        raise ValueError('invalid raw log response')
                return observation
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                raise RunnerProtocolError('远端响应无法校验。') from exc
