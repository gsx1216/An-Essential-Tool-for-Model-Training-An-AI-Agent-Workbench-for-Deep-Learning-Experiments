"""独立 Outbox 投递进程；API 导入时不会自动启动。"""
import argparse
import asyncio
import json
import logging
from getpass import getpass

from app.config import get_event_bus_settings,get_mysql_password
from app.logging_config import configure_logging,log_failure,logging_context
from app.repositories.event_repository import EventRepository
from app.services.event_delivery_service import EventRelayService
from app.services.redis_event_bus import RedisEventBus
from app.models.event_bus import with_redis_password


async def run(once=False,redis_password=None):
    settings=get_event_bus_settings()
    if redis_password is not None:settings=with_redis_password(settings,redis_password)
    bus=RedisEventBus(settings)
    service=EventRelayService(EventRepository(get_mysql_password()),bus,settings)
    failures=0
    try:
        while True:
            with logging_context(new=True):
                try:
                    processed=await service.tick();failures=0
                    if once:return processed
                    if not processed:await asyncio.sleep(1)
                except asyncio.CancelledError:raise
                except Exception as error:
                    log_failure(logging.getLogger(__name__),error,operation='event_relay_worker.loop')
                    if once:raise
                    failures=min(failures+1,6);await asyncio.sleep(min(30,2**failures))
    finally:await bus.aclose()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--once',action='store_true')
    parser.add_argument('--prompt-redis-password',action='store_true',help='交互输入密码，仅本次进程使用，不写入文件')
    args=parser.parse_args()
    configure_logging()
    try:
        password=getpass('Redis 密码（仅本次进程使用）：') if args.prompt_redis_password else None
        print(json.dumps({'processed':asyncio.run(run(args.once,password)),'model_called':False}))
    except KeyboardInterrupt:pass
    except Exception as error:
        log_failure(logging.getLogger(__name__),error,operation='event_relay_worker.start')
        raise SystemExit(1)


if __name__=='__main__':main()
