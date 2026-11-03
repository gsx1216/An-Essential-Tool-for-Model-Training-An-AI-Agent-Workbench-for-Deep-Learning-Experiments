"""复用固定 SSH bootstrap，源码只通过只读辅助程序读取；参数走 stdin。"""

import asyncio
import json
from pathlib import Path
import shlex

from app.models.source_code import SourceDirectory, SourceFile, SourceProtocolError
from app.runner.source_reader import SourceReadError, MAX_LINES_BYTES, relative_path, SENSITIVE_CONTENT
from app.services.runner_transport import BOOTSTRAP

ERROR_CODES = {'PATH_NOT_ALLOWED', 'INVALID_ARGUMENTS', 'UNSUPPORTED_PLATFORM', 'DIRECTORY_TOO_LARGE',
               'UNSUPPORTED_TEXT', 'SENSITIVE_CONTENT', 'LINE_TOO_LONG', 'FILE_TOO_LARGE', 'SOURCE_CHANGED',
               'NOT_FOUND', 'READ_UNAVAILABLE'}


def validate_response(response, payload):
    try:
        if type(response) is not dict or type(response.get('ok')) is not bool:
            raise ValueError('invalid envelope')
        if response['ok'] is False:
            if set(response) == {'ok', 'code'} and type(response['code']) is str and response['code'] in ERROR_CODES:
                raise SourceReadError(response['code'])
            raise ValueError('invalid error')
        if set(response) != {'ok', 'data'}:
            raise ValueError('invalid fields')
        if payload['action']=='environments':
            from app.models.training_project import PythonEnvironments
            data=PythonEnvironments.model_validate(response['data']).model_dump()
            if len({row['python'] for row in data['items']})!=len(data['items']):raise ValueError('duplicate environments')
            return data
        if payload['action']=='resources':
            from app.models.training_project import GpuResources
            return GpuResources.model_validate(response['data']).model_dump()
        model = SourceDirectory if payload['action'] == 'list' else SourceFile
        data = model.model_validate(response['data']).model_dump()
        if data['path'] != payload['path']:
            raise ValueError('path binding mismatch')
        if payload['action'] == 'list':
            if data['offset'] != payload['offset'] or len(data['items']) > payload['limit']:
                raise ValueError('invalid directory window')
            paths = set()
            for row in data['items']:
                try:
                    relative_path(row['path'], directory=row['kind'] == 'directory')
                except SourceReadError:
                    raise ValueError('disallowed directory child') from None
                parent = row['path'].rpartition('/')[0] or '.'
                if parent != payload['path'] or row['path'] in paths:
                    raise ValueError('invalid directory child')
                paths.add(row['path'])
            if data['next_offset'] is not None and (not data['items'] or data['next_offset'] != data['offset'] + len(data['items'])):
                raise ValueError('invalid directory cursor')
        else:
            lines = data['lines']
            if SENSITIVE_CONTENT.search('\n'.join(row['text'] for row in lines)):
                raise SourceReadError('SENSITIVE_CONTENT')
            if (data['start_line'] != payload['start_line'] or len(lines) > payload['line_count']
                or [row['number'] for row in lines] != list(range(data['start_line'], data['start_line'] + len(lines)))
                or data['end_line'] != (lines[-1]['number'] if lines else None)
                or (lines and lines[-1]['number'] > data['total_lines'])
                or (not lines and data['start_line'] <= data['total_lines'])
                or data['next_line'] != (data['end_line'] + 1 if lines and data['end_line'] < data['total_lines'] else None)
                or payload.get('expected_sha256') not in (None, data['file_sha256'])
                or len(json.dumps(lines, ensure_ascii=False, separators=(',', ':')).encode()) > MAX_LINES_BYTES):
                raise ValueError('invalid source window')
        return data
    except SourceReadError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise SourceProtocolError('源码读取响应无法校验。') from error


class SourceTransport:
    def __init__(self, connector, python_command):
        self.connector = connector
        self.command = shlex.join([python_command, '-c', BOOTSTRAP])
        self.source = (Path(__file__).resolve().parents[1] / 'runner/source_reader.py').read_text(encoding='utf-8')

    async def request(self, profile, credentials, payload):
        async with self.connector.connection(profile, credentials) as connection:
            process = await connection.create_process(self.command, encoding=None)
            try:
                process.stdin.write(json.dumps({**payload, '_source': self.source}, ensure_ascii=True).encode())
                await process.stdin.drain()
                process.stdin.write_eof()
                output, error = await asyncio.gather(process.stdout.read(65537), process.stderr.read(4097))
                if len(output) > 65536 or error:
                    raise SourceProtocolError('源码通道返回内容超限或异常。')
                await process.wait_closed()
                if process.exit_status != 0:
                    raise SourceProtocolError('源码通道未正常结束。')
                try:
                    response = json.loads(output)
                except (ValueError, UnicodeError) as error:
                    raise SourceProtocolError('源码响应不是有效 JSON。') from error
                return validate_response(response, payload)
            finally:
                # 包含超时/取消路径；不能留下等待输出的远端会话。
                process.close()
