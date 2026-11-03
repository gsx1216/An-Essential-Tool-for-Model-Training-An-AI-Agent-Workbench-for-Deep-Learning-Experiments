# Redis 聊天缓存：改为 7 天过期

日期：2026-10-27。按用户要求，默认过期时间从 30 分钟改为 7 天。

配置：`CHAT_CONTEXT_CACHE_TTL_SECONDS=604800`。默认值与允许上限都为 604800 秒，最小仍为 60 秒，便于测试。MySQL 持久化、Redis 故障降级、摘要和增量读取逻辑不变。

有效读取通过 MySQL 校验后回填缓存，以及成功写入摘要或模型回复后，缓存都会重新获得 7 天 TTL。不是从会话首次创建开始只保留固定 7 天。长时间不访问导致缓存过期后，仍可从 MySQL 恢复历史。

重启 API 后使用新默认值。已经存在的缓存不会仅因修改代码而自动延长；下次使用并成功更新后按 7 天续期。本机真实 .env 未显式设置该 TTL，无需改写真实 .env。其他部署若已显式设置旧值，需同步调整该环境配置并重启。

本次不批量修改现有 Redis 键，不改事件 Stream 的保留策略，不启动训练或调用真实模型。

## 修改文件（8 个）

- [app/models/context_cache.py](../app/models/context_cache.py)：默认 TTL 和校验上限。
- [config/.env.example](../config/.env.example)：示例配置。
- [tests/test_redis_context_cache.py](../tests/test_redis_context_cache.py)：现有测试增加 7 天默认值与超上限校验。
- [scripts/verify_context_cache_mysql_redis.py](../scripts/verify_context_cache_mysql_redis.py)：检查实际 Redis TTL 接近配置值，而非仅大于零。
- [缓存模块与完整流程](Redis聊天上下文缓存模块与完整流程说明.md)：更新默认 TTL。
- [缓存启动与接口验证](Redis聊天缓存启动与接口验证说明.md)：更新配置说明。
- [系统总体架构](系统总体架构图与模块分层说明.md)：更新缓存保留时间。
- [缓存阶段变更清单](Redis聊天缓存阶段文件变更清单.md)：链接本次调整记录。

## 新增文件（1 个）

- 本说明文档。

## 验证

22 项缓存单元测试通过；使用独立临时 Redis 验证首次写入 TTL=604800 秒，缩短已有键 TTL 后再次写入续期也为 604800 秒。没有调用模型或修改现有 Redis 数据。
