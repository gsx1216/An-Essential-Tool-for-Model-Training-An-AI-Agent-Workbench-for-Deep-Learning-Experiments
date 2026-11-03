"""独立监控进程；读取 MySQL 持久任务，不启动训练或调用模型。"""
import argparse
import asyncio
import json
import logging
from time import monotonic

from app.logging_config import configure_logging,log_failure,logging_context

logger=logging.getLogger(__name__)


async def run_worker(service,*,once=False):
    failures=0
    last_reconcile=None
    while True:
        with logging_context(new=True):
            try:
                if last_reconcile is None or monotonic()-last_reconcile>=60:
                    await asyncio.to_thread(service.repository.reconcile)
                    last_reconcile=monotonic()
                processed=await service.tick()
                failures=0
            except asyncio.CancelledError:raise
            except Exception as error:
                log_failure(logger,error,operation='monitor_worker.loop')
                if once:raise
                failures=min(50,failures+1)
                await asyncio.sleep(service.settings.backoff(failures))
                continue
        if once:return processed
        if not processed:await asyncio.sleep(service.settings.idle_seconds)
        else:await asyncio.sleep(0)  # 单步后允许取消；非到期任务不会被立即再次领取。


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true',help='最多处理一个已到期任务后退出；没有任务也成功退出')
    args=parser.parse_args()
    configure_logging()
    from app.api.dependencies import get_monitor_service
    try:
        result=asyncio.run(run_worker(get_monitor_service(),once=args.once))
        if args.once:print(json.dumps({'processed':bool(result),'training_started':False}))
    except KeyboardInterrupt:pass


if __name__=='__main__':main()
