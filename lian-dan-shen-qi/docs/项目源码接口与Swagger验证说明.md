# 项目源码接口与 Swagger 验证说明

日期：2026-10-28。当前共 45 条路径、56 个 HTTP 操作，仍为 29 张共享业务表。本阶段新增 2 个 GET 接口，不需要数据库迁移。重启 API 后，在 `/docs` 的“项目源码读取”分组查看。

## 1. 准备已有运行

登录后使用 Swagger 的 Authorize 填入自己的 Bearer Token。选择本人已有执行绑定的运行；仅创建实验或训练任务但还没有执行绑定的运行，当前不能确定远端项目，返回 404，不会为了读取代码自动开始训练。

PRNet 已有运行的标准 UUID 示例为 `cbadeeed-4085-4c8e-8337-a880b52db299`，仅该运行所属账号可访问。请使用自己实际拥有的运行。前端保存后端返回的 run_id，再生成查询 URL，无需用户手写 UUID。

两个源码查询接口无需个人模型 Key；聊天仍需要本人 Key。SSH 配置、加密主密钥和网段策略沿用已有部署配置，不在源码查询中重新输入。

## 2. GET /runs/{run_id}/code/files

| 参数 | 位置 | 默认值与含义 |
| --- | --- | --- |
| run_id | path | 本人运行 UUID |
| path | query | `.`，项目内相对目录 |
| offset | query | 0，范围 0～4096 |
| limit | query | 25，范围 1～50 |

首次保持默认值执行。响应包含 entry_path、items、next_offset、retrieved_at。查看子目录时填写返回的相对路径，例如 `models`；翻页使用 next_offset。items 只表示目录条目，不表示已经读取了文件正文。

```text
GET /runs/{run_id}/code/files?path=.&offset=0&limit=25
```

## 3. GET /runs/{run_id}/code/file

| 参数 | 位置 | 默认值与含义 |
| --- | --- | --- |
| run_id | path | 本人运行 UUID |
| path | query | 可省略，默认读取登记入口；也可填写 PRNet_train_cod.py 等实际路径 |
| start_line | query | 1，从 1 计数，上限 1048577 |
| line_count | query | 30，范围 1～60 |
| expected_sha256 | query | 可省略；翻页时填写上一页 file_sha256 |

省略 path 可先看登记入口。入口可能调用真正训练脚本，需要随后读取真正的训练、模型、损失或评测文件，不能仅凭入口几行就声称看过完整模型。

```text
GET /runs/{run_id}/code/file?path=PRNet_train_cod.py&start_line=1&line_count=30
```

下一页使用返回的 next_line 和 file_sha256。保留版本参数，避免把修改前后的代码拼成一个文件解释。

```text
GET /runs/{run_id}/code/file?path=PRNet_train_cod.py&start_line={next_line}&line_count=30&expected_sha256={file_sha256}
```

返回字段详见 [模块说明](项目源码读取工具与代码证据模块说明.md)。由于片段还受到审计大小限制，实际行数可能少于请求行数。响应带 Cache-Control: no-store。

## 4. 错误处理

| 状态 | 含义与处理 |
| --- | --- |
| 401 | 登录缺失或失效，重新登录 |
| 403 | 文件包含凭据模式，或远端路径/符号链接不允许；未返回内容 |
| 404 | 运行不属于本人、没有执行绑定、文件或目录不存在 |
| 409 | 服务器绑定或源码版本已变化，重新确认服务器或重新读取第一页 |
| 413 | 文件超过 1 MiB、目录超过扫描限制、单行无法完整放入片段 |
| 422 | UUID、相对路径、行号、数量、版本参数不符合约束；本地路径校验失败不会访问 SSH |
| 400 | 当前服务器平台或文本编码不支持 |
| 429 / 502 / 504 | 沿用既有 SSH 名额、连接和超时错误 |
| 503 | 配置、存储或协议暂不可用，对外不返回原始内部错误 |

读取失败不会改变训练运行状态。不要把报错改成“读取成功但内容为空”。空文件或超出文件末尾的合法窗口则可以返回空 lines，并保留 total_lines 供客户端判断。

## 5. 验证聊天 Agent

使用已有 `POST /sessions/{session_id}/messages`，每个新问题使用新 request_id，例如：

```json
{
  "content": "检查最近训练的入口代码，先读实际源码，再解释数据加载和损失函数在哪里。",
  "request_id": "5f7d3268-2778-4dad-a065-0a6332a8b156"
}
```

上面 UUID 仅是一次新请求示例，后续问题需换编号。让模型按需查询最近训练、列目录、读源码；不要为了检查代码手动启动训练。

成功读取后，回复末尾出现“代码读取依据（当前源码片段）”，列出实际文件、行号、版本和时间。模型可以使用 `[code_1]` 引用片段。若未取得源码，后端明确说明未取得，不接受“已经读完”的错误说法。

用 `GET /sessions/{session_id}/tool-calls` 检查：

- tool_name 为 list_project_files 或 read_project_file。
- 读取成功时 result_metadata.code_evidence 保存实际提供的完整行。
- 读取失败有明确 outcome，例如 SOURCE_SENSITIVE_CONTENT；没有伪造的 code_evidence。
- 相同文件的后续读取 arguments 中会自动带上本轮已有 file_sha256。

直接调用源码 GET 接口不保存聊天审计；只有通过聊天工具读取的片段随聊天保存。

## 6. 建议按顺序验证

1. 本人运行能列目录，并读到登记入口。
2. 读取实际训练文件，使用 next_line 和 expected_sha256 继续读取。
3. 将 expected_sha256 改成其他 64 位小写十六进制值，应返回 409。
4. 读取 `../x.py` 或 `.env`，应在本地返回 422。
5. 其他账号访问该运行，应返回 404。
6. 通过聊天提出代码检查问题，确认回复和工具审计中的实际片段一致。
7. 发一句普通问候，确认没有新增源码工具调用。

项目自测：

```powershell
cd E:\code\LangChian_New
python -m unittest tests.test_source_code -q
python -m scripts.verify_source_code_mysql
```

第二条使用随机临时 MySQL 数据库、模拟模型和文件替身，不连接训练服务器或调用付费模型。Linux 文件系统边界另已在真实服务器验证。
