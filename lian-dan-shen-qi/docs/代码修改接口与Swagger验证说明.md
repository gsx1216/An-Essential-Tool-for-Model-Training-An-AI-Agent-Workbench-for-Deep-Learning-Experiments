# 代码修改：接口、部署配置与 Swagger 操作

日期：2026-10-28。新增 3 条路径、4 个 HTTP 操作，总计 53 路径/66 操作、34 张业务表。

## 1. 迁移和启动

```powershell
conda activate langchain
cd E:\code\LangChian_New
python -m app.database --code-change-schema
python -m uvicorn app.main:app --reload
```

迁移增量创建 code_changes、code_change_claims，可重复执行，不清空已有数据，不调用模型、SSH 或训练。若本次交付已由助手完成迁移，无需再初始化数据库，重启后端即可。

没有新增 Worker。API 中显示“应用建议与代码修改记录”分组。

## 2. 接口

所有操作校验当前 Token，身份从登录会话取得，不接受请求体 user_id、SSH 身份、根目录、命令或任意 edits。成功响应 Cache-Control 为 no-store。

| 操作 | 用途 | 成功 |
| --- | --- | --- |
| POST /runs/{run_id}/code-changes | 基于已存建议和当前源码生成计划 | 200，修改记录 |
| GET /runs/{run_id}/code-changes | 查询本人运行的修改记录 | 200，列表；limit 1～20 默认 10，offset 0～10000 默认 0 |
| GET /code-changes/{change_id} | 查询计划、完整 diff、目录和检查结果 | 200，修改记录 |
| POST /code-changes/{change_id}/apply | 显式写入独立副本并运行规定检查 | 200，applied 或 checks_failed 的修改记录 |

准备请求体：

```json
{
  "suggestion_task_id": "替换为本人实际建议任务UUID",
  "suggestion_index": 1,
  "request_id": "0b04480f-74f0-4b56-879c-63532f27a6bd"
}
```

suggestion_task_id 来自 GET /runs/{run_id}/suggestions 中的 task_id 或 agent_name=suggestion 的成功任务，不是 analysis task_id。suggestion_index 从 1 开始，对应 content.suggestions 第几条，不是原始训练序号。

新修改用新 request_id；同一计划因网络问题重发复用原编号。不能用同一编号换运行、建议任务或编号。apply 不需要请求体，change_id 来自 prepare 返回。

准备阶段使用当前用户个人模型 Key，可能产生供应商调用用量；应用阶段不再调用模型，但会连接服务器并写独立目录。GET 以及已保存计划的重放不要求个人 Key，不连接 SSH。

## 3. 返回内容

| 字段 | 含义 |
| --- | --- |
| change_id / run_id | 修改记录与原训练运行 |
| suggestion_task_id / suggestion_index | 明确关联的建议任务和编号 |
| status | planning、ready、applying、applied、checks_failed、attention、conflict |
| lease_active | 是否仍有有效处理租约；不返回令牌 |
| error_code | 安全错误分类，不返回底层供应商/驱动异常正文 |
| plan.edits | 精确旧文本、新文本和修改原因 |
| plan.files | 当前 before_sha256、建议时版本、是否已变化 |
| plan.project_checks / excluded_directories | 部署者配置的检查与排除项快照 |
| result.workspace_directory | 服务器真实可见候选源码目录 |
| result.files | 路径、修改前后版本与完整 unified diff |
| result.checks | 每项检查的类型、名称、状态和限长结果 |
| result.check_status | passed、failed、incomplete |
| result.snapshot_file_count / skipped_entry_count | 实际复制的源码数和跳过条目数，不是全项目文件清单 |
| result.python_executable | 执行语法/静态检查及登记测试的辅助 Python |

ready 仅表示计划保存；applied 表示独立副本写入，不能自动等同于全部测试通过。未登记项目测试时 check_status 通常为 incomplete。checks_failed 的差异和副本仍保留，不能当作可训练验收完成。

## 4. 部署者登记数据排除与项目测试

默认读取 config/code_change_checks.json，缺省等价于空登记；公开模板为 config/code_change_checks.example.json。可用 CODE_CHANGE_CHECKS_FILE 指定部署文件，最大 64 KiB。

键必须与 GET /runner/scripts 中实际登记的 script_id 对应。例如实际项目登记 ID 为 prnet_train 时，可写：

```json
{
  "prnet_train": {
    "checks": [],
    "exclude_directories": ["tiny_COD"]
  }
}
```

这是填写例子，不保证你的实际 script_id 就叫 prnet_train。tiny_COD 是实际数据目录才填写；其他项目按自己的数据路径登记。排除目录不是要删除的目录，编辑器只是跳过它，不复制、不修改。

大型数据目录应在准备前配置；排除项冻结进计划，变更配置不改旧计划。漏配导致 SNAPSHOT_TOO_LARGE 时，修正配置并用新 request_id 创建新计划。源码副本默认不携带训练数据或权重，不应直接把它当作完整可训练工程。

已经确认适合本阶段的 CPU 检查，可登记：

```json
{
  "your_registered_script_id": {
    "checks": [
      {
        "name": "cpu_unit_tests",
        "module": "unittest",
        "arguments": ["discover", "-s", "tests"],
        "timeout_seconds": 20
      }
    ],
    "exclude_directories": ["training_data"]
  }
}
```

module 只允许 pytest 或 unittest，最多三项，每项 1～30 秒。默认不运行项目测试；登记检查会实际执行候选项目代码，需由部署者确认不启动训练、不修改原项目。这不是操作系统沙箱。禁止模型或浏览器请求自行提供检查命令。

配置变更后重启 API。可选环境变量 CODE_CHANGE_MODEL_NAME（默认 deepseek-flash）、CODE_CHANGE_MODEL_TIMEOUT_SECONDS（默认 60、范围 1～90）。本阶段不自动修改实际 .env。

## 5. /docs 操作顺序

1. POST /auth/login，Authorize 填原始 access_token；GET /auth/me 确认身份。
2. 选本人已有训练 run_id；GET /runs/{run_id}/suggestions 取得具体 supported 建议及 task_id。insufficient/零建议或旧报告不能直接修改。
3. 确认个人模型 Key configured=true，服务器源码接口能读取该运行项目；大型数据目录按第 4 节配置。
4. POST /runs/{run_id}/code-changes，填写建议任务、编号和新的 request_id。
5. 返回 ready 后查看 plan.edits、plan.files 与 explanation；只有计划，没有应用。
6. POST /code-changes/{change_id}/apply，不填请求体。此操作明确要求在独立目录写入副本并检查。
7. GET 修改记录，查看 result.workspace_directory、完整 diff 和真实 checks。可在服务器终端访问这个可见目录。
8. 同 ID 再次 apply 返回原结果，不重新覆盖候选目录。文件在规划后变化时应返回 409，并保存 conflict/SOURCE_CHANGED。
9. 用另一个账号访问准备、GET、apply，应返回 404；API 不泄露原始凭据。

不需要手动启动训练来验证此阶段。已经完成的记录是历史快照，不实时检查候选目录后来被手改或删除。WORKSPACE_INCOMPLETE 不自动清理或覆盖；检查对应独立目录后用新的请求准备新记录。

## 6. 聊天操作

已有具体建议后，在会话发送新 request_id：

```json
{
  "content": "应用运行【替换为本人真实run_id】的第1条建议，不要启动训练。",
  "request_id": "3a62035f-ffcf-4a0f-9455-a51b575301eb"
}
```

Agent 应先读取已保存建议、准备计划，再应用。也可明确指定已准备修改 ID，或在本会话已有明确计划后说“应用这个修改”。

`如何应用第1条建议？`、`不要应用第1条建议`、引用/条件句只讨论，不提供写工具。当前轮次与原文授权还会在写入事务中再次检查，不能由模型提交 authorized=true 或换 request_id 绕过。

用 GET /sessions/{session_id}/tool-calls 查看 prepare_code_change/apply_code_change 及 metadata 中 change_id。最终回复附程序生成的修改记录、目录、文件与检查状态。查询“查看修改记录”使用只读 list_code_changes/get_code_change，不自动修改或训练。

同一轮最多应用一份修改。每次新修改使用新问题；网络重试复用同一个聊天请求。删除聊天不删除实验修改档案，新的会话仍可按本人运行查询。

## 7. 常见错误与恢复

| HTTP/状态 | 含义与处理 |
| --- | --- |
| 400 | 请求或个人模型配置不符合要求 |
| 401 | 登录失效 |
| 403 / 429 / 502 / 504 | SSH 策略、名额、连接、模型或操作超时；查看原记录，不换编号盲目重复 |
| 404 | 非本人运行、建议或修改记录不存在 |
| 409 | 重用请求换目标、旧报告、版本变化、服务器绑定变化、正在处理或授权不明确 |
| 422 | UUID、建议编号、分页或多余请求字段不符合 Schema |
| 503 | 配置/存储暂不可用；对外不暴露数据库原始错误 |
| attention / WORKSPACE_BUSY | 查询并稍后重试同一记录，原操作可能正在结束 |
| attention / WORKSPACE_INCOMPLETE | 保留不完整目录供排查，新请求重新准备；不自动覆盖 |
| checks_failed | 保存实际失败，不能称为测试已通过；修正方案使用新记录 |

远端已完成但 HTTP/SQL 响应不确定时，同 change_id apply 会恢复已完成结果；不会重新生成模型计划。准备时个人 Key 缺失产生的 attention 可在配置好后复用同一 request_id 重试。

## 8. 自动验证命令

```powershell
python -m unittest tests.test_code_change -q
python -m scripts.verify_code_change_mysql
```

第二条使用随机临时 MySQL 库、模拟模型和远端，不访问训练服务器，不启动训练。真实 Linux 编辑器另用随机临时目录和 CPU unittest 验证，未修改真实 PRNet 项目或权重。

模块解释：[应用建议与代码修改模块说明](应用建议与代码修改模块说明.md)。逐文件变化和最终验证结果：[代码修改阶段文件变更清单](代码修改阶段文件变更清单.md)。
