# Redis 事件通信阶段文件变更清单

日期：2026-10-27。先完成后端，前端暂缓。

本阶段修改 23 个文件，新增 26 个文件，共 49 个。
实际 config/.env 没有修改；安装 redis-py 6.4.0，新增两张业务表后共 27 张；不改原研究代码或 Redis 服务配置。

## 修改文件

- `app/config.py`
- `app/database.py`
- `app/logging_config.py`
- `app/main.py`
- `app/repositories/capacity_repository.py`
- `app/repositories/metric_repository.py`
- `config/.env.example`
- `docs/API接口总览与前端接入约定.md`
- `docs/api/openapi.json`
- `docs/api/接口索引.md`
- `docs/系统总体架构图与模块分层说明.md`
- `docs/项目进度与前后端开发路线.md`
- `requirements.txt`
- `scripts/_chat_api_test_app.py`
- `scripts/verify_database_capacity_mysql.py`
- `sql/capacity_indexes.sql`
- `sql/capacity_tables.sql`
- `tests/test_context_storage_migration.py`
- `tests/test_evaluation.py`
- `tests/test_experiment_migration.py`
- `tests/test_experiment_run_migration.py`
- `tests/test_model_credential_foundation.py`
- `tests/test_turn_lifecycle.py`

## 新增文件

- `app/api/event_routes.py`
- `app/models/event_bus.py`
- `app/repositories/event_repository.py`
- `app/services/event_delivery_service.py`
- `app/services/redis_event_bus.py`
- `app/workers/analysis_event_worker.py`
- `app/workers/event_relay_worker.py`
- `docs/Redis事件通信与事务Outbox模块说明.md`
- `docs/Redis事件通信接口与验收说明.md`
- `docs/Redis事件通信阶段文件变更清单.md`
- `scripts/_temporary_redis.py`
- `scripts/verify_event_bus_mysql_redis.py`
- `sql/claim_event.sql`
- `sql/create_agent_tasks.sql`
- `sql/create_analysis_task.sql`
- `sql/create_event_outbox.sql`
- `sql/enqueue_report_event.sql`
- `sql/event_owned_run.sql`
- `sql/get_event.sql`
- `sql/list_agent_tasks.sql`
- `sql/list_run_events.sql`
- `sql/next_event.sql`
- `sql/receive_report_event.sql`
- `sql/retry_event.sql`
- `sql/settle_event.sql`
- `tests/test_event_bus.py`

## 验证

- 1,101 项单元测试通过，新增 20 项事件/接口/凭据内存处理测试。
- 15 组真实 HTTP/MySQL/Redis 5 故障与恢复验收通过。
- 监控 12 组、指标报告 10 组、容量 8 组回归通过。
- 最后相关 95 项测试通过；临时数据库与临时 Redis 均清理。
- 新增接口 3 个，总计 39 条路径、49 个 HTTP 操作，OpenAPI 已重新导出。
- 没有模型费用、新训练或前端修改；大模型分析执行器尚未实现。

模块解释见 [Redis事件通信与事务Outbox模块说明](Redis事件通信与事务Outbox模块说明.md)，接口与 /docs 验收见 [Redis事件通信接口与验收说明](Redis事件通信接口与验收说明.md)。
