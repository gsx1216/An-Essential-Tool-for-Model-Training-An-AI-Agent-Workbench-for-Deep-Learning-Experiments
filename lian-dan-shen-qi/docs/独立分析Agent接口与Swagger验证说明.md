# 独立分析 Agent：接口、启动与 Swagger 验证

日期：2026-10-27。所有请求在 `E:\code\LangChian_New` 运行的 API 上测试。

2026-10-28 衔接说明：当前整体 32 表、50 路径/62 操作。分析成功时同事务登记 suggestion 任务，建议执行使用独立开关和 Worker，默认关闭。analysis-schema 也会确认交接所需的建议结构。原任务列表现在可能同时有 analysis 和 suggestion，按 agent_name 区分。详见 [建议阶段接口](修改建议Agent接口与Swagger验证说明.md)。下文保留分析阶段首次交付时的数量。

## 1. 本阶段接口

新增 4 条路径、5 个 HTTP 操作，总计 43 条路径、54 个操作。Token 仍通过 Swagger Authorize 输入，不接受请求体里的 user_id。成功响应都有 `Cache-Control: no-store`。

| 方法/路径 | 用途 | 成功 |
| --- | --- | --- |
| GET /me/analysis-settings | 查看本人后台分析开关，缺行默认为 false | 200 |
| PUT /me/analysis-settings | 设置本人开关；启用后允许处理本人未处理队列，包含之前已排队报告，Worker 使用本人 Key | 200 |
| GET /runs/{run_id}/analyses | 按报告版本倒序读取已保存分析；不调用模型 | 200，列表 |
| GET /runs/{run_id}/analysis-tasks/{task_id} | 查看任务状态、次数、租约是否有效和安全错误分类 | 200 |
| POST /runs/{run_id}/analysis-tasks/{task_id}/retry | 将本人未完成任务重新排队，不重新训练、不覆盖成功分析 | 202 |

原 GET /runs/{run_id}/agent-tasks 继续列出简要任务。原事件 retry 只是重新交付同一事件，不会重置已经失败的分析；分析失败使用新的 analysis-tasks retry。

## 2. 字段

`run_id` 和 `task_id` 是 UUID，既支持已有 32 位 hex，也支持标准带连字符格式。不存在与越权统一 404。

PUT 请求：

```json
{"enabled": true}
```

enabled 为严格布尔值，不接收字符串 "true"、数字 1 或额外字段。GET/PUT 返回相同结构。

GET analyses 查询：limit 默认 10，范围 1～20；offset 默认 0，范围 0～10000。没有已保存结果返回空列表，不代表训练失败，也不会自动开始分析。

分析响应每项：

| 字段 | 含义 |
| --- | --- |
| task_id / run_id / report_version | 任务、运行与固定来源报告版本 |
| model_name | 本次执行配置的模型名 |
| is_stale | 当前报告版本/运行终态已与分析来源不同，旧结果不能称为最新结论 |
| created_at | UTC 保存时间 |
| content.schema_version | 分析结果结构版本，当前 1 |
| content.source | 原 run/event、报告版本与报告摘要 |
| content.facts | 程序取自报告的 path/value，不由模型编造 |
| content.inferences | text 与 evidence_paths，仍是待验证推断 |
| content.recommendations | text、evidence_paths、verification，需对照验证的方向，不是论文核实后的自动修改 |
| content.limitations | 模型及预算局限 |
| content.omitted_fact_count | 未进入本次证据包的候选事实数量 |
| content.notice | 固定说明：没有查论文、改代码或启动训练 |

任务详情返回 task_id、event_id、run_id、report_version、status、attempts、attempts_in_cycle、lease_active、last_error_code、created_at、started_at、finished_at。不返回 lease_token、Key 或报告正文。

retry 无请求体，返回 `{"task_id":"...","status":"queued"}`。要求本人已启用；成功任务和有效 running 租约返回 409；过期 running、failed、attention 可恢复，queued 重排仍只有同一个任务。202 只是入队，不等于已分析完成。

## 3. 错误

| 状态码 | 含义 |
| --- | --- |
| 401 | Token 无效或过期 |
| 404 | 运行/任务不存在或不属于本人 |
| 409 | 成功分析不可覆盖、任务仍在执行或本人未启用 |
| 422 | UUID、分页、布尔值或请求结构不正确 |
| 503 | 服务或存储校验异常；客户端只显示“请稍后重试。”，日志定位 |

任务 last_error_code 可能包括 KEY_UNAVAILABLE、MODEL_TIMEOUT、MODEL_UNAVAILABLE、INVALID_MODEL_OUTPUT、INVALID_SOURCE、STORAGE_ERROR、ANALYSIS_INTERRUPTED、WORKER_CANCELLED、ATTEMPTS_EXHAUSTED、INTERNAL_ERROR。模型错误在 Worker 内记录任务状态，不会修改训练自身的 succeeded/failed 状态。

## 4. 启动顺序

增量建表（本次交付会执行并验证；新环境需要运行）：

```powershell
python -m app.database --analysis-schema
```

重启 API 后 /docs 出现“后台训练分析”。API 如需要交互输入 Redis 密码，仍使用：

```powershell
python -m app.serve --prompt-redis-password
```

默认安全检查：

```powershell
python -m app.workers.analysis_worker --once
```

默认返回 enabled=false / ADMIN_DISABLED，不读取个人 Key，不领取任务，不调用模型。

正式启用时，管理员设置 ANALYSIS_WORKER_ENABLED=true，或只在本次进程显式启用：

```powershell
python -m app.workers.analysis_worker --enable-model-calls
```

该命令可能使用已启用用户的本人 Key 产生供应商费用。本阶段不运行它进行真实模型验收。用户也必须先通过本人接口启用；只有管理员开关不足以领取任何默认关闭用户的任务。

监控、事件投递和事件接收仍需原来的独立 Worker；分析执行器只处理已经持久入队的任务，不直接连接 Redis，也不替代接收器。

## 5. Swagger 验证步骤

1. 登录 Authorize；GET 本人开关确认 false；查看既有运行及 GET /agent-tasks 获取 task_id。
2. 查询 GET /analyses，尚无结果时应为 []。
3. 如果计划真实调用，先确认本人 Key、管理员执行器设置和本人启用；不是为了测试接口就必须开启模型。
4. 执行器运行后查询任务：queued → running → succeeded；失败时看安全错误分类，修复后显式 retry。不要用事件 retry 代替分析 retry。
5. GET analyses 检查 source、报告版本、facts、inferences、recommendations、limitations 和 is_stale。
6. 新建聊天后可说“读取这次训练已保存的后台分析”，聊天模型按需调用 get_run_analysis；查询已有结果不会重新跑后台分析。聊天本身仍遵循原用户 Key 和普通聊天计费。
7. 切换另一用户 Token 查询同一 run/task 应 404；关闭本人开关应阻止后续领取和调用前检查。

## 6. 聊天 Tool

新增 `get_run_analysis`，参数：run_id 可选（省略用会话关联）；section 默认 overview，可选 facts/inferences/recommendations；offset 默认 0，范围 0～256。每次最多返回两项，预算不足会标记 truncated 并给出后续偏移。

overview 返回版本、过期标记、各节数量、局限和能力提示。没有结果返回 ANALYSIS_NOT_READY。查询前仍检查 run 归属，本轮 audit/会话关联和 attempt 校验照常执行；模型不能传 user_id、命令或开关参数。

新增工具预留额外输入预算，正式聊天仍使用 48K 应用输入上限和既有 7 天上下文缓存，没有增加自动分析或训练副作用。

## 7. 配置与自动验收

示例配置已列在 config/.env.example；真实 .env 不自动改写。默认模型 deepseek-flash、租约 90 秒、心跳 15 秒、模型超时 120 秒、步骤总超时 180 秒、最多三次中断恢复、输入 49152、输出 2048。

```powershell
python -m unittest tests.test_analysis
python -m scripts.verify_analysis_mysql_redis
```

后者创建随机临时 MySQL 和独立临时 Redis，经过真实登录、报告、Redis 交付、领取和持久结果，模型全部模拟，不连接训练服务器。要求本机 PATH 中有 redis-server，MySQL 测试账号能创建临时库；只清理脚本自己创建的临时资源。

测试范围、数量和全部变更见 [文件变更清单](独立分析Agent阶段文件变更清单.md)。真实供应商输出质量、生产压力、Kubernetes、多 Agent 自动论文建议仍需后续验收。
