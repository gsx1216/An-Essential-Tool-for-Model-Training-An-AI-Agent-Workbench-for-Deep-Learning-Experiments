"""统一记录 HTTP 结果，为客户端提供可关联日志的请求编号。"""

import asyncio
import logging
import time

from fastapi.responses import JSONResponse

from app.logging_config import logging_context, log_failure


logger = logging.getLogger(__name__)


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        with logging_context(new=True) as context:
            started = time.perf_counter()
            response_started = False
            status = 500
            failed = False

            async def send_with_request_id(message):
                nonlocal response_started, status
                if message["type"] == "http.response.start":
                    response_started = True
                    status = message["status"]
                    headers = [(key, value) for key, value in message.get("headers", [])
                               if key.lower() != b"x-request-id"]
                    headers.append((b"x-request-id", context.request_id.encode("ascii")))
                    message = {**message, "headers": headers}
                await send(message)

            try:
                await self.app(scope, receive, send_with_request_id)
            except asyncio.CancelledError:
                failed = True
                status = 499
                raise
            except Exception as error:
                failed = True
                log_failure(logger, error, event="unhandled_exception")
                if response_started:
                    # 已发送响应头时不能再发送第二份响应，也不向服务器日志传播原始异常文本。
                    raise RuntimeError("Response failed after headers were sent.") from None
                try:
                    await JSONResponse(status_code=500, content={"detail": "请稍后重试。"})(
                        scope, receive, send_with_request_id,
                    )
                except Exception:
                    raise RuntimeError("Error response delivery failed.") from None
            finally:
                # 使用路由模板，不记录可能夹带凭据的原始 URL、查询参数或请求体。
                route = getattr(scope.get("route"), "path", "<unmatched>")
                level = logging.ERROR if status >= 500 or (failed and status != 499) else (
                    logging.WARNING if status >= 400 else logging.INFO
                )
                logger.log(level,
                           "request_failed" if failed else "request_completed",
                           extra={"method": scope.get("method"), "route": route,
                                  "status_code": status,
                                  "duration_ms": (time.perf_counter() - started) * 1000})
