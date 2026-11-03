"""独立建议进程：复用分析 Worker 轮询与租约循环，默认关闭。"""
import argparse
import asyncio
from dataclasses import replace
from functools import partial
import json
import logging

from app import config
from app.api.dependencies import get_model_credential_service, get_source_code_service, get_paper_service
from app.logging_config import configure_logging, log_failure
from app.repositories.suggestion_repository import SuggestionRepository
from app.repositories.research_repository import ResearchRepository
from app.services.public_research_client import PublicResearchClient
from app.services.suggestion_service import SuggestionService
from app.services.model_gateway import ModelGateway
from app.services.user_model_gateway_factory import UserModelGatewayFactory
from app.workers.analysis_worker import run as run_loop


def build(settings):
    password = config.get_mysql_password()
    factory = UserModelGatewayFactory(get_model_credential_service(), partial(ModelGateway,
        model_name=settings.model_name, max_retries=0, timeout_seconds=settings.model_timeout_seconds))
    research = ResearchRepository(password)
    return SuggestionService(SuggestionRepository(password), settings=settings, gateway_factory=factory,
        source_factory=get_source_code_service, paper_factory=get_paper_service,
        research_factory=(lambda: PublicResearchClient(research.permit)) if config.get_research_enabled() else None)


async def run(*, once=False, enable_model_calls=False, service=None):
    settings = service.settings if service is not None else config.get_suggestion_settings()
    if enable_model_calls:
        settings = replace(settings, enabled=True)
    if not settings.enabled:
        return {'enabled': False, 'processed': False, 'reason': 'ADMIN_DISABLED'}
    service = service or build(settings)
    service.settings = settings
    return await run_loop(once=once, service=service)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--enable-model-calls', action='store_true', help='仅本进程开启；本人开关仍需启用')
    args = parser.parse_args()
    configure_logging()
    try:
        print(json.dumps(asyncio.run(run(once=args.once, enable_model_calls=args.enable_model_calls))))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        log_failure(logging.getLogger(__name__), error, operation='suggestion_worker.start')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
