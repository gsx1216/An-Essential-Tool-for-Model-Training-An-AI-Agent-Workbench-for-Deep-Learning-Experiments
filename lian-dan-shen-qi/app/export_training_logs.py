"""维护者本机导出已有运行日志；只使用 Runner 的 logs 操作，不启动训练。"""
import argparse
import asyncio
import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from app.api.dependencies import get_runner_service
from app.logging_config import log_failure
import logging


async def export_logs(user_id,run_id):
    service=get_runner_service()
    directory=Path(__file__).resolve().parents[1]/'logs'/'training'
    directory.mkdir(parents=True,exist_ok=True)
    destination=directory/f'{run_id}-output.log'
    offset=0;target=None
    with TemporaryDirectory(dir=directory) as temporary:
        path=Path(temporary)/'output.log'
        with path.open('wb') as output:
            while True:
                result=await service.operate(user_id,run_id,'logs',offset=offset,limit=16384)
                observation=result['observation']
                if observation['source_changed']:
                    raise RuntimeError('远端日志已截断，请重新导出。')
                if target is None:target=observation['log_size']
                raw=base64.b64decode(observation['raw_base64'],validate=True)
                output.write(raw[:max(0,target-offset)])
                following=observation['next_offset']
                if following>=target:break
                if following<=offset:raise RuntimeError('远端日志读取中断，请重试。')
                offset=following
        path.replace(destination)
    return destination,target


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id',type=int,required=True,help='该运行所属用户 ID')
    parser.add_argument('--run-id',type=UUID,required=True,help='已有运行的 UUID')
    args=parser.parse_args()
    try:
        destination,size=asyncio.run(export_logs(args.user_id,args.run_id.hex))
    except Exception as error:
        log_failure(logging.getLogger('app.export_training_logs'),error,operation='export_training_logs')
        parser.exit(1,'导出失败；请查看后端 logs 中本进程的错误记录。\n')
    print(f'已保存训练日志：{destination}（{size} 字节；导出开始时的日志快照）')


if __name__=='__main__':main()
