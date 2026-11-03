"""SSE 传输：有界队列、心跳、断线取消，完成事件只在业务提交后发出。"""
import asyncio
import json
from contextlib import suppress
from starlette.responses import StreamingResponse


def stream_reply(operation,map_error):
    async def events():
        queue=asyncio.Queue(maxsize=32)
        async def delta(text):
            await queue.put(('reset',{}) if text is None else ('delta',{'content':text}))
        async def produce():
            try:content=await operation(delta)
            except Exception as error:
                status,detail=map_error(error)
                await queue.put(('error',{'status':status,'detail':detail}))
            else:await queue.put(('done',{'content':content}))
        task=asyncio.create_task(produce())
        try:
            yield 'event: status\ndata: {"phase":"preparing"}\n\n'
            while True:
                try:kind,data=await asyncio.wait_for(queue.get(),timeout=15)
                except TimeoutError:
                    yield ': keep-alive\n\n';continue
                yield f'event: {kind}\ndata: {json.dumps(data,ensure_ascii=False)}\n\n'
                if kind in {'done','error'}:break
        finally:
            if not task.done():task.cancel()
            with suppress(asyncio.CancelledError):await task
    return StreamingResponse(events(),media_type='text/event-stream',headers={
        'Cache-Control':'no-store','Pragma':'no-cache','X-Accel-Buffering':'no'})
