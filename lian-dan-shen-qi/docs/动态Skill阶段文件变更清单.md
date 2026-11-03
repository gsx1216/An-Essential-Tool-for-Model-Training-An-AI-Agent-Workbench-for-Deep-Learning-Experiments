# 动态Skill阶段：文件变更与验证

日期：2026-10-28。所有路径相对于 `E:\code\LangChian_New`。

新增 **14** 个文件、修改 **25** 个文件、删除0个文件。实际 `.env`、用户原问题和既有Skill安装目录未修改。新增显式依赖PyYAML==6.0.3（本机已安装）；没有新增表、Redis Stream、常驻Worker或付费服务。

当前67条路径、83个HTTP操作、39张共享业务表。仅chat_turns增加可空skill_snapshot_json，保存当轮实际流程、版本和规范化SHA。新增应用目录skills/，与Codex个人Skill目录分离。

## 完成与验证

- 手动查询/选择Skill，先加入系统提示词再计算预算；未选择保持普通聊天，原问题正文不被改写。
- 问题、pending轮次和快照同事务提交；重复请求必须保持问题和选择。并发锁内使用当前读，防止旧事务快照漏掉刚提交的选择。
- 文件升级/删除后，完成重放与失败重试继续使用原快照；本人快照查询不依赖个人Key，删除聊天级联清理快照。
- Skill不增加工具权限，不执行文件中的命令，实际PDF逐字引用/页码核验继续生效。

验证通过：**1,298项全量单元测试**（新增Skill专项15项）；**14组真实MySQL/HTTP Skill联调**；**17组论文联调**；原有聊天HTTP全链回归（发送/重试/归属/幂等/密钥变更/故障回滚/日志隐私）。新Skill与真实Windows PDF解析同时运行的场景也通过。

全部模型为替身，没有付费调用、SSH或真实训练。没有进行真实供应商流程遵循质量、自动推荐、后台Agent选Skill或前端验收；不把模拟模型通过解释为研究建议必定正确。

安装逐文件核对原字节，遇到用户并发修改不覆盖。仅复制本清单；执行可重复的 --skill-schema 确认列/约束，旧轮次保留NULL，不重写旧消息。

## 新增文件

| 文件 | 职责 |
| --- | --- |
| `app/api/skill_routes.py` | 登录后查询共享目录/详情，以及本人会话轮次冻结快照 |
| `app/models/skill.py` | 元信息/摘要/冻结快照、规范化SHA验证、提示词包装、响应和业务异常 |
| `app/services/skill_catalog.py` | 固定部署目录下动态读取SKILL.md，用safe_load和严格元信息模型解析纯文字 |
| `docs/动态Skill接口与Swagger验证说明.md` | 三个GET和聊天字段、实际验证流程、错误码和部署命令 |
| `docs/动态Skill模块与轮次快照说明.md` | 框架、技术栈、模块作用、登录/SQL/预算/重试/工具权限完整流程 |
| `docs/动态Skill阶段文件变更清单.md` | 本文件：全部新增修改及验证范围 |
| `scripts/verify_skills_mysql.py` | 14组真实临时MySQL/HTTP验收，覆盖版本重试、并发/回滚/归属、权限和真实PDF组合 |
| `skills/cod-experiment-analysis/SKILL.md` | 伪装目标检测分析示例流程，连接真实报告、源码、论文和消融方案 |
| `sql/add_chat_turn_skill.sql` | 旧库增量增加可空JSON正文快照及12 KiB容量/类型CHECK |
| `sql/chat_turn_skill_column.sql` | 检查已有列以支持重复迁移 |
| `sql/get_turn_skill.sql` | 按用户/会话/轮次及可选有效attempt读取快照；写事务中用当前锁定读 |
| `sql/inspect_chat_skill_request.sql` | 一条查询核对归属、原问题、完成回复和冻结选择 |
| `sql/save_turn_skill.sql` | 创建pending轮次后，同事务保存其不可变Skill选择 |
| `tests/test_skills.py` | 15项目录/格式/长度/SHA、每轮选择、重放、预算和未改原问题的专项测试 |

## 修改文件

| 文件 | 修改内容 |
| --- | --- |
| `app/api/chat_routes.py` | 传递选中的skill_id，安全映射缺失/选择冲突/配置和快照错误，保留原问题冲突提示 |
| `app/api/dependencies.py` | 组装无用户状态的Skill目录，聊天注入，快照查询独立于模型凭据 |
| `app/api/schemas.py` | 发送消息增加可选skill_id和Swagger说明，保留两项必填和禁止额外字段 |
| `app/database.py` | 完整初始化及 --skill-schema 增量确认轮次快照列，不修改旧消息或触发模型 |
| `app/main.py` | 挂接三个Skill只读操作 |
| `app/repositories/message_repository.py` | 一笔事务保存问题与选择；重放、并发锁下选择一致性、归属/attempt及SHA读取 |
| `app/services/chat_context_factory.py` | 将本轮Skill放入系统提示词后计入上下文预算，不污染消息缓存 |
| `app/services/chat_service.py` | 领取后读取冻结快照用于生成/重试，沿用现有消息、失败及网关清理流程 |
| `app/services/user_chat_service.py` | 先核对原请求和冻结选择；新请求才加载当前目录，完成重放不读文件或个人Key |
| `docs/API接口总览与前端接入约定.md` | 更新67路径/83操作/39表，约定选择与request_id、重试及前端持续选择行为 |
| `docs/api/openapi.json` | 实际路由导出的新Skill类型、聊天可选字段和接口响应 |
| `docs/api/接口索引.md` | 实际路由导出的67路径/83操作列表 |
| `docs/系统总体架构图与模块分层说明.md` | 更新当前架构节点、表字段与第24节Skill链路，标明历史规划 |
| `requirements.txt` | 明确依赖PyYAML==6.0.3，指定本机环境已具备，无需新增服务 |
| `scripts/_chat_api_test_app.py` | 临时库包含快照字段和Skill路由，可显式注入临时Skill目录；旧场景默认不选Skill |
| `scripts/verify_chat_api_mysql.py` | 修正旧脚本对完成重放/冲突/越权应新建空网关的过期断言；验证实际无新增网关行为 |
| `sql/create_chat_turns.sql` | 新库初始定义包含可空Skill快照，不另建表 |
| `tests/test_analysis.py` | 完整初始化测试替身加入新的Skill迁移步骤，不弱化原业务断言 |
| `tests/test_chat_api_models.py` | 确认原两项必填不变，新增字段为可选，继续保留原安全字段校验 |
| `tests/test_context_storage_migration.py` | 完整初始化测试替身加入新的Skill迁移步骤，不弱化原业务断言 |
| `tests/test_evaluation.py` | 完整初始化测试替身加入新的Skill迁移步骤，不弱化原业务断言 |
| `tests/test_experiment_migration.py` | 完整初始化测试替身加入新的Skill迁移步骤，不弱化原业务断言 |
| `tests/test_experiment_run_migration.py` | 完整初始化测试替身加入新的Skill迁移步骤，不弱化原业务断言 |
| `tests/test_model_credential_foundation.py` | 完整初始化测试替身加入新的Skill迁移步骤，不弱化原业务断言 |
| `tests/test_turn_lifecycle.py` | 完整初始化测试替身加入新的Skill迁移步骤，不弱化原业务断言 |

阅读顺序：[模块与技术栈](动态Skill模块与轮次快照说明.md) → [接口与Swagger](动态Skill接口与Swagger验证说明.md) → [系统总体架构图](系统总体架构图与模块分层说明.md)。
