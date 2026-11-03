# 独立分析 Agent 阶段文件变更清单

日期：2026-10-27。项目：`E:\code\LangChian_New`。

本阶段共 56 个文件：新增 27 个，修改 29 个。无新依赖；增量增加两张共享表和 agent_tasks 八个执行字段，业务表由 27 张增至 29 张。

没有改写真实 .env、启用任何真实用户的后台分析、清空旧任务/报告、启动真实训练或调用付费模型。独立分析 Worker 未作为常驻进程自动启动。

## 新增文件

- [app/api/analysis_routes.py](../app/api/analysis_routes.py)
- [app/models/analysis.py](../app/models/analysis.py)
- [app/models/analysis_tools.py](../app/models/analysis_tools.py)
- [app/repositories/analysis_repository.py](../app/repositories/analysis_repository.py)
- [app/services/analysis_service.py](../app/services/analysis_service.py)
- [app/workers/analysis_worker.py](../app/workers/analysis_worker.py)
- [docs/独立分析Agent执行与证据结果模块说明.md](../docs/独立分析Agent执行与证据结果模块说明.md)
- [docs/独立分析Agent接口与Swagger验证说明.md](../docs/独立分析Agent接口与Swagger验证说明.md)
- [docs/独立分析Agent阶段文件变更清单.md](../docs/独立分析Agent阶段文件变更清单.md)
- [scripts/verify_analysis_mysql_redis.py](../scripts/verify_analysis_mysql_redis.py)
- [sql/add_analysis_task_columns.sql](../sql/add_analysis_task_columns.sql)
- [sql/analysis_task_columns.sql](../sql/analysis_task_columns.sql)
- [sql/claim_analysis_task.sql](../sql/claim_analysis_task.sql)
- [sql/create_analysis_preferences.sql](../sql/create_analysis_preferences.sql)
- [sql/create_analysis_results.sql](../sql/create_analysis_results.sql)
- [sql/finish_analysis_task.sql](../sql/finish_analysis_task.sql)
- [sql/get_analysis_preference.sql](../sql/get_analysis_preference.sql)
- [sql/get_analysis_task.sql](../sql/get_analysis_task.sql)
- [sql/list_analysis_results.sql](../sql/list_analysis_results.sql)
- [sql/load_analysis_source.sql](../sql/load_analysis_source.sql)
- [sql/next_analysis_task.sql](../sql/next_analysis_task.sql)
- [sql/reap_analysis_tasks.sql](../sql/reap_analysis_tasks.sql)
- [sql/renew_analysis_task.sql](../sql/renew_analysis_task.sql)
- [sql/retry_analysis_task.sql](../sql/retry_analysis_task.sql)
- [sql/save_analysis_preference.sql](../sql/save_analysis_preference.sql)
- [sql/save_analysis_result.sql](../sql/save_analysis_result.sql)
- [tests/test_analysis.py](../tests/test_analysis.py)

## 修改文件

- [app/api/dependencies.py](../app/api/dependencies.py)
- [app/config.py](../app/config.py)
- [app/database.py](../app/database.py)
- [app/logging_config.py](../app/logging_config.py)
- [app/main.py](../app/main.py)
- [app/models/chat_tools.py](../app/models/chat_tools.py)
- [app/repositories/capacity_repository.py](../app/repositories/capacity_repository.py)
- [app/repositories/chat_tool_repository.py](../app/repositories/chat_tool_repository.py)
- [app/repositories/event_repository.py](../app/repositories/event_repository.py)
- [app/services/event_delivery_service.py](../app/services/event_delivery_service.py)
- [app/services/model_gateway.py](../app/services/model_gateway.py)
- [app/services/read_only_chat_service.py](../app/services/read_only_chat_service.py)
- [config/.env.example](../config/.env.example)
- [docs/API接口总览与前端接入约定.md](../docs/API接口总览与前端接入约定.md)
- [docs/api/openapi.json](../docs/api/openapi.json)
- [docs/api/接口索引.md](../docs/api/接口索引.md)
- [docs/系统总体架构图与模块分层说明.md](../docs/系统总体架构图与模块分层说明.md)
- [docs/项目进度与前后端开发路线.md](../docs/项目进度与前后端开发路线.md)
- [scripts/_chat_api_test_app.py](../scripts/_chat_api_test_app.py)
- [scripts/verify_database_capacity_mysql.py](../scripts/verify_database_capacity_mysql.py)
- [sql/capacity_indexes.sql](../sql/capacity_indexes.sql)
- [sql/capacity_tables.sql](../sql/capacity_tables.sql)
- [sql/create_agent_tasks.sql](../sql/create_agent_tasks.sql)
- [tests/test_context_storage_migration.py](../tests/test_context_storage_migration.py)
- [tests/test_evaluation.py](../tests/test_evaluation.py)
- [tests/test_experiment_migration.py](../tests/test_experiment_migration.py)
- [tests/test_experiment_run_migration.py](../tests/test_experiment_run_migration.py)
- [tests/test_model_credential_foundation.py](../tests/test_model_credential_foundation.py)
- [tests/test_turn_lifecycle.py](../tests/test_turn_lifecycle.py)

## 主要改动

1. 新增独立报告分析执行器，默认管理员/本人双关闭，个人 Key、JSON 输出模式、独立有界上下文、心跳/取消/租约恢复。
2. 程序事实与模型推断分开保存，校验证据路径和固定报告版本，结果与成功状态同事务；不把分析失败写成训练失败。
3. 新增五个 HTTP 操作和 get_run_analysis 只读聊天 Tool，新会话可读取已存结果并看到旧报告标记；查询不重跑模型分析。
4. 添加安全任务编号/版本/次数/错误分类日志，包含源码位置，继续保护 Key、Token、租约令牌和正文。
5. 增量迁移、旧初始化测试替身与容量元数据查询同步适配；更新 OpenAPI、接口总览、架构和进度。

## 验证结果

- 完整单元测试 1,149 项通过；其中新增分析测试 26 项，包括真实 SDK + httpx.MockTransport 的 JSON 请求格式验证，未访问供应商。
- 分析链路 19 组真实 HTTP/MySQL/独立临时 Redis + 模拟模型联调通过：旧任务迁移、双开关、Key 归属、证据与版本、重复事件、跨会话读取、失败重试、租约接管、事务回滚、提交确认丢失、取消/关闭、实际心跳、八并发领取和内容篡改等。
- 既有事件队列 15 组、聊天缓存 19 组、上下文 16 组、容量 8 组回归通过；合计 77 组联调。
- 从实际代码导出 OpenAPI：43 条路径、54 个 HTTP 操作；对应新增 4 路径/5 操作。
- 随机临时数据库与临时 Redis 已清理；验证无付费模型、真实服务器 SSH 或 GPU 作业。

模型推断质量、论文依据、生产压力/Kubernetes 和独立建议 Agent 未在本阶段完成；严格一次上游计费不作保证。

## 阅读顺序

1. [模块、技术栈与完整逻辑](独立分析Agent执行与证据结果模块说明.md)。
2. [接口、字段、启动与 Swagger](独立分析Agent接口与Swagger验证说明.md)。
3. [最新总体架构](系统总体架构图与模块分层说明.md)。
