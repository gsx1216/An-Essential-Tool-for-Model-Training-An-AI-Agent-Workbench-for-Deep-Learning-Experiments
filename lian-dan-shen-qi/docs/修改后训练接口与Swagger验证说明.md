# 修改后训练接口与 Swagger 验证

2026-10-29 更新：当前用户自定义项目不需要额外登记 PRNet 专用模式，统一使用通用 cwd 副本执行；旧 prnet 配置示例不再使用。新的项目接入、接口请求和校验流程见 [通用训练项目接入与接口说明](通用训练项目接入与接口说明.md)。

日期：2026-10-28。所有 ID 都可以传标准带连字符 UUID 或 32 位十六进制 UUID；前端直接保存接口返回值并用于后续调用。

## 1. 启动前准备

在项目根目录使用既有 langchain 环境：

```powershell
cd E:\code\LangChian_New
python -m app.database --revision-schema
python -m uvicorn app.main:app --reload
```

本阶段安装时已执行增量迁移；其他部署实例仍须执行上述迁移。迁移只增加两张表及一项归属索引，不清空记录、不运行训练。

确认 `config/revision_scripts.json` 中登记的是本机 `/runner/scripts` 中真实的 script_id：

```json
{
  "prnet_train": "prnet"
}
```

也可通过服务器环境变量 `REVISION_SCRIPTS_FILE` 指定登记文件。没有登记的入口返回 409；不会自动放行所有脚本。`revision_scripts.example.json` 展示两种模式，通用 cwd 模式需要部署者审核输入路径约定，详见模块说明。

原服务器登录方式、指纹、允许网段、Python 和数据/权重沿用原部署。准备和启动不需要个人模型 Key；创建之前的修改计划，以及使用聊天模型时仍各自需要已配置的个人 Key。

## 2. 新增接口

| 方法 | 路径 | 成功状态 | 作用 |
| --- | --- | --- | --- |
| POST | `/code-changes/{change_id}/training-runs` | 200 | 只读预检候选文件，创建 created 新运行及原运行/修改关联；不训练。 |
| GET | `/code-changes/{change_id}/training-runs?limit=10` | 200 | 最近最多 20 条本人候选运行；修改越权返回 404。 |
| GET | `/runs/{run_id}/revision` | 200 | 读取本人新运行的来源、参数、评测、指纹和检查状态。 |
| POST | `/runs/{run_id}/revision/start` | 202 | 明确启动同一个已准备运行，无请求体；重复不会创建另一训练。 |
| GET | `/runs/{run_id}/comparison` | 200 | 读取与原运行的已保存报告对比；无报告时返回原因。 |

均要求 Bearer Token，成功响应带 `Cache-Control: no-store`。

准备请求体：

```json
{
  "request_id": "f08c18ce-8b82-44ce-9320-1ce6facbd3ab",
  "parameters": {}
}
```

`parameters={}` 保持原训练参数；提供值时只覆盖原参数中的相应项，并执行现有登记规则校验。例如需要已登记允许多轮时才可传 `{"epochs":2}`。不支持 shell、user_id、服务器路径、数据路径、密钥或模型自报的 authorized。

同一次操作重试复用 request_id；另一份独立新训练必须生成新 UUID。上面的 UUID 只是示例，不要对每次新训练都复用它。

## 3. 准备/详情返回字段

| 字段 | 含义 |
| --- | --- |
| run_id / original_run_id / change_id | 新运行、原运行、代码修改记录的关联。 |
| status | 当前数据库观测状态；准备后为 created。 |
| parameters / evaluation | 合并后的参数，以及从原运行复制的完整评测声明。 |
| server_id / script_id / mode | 冻结的目标和入口模式，不允许启动接口另换目标。 |
| candidate_directory | 应用建议生成的候选代码目录。真正执行前另复制到本运行的固定源码目录。 |
| source_digest / source_file_count | 整个候选文件清单的摘要和数量，不是权重哈希。 |
| check_status | 前序代码修改的检查状态。incomplete 不等于全部通过。 |
| created_at / notice | 时间和使用说明。 |

启动返回沿用 `OperationResponse`：`execution` 和 `observation`。`execution.output_directory` 是预定可见产物目录；`observation.outputs.status=published` 才说明已发布。queued/running/202 都不能表示训练已经完成。

## 4. 在 /docs 中完整验证

1. 登录 `/auth/login`，点击 Swagger 的 Authorize 填入返回的 Token。
2. 确认原训练 run 已结束，且有评测声明；查看其建议及代码修改记录。
3. 若还没有已应用修改，按前一阶段文档生成计划并 POST `/code-changes/{id}/apply`。确认 status=applied，检查没有 failed。副本应完整包含该已登记入口及需要的源码。PRNet 数据目录如 tiny_COD 应在代码快照配置中排除；该配置方法见前序代码修改文档。
4. 打开 POST `/code-changes/{change_id}/training-runs`，填实际 change_id、新 request_id，parameters 先用 `{}`。执行后保存新 run_id。
5. GET `/runs/{新run_id}/revision`，确认原 run_id、change_id、参数和 source_digest；此时 status 应为 created。GET `/runs/{新run_id}/execution` 此时应尚无执行绑定。
6. **仅在你确实要使用服务器算力时**，POST `/runs/{新run_id}/revision/start`。无需到服务器手动输入训练命令。此步骤会实际启动已登记项目；本阶段自动验收未替你执行这一步真实 GPU 训练。
7. 用原有 POST `/runs/{新run_id}/refresh`、GET `/runs/{新run_id}/logs` 观察。再次启动同一个 run 不会产生第二次作业。
8. 原有监控 Worker 继续采集/生成终态报告。如果未运行后台监控，仍可使用已有指标同步及报告接口按其文档手动操作，然后查看 GET `/runs/{新run_id}/report`。
9. GET `/runs/{新run_id}/comparison`。如果提示 REPORT_NOT_READY、COLLECTION_INCOMPLETE 或 STALE_REPORT，先处理对应采集/报告，不能把“没有对比值”当成模型更好。
10. GET `/runs/{新run_id}/code/file` 查看本次实际固定的源码，`frozen_source_match` 显示是否与新运行清单相同；原项目和候选目录的后续内容不替代该执行记录。

已有 POST `/runs/{run_id}/start` 也能启动该候选 run，但 server_id/script_id 必须与计划一致，仍会使用 revision 绑定；不会因为走旧接口就运行原代码。

## 5. 聊天验证

可先说“列出运行 <原run_id> 的修改记录”，取得真实 change_id。然后在**当前新提问**里明确说：

```text
启动修改副本 <change_id> 的训练，保持原参数
```

工具流程是 `prepare_revision_run → start_revision_run`。也可先通过 API 准备，再说“启动修改副本 <新run_id> 的训练”。本阶段要求明确给出 ID，前端以后可以通过选择器补全，不要求最终用户手输。

只说“如何启动这个修改？”、“查看修改记录”或引用日志中的启动文字不会提供写工具。模型不能自己传入 authorized=true 或用户 ID。

训练后可说“对比修改后运行 <新run_id> 与原结果”，使用 `compare_revision_run`。聊天最多展示 8 组指标，`series_total`、`display_truncated`/外层 `truncated` 提示截断；完整结果看对比 API。工具不会自动重建报告。

## 6. 对比响应怎样读

```json
{
  "original_run_id": "原运行编号",
  "run_id": "新运行编号",
  "change_id": "修改编号",
  "comparable": true,
  "reasons": [],
  "original_report_version": 1,
  "candidate_report_version": 1,
  "parameter_changes": {},
  "series": [
    {
      "dataset": "cod10k",
      "split": "test",
      "metric_name": "mae",
      "comparable": true,
      "reason": null,
      "direction": "minimize",
      "original_last": 0.03,
      "candidate_last": 0.02,
      "original_step": 1,
      "candidate_step": 1,
      "delta": -0.01,
      "outcome": "improved"
    }
  ],
  "notice": "比较范围与局限说明"
}
```

这表示同一声明、数据集/split/指标和最后 step 的**观测值**更好；不是已证明新模型更强。协议声明不能代替实际数据/评测代码审查，单次结果不能证明因果。

没有报告、不是成功运行、报告过期或不完整时，顶层 `comparable=false`、reasons 给出原因，series 不形成数值结论。某一组缺失、最后 step 不同或有质量标记时，该组 `comparable=false`、delta=null、outcome=not_compared，不补零。

## 7. 异常与预期

| 情况 | 预期 |
| --- | --- |
| 未登录 / Token 失效 | 401。 |
| 他人的修改、新运行或普通 run 当 revision 查询 | 404。 |
| 多传身份/命令等字段，UUID 格式错误 | 422。 |
| 参数类型/范围或未登记参数 | 400。 |
| 没有入口模式、修改未应用、检查失败、源码/请求编号冲突 | 409；核对记录与部署配置。 |
| SSH 指纹不匹配 / 不在允许网段 / 认证失败 | 沿用已有安全错误映射；分别 409 / 403 / 400。 |
| SSH 暂时失败或超时 | 502 / 504；查询原 run 后重试同一编号，不新建运行重跑。 |
| 后端存储异常 | 503，用户只看到稍后重试，详细位置写日志。 |
| 启动前候选版本改变 | 运行最终 failed，error_code=REVISION_SOURCE_CHANGED，不会启动训练；核对代码后重新准备新 request/run。 |

## 8. 自动验证命令

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m scripts.verify_revision_mysql
python -m scripts.verify_training_chat_mysql
python -m scripts.verify_code_change_mysql
python -m scripts.export_api_docs
```

前三类接口验收使用随机临时库及模拟模型；训练聊天回归会在本机回环 SSH 的临时目录运行 CPU 小任务。不得把它们改成生产业务库。日志不打印密码或 API Key。

如需在已配置服务器验证固定 Linux CPU 样例，可由维护者明确选择本人已绑定运行：

```powershell
python -m scripts.verify_revision_linux --user-id <已有用户ID> --run-id <本人已有运行ID>
```

它复用该服务器的指纹/加密凭据，仅在 `/tmp` 随机目录检查源码与 CPU 执行，不改真实 PRNet、不启动 GPU。它验证连接和代码路径，不能替代真实科研训练的效果验证。
