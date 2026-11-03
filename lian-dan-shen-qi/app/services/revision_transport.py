"""只读核对通用项目副本；所有入口均使用相同的源码固定流程。"""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import shlex

from app.errors import RunnerProtocolError
from app.models.revision_run import RevisionConflict
from app.runner.revision_source import canonical
from app.services.runner_transport import BOOTSTRAP


def validate_inspection(value, payload):
    if type(value) is not dict or value.get('ok') is not True:
        raise RevisionConflict('修改副本、登记入口或输入资源无法核对，请检查配置和版本。')
    try:
        data = value['data']
        rows = data['files']
        if (set(data) != {'files','source_digest','mode'} or data['mode'] != 'cwd'
                or payload['mode'] != 'cwd' or type(rows) is not dict or not 1 <= len(rows) <= 512):
            raise ValueError('shape')
        for path, sha in rows.items():
            if (not isinstance(path,str) or len(path)>512 or path.startswith('/') or '\\' in path
                or any(part in {'','..','.'} or part.startswith('.') for part in path.split('/'))
                or not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha)):
                raise ValueError('inventory')
        if hashlib.sha256(canonical(rows).encode()).hexdigest() != data['source_digest']:
            raise ValueError('digest')
        if payload['spec']['path'] not in rows or any(rows.get(row['path']) != row['after_sha256'] for row in payload['changed_files']):
            raise ValueError('binding')
        return data
    except (ValueError, KeyError, TypeError, RuntimeError) as error:
        raise RunnerProtocolError('修改副本预检响应无法核对。') from error


class RevisionTransport:
    def __init__(self, connector, python_command):
        self.connector = connector
        self.command = shlex.join([python_command, '-c', BOOTSTRAP])
        self.source = (Path(__file__).resolve().parents[1]/'runner/revision_source.py').read_text(encoding='utf-8')

    async def request(self, profile, credentials, payload):
        async with self.connector.connection(profile, credentials) as connection:
            process = await connection.create_process(self.command, encoding=None)
            try:
                process.stdin.write(json.dumps({**payload, '_source':self.source}, ensure_ascii=True).encode())
                await process.stdin.drain(); process.stdin.write_eof()
                output, error = await asyncio.gather(process.stdout.read(131073),process.stderr.read(4097))
                await process.wait_closed()
                if len(output)>131072 or error or process.exit_status!=0:
                    raise RunnerProtocolError('修改副本预检通道失败。')
                try: value = json.loads(output)
                except (ValueError,UnicodeError) as error: raise RunnerProtocolError('预检不是 JSON。') from error
                return validate_inspection(value,payload)
            finally: process.close()

