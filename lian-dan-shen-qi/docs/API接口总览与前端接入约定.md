# API 接口总览与前端接入约定

2026-10-29 通用训练接入更新：当前 **69 条路径、85 个 HTTP 操作，未新增业务表**。用户可通过 POST /training-tasks 的 project 配置自己的 Python 环境、训练入口、参数映射、GPU 和指标规则；新增 POST /training-projects/files、POST /training-projects/file 用于训练前只读查看项目。已移除 PRNet 专用适配与部署脚本，新任务统一采用通用项目配置。详见 [通用训练模块、接口与验证](通用训练项目接入与接口说明.md)、[文件变更清单](通用训练项目接入文件变更清单.md)。下方阶段数量为历史记录，以本段和当前导出索引为准。


更新日期：2026-10-28。本文件是当前前端联调基线，并约定后续接口文档的交付方式。

2026-10-28 动态Skill更新：当前 **67条路径、83个HTTP操作、39张共享业务表**。新增GET /skills、GET /skills/{skill_id}、GET /sessions/{session_id}/turns/{turn_id}/skill；发送消息增加可选skill_id。每轮手动选择部署者批准的流程，正文/版本/摘要随问题同事务冻结，失败重试沿用原快照；不能用Skill替代执行授权。只新增chat_turns可空JSON字段，不增加表或Worker。详见 [模块与技术栈](动态Skill模块与轮次快照说明.md)、[接口与Swagger](动态Skill接口与Swagger验证说明.md)、[全部文件](动态Skill阶段文件变更清单.md)。以下数量保留历史，以本段及当前导出索引为准。

同日论文切分修订：当前数量仍为64路径/80操作/39表。论文块改为段落优先、最多6,000字符；默认读取12,000字节、最多18,000字节，返回前后块序号供跨页继续读取。已有ready论文使用 `POST /papers/{paper_id}/retry?rebuild=true` 显式重建，旧块ID失效、历史证据快照保留。见 [切分与完整方法读取](论文段落切分与完整方法读取说明.md)、[本次文件清单](论文切分改进阶段文件变更清单.md)。

2026-10-28 论文知识库更新：当前 **64 条路径、80 个 HTTP 操作、39 张共享业务表**。新增本人 PDF 上传、状态、列表、原件下载、按页分块读取、关键词检索、失败重试和删除，共 9 个操作。聊天与建议 Agent 按需使用论文工具，引用须来自实际读取的片段；上传不调用模型、不启动训练。使用 MySQL ngram 关键词检索，没有向量服务。详见 [模块与技术栈](论文知识库与RAG模块说明.md)、[接口与 Swagger](论文知识库接口与Swagger验证说明.md)、[完整文件清单](论文知识库阶段文件变更清单.md)。以下阶段数量为历史记录，以本段及最新导出索引为准。

2026-10-28 修改后训练更新：当前 **57 条路径、71 个 HTTP 操作、36 张共享业务表**。新增准备/列表/详情/显式启动/同协议报告对比 5 个操作，复用原 Runner、监控和报告，不新增 Worker 或依赖。前端保存 change_id、新 run_id 及原运行关系；点击准备生成 request_id，重试复用，点击启动另发 POST；不可把 created/202 当训练完成。详见 [模块与技术栈说明](修改后训练与结果对比模块说明.md)、[接口与 Swagger](修改后训练接口与Swagger验证说明.md)、[完整文件清单](修改后训练阶段文件变更清单.md)。以下各阶段数量为历史记录，以本段及最新导出索引为准。

2026-10-28 代码修改更新：当前 **53 条路径、66 个 HTTP 操作、34 张共享业务表**。新增计划准备、独立副本应用和记录查询 4 个操作；模型只生成实际已读文件的精确替换，代码版本和服务器绑定在应用前再次核对。新增 code_changes/code_change_claims，无新增 Worker/依赖。详见 [模块说明](应用建议与代码修改模块说明.md)、[接口与配置](代码修改接口与Swagger验证说明.md)、[完整文件清单](代码修改阶段文件变更清单.md)。以下各阶段数量保留历史，以当前导出索引及本段为准。

2026-10-28 修改建议更新：当前 **50 条路径、62 个 HTTP 操作、32 张共享业务表**。建议阶段复用 agent_tasks，新增独立开关、结果和工具证据三张表；分析成功事务交接、建议 Worker 按需读取源码/论文、证据不足可恢复、新聊天 get_run_suggestions 读取已存结果。新增 6 个操作见 [建议接口与 Swagger 验证](修改建议Agent接口与Swagger验证说明.md)、[模块说明](修改建议Agent模块与证据流程说明.md) 和 [文件清单](修改建议Agent阶段文件变更清单.md)。以下各阶段数量保留历史，以本段及代码导出索引为准。

2026-10-28 源码读取更新：当前 45 条路径、56 个 HTTP 操作，仍为 29 张共享业务表。新增本人运行的 `GET /runs/{run_id}/code/files`、`GET /runs/{run_id}/code/file`；无需个人模型 Key。聊天按需使用 list_project_files/read_project_file，实际代码片段随现有工具审计保存。详见 [源码接口与 Swagger 验证](项目源码接口与Swagger验证说明.md) 和 [模块说明及文件清单](项目源码读取工具与代码证据模块说明.md)。

2026-10-28 行为修复：已完成的聊天请求按本人会话、同一 request_id 和相同原问题返回已保存回复，不再要求个人模型 Key；同一编号换问题仍返回 409，越权或已删除会话返回 404。新问题缺少 Key 时仍不写入消息。工具结果预算改为随完整输入预算分配，最多 16 KiB；审计元数据不再新增 result_sha256，已有历史字段保留。接口路径、请求体和公开响应模型均未新增，仍为 43 条路径、54 个 HTTP 操作。详见 [冗余清理与行为修复说明](冗余清理与行为修复说明.md)。

最新分析执行更新：当前 43 条路径、54 个 HTTP 操作，29 张共享业务表。新增本人分析开关、已存分析、任务详情和重试，以及聊天 get_run_analysis 只读工具；双开关默认关闭，查询不启动分析。详见第 9 节和 [独立分析接口文档](独立分析Agent接口与Swagger验证说明.md)。前端仍后置。

2026-10-27 Redis 通信更新：新增 GET /runs/{run_id}/events、GET /runs/{run_id}/agent-tasks、POST /runs/{run_id}/events/{event_id}/retry。所有接口仅访问 MySQL，并校验本人运行归属，不连接 Redis、调用模型或启动训练。received 表示通信已接收，queued 表示待分析任务，不表示分析完成。详见 [Redis事件通信接口与验收说明](Redis事件通信接口与验收说明.md)。用户已明确后端优先，前端页面暂缓。

2026-10-27 更新：执行响应新增 output_directory（预定服务器可见目录）；refresh 的 observation.outputs 提供 published / not_ready / unavailable、directory、file_count、error_code。前端分别展示训练状态与产物发布状态。新运行可通过后端 RUNNER_OUTPUT_DIRECTORY 选择项目内可见目录。详见 [训练产物可见目录与保存位置配置说明](训练产物可见目录与保存位置配置说明.md)。

## 1. 从哪里查接口

- 后端运行时的 `/docs`：可交互测试。
- 运行时的 `/openapi.json`：实际应用接口定义。
- 仓库快照：[docs/api/openapi.json](api/openapi.json)。
- 从真实路由生成的 [完整接口索引](api/接口索引.md)。
- 业务详解：[对话调用训练Tool与多项目任务说明](对话调用训练Tool与多项目任务说明.md)、[PRNet接入Runner与指标报告说明](PRNet接入Runner与指标报告说明.md)。

重新导出当前定义：

```powershell
cd E:\code\LangChian_New
python -m scripts.export_api_docs
```

导出只读取应用路由与模型，不连接 MySQL、SSH 或模型供应商。接口改变后应重新导出，仓库快照不能替代运行中服务的定义。

现有部分接口的响应仍是通用 dict，因此 OpenAPI 中对应响应 Schema 较宽泛；报告 data 也包含嵌套事实结构。这些接口在前端联调时要结合业务说明与实际响应，后续逐项补充明确响应模型，不能直接宣称所有接口已经具备完整强类型契约。

## 2. 通用约定

开发地址示例：`http://127.0.0.1:8000`。所有业务接口当前直接使用 `/auth`、`/sessions`、`/runs` 等路径，没有已实现的 `/api/v1` 前缀。正式域名与 HTTPS 由后续部署确定。

JSON 请求使用 `Content-Type: application/json`。需登录的接口使用：

```http
Authorization: Bearer <access_token>
```

Token 来自 `POST /auth/login`；登录响应含 access_token、token_type、expires_in 和 user。前端按实际 expires_in 处理，不自行假定永不过期。用户身份由后端验证 Token 后确定，不能在请求体中自填 user_id 代替鉴权。

会话、任务、计划、实验、运行及服务器的 ID 都以字符串保存和传递；已有接口接受的 UUID 由各自模型验证。前端不要把 UUID 转换为数字或自行推算对应关系。

服务端时间按 UTC 处理；前端展示时转换到用户时区，例如 Asia/Shanghai，并明确显示当地时间。消息 seq 是会话内顺序，不是数组索引。

`204` 响应没有 JSON 正文，前端不要对退出、删除或某些保存成功操作无条件调用 response.json()。

跨端口前端联调需要显式设计开发代理或 CORS 配置，当前不假定任意来源跨域已开放。

## 3. 前端主流程对应哪些接口

| 前端操作 | 接口 | 说明 |
| --- | --- | --- |
| 注册 / 登录 | POST /auth/register、POST /auth/login | 登录后使用返回的 Token |
| 当前用户 / 退出 | GET /auth/me、POST /auth/logout | 退出使对应登录凭证失效 |
| 新建 / 列出会话 | POST /sessions、GET /sessions | 新会话 ID 由服务端产生 |
| 发送消息 | POST /sessions/{session_id}/messages | 传 content 和 request_id，当前完整 JSON 返回 |
| 重试未完成回复 | POST /sessions/{session_id}/retry | 不另存一条“继续”，可能恢复原工具操作 |
| 聊天历史与删除 | 见完整索引中的历史及会话接口 | 按实际路由参数分页，删除聊天不代表停止训练 |
| 工具执行记录 | GET /sessions/{session_id}/tool-calls | 用于显示执行过程及结果代码，不是模型思维过程 |
| 模型配置 | /me/model-credentials/deepseek 的 GET / PUT / DELETE | 读取只返回配置状态，不回显原 Key |
| 服务器配置 | /servers 及 /servers/{server_id}/test | 保存配置不等于训练，test 只测试连接 |
| 训练任务目录 | GET /training-tasks、POST /training-tasks | 创建目录配置不开始训练 |
| 当前聊天训练计划 | GET /sessions/{session_id}/training-plan | 可读取已持久化的 plan_id / run_id |
| 实验 / 运行 | /experiments 及 /experiments/{experiment_id}/runs | 定义实验与创建 run 分开 |
| 启动 / 停止 | POST /runs/{run_id}/start、POST /runs/{run_id}/stop | 显式操作，有幂等与归属校验 |
| 后台监控 | GET /runs/{run_id}/monitor、POST /runs/{run_id}/monitor/retry | 读取采集状态 / 重新排队，不重新训练 |
| 状态 | GET /runs/{run_id}/execution | 当前数据库中最后观测状态，不自动连接服务器 |
| 刷新状态 | POST /runs/{run_id}/refresh | 当前显式连接远端并写回观测 |
| 日志 | GET /runs/{run_id}/logs | 按字节 offset 分页，保留服务端 next_offset |
| 指标 | GET /runs/{run_id}/metrics、GET /runs/{run_id}/metrics/latest | 从数据库读取，按 dataset / split / metric 分组 |
| 指标采集 | POST /runs/{run_id}/metrics/sync | 当前仍是显式采集接口 |
| 报告 | POST /runs/{run_id}/report、GET /runs/{run_id}/report | 分别生成与读取，关注版本、过期及完整性 |
| 论文上传与列表 | POST /papers、GET /papers | 上传是原始 application/pdf 正文，request_id/title 为 query；查看 ready/failed 状态 |
| 论文检索与原文 | POST /papers/search、GET /papers/chunks/{chunk_id} | 搜索预览不能作为已读证据；先读实际正文再引用 |
| 论文详情、下载、分块 | GET /papers/{paper_id}、/file、/chunks | 仅本人可见；按实际 next_offset 分页 |
| 论文重试与删除 | POST /papers/{paper_id}/retry、DELETE /papers/{paper_id} | 删除原件和索引，历史聊天/建议证据快照保留；204 无正文 |
| Skill目录与详情 | GET /skills、GET /skills/{skill_id} | 登录后查共享部署流程，不需要模型Key；不接收上传或任意路径 |
| 本轮Skill选择与追溯 | 原发送消息的skill_id；GET /sessions/{session_id}/turns/{turn_id}/skill | 每轮选或不选；turn_id就是发送时request_id；旧版本快照保留 |

具体 query 参数、字段约束和已声明响应模型，以当前 OpenAPI 为准；不要为整个系统假定一种分页格式。

## 4. 聊天请求示例与重试规则

```http
POST /sessions/<自己的session_id>/messages
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "content": "启动 PRNet，batch_size=4",
  "request_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

成功响应结构：

```json
{
  "content": "模型返回并已保存的回复正文"
}
```

上面的 UUID 只示意格式。首次发送一条新消息前，浏览器生成 request_id，例如 crypto.randomUUID()。同一条请求因网络中断重发时保留原 ID 和原 content；新的用户消息生成新 ID。更换 ID 不能当作“安全重试”，尤其是训练指令。

现在发送还可带 `skill_id`，真实ID从GET /skills取得，不填或null表示本轮不选。相同request_id重发还必须保持原Skill选择，不能借旧编号切换流程；retry接口则自动使用原快照。前端如希望连续使用某Skill，需在每条新消息中明确提交；不自动继承上一轮。

当前聊天响应仅有 content，不要假定它已经包含结构化 tool_calls、plan_id 或 run_id 字段。工具记录和最新训练计划从专门接口读取。模型可能采用不同表述，前端不应靠正则解析回复文字来判断真实训练状态。

请求失败不代表远端没有训练：可能已经启动，只是模型最后的回答或返回网络失败。优先查询原会话计划、关联 run 和执行状态；重试原未完成回复，不新建训练任务来“补发”。

## 5. 训练计划与状态展示

`GET /sessions/{session_id}/training-plan` 返回最新计划，主要字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| plan_id / task_id | string | 这次计划和可复用的任务配置 |
| name | string | 用户配置的训练任务名称 |
| experiment_id / server_id / script_id | string | 选定的实验、服务器和登记入口 |
| parameters | object | 本次确定的参数 |
| run_id | string 或 null | 已关联的实际运行；null 表示尚未关联 |
| launch_claimed | boolean | 已有轮次领取启动计划；不代表已成功训练完成 |

当前最新计划不等于所有运行列表。聊天没有计划时返回 404，前端可显示“尚未准备训练计划”。

运行状态当前包括 created、queued、running、succeeded、failed、cancelled。`202` 启动响应只是启动请求结果；以 execution 和后续观测判断实际状态。未确认、网络异常和终态必须区别展示，不能把所有请求错误都改成“训练失败”。

日志偏移按字节，不能以中文字符长度推算。指标曲线按 dataset + split + metric_name 分组，不能将不同数据集混成同一条曲线。报告需要展示 version、is_stale、collection_complete 等信息，缺数据时不补零。

独立 MySQL 监控 Worker 已实现。启动 Worker 后，自动刷新状态、增量采集指标和在终态生成报告；只启动 API 不会运行监控循环。前端使用监控 / 执行 / 指标 / 报告读取接口即可。恢复接口只重排监控，不重新训练；当前仍没有 SSE / WebSocket。详见 [后台监控接口与前端联调说明](后台监控接口与前端联调说明.md)。

## 6. 错误与前端处理

业务错误通常是 `{"detail":"提示文字"}`。校验异常经过自定义处理，前端应以实际响应和具体接口文档为准，不自行假设 FastAPI 默认的 detail 数组。错误代码有各自业务含义：

| 状态码 | 常见含义 / 建议处理 |
| --- | --- |
| 400 / 422 | 参数或结构不符合要求，提示修改；不是每个接口都同时使用两者 |
| 401 | 未登录、凭证过期或已撤销，引导重新登录 |
| 403 | 目标网络 / 脚本未允许，不是重新输入用户 ID 就能绕过 |
| 404 | 不存在或不属于当前用户；不尝试枚举其他用户资源 |
| 409 | 同名、幂等冲突、未完成回复或状态冲突，按具体提示恢复 |
| 413 | 上下文或工具预算过大，缩小范围 / 拆分任务 |
| 429 | 调用或连接繁忙，避免持续立即重试 |
| 500 / 502 / 503 / 504 | 内部、上游、暂不可用或超时；记录请求关联信息并恢复原操作 |

不是每个接口都支持表中所有状态码。数据库或模型原始异常不直接展示给用户。

## 7. 后续接口的文档交付标准

每次新增或修改接口，必须同步记录：HTTP 方法、路径、用途、鉴权、所有 path/query/body 字段的类型/必填/默认/约束、成功响应字段与示例、错误码与示例、分页、幂等、异步完成与轮询语义、用户归属及前端调用顺序。

优先用 Pydantic response_model 声明公开响应，再补业务说明；更新接口索引和 OpenAPI 快照。对新增与变更做正常请求、参数错误、未登录、越权和重复请求的相关验证，并列出修改文件。

当前开发顺序与进度见 [项目进度与前后端开发路线](项目进度与前后端开发路线.md)。

## 8. 聊天上下文缓存的前端约定（2026-10-27）

聊天已接入 Redis 上下文缓存，HTTP 请求和响应结构不变，当前仍为 39 条路径、49 个 HTTP 操作。前端不传 Redis 地址、用户 ID、缓存版本或摘要；登录身份继续由 Token 确定，服务端始终检查会话归属。

发送/重试会优先读取加密缓存，并通过 MySQL 检查生成尝试、摘要版本和消息末尾，按需补齐增量。用户问题、AI 回复和摘要仍及时提交 MySQL。Redis 过期/故障不会丢失已提交聊天，也不新增一个“缓存不可用”的业务错误。

GET 会话消息继续提供原 MySQL 分页历史，不把摘要和近期上下文当作全部历史。DELETE 会话成功后会清理缓存正文；不存在或越权仍为原 404 语义。没有增加 SSE/WebSocket 或缓存管理接口。

启动、Swagger 测试、降级边界见 [Redis聊天缓存启动与接口验证说明](Redis聊天缓存启动与接口验证说明.md)。

## 9. 独立后台分析（2026-10-27）

当前 43 条路径、54 个 HTTP 操作，MySQL 29 张业务表。新增本人分析偏好 GET/PUT、运行已存分析列表、分析任务详情与显式重试。完整字段、鉴权、分页、状态码、启用范围和示例见 [独立分析Agent接口与Swagger验证说明](独立分析Agent接口与Swagger验证说明.md)。

本人开关默认 false；启用允许 Worker 用本人 Key 处理包含历史已排队报告在内的任务。管理员开关也必须启用。前端不因为用户进入报告页自动调用 PUT 开启，更不将 GET 查询当作启动分析。后台真实调用可能产生供应商费用，当前验收模型为模拟对象。

queued/running 是任务状态；succeeded 才表示分析已提交。分析失败不修改训练状态。展示 facts、inferences、recommendations、limitations 时区分事实与待验证推断/方向，并展示来源报告版本和 is_stale，不能把旧分析称为最新报告结论。未核实论文，不显示为论文支持的自动修改。

读取接口无需模型 Key 或 Redis；新聊天可通过 get_run_analysis 查询已存结果，不会重跑分析。原聊天请求字段保持不变；没有新增 SSE/WebSocket。
