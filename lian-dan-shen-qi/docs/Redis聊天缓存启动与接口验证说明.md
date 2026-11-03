# Redis 聊天缓存：启动、接口与验证

日期：2026-10-27。

## 1. 如何启动

先激活现有 langchain 环境，在 `E:\code\LangChian_New` 运行。没有新安装步骤，没有数据库迁移步骤。

如果你的 Redis 已在服务器环境变量中配置好 REDIS_URL：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

如果本机 Redis 有密码、又不希望把密码保存到文件：

```powershell
python -m app.serve --prompt-redis-password
```

输入的是 Redis 服务密码，不是 MySQL 密码，也不是系统登录密码。输入不回显，仅对这次 API 进程生效，不写入 .env，不放在命令行参数。这个入口没有自动启用 reload，修改代码后手动停止并重启。

不要同时用两条命令启动到相同端口。API 已在运行时，先停止旧进程再使用新代码启动。只启动 API 不会自动启动训练监控、事件投递或事件接收 Worker；它们仍按原文档独立启动。

如果未提供 Redis 所需认证，缓存会记录脱敏告警并退回 MySQL，聊天仍可用，但这不表示 Redis 缓存已经命中。

## 2. 配置

只修改示例配置并不会自动写入本机真实 .env。可以通过部署环境设置：

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| REDIS_URL | redis://127.0.0.1:6379/0 | 与事件模块复用 Redis 地址；需要认证时按部署方式传入 |
| CHAT_CONTEXT_CACHE_ENABLED | true | false 时完全绕过缓存 |
| CHAT_CONTEXT_CACHE_PREFIX | experiment_assistant | 项目/环境隔离前缀；生产与测试应区分 |
| CHAT_CONTEXT_CACHE_TTL_SECONDS | 604800（7 天） | 通过数据库验证后回填、已提交摘要/回复更新时续期；范围 60～604800 秒 |
| CHAT_CONTEXT_CACHE_MAX_ENTRY_BYTES | 1048576 | 1 MiB；加密后的实际条目也受限，超出时跳过缓存 |
| CHAT_CONTEXT_CACHE_FAILURE_BACKOFF_SECONDS | 10 | 故障后暂时绕过 Redis 的时间 |
| MODEL_CREDENTIAL_ENCRYPTION_KEY | 现有固定部署主密钥 | 已有个人凭据使用的配置；派生缓存专用密钥，不自动更换 |

Redis URL 和主密钥仅在后端配置。没有新增让普通用户提交 Redis/MySQL 管理员密码的接口。

## 3. 接口契约

没有新增或删除 HTTP 操作，没有修改请求和响应字段，OpenAPI 应与本阶段之前一致。现有 39 条路径、49 个 HTTP 操作保留。

| 接口 | 缓存关系 |
| --- | --- |
| POST /sessions/{session_id}/messages | 先持久化问题；加载/校验缓存上下文；回复提交后更新缓存 |
| POST /sessions/{session_id}/retry | 复用原待回复问题；缓存不能绕过本次生成尝试校验 |
| GET /sessions/{session_id}/messages | 继续通过原 MySQL 分页历史接口展示完整原文，不从模型上下文缓存冒充完整历史 |
| DELETE /sessions/{session_id} | 删除本人会话与原文后清理缓存正文，写入短期墓碑 |

Token 仍由 require_current_user 验证。所有受保护会话操作仍检查所属用户。缓存未命中/Redis 故障不新增 HTTP 错误；真正的 MySQL 故障、会话不存在、越权、轮次冲突、模型错误仍使用原安全错误响应。

## 4. 在 /docs 里如何验证

1. 注册/登录，复制 Token 并 Authorize；配置本人 DeepSeek Key。手动发送给真实模型可能产生供应商费用，本阶段自动验收没有调用真实模型。
2. POST /sessions 创建新聊天，复制返回的 session_id。不要手填另一个用户的 ID。
3. POST /sessions/{session_id}/messages 提交问题。第一次预期缓存未命中，日志记录 load_context，回复成功后缓存到 AI 消息末尾。
4. 同一会话发送第二个问题，使用新的 request_id。日志应出现“上下文缓存命中”及“上下文缓存数据库校验 mode=delta”。这证明补齐新增问题，而不是重读整段历史。
5. 重发完全相同的 request_id 和正文，应返回已保存回复，不新增消息、不再次调用模型。
6. GET 消息历史，完整 human/ai 都应存在；摘要不会删除原文。
7. 想测试自然过期，可在环境中把 TTL 设为 60 后重启 API；聊天后超过 60 秒不操作，再发送问题，应自动从 MySQL 重建缓存。
8. DELETE 会话，再 GET 历史或发送问题应 404，不允许利用旧缓存继续访问。

完整示例请求体仍是：

```json
{
  "content": "请根据我们前面讨论的训练结果继续分析",
  "request_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

这个 UUID 仅是示例。每次新提问应生成新的 request_id；网络重试同一提问时复用原值。

## 5. 自动化验证

新增缓存单元测试：

```powershell
python -m unittest tests.test_redis_context_cache
```

完整单元回归：

```powershell
python -m unittest discover -s tests
```

真实 HTTP/MySQL/Redis 缓存联调：

```powershell
python -m scripts.verify_context_cache_mysql_redis
```

联调要求可用 MySQL 管理员测试权限以及 PATH 中存在 redis-server。示例本机 Redis 可执行程序位于 D:\Redis，可以只在当前终端临时补入 PATH，不必修改现有服务。脚本创建并仅清理自己随机命名的临时数据库和独立随机端口 Redis；不操作生产聊天、已有 Redis 数据或现有 Redis 服务配置。模型网关全部为替身，不调用真实模型、SSH 或 GPU。

本阶段验证结果与全部文件清单见 [Redis聊天缓存阶段文件变更清单](Redis聊天缓存阶段文件变更清单.md)。

## 6. 运维观察

正常可观察：缓存命中/未命中、数据库校验 mode=delta/unchanged/reload、tail_seq、summary_version。

告警“上下文缓存暂时绕过 ... fallback=mysql”表示缓存发生网络、认证、格式或配置问题，需在后端排查。日志不包含 Redis 连接 URL、密码、完整消息或 API Key。不要使用 MONITOR 等方式把实际敏感内容复制到普通用户界面。

缓存正文是密文，Redis 工具直接查看 payload 不会显示对话原文；聊天可视化仍使用原历史接口。这与训练权重可见目录是不同问题，权重保存位置不受本阶段影响。
