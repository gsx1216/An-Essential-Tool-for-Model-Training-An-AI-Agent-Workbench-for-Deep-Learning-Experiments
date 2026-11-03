"""Redis 5+ Streams 适配器：消费组、显式 ACK、旧 pending 消息领取与隔离死信。"""
import hashlib
import re

from redis.asyncio import Redis
from redis.exceptions import ResponseError


class RedisEventBus:
    def __init__(self,settings,client=None):
        self.settings=settings
        self.client=client or Redis.from_url(settings.redis_url,decode_responses=True,
            socket_connect_timeout=3,socket_timeout=10,health_check_interval=30,max_connections=8)

    async def aclose(self):await self.client.aclose()

    async def ensure_group(self):
        try:await self.client.xgroup_create(self.settings.stream,self.settings.analysis_group,id='0',mkstream=True)
        except ResponseError as error:
            if not str(error).startswith('BUSYGROUP'):raise

    async def publish(self,event_id):
        return await self.client.xadd(self.settings.stream,{'schema_version':'1','event_id':event_id})

    async def read(self,consumer,*,block=True):
        if not re.fullmatch('[A-Za-z0-9_-]{1,96}',consumer):raise ValueError('消费者名称错误。')
        await self.ensure_group()
        # 兼容 Redis 5；不依赖 Redis 6.2 才加入的 IDLE 筛选或 XAUTOCLAIM。
        pending=await self.client.xpending_range(self.settings.stream,self.settings.analysis_group,'-','+',20)
        ids=[item['message_id'] for item in pending if item['time_since_delivered']>=self.settings.reclaim_idle_ms]
        if ids:
            rows=await self.client.xclaim(self.settings.stream,self.settings.analysis_group,consumer,
                min_idle_time=self.settings.reclaim_idle_ms,message_ids=ids[:1])
            if rows:return rows[0]
        options={'count':1}
        if block:options['block']=self.settings.block_ms
        rows=await self.client.xreadgroup(self.settings.analysis_group,consumer,{self.settings.stream:'>'},**options)
        if not rows:return None
        return rows[0][1][0]

    async def ack(self,message_id):
        return await self.client.xack(self.settings.stream,self.settings.analysis_group,message_id)

    async def dead_letter(self,message_id,fields,reason):
        # 不把原消息正文或任意 Redis 字段复制到死信和日志。
        digest=hashlib.sha256(repr(sorted(fields.items())).encode('utf-8',errors='replace')).hexdigest()
        await self.client.xadd(self.settings.dead_stream,{'source_message_id':message_id,'reason':reason,'payload_digest':digest})
        await self.ack(message_id)
