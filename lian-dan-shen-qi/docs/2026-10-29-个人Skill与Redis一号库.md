# 个人 Skill 与 Redis 1 号库

更新日期：2026-10-29。

## 功能与存储位置

个人 Skill 按用户隔离，正文按需缓存到 Redis **1 号库**，不设置过期时间，可在“资料与建议 → Skill 工作流”手动删除。MySQL 的 `personal_skills` 表仍保存持久记录。

实际缓存键：

```text
experiment_assistant:skill:v1:{user_id}:{skill_id}
```

例如用户 2 的某条个人 Skill：

```text
experiment_assistant:skill:v1:2:personal-0123456789abcdef0123456789abcdef
```

不同用户的键不同。客户端不能指定存储用户；后端从登录 Token 取得当前用户，所有查询、删除都限定 `user_id`。逻辑库号用来区分用途，用户隔离依靠身份校验、SQL 归属条件和带用户 ID 的键。

原有部署侧公共模板仍读取 `skills/{名称}/SKILL.md`，不写入个人缓存，普通用户不能删除公共模板。本次没有把个人 Skill 变成公共资源。

## 为什么仍保留 MySQL

Redis 也使用内存，读取还包含网络和序列化开销；进程内存读取通常更快。引入 Redis 的目的，是让多个后端进程读取同一用户的 Skill 时共用正文缓存，统一管理，而不是因为 Python 内存本身慢。

原实现也没有把所有 Skill 正文长期存成 Python 列表。`@lru_cache(maxsize=1)` 缓存的是目录服务、仓储和连接池对象，不是所有用户的 Skill 正文。本轮还改为直接在 SQL 中去掉列表的 `instructions`，前端创建成功后也只把摘要字段加入选择列表。

为保证删除和权限及时生效，缓存命中前仍会读取 MySQL 中该用户、该 Skill 的摘要值与存在性。**没有承诺每次完全不查 MySQL**；缓存节省的是完整正文的重复读取。这个小查询也防止 Redis 故障期间遗留的缓存让已经删除的 Skill 重新可用。

## 读取流程

```text
Token 确认当前用户
    ↓
确认个人 Skill 属于此用户且仍存在，读取保存的摘要值
    ↓
读取 Redis 1 号库中这个用户的对应键
    ├─ 命中且快照校验通过 → 使用正文
    └─ 未命中 / 损坏 / Redis 暂不可用 → 从 MySQL 读取正文
                                           ↓
                                  Redis 正常时回填，不设 TTL
```

Skill 创建时先提交 MySQL；第一次查看正文或用于新问题时按需填入 Redis。仅打开列表不会预加载所有正文。已有个人 Skill 无需迁移，首次使用会自动进入缓存。

MySQL 查询使用共享行锁，缓存读取及回填在该读事务结束前完成。删除获得排他行锁，因此不会出现“删除已经清掉缓存，较早读取又把永久缓存填回来”的情况。

缓存快照继续校验已有的 SHA-256、Skill 标识和长度限制。Redis 不能成为跨用户访问或跳过来源校验的途径。

## 删除流程与历史快照

1. 点击个人卡片“删除”，确认名称和历史保留范围。
2. 后端验证登录身份，以 `user_id + skill_id` 删除 MySQL 个人记录。
3. 提交成功后删除本人对应的 Redis 键。
4. 前端移除卡片、清除当前选中项并关闭对应正文抽屉。

历史聊天中已经使用的 Skill 冻结快照保留，仍可核对这一轮实际用了哪些指令。删除后不能再为新问题选择它；原轮次重试继续使用原先保存的快照，不从目录换成另一个版本。

Redis 不可用时，MySQL 读取和删除仍可完成，错误写入本机日志。若清理 Redis 失败，旧键可能暂留，但已经删除的 MySQL 记录会阻止它被使用；重复请求删除仍会尝试清理本人对应键，并对不存在的记录返回 404。本次不增加全库扫描或清空操作。

## 配置和查看方法

复用后端已有 `config/.env` 中的 `REDIS_URL` 的主机、端口、密码与 TLS 设置，**单独覆盖数据库号为 1**。不会将事件队列和聊天缓存一起移动到 1 号库，不需要另设一个 Redis 密码，也不新增运行时依赖。

缓存用 `SET key value` 保存，不传 `EX` 或 `PX`。如果原来同名键带有 TTL，普通 SET 会去掉该键 TTL。检查时：

```text
SELECT 1
SCAN 0 MATCH experiment_assistant:skill:v1:2:* COUNT 100
TTL 这里填实际键名
```

`TTL = -1` 表示存在且不设过期时间；`-2` 表示键不存在。SCAN 可能返回非零游标，需要按返回游标继续查看。不要对已有业务库执行 FLUSHDB。

## 接口文档

现有接口保持：

| 方法与路径 | 作用 |
| --- | --- |
| `POST /skills` | 创建本人的个人 Skill，参数为 `title` 和 `instructions` |
| `GET /skills` | 返回部署模板和本人个人 Skill 的摘要，不包含正文 |
| `GET /skills/{skill_id}` | 按需读取正文、版本和摘要值 |
| `GET /sessions/{session_id}/turns/{turn_id}/skill` | 查看本人历史轮次的冻结快照 |

新增删除接口：

```http
DELETE /skills/{skill_id}
Authorization: Bearer 登录凭证
```

无请求体，不接受客户端传入 `user_id`。

| 状态码 | 含义 |
| --- | --- |
| 204 | 已删除，无响应正文 |
| 401 | 未登录或登录已失效 |
| 404 | 个人 Skill 不存在、不属于当前用户，或尝试删除公共部署模板 |
| 422 | Skill 标识格式不符合要求 |
| 503 | 持久存储或配置暂不可用，稍后重试 |

OpenAPI 快照与前端接口类型已同步。Redis 故障是可降级的缓存问题，不单独把数据库已成功的删除变成失败。

## 模块与技术栈

| 文件 | 作用 |
| --- | --- |
| 后端 `app/services/redis_skill_cache.py`（新增） | redis-py 同步连接池、固定 DB1、用户键、不设 TTL、缓存故障降级 |
| `app/repositories/personal_skill_repository.py` | MySQL 持久化、归属检查、共享锁读取、事务删除及缓存协作 |
| `app/services/skill_catalog.py` | 区分部署模板与个人 Skill，新增个人删除入口 |
| `app/api/skill_routes.py` | FastAPI 删除路由，复用 Token 依赖；用 `asyncio.to_thread` 执行同步仓储 |
| `app/api/dependencies.py` | 组装服务与连接池，不保存某个用户的数据 |
| `app/config.py` | 从现有后端环境文件读取 Redis 连接配置 |
| `sql/get_personal_skill_identity.sql`（新增） | 按当前用户读取摘要值，并持有共享行锁 |
| `sql/delete_personal_skill.sql`（新增） | 按用户和 Skill 双条件删除 |
| `sql/list_personal_skills.sql` | MySQL JSON_REMOVE 去掉正文，只返回元数据 |
| 前端 `src/api/index.ts` | 新增 `deleteSkill` 接口调用 |
| `src/views/LibraryView.vue` | 确认删除、同步选择状态、按需查看正文 |

沿用 Python、FastAPI、Pydantic、mysql-connector、redis-py；前端使用 Vue 3、TypeScript、Element Plus，测试使用 unittest、Vitest。不增加新表或新依赖。

## 已完成验证

- 后端相关 33 项测试通过：Redis 库号覆盖、用户键、无 TTL、归属校验、缓存异常、事务删除、登录与公共模板保护、原有快照及重试逻辑。
- 真实 Redis + 临时 MySQL + HTTP 联调通过：TTL=-1、越权读取与删除拒绝、元数据列表、并发删除后缓存不复活、持久记录清除、历史快照保留、残留缓存不能绕过删除。
- 前端 47 项测试通过，Lint、生产构建和类型检查通过，92 处接口路径/方法与后端快照一致。
- API 已重启加载新接口；未启动训练、SSH 或真实模型分析。验证只清理脚本创建的随机测试键与临时数据库，没有删除用户实际 Skill。

复验命令：

```powershell
cd E:\code\LangChian_New
python -m unittest tests.test_skill_cache tests.test_skills tests.test_pdf_recovery_personal_skills -q
python -m scripts.verify_personal_skill_cache
```

完整新增、修改文件见 [文件清单](05-新增与修改文件清单.md)。
