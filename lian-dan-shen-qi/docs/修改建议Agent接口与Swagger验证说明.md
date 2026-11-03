# 修改建议 Agent：接口、启动和 Swagger 验证

日期：2026-10-28。新增 5 条路径、6 个 HTTP 操作，当前合计 50 路径/62 操作、32 张共享表。需要增量迁移；不会清空原库。

## 1. 更新与迁移

停止旧 API/分析进程后，在项目根目录执行：

```powershell
conda activate langchain
cd E:\code\LangChian_New
python -m app.database --suggestion-schema
python -m uvicorn app.main:app --reload
```

迁移扩展 agent_tasks 的阶段类型，创建三个建议表，为已有成功分析补登记待建议任务；可以重复执行。所有建议开关默认关闭，不会调用模型、联网、SSH 或训练。完整数据库初始化和 analysis-schema 也会确认这个依赖结构。

当前交付若已由助手完成迁移，无需再次初始化或清空数据库，重启 API 即可。

## 2. 六个 HTTP 操作

所有操作先校验 Bearer Token，使用当前用户身份；请求不提供 user_id。查询与重排本身都不调用模型。

| 方法与路径 | 用途 | 成功响应 |
| --- | --- | --- |
| GET /me/suggestion-settings | 本人建议开关 | 200，`{"enabled":false}` |
| PUT /me/suggestion-settings | 设置本人建议授权，包含历史待处理任务 | 200，当前 enabled |
| GET /runs/{run_id}/suggestions | 查询已存建议 | 200，SuggestionResponse 列表 |
| GET /runs/{run_id}/suggestion-tasks/{task_id} | 查询任务状态、次数、有效租约布尔值和错误分类 | 200，任务对象 |
| POST /runs/{run_id}/suggestion-tasks/{task_id}/retry | 显式恢复失败或证据不足任务 | 202，queued |
| GET /runs/{run_id}/suggestion-tasks/{task_id}/evidence | 最近 24 条工具调用与实际返回结果 | 200，记录列表 |

结果查询 limit 默认 10、范围 1～20；offset 默认 0、范围 0～10000。run_id/task_id 是后端返回的 UUID，Swagger 手工输入优先用带连字符的标准形式。

主要错误：401 登录失效；404 运行或该阶段任务不存在/不属于本人；409 仍在执行、未启用本人开关或已有具体成功建议；422 请求字段/UUID/范围错误；503 配置、存储或已存内容暂不可用，对外使用安全提示。成功响应带 `Cache-Control: no-store`。

## 3. 后台执行开关与命令

建议 Worker 是独立进程，启动 API 不会自动启动它。参考 `config/suggestion.env.example`，真实 .env 不会由代码自动改写。

不开启模型的检查：

```powershell
python -m app.workers.suggestion_worker --once
```

默认返回 enabled=false、reason=ADMIN_DISABLED，不连接数据库或读取个人 Key。

需要实际生成时，在本人开关已经明确启用后，管理员可使用：

```powershell
python -m app.workers.suggestion_worker --once --enable-model-calls
```

这条命令会真实使用已启用用户的个人模型 Key、只读 SSH 和公开资料网络。它从队列领取一个符合条件的用户任务，不保证恰好是刚才界面打开的 run；多用户环境应按任务状态确认处理目标。模型请求可能消耗该用户供应商用量。

常驻执行可配置 `SUGGESTION_WORKER_ENABLED=true` 后运行：

```powershell
python -m app.workers.suggestion_worker
```

继续使用既有 `RESEARCH_ENABLED` 控制公开资料访问。关闭时工具会记录 RESEARCH_DISABLED，不能生成正文有据的具体建议。原监控/投递/接收/分析 Worker 的启动方式保持原约定；建议任务不需要新 Redis Stream。

## 4. /docs 完整验证顺序

### 4.1 登录

执行 POST /auth/login，复制 access_token；点击 Authorize，只填写 Token 本身，不加 Bearer 前缀。用 GET /auth/me 确认账号。

### 4.2 选择有成功分析的运行

使用 GET /runs/latest-training 或本人实验运行列表取得 run_id。查询：

```text
GET /runs/{run_id}/analyses
GET /runs/{run_id}/agent-tasks
```

analyses 应有已保存分析；任务列表中应出现 agent_name=suggestion。仅有实验、训练目录或报告，不等于分析已经成功。

旧成功分析在迁移时会补登记；新成功分析在同一提交中自动登记。重复报告事件不会生成重复建议任务。若分析还未完成，按原分析阶段流程检查 analysis 任务，不能直接把报告 ID 或 analysis task_id 填到建议任务接口。

### 4.3 先验证只读条件

调用上阶段 GET /runs/{run_id}/code/files、code/file，确认远端项目能读；无执行绑定或 SSH 不可用不会自动启动训练。

GET /me/model-credentials/deepseek 应显示 configured=true，供实际建议生成使用；直接查询已保存建议不要求个人 Key。

### 4.4 启用本人建议开关

先 GET /me/suggestion-settings，默认 false。需要生成时 PUT：

```json
{"enabled": true}
```

这是建议阶段独立授权。本人 analysis-settings 开关不代替它；已有上游分析时，不需要为了读已有分析重新开启分析调用。

### 4.5 运行建议进程并查状态

按第 3 节启动 Worker。复制 agent-tasks 中 suggestion 的 task_id，然后查询：

```text
GET /runs/{run_id}/suggestion-tasks/{task_id}
```

queued 表示未领取；running 表示处理中；failed/attention 查看 last_error_code。succeeded 之后仍需看结果的 evidence_status。

### 4.6 查结果与实际证据

```text
GET /runs/{run_id}/suggestions?limit=10&offset=0
GET /runs/{run_id}/suggestion-tasks/{task_id}/evidence
```

结果中的 content 重点看：

| 字段 | 含义 |
| --- | --- |
| source | 报告版本/摘要、上游分析 task_id 和摘要 |
| facts | 上游分析实际保留的程序事实 |
| suggestions | 具体方案、假设、适用条件、验证方法和三类引用 |
| code_evidence | 真实读取路径、完整行、文件版本、读取时间、入口匹配信息 |
| paper_evidence | 实际提供的公开资料正文/摘要片段及来源信息 |
| evidence_status | supported 或 insufficient |
| limitations / notice | 信息缺口和验证边界 |

`succeeded + insufficient` 是“本次检查已完成，但证据不足、零条建议”，不是成功提出了修改方案。`is_stale=true` 表示报告版本或运行状态已经更新，应展示旧结果标记；不是实时远端文件变化检测。

工具审计中可能包含明确的 SOURCE_NOT_FOUND、SOURCE_SOURCE_CHANGED、RESEARCH_DISABLED、READ_UNAVAILABLE、RESULT_BUDGET 等结果。attempt_number 是当前返回批次内的尝试分组，call_index 是该尝试内序号；不会返回原始租约令牌或 SSH 凭据。

### 4.7 验证恢复与隔离

修复连接或资料条件后，对 failed、attention 或 insufficient 任务调用 retry，应返回 202。运行中、本人开关关闭、已有具体成功建议时返回 409。重试不重新训练；证据不足的旧零建议诊断被替换，旧工具记录保留。

使用另一个账号查询该 run/task/results/evidence，应返回 404。把 analysis 的 task_id 填进 suggestion 接口也应返回 404。

## 5. 新聊天读取保存的建议

通过 POST /sessions 创建新会话，发送新 request_id：

```json
{
  "content": "请查看运行【替换为本人真实run_id】已经保存的修改建议，说明对应代码位置、论文依据、适用条件和验证方法。",
  "request_id": "ed211506-9218-48f4-9418-6895cbfd08d9"
}
```

后续新问题换新的 request_id；同一问题网络重发复用原编号。查看 GET /sessions/{session_id}/tool-calls，应能看到 get_run_suggestions；查询保存结果不应触发新的后台任务或论文请求。

聊天工具提供概览或单条预览，完整结果看 suggestions API。没有已保存结果时返回 SUGGESTIONS_NOT_READY，不把排队或证据不足说成已经修改完成。

## 6. 不调用付费模型的自动验证

```powershell
python -m unittest tests.test_suggestion -q
python -m scripts.verify_suggestion_mysql
```

第二条使用随机临时 MySQL 库、模拟模型/SSH/公开资料，退出后删除自己创建的临时库。它测试保存和权限流程，不评估真实论文相关性或模型建议质量，不修改训练服务器。

模块解释：[修改建议Agent模块与证据流程说明](修改建议Agent模块与证据流程说明.md)。完整文件清单与验收结果：[阶段文件变更清单](修改建议Agent阶段文件变更清单.md)。
