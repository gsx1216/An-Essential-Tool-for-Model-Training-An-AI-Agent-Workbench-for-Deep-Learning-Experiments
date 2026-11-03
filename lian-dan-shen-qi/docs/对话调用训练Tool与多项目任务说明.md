# 在对话里让 Agent 调用训练 Tool

2026-10-29 更新：本文保留早期 Tool 阶段说明。当前已支持用户自行配置通用 Python 项目，以及训练前查看服务器目录、README 和入口；无需为每个网络编写专用适配器。PRNet 专用适配器已移除。当前完整流程见 [通用训练项目接入与接口说明](通用训练项目接入与接口说明.md)。


日期：2026-10-26。项目：`E:\code\LangChian_New`。

## 1. 你实际怎样使用

首次把一个研究项目配置成自己的“训练任务”，以后直接在聊天里说：

> 启动 PRNet，epochs=1，batch_size=4。

或者：

> 启动点云分割，epochs=3。

Agent 查询你自己的任务目录，理解选择和参数，调用工具启动对应任务。PRNet 不再是聊天代码里唯一的训练目标。日常不用手动切换启动接口，也不需要自己输入 run_id。

用户“如何启动 PRNet”“不要启动 PRNet”“最近训练结果怎么样”都不会被后端当成直接启动指令。只说“开始训练”但还没有明确项目时，Agent 应先询问目标。已经在本聊天准备了计划后，可以说“执行这个训练”或“就按刚才的参数执行”。

**首次接入仍需要配置入口。** 新论文的 train.py、运行环境、参数格式各不相同，当前不会仅凭一个陌生项目名称自动猜命令。先登记经过核对的 Runner 入口，然后为用户配置任务；后续都走聊天工具。这一阶段没有实现自动扫描服务器、自动安装环境或任意 shell 执行。

## 2. Tool 是什么，与 @tool 有什么关系

Tool 是一份“模型可见的函数说明 + 后端实际执行函数”。模型返回函数名称和 JSON 参数，不是在自己的回答文本里执行 Python。

项目已经通过 `ModelGateway.invoke()` 中的 `self.model.bind_tools(...)` 将工具定义交给模型，本阶段继续使用这条链路。Pydantic 生成参数的 JSON Schema，后端按固定名称分发。因此不必为了使用工具，再给每个方法加 LangChain 的 `@tool` 装饰器。`@tool` 可以帮助从函数生成说明，但它本身不负责用户鉴权、数据库事务或重试去重。

本阶段的四个工具：

| 工具 | 用途 | 会启动训练吗 |
| --- | --- | --- |
| `list_training_tasks` | 按名字查询本人配置的任务；有界分页 | 不会 |
| `get_training_task` | 读取任务默认参数和允许范围 | 不会 |
| `prepare_training_run` | 保存本轮选定的任务与参数，生成内部计划 | 不会 |
| `start_training_run` | 根据当前用户的明确指令执行计划 | 会 |

模型可以自动串起这几次调用。“准备计划”是后端内部记录，不要求每次让用户手动批准一次。目标和参数明确时，一条对话即可完成发现、准备和启动。只有信息不完整、存在冲突或指令不明确时才需要补充。

## 3. 整体流程

```mermaid
sequenceDiagram
    participant U as 用户
    participant API as 聊天接口
    participant A as 模型/工具循环
    participant T as TrainingTaskService
    participant DB as MySQL
    participant R as Runner/SSH
    U->>API: 启动点云分割，epochs=3
    API->>DB: 校验登录、会话归属，保存原始提问并领取生成尝试
    API->>A: 上下文与工具定义
    A->>T: list_training_tasks / get_training_task
    T->>DB: 只查询当前用户任务
    A->>T: prepare_training_run(task_id, parameters)
    T->>DB: 保存不可变计划，同一轮只保存一份
    A->>T: start_training_run(plan_id)
    T->>DB: 重新校验当前用户原文、尝试、计划归属并领取执行权
    T->>DB: 幂等创建 run，记录计划与 run 的关联
    T->>R: 启动已登记的对应脚本
    R-->>T: 启动观测状态
    T-->>A: plan_id、run_id、参数和实际状态
    A-->>U: 已提交；返回运行编号与状态
```

FastAPI 的 `require_current_user` 根据 Bearer Token 确定当前 user_id。这个身份由后端传进工具，工具参数没有 user_id、SSH 密码、命令或脚本路径字段。模型不能通过传入另一个 user_id 读取或启动别人的训练。

## 4. 四种 ID 不要混淆

| ID | 表示什么 | 何时产生 |
| --- | --- | --- |
| task_id | 可反复使用的用户训练任务配置 | 首次配置一个项目 |
| plan_id | 这一次选定的任务、服务器与参数 | 模型调用准备工具 |
| run_id | 数据库中的某一次真实运行记录 | 启动工具内部幂等创建 |
| job_id | Runner 对应的远端执行目录 / 任务 | Runner 绑定启动目标 |

同一个 task 可以有很多次独立训练。新的用户提问可以生成新 plan；同一 plan 永远复用自己的 run，同一 run 复用自己的 job。回答失败或网络重试不等于用户要求再训练一次。

## 5. 首次如何配置任务

后端文件保存后完成增量迁移，并重启 Uvicorn。已有项目可使用：

```powershell
cd E:\code\LangChian_New
python -m app.database --training-tools-schema
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

增量迁移可重复执行，不清空已有聊天或实验；同时确认工具审计支持每轮最多六次调用。

在 `/docs` 登录并 Authorize 后：

1. 创建自己的服务器配置，并通过连接测试。密码 / 私钥继续使用已有加密凭据模块，不放到训练参数中。
2. 创建 kind 为 training 的实验及对应的评测声明。PRNet 可参考之前的实验示例。
3. `GET /runner/scripts` 查看部署者已登记的训练入口。当前真实服务器已经安装的是 prnet_train；其他项目应先接入自己的入口和参数规则。
4. 调用 `POST /training-tasks`，例如：

```json
{
  "name": "PRNet",
  "experiment_id": "替换为自己的实验UUID",
  "server_id": "替换为自己的服务器UUID",
  "script_id": "prnet_train",
  "default_parameters": {
    "epochs": 1,
    "batch_size": 4,
    "learning_rate": 0.00005
  }
}
```

中文占位符不能原样提交。默认参数必须满足入口的登记规则，配置目录本身不会开始训练。任务名称在同一个用户下不能重复，不同用户可以使用相同名称。尽量使用清楚、彼此容易区分的项目名称。

再用同一个 Token 创建聊天，配置本人模型 Key，在原 `POST /sessions/{session_id}/messages` 提交“启动 PRNet，batch_size=4”。这一步调用的仍是原聊天接口，不是新增一个只会回答“已启动”的假接口。

任务查询：

- `GET /training-tasks?query=PRNet`：本人任务目录。
- `GET /training-tasks/{task_id}`：任务详情和参数规则。
- `GET /sessions/{session_id}/training-plan`：本聊天最新计划，检查是否关联了 run_id；模型回答失败后也可以读取。
- `GET /sessions/{session_id}/tool-calls`：实际工具调用及结果代码，新增 plan_id 元数据辅助追踪。

启动状态、日志、指标、报告继续复用已有 `/runs/...` 接口。工具返回 running / queued 只是当时观测，不是训练完成。现已接入独立后台监控 Worker，运行后自动刷新 / 同步指标并在终态生成报告；未运行 Worker 时手动接口仍可用。查询已有报告不会重新训练，详见 [后台训练监控Worker与恢复机制说明](后台训练监控Worker与恢复机制说明.md)。

## 6. 新模块分别负责什么

### app/models/training_task.py

定义创建任务的请求模型、四个工具参数、JSON Schema 所需的说明、训练工具提示词、规范 JSON 摘要，以及启动指令检查。

Pydantic 的 `extra='forbid'` 拒绝未知字段；UUID 验证 ID；`canonical_parameters` 拒绝凭据、NaN 和过深结构。参数只接受有界 JSON。模型不能自己传一个 `authorized=true` 绕过检查。

`require_start_instruction()` 读取当前人类原文，排除否定、疑问、条件句、示例、代码和引用中的命令。明确提到目标名称 / ID，或对已有计划作明确指代，才允许继续。这是保守的指令格式检查，不是完整自然语言意图识别；复杂或不支持的句式可能需要用户重述为“启动 任务名”。ASCII 项目名按标识符边界匹配，PRNet2 不能误授权启动 PRNet。

### app/repositories/training_repository.py

只负责 MySQL 交互。读取单独 SQL 文件，使用参数化查询；所有读取都带当前用户 / 会话归属条件。

准备、领取启动权时，先锁会话行并检查当前 ContextAttempt：轮次仍为 running、attempt_id 一致、租约有效、问题仍在尾部。启动授权读取数据库中真实的人类消息，不使用模型摘要、压缩后的问题或工具返回内容。

计划写入后校验摘要。每个会话轮次至多一份计划；每个启动轮次只允许一个 plan，即使模型在同一轮先重放旧计划，再尝试新计划，也会被拒绝。数据库事务在 SSH 前提交，不会一边训练一边持有会话行锁。

### app/services/training_task_service.py

连接目录、计划和已有 Runner：

- 创建任务：确认本人的 training 实验、本人服务器、登记脚本和默认参数，保存配置快照，不连 SSH。
- 准备计划：合并默认参数与用户覆盖值，重新验证范围，保存选定任务和参数。
- 启动计划：重新检查归属、服务器绑定、脚本规则 / 摘要和评测版本是否变化，再领取执行权。
- 用计划中固定的 run_request_id 调用已有 `ExperimentRunService.create_run()`，并持久化 run_id，然后调用 `RunnerService.start()`。
- 网络不确定时返回 `START_UNCONFIRMED` 和已知 run_id，保留事实记录，不宣称失败后可以新建一个任务重跑。

因此执行流程没有 `if project == 'PRNet'`，不同项目通过任务配置选择不同入口。PRNet 的专用适配脚本仍保留自己的 GPU / 轮数限制，通用工具不会替它绕开限制。

### app/api/training_task_routes.py

提供任务创建 / 查询和最新计划查询。`Depends(require_current_user)` 注入身份，`Depends(get_training_task_service)` 注入业务服务；装饰器将路径挂在 router 上，`app.main` 的 include_router 才将它们接到应用。

未登录返回 401，别人或不存在的记录返回 404，重复任务名称 / 配置冲突返回 409，错误参数返回 400，底层故障返回通用“请稍后重试”，细节在安全日志里。

### 现有工具循环如何修改

`ReadOnlyChatService` 为兼容已有调用保留了旧名字，但现在可选接入 training_factory。在当前原始提问涉及训练操作 / 任务目录时启用训练工具；该轮不同时开放论文研究工具。普通查询与研究路径继续保留。

工具循环将模型 args 用 Pydantic 验证，再交给 TrainingTaskService；结果作为 tool 消息返回给模型，调用记录写入已有审计表。关键副作用在专门计划 / 领取表中先保存，所以不能只靠最后一条聊天回答或最后一次审计证明是否启动过。

每轮最多六次调用，只有足够的输出预算才执行准备 / 启动，避免执行完却因结果预算不足完全丢失回执。执行与查询都沿用原请求的登录身份，模型不接触数据库密码或 SSH 明文。

## 7. 新增三张表存什么

| 表 | 内容 | 约束 |
| --- | --- | --- |
| training_tasks | 名称、实验、服务器、script_id、默认参数和配置快照 | 用户内名称唯一，实验 / 服务器复合归属外键 |
| chat_training_plans | 本轮任务 / 参数快照、摘要、固定 run_request_id、关联 run_id | 每轮计划唯一，计划属于当前用户和会话 |
| training_launch_claims | 哪一条原始用户消息领取了哪份计划 | session_id + turn_id 唯一；防止同轮多计划，包括重试 |

业务表由 21 张增至 24 张，仍然是所有用户共用表，通过 user_id 隔离。任务目录不存 SSH 密码 / 私钥。删除聊天会级联删除该聊天的计划与领取记录，不删除独立运行或远端检查点；同一用户的任务目录仍然保留。

目前任务配置不可变，没有更新 / 删除任务的 API。切换入口或规则应配置新版本名称；仍被任务关联的服务器会受到外键保护，不能直接删除。后续可增加显式停用和版本管理界面。

## 8. 技术栈与包

| 技术 / 包 | 用途 |
| --- | --- |
| FastAPI / Starlette | 路由、依赖注入、HTTP / OpenAPI 与 TestClient |
| Pydantic | 工具参数校验、UUID、JSON Schema；拒绝额外字段 |
| LangChain / ChatDeepSeek | 复用已有 bind_tools，模型选择工具并产生 JSON 参数 |
| asyncio | 异步服务、超时，to_thread 调用同步 MySQL 仓储 |
| mysql-connector-python | 参数化 SQL、事务、行锁、唯一约束与复合外键 |
| AsyncSSH | 复用原固定主机指纹、认证、协议传输和 Runner |
| cryptography | 复用原 SSH / 模型凭据加密，未设计新的明文密码存储 |
| json / hashlib / uuid / pathlib / re | 规范快照、摘要、服务端 ID、SQL 路径与有界格式检查 |
| unittest / mock | 单元测试、模拟模型，避免付费调用 |

没有新增第三方依赖，没有接入新的付费服务，没有修改主密钥、`.env`、SSH 密码或真实研究项目源码。

## 9. 验证范围

本阶段使用真实 HTTP、随机临时 MySQL、真实回环 SSH 和两个不同的微型 CPU 入口。验证多项目选择、参数覆盖、实际启动、成功请求重放、启动后回答失败恢复、否定 / 讨论拒绝、跨会话与用户隔离、同轮两计划拒绝、旧 attempt 拒绝、删除聊天以及重复 DDL。

模型响应由验收脚本明确模拟，测试不花费模型费用，没有再次在真实训练服务器上启动 GPU 作业。上一阶段已经验收 PRNet 的真实 GPU Runner 链路；本阶段验收的是新增聊天工具如何选择并调用它。真实供应商模型的自然语言理解、工具选择和最终表述仍需使用你自己的 Key 在 `/docs` 做实际对话测试，不能把模拟模型验收视为模型行为百分百正确。

完整逐文件清单和本次测试数量见 [对话训练Tool阶段文件变更清单](对话训练Tool阶段文件变更清单.md)。

## 10. 后续顺序

先用本人账号配置并测试“启动 PRNet”的完整对话。接着可加入项目入口登记界面 / 更多项目适配，独立 MySQL 监控 Worker 已完成自动刷新、采集和生成报告；后续可接 Redis Streams 做任务交付。普通脚本负责事实采集，Agent 通过工具结合报告和论文证据给出分析，之后再接可审计的代码修改闭环。
