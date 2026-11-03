# 论文知识库接口与 Swagger 验证说明

日期：2026-10-28。使用项目的 Python 环境，工作目录 `E:\code\LangChian_New`。本阶段新增 9 个 HTTP 操作；当前全后端 64 条路径、80 个操作。

## 1. 启动前

```powershell
cd E:\code\LangChian_New
python -m pip install -r requirements.txt
python -m app.database --paper-schema
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

已有运行中的 API 和 suggestion Worker 需要重启后加载新代码。`--paper-schema` 仅增量确认论文三表及全文索引，可重复执行，不清空原有聊天、运行和报告，不调用模型或 SSH。新库仍按原项目完整初始化流程先创建用户等基础表。

新依赖为 pypdf==6.21.3，没有新增必填 `.env` 配置，也没有额外向量服务。MySQL 需要支持本项目使用的 MySQL 8 ngram 全文索引和现有 CHECK/排序规则。为了容纳上限 10 MiB 的二进制及驱动转义开销，部署时建议 `max_allowed_packet` 至少 32 MiB，检查命令为 `SHOW VARIABLES LIKE 'max_allowed_packet';`。脚本不自动修改全局数据库设置。

打开 `http://127.0.0.1:8000/docs`，按原流程注册/登录，点 Authorize 填登录返回的 access_token。以下所有接口都需要有效 Token；不填写 user_id。论文接口默认不调用模型，因此检索测试不需要个人模型 Key；聊天或建议生成仍需本人 Key。

## 2. 先上传一篇可选中文字的 PDF

展开 **个人论文知识库与 RAG → POST /papers**，点 Try it out：

- `request_id`：本次上传的新 UUID，例如先在本地 `python -c "from uuid import uuid4;print(uuid4())"`。重试同一次上传复用该值，换文件生成新的值。
- `title`：你设置的易识别标题，例如“边界监督论文”；1～200 字符，不能全空白。
- Request body 媒体类型：`application/pdf`，通过文件选择框选择 PDF。

这是**原始 PDF 请求正文**，不是 JSON，也不是 multipart 表单。不要填写服务器路径、base64 或把文件名当正文。Swagger 的文件选择来自实际 OpenAPI 的 binary 声明；若本地文档组件未显示选择框，使用下面 curl 命令验证同一个接口。

```powershell
curl.exe -X POST "http://127.0.0.1:8000/papers?request_id=<新UUID>&title=MyPaper" -H "Authorization: Bearer <你的Token>" -H "Content-Type: application/pdf" --data-binary "@C:\papers\paper.pdf"
```

将占位符和路径换成自己的值。命令行中的 Token 不要发布到截图或提交代码。

成功创建资源返回 201，示意：

```json
{
  "paper_id": "上传响应中的真实ID",
  "request_id": "本次上传请求ID",
  "title": "边界监督论文",
  "status": "ready",
  "error_code": null,
  "file_sha256": "原件SHA256",
  "file_bytes": 123456,
  "page_count": 10,
  "chunk_count": 42,
  "empty_pages": [],
  "parser_version": "pypdf-6.21.3/paragraph-v2",
  "created_at": "UTC时间",
  "updated_at": "UTC时间"
}
```

示意 ID/时间不是可直接提交的值。保存真实 paper_id。201 表示论文资源已经创建，必须继续看 status：`failed` 表示原件保留但没有可搜索索引，不能把它当解析成功。复制/扫描图片组成的 PDF 可能失败；可选中文字的 PDF 也可能因编码/布局导致提取质量不足。

## 3. 九个接口及参数

| 方法与路径 | 参数 | 返回 / 用途 |
| --- | --- | --- |
| POST `/papers` | query: request_id(UUID)、title；body: application/pdf | 201 PaperResponse；保存原件并进行有界解析 |
| GET `/papers` | offset 默认0，0～10000；limit 默认20，1～20 | items、offset、next_offset；仅本人元信息 |
| GET `/papers/{paper_id}` | 真实 UUID | 当前状态、数量、错误代码等 |
| GET `/papers/{paper_id}/file` | 真实 UUID | PDF 原件附件，文件名为 ID.pdf；failed 也可下载 |
| GET `/papers/{paper_id}/chunks` | offset 默认0，0～3000；limit 默认20，1～20 | 按全篇 chunk_index 排序的预览和真实块 ID |
| POST `/papers/search` | JSON: query、可选 paper_id、limit | 本人 ready 论文的关键词候选；不调用模型 |
| GET `/papers/chunks/{chunk_id}` | offset 默认0，0～6000；max_bytes 默认12000，256～18000 | 段落正文、页码、位置、指纹、前后块序号和后续读取位置 |
| POST `/papers/{paper_id}/retry` | 无正文；query rebuild 默认false | 默认失败/过期处理重试；true 显式重建已有索引，旧块ID失效 |
| DELETE `/papers/{paper_id}` | 无请求正文 | 204，无 JSON 正文；原件和块一起删除 |

GET 下载使用 application/pdf，其余正常查询响应为 JSON。业务响应设置 `Cache-Control: no-store`。所有请求都限制归属；即使猜中其他用户真实 UUID，也不能查看或删除其内容。

## 4. 手动验证检索和原文

1. GET `/papers`，确认刚上传论文 status=ready。
2. POST `/papers/search`，填写论文实际存在的关键词。英文论文先用英文关键词，例如：

```json
{
  "query": "boundary supervision",
  "limit": 3
}
```

只检索某篇时加 `"paper_id": "该篇真实UUID"`。query 为2～180字符，limit 为1～5。可试“伪装目标检测”，前提是 PDF 真有对应中文文字；中文问题不会自动把库里的英文内容变成中文索引。

3. 复制命中项的 chunk_id，调用 GET `/papers/chunks/{chunk_id}`。搜索 preview 不是完整正文；本接口返回的 text 才是读到的有界内容。
4. 若 next_offset 不为 null，保持 chunk_id，把 offset 改为 next_offset 继续读。max_bytes 按 UTF-8 字节限制，offset 按规范化字符计数，不要自己按字节相加。
5. 要看下一块，用 GET `/{paper_id}/chunks` 查对应 chunk_id。工具调用也可以使用 `paper_id + chunk_index`；HTTP 读取路由使用 chunk_id。

原来已上传的1,000字符分块不会因为重启自动变大。需要用 POST `/{paper_id}/retry` 设置 **rebuild=true**，原件不变，重新解析并按paragraph-v2重建。重建期间暂不可搜索；活跃处理仍返回409。默认false继续保持ready只读重放行为。成功后旧chunk_id不可新读，之前聊天/建议证据快照保留。

`page_number` 是从1开始的物理页码。`chunk_index`、页内起点 page_char_start 和块内 offset 从0开始。文件 SHA 对应原件；content_sha256 对应整块规范化文字，不是本次被裁剪的短 text 的 SHA。该设计用于标记来源版本。

每块上限6,000字符，长页优先在段落/句末切分；跨页方法需要继续读取前后块。不是“6000字符必定覆盖整个模块”。具体规则、overlap和示例见 [切分与完整方法读取](论文段落切分与完整方法读取说明.md)。

## 5. 在聊天中验证 RAG

在已配置个人模型 Key 的用户下创建或进入一个聊天，使用原有聊天接口提交新问题和**新的** request_id：

```json
{
  "content": "请查看我上传的边界监督论文，先检索再读取相关正文，解释该方法并引用实际页码；证据不足请明确说明。",
  "request_id": "替换为本次新生成的UUID"
}
```

模型按需选择 list/search/read 工具，不强制每次闲聊都查库。多篇论文指代不清时应先澄清。使用之前已完成聊天 request_id 会重放旧回答，不会运行新的论文查询。

正常论文回答包含解释、实际引用的论文标题/物理页码/片段 ID/逐字原文，以及局限。只检索不读取时只能展示实际候选，不能得到“已验证全文”的结论。重新新建本人聊天仍可读同一论文，不需要再次上传。

可再测试：“根据上传论文分析我最近一次训练结果，并结合实际代码提出依据”。模型需依次读取已有运行/报告、源码和论文；如果缺报告、源码绑定或相关文字，应明确缺失。该只读分析不应启动训练，也不自动修改代码。

为避免私人原文进入联网搜索，实际读取私有 PDF 后本轮撤下公开联网工具。需要混合引用公开资料与私有论文时，先读公开资料再读 PDF；下一轮仍按新的实际工具证据验证，不能把旧片段冒充本轮已经读过。

模型的论文回答内部使用严格 JSON，前端/普通用户看到的是后端验证后生成的普通回答文本，无需自己提交 citations。引用验证不能代替人工审阅科学判断。

## 6. 验证独立建议 Agent

沿用已有分析/建议启用流程和 Worker 启动方式；新增论文后，不会自动重新生成已经完成的旧建议。针对尚待处理的建议任务，Worker 可以按需检索本人 PDF；证据不足的任务可通过既有接口显式重试。

查看保存的建议，PDF evidence 类型应为 `uploaded_pdf`，包含真实 paper_id、chunk_id、page_number、文件/片段 SHA、实际 text 和读取时间，url 为 null。每条具体建议仍需同时关联实际训练事实、代码行和逐字论文引文。没有相关论文或只找到摘要时不能冒充有正文依据。

## 7. 失败恢复和删除验证

| 场景 | 预期 |
| --- | --- |
| 无 Token / 无效 Token | 401；不允许读写本人知识库 |
| 另一用户访问某 paper_id/chunk_id | 404，不泄漏存在性 |
| 非 application/pdf、JSON 或无 PDF 文件头 | 415 |
| 请求参数不合规 / UUID 不合法 | 路由校验失败，按现有参数错误规范返回 |
| 原始文件超过10 MiB | 413；不索引 |
| 新增第3个同时处理的请求（同一进程） | 429，稍后重试 |
| 同上传 request_id 更换标题或文件 | 409；原记录保留 |
| 解析超时 / 无文字 / 加密 / 损坏 | 资源通常201创建但 status=failed，查看 error_code |
| 整体90秒超时 | 504；根据原 request_id 查原记录，不盲目反复创建 |
| 底层数据库等不可用 | 503“请稍后重试。”；细节查安全日志 |
| processing 活跃时 retry/delete | 409；等结束或180秒租约过期 |
| failed 或失联过期后 POST retry | 使用已保存原件重新解析；格式本身不支持则仍失败 |
| ready 再 retry | 返回同一份现有结果，不重做索引 |

依次验证原件下载、搜索、读块，然后 DELETE；204 没有正文。删除后上述详情/下载/块读取应404，搜索不再返回它。之前聊天或建议里的已保存证据是历史快照，仍然保留。删除聊天则不删除论文。

PDF 失败代码：INVALID_PDF（损坏/不支持）、ENCRYPTED_PDF（加密）、NO_EXTRACTABLE_TEXT（无文字）、PDF_LIMIT（页数/文字/资源限制）、PDF_TIMEOUT（子进程超时）、PARSER_UNAVAILABLE（解析器无法使用）、PROCESS_INTERRUPTED（处理被取消）。错误代码不等于程序崩溃，也不会清空其他论文。

## 8. 可重复自动验证

```powershell
python -m unittest tests.test_paper_rag
python -m scripts.verify_paper_rag_mysql
python -m scripts.export_api_docs
```

联调脚本新建随机临时数据库，执行真实 Windows PDF 子进程、MySQL 全文检索和 FastAPI 请求，结束删除**本脚本临时库**；模型、SSH 和公开资料为替身，不向真实供应商请求，不启动真实训练。测试账户、文档和 Key 都是明确的测试夹具，不是生产数据。

当前切分改进通过1,283项全量单元测试、17组论文联调；初版还通过公开研究11组、建议11组、对话训练10组回归，后者仅3次本机回环SSH微型CPU作业。本次没有训练或付费调用。未做真实Linux PDF运行验收、真实模型回答质量验收或完整部署压测。切分改进完整文件和验证见 [本次变更清单](论文切分改进阶段文件变更清单.md)。

接口快照：[openapi.json](api/openapi.json)、[接口索引](api/接口索引.md)。实现说明：[论文知识库与RAG模块说明](论文知识库与RAG模块说明.md)；全部新增/修改文件：[变更清单](论文知识库阶段文件变更清单.md)。
