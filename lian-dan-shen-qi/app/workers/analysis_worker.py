"""分析执行进程：管理员和本人双开关；默认不领取任务、不调用模型。"""

import argparse
import asyncio
from dataclasses import replace
from functools import partial
import json
import logging

from app.api.dependencies import get_model_credential_service
from app.config import get_analysis_settings, get_mysql_password
from app.logging_config import configure_logging, logging_context, log_failure
from app.repositories.analysis_repository import AnalysisRepository
from app.services.analysis_service import AnalysisService
from app.services.model_gateway import ModelGateway
from app.services.user_model_gateway_factory import UserModelGatewayFactory


def build(settings):
    factory = None
    if settings.enabled:
        factory = UserModelGatewayFactory(get_model_credential_service(),
            partial(ModelGateway, model_name=settings.model_name, max_retries=0,
                    timeout_seconds=settings.model_timeout_seconds))
    return AnalysisService(AnalysisRepository(get_mysql_password()), gateway_factory=factory, settings=settings)


async def run(*, once=False, enable_model_calls=False, service=None):
    settings = get_analysis_settings() if service is None else service.settings
    if enable_model_calls:
        settings = replace(settings, enabled=True)
    if not settings.enabled:
        return {"enabled": False, "processed": False, "reason": "ADMIN_DISABLED"}
    service = service or build(settings)
    failures = 0
    while True:
        with logging_context(new=True):
            try:
                processed = await service.tick()
                failures = 0
                if once:
                    return {"enabled": True, "processed": processed}
                if not processed:
                    await asyncio.sleep(settings.idle_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_failure(logging.getLogger(__name__), error, operation="analysis_worker.loop")
                if once:
                    raise
                failures = min(5, failures + 1)
                await asyncio.sleep(min(30, 2**failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--enable-model-calls", action="store_true", help="仅本次进程开启；仅处理本人明确启用分析的用户")
    args = parser.parse_args()
    configure_logging()
    try:
        print(json.dumps(asyncio.run(run(once=args.once, enable_model_calls=args.enable_model_calls))))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        log_failure(logging.getLogger(__name__), error, operation="analysis_worker.start")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
