# Redis 聊天缓存阶段文件变更清单

后续调整：用户要求缓存保留 7 天，已将默认 TTL 及校验上限调整为 604800 秒；该次新增/修改文件和验证见 [7 天过期调整说明](Redis聊天缓存7天过期调整说明.md)。以下 23 文件清单保留初次缓存接入阶段范围。

日期：2026-10-27。项目：`E:\code\LangChian_New`。

本阶段共 23 个文件：新增 9 个，修改 14 个。真实 `config/.env`、生产聊天与训练数据、已有 Redis 配置均未修改。无新依赖、无新表、无数据库迁移。

## 新增文件

- [app/models/context_cache.py](../app/models/context_cache.py)
- [app/serve.py](../app/serve.py)
- [app/services/redis_context_cache.py](../app/services/redis_context_cache.py)
- [docs/Redis聊天上下文缓存模块与完整流程说明.md](../docs/Redis聊天上下文缓存模块与完整流程说明.md)
- [docs/Redis聊天缓存启动与接口验证说明.md](../docs/Redis聊天缓存启动与接口验证说明.md)
- [docs/Redis聊天缓存阶段文件变更清单.md](../docs/Redis聊天缓存阶段文件变更清单.md)
- [scripts/verify_context_cache_mysql_redis.py](../scripts/verify_context_cache_mysql_redis.py)
- [sql/load_context_marker.sql](../sql/load_context_marker.sql)
- [tests/test_redis_context_cache.py](../tests/test_redis_context_cache.py)

## 修改文件

- [app/api/dependencies.py](../app/api/dependencies.py)
- [app/config.py](../app/config.py)
- [app/main.py](../app/main.py)
- [app/repositories/context_repository.py](../app/repositories/context_repository.py)
- [app/services/chat_context_factory.py](../app/services/chat_context_factory.py)
- [app/services/chat_service.py](../app/services/chat_service.py)
- [app/services/chat_session_service.py](../app/services/chat_session_service.py)
- [app/services/context_service.py](../app/services/context_service.py)
- [app/services/user_chat_service.py](../app/services/user_chat_service.py)
- [config/.env.example](../config/.env.example)
- [docs/API接口总览与前端接入约定.md](../docs/API接口总览与前端接入约定.md)
- [docs/系统总体架构图与模块分层说明.md](../docs/系统总体架构图与模块分层说明.md)
- [docs/项目进度与前后端开发路线.md](../docs/项目进度与前后端开发路线.md)
- [scripts/_chat_api_test_app.py](../scripts/_chat_api_test_app.py)

## 验证结果

- 1,123 项单元测试通过，其中新增缓存相关单元测试 22 项。
- 新增真实 HTTP / MySQL / 独立临时 Redis 5.0.14.1 缓存验收 19 组通过。
- 原有上下文真实 HTTP / MySQL 回归 16 组、Redis Streams 事件通信回归 15 组通过；合计 50 组联调。
- 多用户隔离、密文篡改、TTL 过期、增量读取、摘要更新、模型失败重试、重复请求、回复落库后缓存故障、MySQL 故障、并发旧值覆盖、超过 2^53 的序号比较、删除墓碑、缓存关闭与超大条目跳过均已覆盖。
- 实际关闭端口的 TCP 连接失败、Redis 错误认证均确认可降级。
- 实际 app.openapi() 与原快照完全相同，39 条路径、49 个 HTTP 操作，无前端字段变更。
- 网关为模拟对象，无付费模型调用、无真实 SSH/GPU 训练；临时数据库和临时 Redis 退出后清理。

验收不代表生产并发压力、真实模型回答质量、Kubernetes 部署或独立分析/建议 Agent 已完成。

## 阅读顺序

1. [缓存模块与完整流程](Redis聊天上下文缓存模块与完整流程说明.md)。
2. [启动、Swagger 与接口验证](Redis聊天缓存启动与接口验证说明.md)。
3. [总体架构](系统总体架构图与模块分层说明.md)。
