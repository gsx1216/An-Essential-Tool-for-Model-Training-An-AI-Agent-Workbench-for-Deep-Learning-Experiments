# Runner 阶段文件变更清单

日期：2026-10-25。项目：`E:\code\LangChian_New`。

新增 **28** 个文件，修改 **23** 个文件，共 **51** 个；没有删除或重命名源码文件。

本阶段实现受控执行基础；Skill/RAG 为文档规划，尚未实现。

## 新增文件

| 文件 | 用途 |
| --- | --- |
| [app/api/runner_routes.py](../app/api/runner_routes.py) | 六个鉴权执行接口、请求响应与安全错误映射 |
| [app/models/runner.py](../app/models/runner.py) | 部署注册表、参数规则、启动摘要和远端协议校验 |
| [app/repositories/runner_repository.py](../app/repositories/runner_repository.py) | 持久化绑定、快照校验、归属及单向状态更新 |
| [app/runner/remote_agent.py](../app/runner/remote_agent.py) | 标准库独立监督器、心跳、日志、幂等及取消 |
| [app/services/runner_service.py](../app/services/runner_service.py) | 执行编排、快照复用及网络不确定性处理 |
| [app/services/runner_transport.py](../app/services/runner_transport.py) | 固定 SSH bootstrap、stdin JSON 和有界协议校验 |
| [config/runner_scripts.example.json](../config/runner_scripts.example.json) | 非活动脚本注册样例，含演示脚本实际哈希 |
| [docs/Runner执行与状态日志说明.md](../docs/Runner执行与状态日志说明.md) | 完整模块、包、流程、SQL、配置、验证及限制 |
| [docs/Runner阶段文件变更清单.md](../docs/Runner阶段文件变更清单.md) | 本阶段完整逐文件变更记录 |
| [docs/Skill与论文RAG接入规划.md](../docs/Skill与论文RAG接入规划.md) | Skill、上传/搜索论文入库与带引用问答设计 |
| [examples/runner_smoke.py](../examples/runner_smoke.py) | 只输出演示步骤的 --config 示例，不训练模型 |
| [scripts/_runner_ssh_test_server.py](../scripts/_runner_ssh_test_server.py) | 真实回环 SSH 命令通道与本机微型执行器 |
| [scripts/verify_runner_mysql.py](../scripts/verify_runner_mysql.py) | 八组 HTTP/MySQL/真实 SSH/微型进程验收 |
| [sql/add_runner_run_owner_index.sql](../sql/add_runner_run_owner_index.sql) | 旧运行表增加归属唯一父键 |
| [sql/add_runner_server_owner_index.sql](../sql/add_runner_server_owner_index.sql) | 旧服务器表增加归属唯一父键 |
| [sql/create_run_execution.sql](../sql/create_run_execution.sql) | 本人范围内原子创建执行绑定 |
| [sql/create_run_executions.sql](../sql/create_run_executions.sql) | 执行表、任务唯一键和复合归属外键 |
| [sql/get_run_execution.sql](../sql/get_run_execution.sql) | 按用户联表读取内部执行记录 |
| [sql/lock_runner_run.sql](../sql/lock_runner_run.sql) | 锁住本人 run 以串行首次绑定 |
| [sql/observe_execution_run.sql](../sql/observe_execution_run.sql) | 持久化真实观测并拒绝非终态覆盖终态 |
| [sql/queue_execution_run.sql](../sql/queue_execution_run.sql) | 首次绑定将 created 改为 queued |
| [sql/runner_parent_indexes.sql](../sql/runner_parent_indexes.sql) | 核对两个父索引实际列序与唯一性 |
| [sql/touch_run_execution.sql](../sql/touch_run_execution.sql) | 更新本人任务最近检查时间 |
| [tests/test_remote_runner.py](../tests/test_remote_runner.py) | 真实微型进程、并发、取消、预算、心跳与快照重绑测试 |
| [tests/test_runner_api.py](../tests/test_runner_api.py) | 路由身份、输入、公开响应和安全异常测试 |
| [tests/test_runner_core.py](../tests/test_runner_core.py) | 注册表、参数和业务流程测试 |
| [tests/test_runner_migration.py](../tests/test_runner_migration.py) | 选择性迁移和父键校验测试 |
| [tests/test_runner_transport.py](../tests/test_runner_transport.py) | 命令与 stdin 分离、协议和日志范围测试 |

## 修改文件

| 文件 | 用途 |
| --- | --- |
| [app/api/dependencies.py](../app/api/dependencies.py) | 组装 Runner 固定协作者 |
| [app/api/server_connection_routes.py](../app/api/server_connection_routes.py) | 已绑定服务器删除转为 409 |
| [app/config.py](../app/config.py) | 读取部署脚本注册表和远端 Python |
| [app/database.py](../app/database.py) | 增量补归属父键和 --runner-schema 入口 |
| [app/errors.py](../app/errors.py) | Runner 及服务器在用异常 |
| [app/logging_config.py](../app/logging_config.py) | 新增异常安全原因分类 |
| [app/main.py](../app/main.py) | 正式应用挂载 Runner 路由 |
| [app/repositories/capacity_repository.py](../app/repositories/capacity_repository.py) | 容量检查扩展至十一张表 |
| [app/repositories/server_connection_repository.py](../app/repositories/server_connection_repository.py) | 外键拒绝删除转为明确业务异常 |
| [app/runner/__init__.py](../app/runner/__init__.py) | 说明 Runner 包职责 |
| [app/services/ssh_connector.py](../app/services/ssh_connector.py) | 共用安全连接上下文，保留原连接测试 |
| [docs/数据库容量检查与查询验证说明.md](../docs/数据库容量检查与查询验证说明.md) | 更新十一张表与当前回归结果 |
| [docs/数据库设计决定与后续任务.md](../docs/数据库设计决定与后续任务.md) | 记录 Runner 已完成范围和后续路线 |
| [docs/服务器连接与有据建议设计.md](../docs/服务器连接与有据建议设计.md) | 更新执行基础及后续研究能力边界 |
| [docs/服务器连接配置与SSH工具基础说明.md](../docs/服务器连接配置与SSH工具基础说明.md) | 注明后续 Runner 和在用配置删除规则 |
| [docs/系统模块与摘要持久化说明.md](../docs/系统模块与摘要持久化说明.md) | 增加第 19/20 节及当前技术能力 |
| [scripts/_chat_api_test_app.py](../scripts/_chat_api_test_app.py) | 临时库、Runner 路由和隔离依赖 |
| [scripts/verify_database_capacity_mysql.py](../scripts/verify_database_capacity_mysql.py) | 临时容量库纳入执行表 |
| [scripts/verify_experiment_runs_mysql.py](../scripts/verify_experiment_runs_mysql.py) | 旧结构模拟适配新增子表外键，恢复 Runner 表 |
| [sql/capacity_indexes.sql](../sql/capacity_indexes.sql) | 纳入执行表索引诊断 |
| [sql/capacity_tables.sql](../sql/capacity_tables.sql) | 纳入执行表容量元数据 |
| [sql/create_experiment_runs.sql](../sql/create_experiment_runs.sql) | 新建运行表包含 Runner 归属父键 |
| [sql/create_server_connections.sql](../sql/create_server_connections.sql) | 新建服务器表包含 Runner 归属父键 |

## 数据库、环境与验证

- 新增 run_executions；experiment_runs 和 server_connections 分别新增 (id,user_id) 唯一父键，复合外键强制同用户归属。
- 通过 --runner-schema 增量迁移，不删除旧记录，不回填旧聊天，不自动启动训练。
- 没有新增第三方依赖，没有改写 .env，也没有激活示例注册表或连接真实训练服务器。
- 新增 36 项单元测试，完整 854 项通过；新增 8 组 HTTP/MySQL/真实回环 SSH/微型进程验收通过。
- 旧服务器 10 组、运行 15 组、容量 8 组、上下文聊天 16 组回归通过；测试库已清理，没有收费模型调用。
- Windows 微型进程与 SSH 协议已验证；Linux flock/进程组专用分支、真实 GPU 训练、后台轮询及异常接管仍待验收或开发。

说明：[Runner执行与状态日志说明](Runner执行与状态日志说明.md)；后续设计：[Skill与论文RAG接入规划](Skill与论文RAG接入规划.md)。
