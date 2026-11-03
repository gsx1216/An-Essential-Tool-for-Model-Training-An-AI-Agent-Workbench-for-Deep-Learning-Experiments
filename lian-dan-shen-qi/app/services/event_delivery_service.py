"""报告事件可靠投递和 Inbox 入队，不调用模型、SSH 或训练工具。"""
import asyncio
import logging

from redis.exceptions import RedisError

from app.logging_config import log_failure,logged_operation
from app.models.event_bus import EventNotFoundError,EventPayloadError
from app.models.experiment_run import normalize_run_uuid
from app.repositories.event_repository import decode_event

logger=logging.getLogger(__name__)


class EventRelayService:
    def __init__(self,repository,bus,settings):self.repository,self.bus,self.settings=repository,bus,settings

    @logged_operation('event_relay.tick',log_success=False)
    async def tick(self):
        lease=await asyncio.to_thread(self.repository.claim,self.settings.lease_seconds)
        if lease is None:return False
        status,count,delay,code,identifier='sent',0,self.settings.reconcile_seconds,None,None
        try:
            decode_event(lease)
            async with asyncio.timeout(8):identifier=await self.bus.publish(lease['id'])
        except asyncio.CancelledError:raise  # 租约到期后恢复，不误确认投递。
        except (EventPayloadError,EventNotFoundError) as error:
            status,count,delay,code='attention',min(50,lease['failure_count']+1),0,'PAYLOAD_INVALID'
            log_failure(logger,error,operation='event_relay.invalid_snapshot')
        except Exception as error:
            count=min(50,lease['failure_count']+1)
            status='attention' if count>=self.settings.max_failures else 'pending'
            delay=min(300,5*2**min(count-1,6))
            code='REDIS_UNAVAILABLE' if isinstance(error,(RedisError,TimeoutError,OSError)) else 'INTERNAL_ERROR'
            log_failure(logger,error,operation='event_relay.publish')
        saved=await asyncio.to_thread(self.repository.settle,lease['id'],lease['lease_token'],status=status,
            failure_count=count,delay=delay,error_code=code,redis_id=identifier)
        logger.info('event_delivery_completed', extra={'event_id':lease['id'],'run_id':lease['run_id'],
                    'delivery_status':status,'state_saved':bool(saved)})
        return True


class AnalysisEventReceiver:
    """这里只创建 queued 分析任务；独立 analysis_worker 执行模型分析。"""
    def __init__(self,repository,bus,consumer):self.repository,self.bus,self.consumer=repository,bus,consumer

    @logged_operation('analysis_event_receiver.tick',log_success=False)
    async def tick(self,*,block=True):
        message=await self.bus.read(self.consumer,block=block)
        if message is None:return False
        identifier,fields=message
        try:
            if type(fields) is not dict or set(fields)!={'schema_version','event_id'} or fields['schema_version']!='1':
                raise EventPayloadError('消息信封不符合要求。')
            event_id=normalize_run_uuid(fields['event_id'])
            await asyncio.to_thread(self.repository.receive,event_id)
        except (ValueError,EventPayloadError) as error:
            log_failure(logger,error,operation='analysis_event_receiver.reject',level=logging.WARNING)
            await self.bus.dead_letter(identifier,fields,'INVALID_EVENT')
            logger.warning('event_message_rejected', extra={'message_id':identifier})
            return True
        # 数据库故障和进程取消不会走到 ACK；重建消费者后会重新领取。
        await self.bus.ack(identifier)
        logger.info('event_task_enqueued', extra={'event_id':event_id,'message_id':identifier})
        return True
