# 修改建议 Agent 阶段：文件变更清单与验收结果

日期：2026-10-28。以下路径相对于 `E:\code\LangChian_New`。

新增 32 个文件、修改 20 个文件，无删除文件。真实 `config/.env`、用户凭据和训练服务器源码/权重未修改；没有新增依赖。

验证通过：

- 完整单元测试 1,205 项，其中建议专项 23 项。
- 建议阶段真实 HTTP/MySQL 验收 11 组：临时库、模拟模型/SSH/公开资料；覆盖旧约束迁移、事务交接/回滚、用户/阶段隔离、默认关闭、两领取者、恢复、聊天读取和日志。
- 原分析阶段真实 HTTP/MySQL/临时 Redis 回归 19 组：租约、取消、重复交付、原子保存、权限和损坏结果拒绝等保持有效。
- API 导出 50 路径/62 操作。当前共 32 张共享业务表。

测试数据库和临时 Redis 已清理。未调用真实付费模型，未启动训练；不把模拟资料当作真实论文或真实模型建议质量评测。

迁移会补登记既有成功分析的建议任务，但默认开关保持关闭，不会因此调用模型。证据不足的显式重试可以替换零建议诊断，旧工具记录保留；已有具体建议不覆盖。

## 新增文件

| 文件 | 用途 |
| --- | --- |
| `app/api/suggestion_routes.py` | 六个建议阶段 HTTP 操作 |
| `app/models/suggestion.py` | 建议配置、Schema、三类证据与来源核对 |
| `app/models/suggestion_tools.py` | 聊天参数和单条预览 |
| `app/repositories/suggestion_repository.py` | 独立偏好/结果 SQL、租约复用、证据记录与恢复 |
| `app/services/suggestion_service.py` | 有界只读工具、先保存证据再提供模型、验证并提交结果 |
| `app/workers/suggestion_worker.py` | 独立建议进程与默认关闭开关 |
| `config/suggestion.env.example` | 公开配置示例，不含任何密钥 |
| `docs/修改建议Agent接口与Swagger验证说明.md` | 迁移、开关、命令、错误码和完整验证顺序 |
| `docs/修改建议Agent模块与证据流程说明.md` | 模块、技术栈、身份、证据、存储及状态逻辑 |
| `docs/修改建议Agent阶段文件变更清单.md` | 本文件：实际差异与验收结果 |
| `scripts/verify_suggestion_mysql.py` | 随机临时库的 HTTP/MySQL 建议验收 |
| `sql/backfill_suggestion_tasks.sql` | 为已有成功分析补登记，不调用模型 |
| `sql/clear_insufficient_suggestion.sql` | 显式重试仅清除零建议诊断，保留工具记录 |
| `sql/create_suggestion_evidence.sql` | 任务尝试工具证据表 |
| `sql/create_suggestion_preferences.sql` | 本人建议授权表 |
| `sql/create_suggestion_results.sql` | 建议结果与快照表 |
| `sql/create_suggestion_task.sql` | 幂等登记建议任务 |
| `sql/extend_suggestion_agent.sql` | 增量扩展任务类型，不删除业务记录 |
| `sql/get_suggestion_preference.sql` | 读取本人开关 |
| `sql/get_suggestion_result.sql` | 重试前锁定并核对已存结果 |
| `sql/get_suggestion_task.sql` | 限定建议阶段、本人运行和任务 |
| `sql/list_suggestion_evidence.sql` | 读取最近 24 条工具记录 |
| `sql/list_suggestion_results.sql` | 读取结果及当前报告过期标记 |
| `sql/load_suggestion_source.sql` | 联查固定报告与上游成功分析 |
| `sql/next_suggestion_task.sql` | 按本人开关与队列到期条件领取 |
| `sql/reap_suggestion_tasks.sql` | 超过尝试次数转 attention |
| `sql/renew_suggestion_task.sql` | 本人授权仍有效时续租 |
| `sql/save_suggestion_evidence.sql` | 写入本次租约工具证据 |
| `sql/save_suggestion_preference.sql` | 保存本人开关 |
| `sql/save_suggestion_result.sql` | 写入结构化建议结果 |
| `sql/suggestion_agent_check.sql` | 读取既有任务类型约束 |
| `tests/test_suggestion.py` | 23 项建议专项测试 |

## 修改文件

| 文件 | 用途 |
| --- | --- |
| `app/api/analysis_routes.py` | 共享错误映射使用通用任务表述 |
| `app/api/dependencies.py` | 组装只读建议查询服务及聊天读取工厂 |
| `app/config.py` | 读取建议阶段配置，默认关闭 |
| `app/database.py` | 增量迁移三张表、任务类型和历史分析交接 |
| `app/main.py` | 挂载建议路由 |
| `app/models/chat_tools.py` | 可选注册 get_run_suggestions |
| `app/models/event_bus.py` | 公开任务类型允许 suggestion |
| `app/repositories/analysis_repository.py` | 分析成功原子交接、复用内容校验及重试衔接 |
| `app/repositories/chat_tool_repository.py` | 保存聊天建议读取工具审计 |
| `app/services/analysis_service.py` | 共享生命周期日志区分 analysis/suggestion |
| `app/services/read_only_chat_service.py` | 按需查询本人已存建议，空结果不触发生成 |
| `docs/API接口总览与前端接入约定.md` | 更新当前数量及建议接入链接 |
| `docs/api/openapi.json` | 从实际路由重新导出响应 Schema |
| `docs/api/接口索引.md` | 导出 50 路径/62 HTTP 操作索引 |
| `docs/独立分析Agent接口与Swagger验证说明.md` | 说明事务交接和双阶段任务的兼容变化 |
| `docs/系统总体架构图与模块分层说明.md` | 更新当前图与第 20 节 |
| `scripts/_chat_api_test_app.py` | 注入建议替身与临时表，不构造真实供应商模型 |
| `scripts/verify_analysis_mysql_redis.py` | 兼容当前表数与双阶段任务，保留原分析回归检查 |
| `sql/create_agent_tasks.sql` | 新建任务表允许两个 Agent 阶段 |
| `sql/get_analysis_task.sql` | 限定 analysis 阶段，避免跨阶段 task_id 查询 |

模块说明：[修改建议Agent模块与证据流程说明](修改建议Agent模块与证据流程说明.md)；接口：[修改建议Agent接口与Swagger验证说明](修改建议Agent接口与Swagger验证说明.md)。
