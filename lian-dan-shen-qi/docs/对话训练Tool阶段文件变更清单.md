# 对话训练 Tool 阶段文件变更清单

日期：2026-10-26。目标项目：`E:\code\LangChian_New`。

用户在对话指定任务和参数，Agent 调用 Tool，后端选择本人配置、持久化计划并复用 Runner。PRNet 不再是通用工具的唯一目标；首次新项目接入仍须登记对应入口。

## 新增文件（23 个）

| 文件 | 用途 |
| --- | --- |
| `app/models/training_task.py` | 训练任务/API/Tool 契约、工具说明、参数快照摘要与直接启动原文检查。 |
| `app/repositories/training_repository.py` | 本人目录、不可变计划、原文读取、attempt 校验、原子启动领取和 run 关联。 |
| `app/services/training_task_service.py` | 通用目录和启动业务，合并参数、校验版本、幂等创建 run 并调用 Runner。 |
| `app/api/training_task_routes.py` | 登录保护的任务创建/列表/详情及本聊天最新计划查询。 |
| `scripts/verify_training_chat_mysql.py` | 10 组真实 HTTP/MySQL/回环 SSH 验收，两个微型入口、三个 CPU 作业、模拟模型。 |
| `tests/test_training_tools.py` | 22 项新增测试：契约、授权格式、配置变化、参数、重试、回执序列化及迁移。 |
| `docs/对话调用训练Tool与多项目任务说明.md` | 用户对话示例、Tool 原理、接口配置、每个核心模块/包/事务及验证边界。 |
| `docs/对话训练Tool阶段文件变更清单.md` | 逐文件列出本阶段全部新增与修改。 |
| `sql/create_training_tasks.sql` | 创建本人训练目录表，归属外键与用户内名称唯一。 |
| `sql/create_chat_training_plans.sql` | 创建不可变计划表，固定请求编号、摘要和运行关联。 |
| `sql/create_training_launch_claims.sql` | 创建同轮唯一启动领取表，防止跨计划重复执行。 |
| `sql/create_training_task.sql` | 参数化新增用户任务，不启动作业。 |
| `sql/get_training_task.sql` | 按用户及任务 ID 读取配置。 |
| `sql/list_training_tasks.sql` | 按用户及名称查询有界任务列表。 |
| `sql/get_training_question.sql` | 读取本轮落库的 human 原文，不取摘要。 |
| `sql/create_chat_training_plan.sql` | 写入本轮不可变计划。 |
| `sql/get_chat_training_plan.sql` | 读取本人、本会话的指定计划。 |
| `sql/get_chat_training_plan_turn.sql` | 同轮准备重放时读取原计划。 |
| `sql/latest_chat_training_plan.sql` | 读取本聊天最新计划，支持明确指代。 |
| `sql/claim_chat_training_plan.sql` | 记录首次领取计划的轮次。 |
| `sql/bind_training_plan_run.sql` | 在领取后绑定唯一实际运行。 |
| `sql/get_training_launch_claim.sql` | 查询当前轮次已领取的计划。 |
| `sql/create_training_launch_claim.sql` | 在短事务内写入唯一启动领取。 |

## 修改文件（20 个）

| 文件 | 改动 |
| --- | --- |
| `app/models/chat_tools.py` | 可选暴露训练函数 Schema；截断任务列表时保留分页位置。 |
| `app/services/read_only_chat_service.py` | 保留兼容类名，新增训练工具模式、受限分发、预算及 plan_id 审计元数据。 |
| `app/repositories/chat_tool_repository.py` | 允许记录四个训练工具名，继续执行当前 attempt 与会话归属校验。 |
| `app/api/dependencies.py` | 组装 TrainingTaskService，并向用户聊天注入懒加载训练工具工厂。 |
| `app/main.py` | 挂载训练任务路由。 |
| `app/logging_config.py` | 新增任务不存在、计划冲突和缺少明确启动指令的安全中文日志原因。 |
| `app/database.py` | 增量创建三张表，初始化包含新表，CLI 添加 --training-tools-schema 并确认六次工具预算。 |
| `scripts/_chat_api_test_app.py` | 隔离测试应用支持训练工具、临时表和依赖注入，原验收默认行为保留。 |
| `app/repositories/capacity_repository.py` | 容量诊断表白名单更新到 24 张。 |
| `sql/capacity_tables.sql` | 固定元数据表清单增加训练目录、计划与领取表。 |
| `sql/capacity_indexes.sql` | 索引元数据表清单与全部业务表一致。 |
| `scripts/verify_database_capacity_mysql.py` | 容量验收临时库包含新增三表。 |
| `docs/系统总体架构图与模块分层说明.md` | 更新当前图、24 张表职责、对话训练 Tool 流程与当前能力。 |
| `docs/PRNet接入Runner与指标报告说明.md` | 保留历史阶段记录，增加当前对话训练说明的入口。 |
| `tests/test_context_storage_migration.py` | 初始化隔离测试补充新迁移 mock；保留原模块验证，不在该测试中重复执行另一项迁移。 |
| `tests/test_evaluation.py` | 初始化隔离测试补充新迁移 mock；保留原模块验证，不在该测试中重复执行另一项迁移。 |
| `tests/test_experiment_migration.py` | 初始化隔离测试补充新迁移 mock；保留原模块验证，不在该测试中重复执行另一项迁移。 |
| `tests/test_experiment_run_migration.py` | 初始化隔离测试补充新迁移 mock；保留原模块验证，不在该测试中重复执行另一项迁移。 |
| `tests/test_model_credential_foundation.py` | 初始化隔离测试补充新迁移 mock；保留原模块验证，不在该测试中重复执行另一项迁移。 |
| `tests/test_turn_lifecycle.py` | 初始化隔离测试补充新迁移 mock；保留原模块验证，不在该测试中重复执行另一项迁移。 |

## 验证记录

- 1035 项单元测试通过，其中训练 Tool 新增 22 项。
- 训练 Tool：10 组真实 HTTP / MySQL / 回环 SSH 验收，两个不同项目入口、三个 CPU 微型作业。
- 既有只读聊天工具 12 组、公开研究工具 11 组、Runner 8 组回归通过。
- 容量检查 8 组回归通过，元数据与索引白名单覆盖 24 张业务表。
- 模型行为全部明确模拟；无付费模型调用，没有再次启动真实训练服务器 GPU 作业。
- 临时验收库退出时删除，微型作业通过监督器完成/清理，生产账户和模型 Key 未用于测试。
- 本阶段新增三表，业务表共 24 张；没有修改 `.env`、SSH 凭据、主密钥或任何 PRNet 研究源码。

后续真实供应商模型选择工具的行为仍须通过本人 Key 验证。Redis、后台自动采集、自动扫描陌生项目及代码修改闭环均未在本阶段实现。

详细模块逻辑和使用步骤：[对话调用训练Tool与多项目任务说明](对话调用训练Tool与多项目任务说明.md)。
