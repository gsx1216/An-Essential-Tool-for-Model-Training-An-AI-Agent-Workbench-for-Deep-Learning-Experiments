"""复用本人已存服务器连接，在 Linux /tmp 运行固定 CPU 样例；不训练真实项目。"""
import argparse
import asyncio
import json
from pathlib import Path

from app.api.dependencies import get_source_code_service
from app.models.experiment_run import normalize_run_uuid
from app.validators import validate_positive_integer

ROOT=Path(__file__).resolve().parents[1]


async def verify(user_id,run_id):
    service=get_source_code_service()
    execution=await asyncio.to_thread(service.executions.get,validate_positive_integer(user_id,'用户 ID'),normalize_run_uuid(run_id))
    profile=await service.servers.get(user_id,execution['server_id'])
    if profile.binding()!=execution['launch']['server_binding']:raise ValueError('服务器绑定发生变化。')
    credentials=service.servers.cipher.decrypt(user_id,profile)
    driver='''
checks={'__name__':'revision_cpu_tests'}
exec(request['checks_source'],checks)
rows=checks['verify'](request['helper_source'],request['runner_source'])
print(json.dumps({'python':sys.version.split()[0],'checks':rows}))
'''
    payload={'_source':driver,'helper_source':(ROOT/'app/runner/revision_source.py').read_text(encoding='utf-8'),
        'runner_source':(ROOT/'app/runner/remote_agent.py').read_text(encoding='utf-8'),
        'checks_source':(ROOT/'tests/revision_linux_cases.py').read_text(encoding='utf-8')}
    try:
        async with asyncio.timeout(60):
            async with service.servers.connector.connection(profile,credentials) as connection:
                process=await connection.create_process(service.transport.command,encoding=None)
                try:
                    process.stdin.write(json.dumps(payload).encode());await process.stdin.drain();process.stdin.write_eof()
                    output,error=await asyncio.gather(process.stdout.read(32769),process.stderr.read(8193))
                    await process.wait_closed()
                    if error or len(output)>32768 or process.exit_status!=0:
                        # 固定 CPU 样例只有临时路径；输出测试 traceback，不接触原项目内容。
                        raise RuntimeError('Linux CPU sample failed: '+error.decode('utf-8',errors='replace'))
                    result=json.loads(output)
                finally:process.close()
    finally:credentials.clear()
    print('Linux Python:',result['python'])
    for row in result['checks']:print('PASS:',row)
    print('ALL',len(result['checks']),'Linux CPU groups passed; temporary directories cleaned; no real model or GPU training.')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id',type=int,required=True)
    parser.add_argument('--run-id',required=True)
    args=parser.parse_args()
    asyncio.run(verify(args.user_id,args.run_id))


if __name__=='__main__':main()
