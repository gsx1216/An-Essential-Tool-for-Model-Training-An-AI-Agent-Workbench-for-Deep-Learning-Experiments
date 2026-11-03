"""固定 SSH 编辑协议；计划和部署者检查配置通过 stdin 传入，不拼接命令。"""
import asyncio
import json
from pathlib import Path
import shlex

from app.models.code_change import ChangeOutcome, ChangeProtocolError, ChangeConflict
from app.services.runner_transport import BOOTSTRAP

ERRORS = {'INVALID_PLAN', 'WORKSPACE_CONFLICT', 'WORKSPACE_INCOMPLETE', 'WORKSPACE_BUSY', 'SOURCE_CHANGED',
    'REPLACEMENT_CONFLICT', 'PATH_NOT_ALLOWED', 'UNSUPPORTED_PLATFORM', 'UNSUPPORTED_TEXT', 'FILE_TOO_LARGE',
    'SENSITIVE_CONTENT', 'LINE_TOO_LONG', 'SNAPSHOT_TOO_LARGE', 'DIFF_TOO_LARGE', 'RESULT_TOO_LARGE',
    'NOT_FOUND', 'READ_UNAVAILABLE', 'EDIT_UNAVAILABLE'}


def validate_result(response, payload):
    try:
        if type(response) is not dict or type(response.get('ok')) is not bool:
            raise ValueError('invalid response')
        if not response['ok']:
            if set(response) != {'ok', 'code'} or response['code'] not in ERRORS:
                raise ValueError('invalid error')
            error = ChangeConflict('修改副本未完成，请检查文件版本或工作目录。')
            error.code = response['code']
            raise error
        if set(response) != {'ok', 'data'}:
            raise ValueError('invalid envelope')
        result = ChangeOutcome.model_validate(response['data']).model_dump()
        expected = {row['path']: row['before_sha256'] for row in payload['plan']['files']}
        if (result['change_id'] != payload['change_id'] or result['plan_digest'] != payload['plan_digest']
            or result['workspace_directory'] != payload['work_dir'].rstrip('/') + '/code_changes/' + payload['change_id'] + '/project'
            or len(result['files']) != len(expected) or {row['path']: row['before_sha256'] for row in result['files']} != expected
            or any(not row['diff'] or row['before_sha256'] == row['after_sha256'] for row in result['files'])
            or result['check_status'] != ('failed' if any(row['status'] == 'failed' for row in result['checks']) else
                'incomplete' if any(row['status'] in {'incomplete', 'not_configured'} for row in result['checks']) else 'passed')):
            raise ValueError('result binding mismatch')
        return result
    except ChangeConflict:
        raise
    except (ValueError, TypeError, KeyError) as error:
        raise ChangeProtocolError('远端修改结果无法校验。') from error


class CodeChangeTransport:
    def __init__(self, connector, python_command):
        self.connector = connector
        self.command = shlex.join([python_command, '-c', BOOTSTRAP])
        root = Path(__file__).resolve().parents[1] / 'runner'
        self.source = (root/'code_editor.py').read_text(encoding='utf-8')
        self.reader_source = (root/'source_reader.py').read_text(encoding='utf-8')

    async def request(self, profile, credentials, payload):
        async with self.connector.connection(profile, credentials) as connection:
            process = await connection.create_process(self.command, encoding=None)
            try:
                process.stdin.write(json.dumps({**payload, '_source': self.source, '_reader_source': self.reader_source}, ensure_ascii=True).encode())
                await process.stdin.drain(); process.stdin.write_eof()
                output, error = await asyncio.gather(process.stdout.read(131073), process.stderr.read(4097))
                if len(output) > 131072 or error:
                    raise ChangeProtocolError('代码修改通道返回异常或超限。')
                await process.wait_closed()
                if process.exit_status != 0:
                    raise ChangeProtocolError('代码修改通道未正常结束。')
                try: value = json.loads(output)
                except (ValueError, UnicodeError) as error:
                    raise ChangeProtocolError('代码修改响应不是 JSON。') from error
                return validate_result(value, payload)
            finally:
                process.close()
