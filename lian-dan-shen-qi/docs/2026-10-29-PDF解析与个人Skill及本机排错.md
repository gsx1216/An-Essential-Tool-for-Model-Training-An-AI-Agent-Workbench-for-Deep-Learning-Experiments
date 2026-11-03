# PDF 解析、个人 Skill 与本机排错日志

更新日期：2026-10-29。前端目录为 `C:\Users\gsx\Desktop\炼丹神器前端`，后端目录为 `E:\code\LangChian_New`。

## 这次实际解决了什么

RDNet 上传后一直显示“解析中”，不是模型没有理解论文。后端日志 `logs/2026-10-29/app-34516.log` 在 08:30:48 UTC 记录了 `NotImplementedError`，追踪号是 `1e9319462d284fd4a72421a0dbc755f5`。调用链为：

```text
POST /papers
  → PaperService.upload
  → PaperService._process
  → PdfParser.parse
  → asyncio.create_subprocess_exec
  → SelectorEventLoop 不支持子进程
```

Windows 的 Uvicorn 热重载使用 Selector 事件循环，而原解析器依赖支持子进程的循环。原件已存入 MySQL，但解析尚未启动，异常又没有转成失败状态，页面才一直显示“解析中”。

现在 Windows 解析任务在工作线程中创建自己的 Proactor 事件循环，仍通过独立 Python 子进程解析 PDF。没有修改整个 API 的事件循环策略。保留现有的超时、进程回收和内存限制；取消请求时会等待有界解析任务清理完成，再释放解析名额。

解析遇到未预期异常时，会记录原始异常链，标记本次解析中断，并返回通用服务错误。进程崩溃来不及写失败状态时，读取端也会把租约已过期的 processing 记录显示为可重试的失败；不会阻塞论文永久停在处理中。有效租约内的任务仍显示解析中。

已经用原件恢复了你的 RDNet：13 页、17 个正文片段，状态为 ready，解析版本为 `pypdf-6.21.3/paragraph-v2`。没有重新上传文件，也没有调用付费模型。

## 资料与建议如何使用

1. “论文资料库”上传文本 PDF，后端保存原件、解析状态和正文分块。支持最多 10 MiB、200 页；扫描件当前需要先进行 OCR。
2. 解析完成后，可以检索关键词、查看正文片段和物理页码，或下载原件。页面会每 7 秒刷新已加载的处理中论文，切走页面后停止轮询。
3. 打开片段后点击“在聊天中讨论”，会带上论文 ID、标题和页码作为聊天草稿。发送后由模型调用论文工具读取相关正文，再依据内容回答。
4. 也可以直接在聊天中明确说“读取我的论文《RDNet》，说明它解决的问题、方法和证据”。论文不会在每次聊天时全文塞进上下文。
5. “会话来源与建议”显示对应会话已经保存的来源与研究建议。上传论文不会自动产生建议，需要实际在聊天或分析流程中使用资料。

当前检索技术是 **MySQL ngram 全文关键词检索**，还不是向量语义检索。返回的片段有论文 ID、页码、片段编号和 SHA256，便于核对来源。

## 个人 Skill 如何新增和使用

进入“资料与建议 → Skill 工作流 → 新增个人 Skill”，填写名称和提示词。例如：

```text
名称：伪装目标检测论文分析

提示词：
先读取论文中问题定义、动机与方法的正文。
再检查相关模型代码，区分论文描述与实际实现。
结合训练结果提出修改建议，指出依据的页码、文件和指标。
资料不足时说明还缺什么，不编造实验结论。
```

保存后，点击“用于新问题”。名称最多 80 字符；提示词最多 6144 个 UTF-8 字节，约 2048 个汉字，页面会显示实际字节数。

保存本身不调用模型，不执行脚本，也不会开始训练。Skill 在这里是一组文字工作流指令；它不能替代用户对训练操作的明确授权，也不会扩大工具权限。

### 个人 Skill 的调用链

```text
LibraryView.vue 表单
  → api.createSkill
  → POST /skills
  → require_current_user 校验 Bearer Token，取得真实 user_id
  → Pydantic 校验名称、正文、长度和 UTF-8 字节数
  → SkillCatalog.create
  → PersonalSkillRepository.create
  → MySQL personal_skills

发送新问题并选择 Skill
  → UserChatService 使用当前 user_id 读取 Skill
  → SkillCatalog 合并公共目录与个人存储
  → 保存该轮实际使用的版本、正文和 SHA256 快照
  → 将指令纳入上下文预算后请求模型
```

`personal_skills` 表保存 `skill_id`、`user_id`、`snapshot_json`、`created_at`。ID 由后端生成，格式为 `personal-` 加随机 UUID；公共 Skill 继续来自后端 `skills/<id>/SKILL.md`。`personal-` 前缀留给个人 Skill。

读取个人 Skill 的 SQL 必须同时限定 `user_id` 和 `skill_id`。其他用户即使知道 ID，也得到 404。旧轮次继续使用自己的冻结快照，重试不会悄悄换用其他提示词。

第一版支持创建、查看和选择；不提供个人 Skill 编辑/删除或 GitHub 插件安装。这些是不同的能力。

### 接口契约

| 接口 | 作用 | 返回 |
| --- | --- | --- |
| `POST /skills` | 创建本人 Skill | 201，SkillSnapshot |
| `GET /skills` | 列出公共 Skill 和本人的 Skill | SkillList |
| `GET /skills/{skill_id}` | 读取有权访问的 Skill 正文和快照信息 | SkillSnapshot；不可访问时 404 |

创建请求不接收客户端指定的 user_id：

```json
{
  "title": "伪装目标检测论文分析",
  "instructions": "先读取论文与实际代码，再结合实验结果提出有来源的建议。"
}
```

缺少有效登录凭证返回 401；参数不合法返回 422；数据库等内部故障返回通用 503，详细原因写入本机日志。实际文档见后端 `/docs` 和本目录 `api/openapi.json`。

## 排错日志只给维护者本机查看

本次没有新增前端系统日志页面，也没有提供向网页账号公开整个后端日志的接口。前端只保留已有的通用错误提示、追踪号以及原有的训练运行日志功能。

系统排错日志默认位于：

```text
E:\code\LangChian_New\logs\2026-10-29\app-进程号.log
```

日期按 UTC 划分。每个进程独立一个文件，重启后进程号会改变；Worker 和测试进程也可能生成各自的文件。单个文件达到 5 MiB 会轮转，最多 3 个备份，默认保留 7 天。部署时可以通过 `APP_LOG_DIR` 指定日志目录。

在桌面前端目录打开 PowerShell，运行本机辅助脚本：

```powershell
cd "C:\Users\gsx\Desktop\炼丹神器前端"
.\查看系统日志.ps1
```

选择对应后端进程的文件，脚本显示最后 100 行并跟随新增内容，按 Ctrl+C 退出。该脚本是维护工具，不由网页执行，编码为 UTF-8 BOM，兼容 Windows PowerShell 5.1。

其他用法：

```powershell
# 只看一次，不持续等待
.\查看系统日志.ps1 -FileIndex 0 -Once

# 后端日志在其他位置
.\查看系统日志.ps1 -LogDirectory "D:\services\experiment-assistant\logs"

# 按追踪号查找，并显示后续调用栈；把这里换成实际请求追踪号
Select-String -LiteralPath "E:\code\LangChian_New\logs\2026-10-29\app-34516.log" `
  -Pattern "1e9319462d284fd4a72421a0dbc755f5" -Context 0,14
```

### 一条错误怎么看

```text
时间 | ERROR | app.services.pdf_parser:parse:行号 [app/services/pdf_parser.py]
request_id=同一次请求的追踪号
执行失败 operation=paper_service.upload error_type=NotImplementedError
  原因：...
  异常链[1]：NotImplementedError
    app/services/paper_service.py:行号 in ...
    app/services/pdf_parser.py:行号 in ...
    base_events.py:行号 in ...
```

先找 ERROR，再看 `operation` 确定失败阶段；首行给出业务文件和行号，异常链展示从业务代码进入第三方库的路径。使用同一个 `request_id` 串起鉴权、仓储、服务和 HTTP 结果。

PDF 已知失败还会写 `paper_code` 和中文原因，例如 `NO_EXTRACTABLE_TEXT` 表示没有可提取文字，`PDF_TIMEOUT` 表示解析超时。不会把 API Key、密码、论文全文、提示词或函数参数写入日志。

远端训练的标准输出与错误输出保存在训练服务器对应作业的 `output.log`。本次通过现有后端只读接口确认你的已完成运行存在 1950 字节日志；这和后端系统日志是两类文件。没有为检查日志启动或停止训练。

为了让维护者不打开网页也能看训练错误，新增了本机导出命令。在后端项目目录、langchain 环境中运行：

```powershell
cd E:\code\LangChian_New
python -m app.export_training_logs --user-id 2 --run-id cbadeeed-4085-4c8e-8337-a880b52db299
```

把用户 ID 和运行 ID 换成实际所属记录。它复用现有服务器配置和 Runner 的 `logs` 操作，不需要重新填写 SSH 密码，不启动/停止训练。下载到导出开始时的日志长度为止；正在运行的任务后续新日志可再次导出。

这次已导出到：

```text
E:\code\LangChian_New\logs\training\cbadeeed40854c8e8337a880b52db299-output.log
```

可以用编辑器打开，搜索 `Traceback`、`Error`、`Exception`。导出失败会在后端本进程系统日志中记录异常链。新快照成功保存后才替换上次导出的文件；连接失败不会把旧日志清空。

## 模块与技术栈

| 模块 | 职责与使用的技术 |
| --- | --- |
| `app/services/pdf_parser.py` | asyncio 工作线程、Windows Proactor 循环、独立 Python 子进程、超时及进程回收 |
| `app/services/pdf_parser_process.py`（复用） | pypdf 提取文字，使用已有段落切分器形成正文块；不使用大模型解析 |
| `app/services/paper_service.py` | 上传/重试编排，调用解析和存储，记录解析失败与中断 |
| `app/repositories/paper_repository.py` | MySQL 原件、状态与分块；识别过期解析租约 |
| `app/models/skill.py` | Pydantic 请求校验、UTF-8 字节限制、不可变 Skill 快照与 SHA256 |
| `app/repositories/personal_skill_repository.py` | mysql-connector-python 执行参数化 SQL、事务提交/回滚及用户归属过滤 |
| `app/services/skill_catalog.py` | 合并文件部署的公共 Skill 和本人 MySQL Skill |
| `app/api/skill_routes.py` | FastAPI 路由、Depends 鉴权、asyncio.to_thread 调用同步存储 |
| `app/services/user_chat_service.py` | 发送问题时按当前用户读取 Skill，沿用已有轮次快照和重试逻辑 |
| `app/logging_config.py` | Python logging、文件轮转、请求上下文、文件/函数/行号定位、经过白名单筛选的异常信息 |
| `src/views/LibraryView.vue` | Vue 3 页面、Element Plus 表单、Pinia Skill 列表及解析状态刷新 |
| `src/lib/papers.ts` | 将解析错误码转成用户能理解的状态提示，不输出服务调用栈 |
| `查看系统日志.ps1` | 维护者本机选择日志并实时读取；不通过网页公开日志 |
| `app/export_training_logs.py` | argparse 读取已有用户/运行 ID，asyncio 复用 SSH Runner，只读下载日志快照到本地 |

## 验证记录

- 原有 PDF/Skill 测试 44 项通过，包含真实 PDF 子进程解析、超时回收和轮次快照流程。
- 本次新增 8 项后端测试通过：Windows Selector 循环下解析、过期状态恢复、异常标记、个人 Skill 长度校验、仓储用户过滤、目录隔离、HTTP 鉴权与身份传递、PDF 错误日志。
- 日志相关既有测试 28 项通过；校验可读错误、调用链和敏感信息保护。
- 真实 MySQL + HTTP 创建、列表、详情、跨用户不可访问验证通过，临时 Skill 已精确删除。
- 前端共 30 项测试通过，其中新增 2 项组件测试覆盖个人 Skill 保存和 processing → ready 后加载索引。布局依赖的第三方页签/下拉框使用测试替身。
- 前端类型检查、ESLint、生产构建及 83 处 API 路径/方法契约检查通过。
- 本机日志脚本通过 Windows PowerShell 5.1 语法解析和单次读取验证。
- 本机训练日志导出已通过真实 SSH 只读验证，保存了对应运行的 1950 字节日志。

使用时刷新浏览器；后端若未使用热重载，则重启一次。现有数据库已增量创建个人 Skill 表；其他部署环境运行 `python -m app.database --skill-schema`。本次没有付费模型调用或新的 GPU 作业。
