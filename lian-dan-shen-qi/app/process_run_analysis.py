"""仅处理指定用户的一次运行；默认只检查，--execute 才会调用用户的模型服务。"""
import argparse
import asyncio
from dataclasses import replace
import json

from app.config import get_mysql_password, get_analysis_settings, get_suggestion_settings
from app.logging_config import configure_logging
from app.models.experiment_run import normalize_run_uuid
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.suggestion_repository import SuggestionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.metric_repository import MetricRepository
from app.validators import validate_positive_integer
from app.workers.analysis_worker import build as build_analysis
from app.workers.suggestion_worker import build as build_suggestions


def inspect_run(user_id, run_id):
    user_id = validate_positive_integer(user_id, '用户 ID')
    run_id = normalize_run_uuid(run_id)
    password = get_mysql_password()
    report = MetricRepository(password).report(user_id, run_id)
    if report.get('is_stale') or report['data'].get('collection_complete') is not True or report['data'].get('status') not in {'succeeded', 'failed', 'cancelled'}:
        raise ValueError('需要运行结束且采集完整的事实报告。')
    events = EventRepository(password)
    # 报告版本对应稳定事件编号；只从已验证归属的列表中选择，不扫描其他用户队列。
    from app.repositories.event_repository import report_event_id
    event_id = report_event_id(run_id, report['version'])
    found = False
    for offset in range(0, 10001, 50):
        rows = events.list(user_id, run_id, limit=50, offset=offset)
        if any(row['event_id'] == event_id for row in rows):
            found = True
            break
        if len(rows) < 50:
            break
    if not found:
        raise ValueError('当前报告尚无可处理的报告事件。')
    return {
        'user_id': user_id, 'run_id': run_id, 'report_version': report['version'], 'event_id': event_id,
        'analysis_enabled': AnalysisRepository(password).preference(user_id)['enabled'],
        'suggestions_enabled': SuggestionRepository(password).preference(user_id)['enabled'],
        'destination': 'api.deepseek.com',
        'data_scope': '本次运行的事实报告；建议阶段可读取相关代码、论文片段并检索公开资料。使用该用户已保存的模型 Key，可能产生费用。',
    }


async def process(user_id, run_id, *, execute=False):
    plan = await asyncio.to_thread(inspect_run, user_id, run_id)
    if not execute:
        return {**plan, 'mode': 'inspect_only', 'model_called': False}
    if not plan['analysis_enabled']:
        raise ValueError('请先由该用户开启自动分析。')
    # 不修改全局开关、不启动常驻 Worker、不扫描全局待办队列。
    await asyncio.to_thread(EventRepository(get_mysql_password()).receive, plan['event_id'])
    analysis = build_analysis(replace(get_analysis_settings(), enabled=True))
    processed = await analysis.tick(user_id=plan['user_id'], run_id=plan['run_id'])
    suggested = False
    if plan['suggestions_enabled']:
        suggestions = build_suggestions(replace(get_suggestion_settings(), enabled=True))
        suggested = await suggestions.tick(user_id=plan['user_id'], run_id=plan['run_id'])
    # processed 表示领取过任务，成功/失败以数据库任务状态和结果为准。
    tasks = await asyncio.to_thread(EventRepository(get_mysql_password()).list, plan['user_id'], plan['run_id'], tasks=True)
    return {**plan, 'mode': 'execute_once', 'analysis_task_claimed': processed,
            'suggestion_task_claimed': suggested, 'tasks': tasks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id', type=int, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--execute', action='store_true', help='会向外部模型发送此运行的数据，并使用用户 Key 计费。')
    args = parser.parse_args()
    configure_logging()
    print(json.dumps(asyncio.run(process(args.user_id, args.run_id, execute=args.execute)), ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
