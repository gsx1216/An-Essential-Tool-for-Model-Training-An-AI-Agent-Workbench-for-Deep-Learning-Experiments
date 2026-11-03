# 动态Skill接口与Swagger完整验证

日期：2026-10-28。当前67条路径、83个HTTP操作。新增3个GET操作，已有发送消息新增可选skill_id；聊天成功响应仍为 `{"content":"..."}`。

## 1. 升级和启动

```powershell
cd E:\code\LangChian_New
python -m pip install -r requirements.txt
python -m app.database --skill-schema
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

本阶段安装已执行增量迁移；上述命令用于以后重复部署。迁移仅确认chat_turns可空快照字段，不清空历史、不加载Skill、不连接模型或SSH。新库先沿用原完整初始化流程。程序升级后重启API；之后只更新Skill文字，新问题会读到新内容，不必为每次文字更新重启。

唯一新增显式依赖PyYAML==6.0.3，本机已有该版本。本阶段没有增加必填.env配置。打开 `/docs`，登录后点Authorize填写access_token；所有Skill操作都需要有效Token。

## 2. 先查可用目录

调用 **动态Skill → GET /skills**，无需个人模型Key。返回示意：

```json
{
  "items": [
    {
      "skill_id": "cod-experiment-analysis",
      "title": "伪装目标检测实验分析",
      "description": "结合真实训练报告、模型源码和论文原文，分析伪装目标检测实验并提出可验证的改进建议。",
      "version": "1.0.0",
      "sha256": "实际返回的64位摘要值"
    }
  ]
}
```

复制真实skill_id，不填写Swagger通用占位符string。调用 GET `/skills/{skill_id}` 可以查看同样元信息以及instructions正文。该目录由部署者批准后共享给登录用户，不提供POST上传接口。

## 3. 选择Skill发送一条问题

配置本人模型Key，创建或进入本人会话。调用原有：

```http
POST /sessions/<真实session_id>/messages
Authorization: Bearer <access_token>
Content-Type: application/json
```

请求正文：

```json
{
  "content": "请分析我最近一次伪装目标检测训练，先核对报告和实际代码，再结合论文给出有依据的消融建议；缺少资料请明确说明。",
  "request_id": "替换为本次新生成的UUID",
  "skill_id": "cod-experiment-analysis"
}
```

request_id的示例文字不能直接提交。可在本地执行 `python -c "from uuid import uuid4;print(uuid4())"`，复制完整UUID；前端将来使用crypto.randomUUID()。

这条请求选择分析流程，不表示训练。模型仍按需读取本人运行、报告、源码、论文；不存在的资料应说明不足。没有模型Key时新问题仍按原规则拒绝，不会因为选Skill而绕过供应商配置。

正常返回200和已保存的content。不需要客户端发送history、instructions、Skill版本、SQL密码或user_id。多余字段会422。

## 4. 查看这一轮真正用了哪个版本

调用新接口：

```http
GET /sessions/<同一session_id>/turns/<上面request_id>/skill
Authorization: Bearer <access_token>
```

request_id就是turn_id，不是消息seq。返回：

```json
{
  "turn_id": "真实轮次ID",
  "skill": {
    "skill_id": "cod-experiment-analysis",
    "title": "伪装目标检测实验分析",
    "description": "当时的说明",
    "version": "1.0.0",
    "sha256": "当时快照的摘要值",
    "instructions": "当时实际加载的完整Skill正文"
  }
}
```

这里读取MySQL的历史快照，不重新读取当前磁盘文件，因此Skill以后升级/移除仍可追溯。本人旧轮次或未选Skill时返回 `skill: null`。接口不调用模型，也不要求个人Key。

## 5. 三个新增接口和原接口扩展

| 方法与路径 | 输入 | 返回 / 作用 |
| --- | --- | --- |
| GET `/skills` | 无业务参数 | items元信息列表；无instructions |
| GET `/skills/{skill_id}` | 从目录选出的字符串ID | 当前部署版本、摘要值和正文 |
| GET `/sessions/{session_id}/turns/{turn_id}/skill` | 本人会话和轮次UUID | 当时冻结的Skill，或null |
| POST `/sessions/{session_id}/messages`（原有） | 原content/request_id，新增可选skill_id | 仍返回content；同一请求不能改变选择 |
| POST `/sessions/{session_id}/retry`（原有） | 无新增正文或Skill参数 | 未完成轮次按原快照重试 |

Skill ID最多64字符，需小写字母开头，只包含小写字母、数字和连字符；UUID仍接受现有标准形式或32位形式。业务响应设置no-store，身份始终来自Token。

## 6. 不选择、切换和重试

- 普通聊天：省略skill_id或传null，本轮不加载Skill。上一轮选过不等于下一轮自动选中；前端想持续选用时，每条新消息都提交该ID。
- 切换Skill：新问题生成新的request_id。复用旧编号却换Skill，或从选中改成null，返回409。
- 网络重发：保持同一个request_id、原content和原skill_id。已完成时直接返回原回复，不重复调用模型。
- 模型失败：调用原 `/sessions/ID/retry`，不新增“继续”，不再提交skill_id；也可以原封不动重发原请求。
- 失败后目录更新或文件删除：仍用数据库保存的原流程；不会自动替换成新版本。新的不同请求才加载当前目录。
- 删除个人Key：已完成请求仍可重放、快照仍可查询；新的模型生成或未完成重试仍需本人有效Key。

冻结的是Skill正文和元信息，后端权限检查、当前Key和全局安全策略仍以运行中的应用为准。

## 7. 建议的手工验收顺序

1. 不配置模型Key，先验证目录、详情都能查询。
2. 配置Key，在已有实际训练/论文资料的用户下选择Skill提问，检查回答是否区分事实与假设。
3. 用request_id查询轮次快照，核对ID、版本、正文和摘要值。
4. 下一条问题不传skill_id，确认新轮次快照为null。
5. 原请求换成其他Skill或null重发，确认409；原封不动重发应重放原回复。
6. 换另一个用户登录，访问前一个用户的轮次快照，应404。
7. 如果由部署者修改Skill版本，新问题应保存新版本；旧轮次仍显示旧版本。生产资料不要为了测试随意移除，版本变化/删除/模型失败的组合已由自动化临时夹具验证。
8. 选择Skill结合本人PDF提问，论文引用仍必须是实际读取的逐字引文和真实物理页码；查看工具审计，不接受只按流程名字编造的依据。

## 8. 错误与排查

| 场景 | 预期 |
| --- | --- |
| 无登录或Token失效 | 401 |
| skill_id不符合格式、UUID不合法、额外instructions/user_id | 422，沿用安全参数错误格式 |
| 目录没有该Skill | 404，新问题不会因此被保存 |
| 快照不是自己的或会话/轮次已删除 | 404，不泄漏其他用户记录 |
| 同请求编号改变Skill选择 | 409，提示为新选择生成新request_id |
| 同请求编号改变问题 | 原有409问题冲突提示 |
| Skill格式/大小错误，或快照损坏，或数据库不可用 | 固定503“请稍后重试。”；详细操作位置看安全日志 |
| 新生成缺少个人模型Key | 原有400，先配置本人Key |
| 上下文不足或模型超时等 | 仍按现有聊天413/502/504等规则处理，不绕过预算 |

日志不写正文和Key。需要查看具体使用的Skill，通过本人轮次快照接口；sha256是元信息和正文规范化JSON的摘要，不是SKILL.md原字节哈希。

## 9. 自动验证和文档导出

```powershell
python -m unittest tests.test_skills
python -m scripts.verify_skills_mysql
python -m scripts.export_api_docs
```

Skill联调使用随机临时MySQL库和临时Skill文件，覆盖版本修改/删除、重试、并发、事务回滚、越权和引文；结束仅删除脚本创建的临时库。模型/SSH均为替身，不启动训练或请求付费模型。

本阶段通过1,298项全量单元测试、14组Skill联调、17组论文联调及原有聊天HTTP回归。没有真实模型流程遵循质量验收、自动Skill选择或前端页面验收。

[模块与技术栈](动态Skill模块与轮次快照说明.md) · [完整文件清单](动态Skill阶段文件变更清单.md) · [从代码导出的接口索引](api/接口索引.md)。
