# 服务器连接配置与 SSH 工具基础说明

日期：2026-10-25。项目：`E:\code\LangChian_New`。本阶段采用你最后确认的**私钥登录**，同时兼容 SSH 密码登录。两种方式都必须填写远程用户名。本文记录服务器连接阶段；后续 Runner 已完成本地验证，最新能力见 [Runner执行与状态日志说明](Runner执行与状态日志说明.md)。

## 1. 现在完成到哪里

已经实现本人服务器配置的创建、分页查询、详情、删除，以及 SSH 连接和认证测试。新增共享表 `server_connections`，每条配置通过 `user_id` 归属用户；没有为每个用户创建独立表。

创建配置只保存信息，不连接服务器；点击测试才发起连接。普通私钥、带口令的私钥和密码三条路径均经过本地真实 SSH 协议验证。测试没有调用模型、执行远程命令或启动训练，也没有连接你的真实训练服务器。

整体最终流程如下，虚线之后仍待实现：

```text
用户登录 → 保存自己的服务器配置 → 主机指纹与 SSH 认证测试
                                           ↓
------------------------------------------- 后续阶段
聊天 → 模型判断是否需要工具 → 后端校验身份、工具参数与执行策略
     → Runner 绑定 run_id 和本人 server_id → 启动已登记脚本
     → 状态、日志、指标与结果持久化 → 模型基于工具结果回答
     → 研究检索与证据引用 → 修改提案、实际 diff、验证运行
```

普通聊天不会因为多了这些接口就自动训练。尚未给模型绑定 `start_run` 等工具。后续 Runner 阶段已用 run_executions 关联运行和服务器，并实现受控执行入口；创建配置和连接测试本身仍不会启动训练。

## 2. 先理解三种不同的凭据

| 内容 | 证明什么 | 在哪里使用 |
| --- | --- | --- |
| 系统登录 Token | 你是本系统的哪个用户 | `require_current_user` 查询登录记录 |
| SSH 用户私钥及可选口令，或 SSH 密码 | 你能否登录指定服务器账号 | AsyncSSH 认证；加密保存在数据库 |
| 服务器主机公钥指纹 | 对端是不是你预期的服务器 | SSH 握手时固定 SHA256 指纹核验 |

私钥登录是“不输入服务器账号的密码”，仍然要填 `username`。`passphrase` 是解锁私钥的口令，和服务器账号密码不同。没有口令的私钥可省略该字段或传 `null`。本次没有增加匿名登录配置，也不会偷偷使用运行后端的 Windows 用户名、SSH agent 或本地默认私钥。

另外，`MODEL_CREDENTIAL_ENCRYPTION_KEY` 是部署者持有的固定 Fernet 主密钥，名字来自早期模型凭据功能，**不是 DeepSeek API Key**。本阶段复用它加密 SSH 凭据，但使用独立用途标记和绑定信息，使模型 Key 密文不能直接当作 SSH 凭据。

## 3. 接口及调用顺序

| 方法与路径 | 用途 | 成功状态 |
| --- | --- | --- |
| `POST /servers` | 保存自己的配置，返回服务器 UUID | 201 |
| `GET /servers?limit=20` | 本人的有界分页列表 | 200 |
| `GET /servers/{server_id}` | 本人的单条公开配置 | 200 |
| `DELETE /servers/{server_id}` | 删除自己的配置 | 204，无正文 |
| `POST /servers/{server_id}/test` | 验证网络、主机身份和 SSH 认证 | 200 |

五个操作都依赖登录。客户端不能提交 `user_id`、训练命令或任意附加字段。查询返回主机、端口、用户名等公开配置，不返回密码、私钥、口令或数据库密文。

列表默认 20 条，最大 100 条；按照 `created_at DESC, id DESC` 排序。下一页使用上一页的 `next_cursor` 作为 `before_id`。游标也必须属于当前用户。当前没有更新接口，未使用配置可删除后重建；Runner 已绑定的配置受数据库外键保护，删除返回 409，避免破坏查询与恢复。

## 4. 如何在 /docs 里验证

在已激活的 langchain 环境、项目根目录运行：

```powershell
Set-Location E:\code\LangChian_New
python -m app.database --server-schema
python -m uvicorn app.main:app --reload
```

`--server-schema` 仅创建本阶段新表，需要已有 `users` 表；第一次初始化整个项目才使用原来的全量初始化方式。重复执行不会删除已有服务器配置，不会清空聊天或运行记录。

打开 `http://127.0.0.1:8000/docs`，先登录取得 Token，再通过 Authorize 授权。在“训练服务器”分组使用 `POST /servers`。

私钥请求体示意如下；IP、用户名、指纹、目录和私钥均要换成自己的真实值，这段占位内容不能直接用于连接：

```json
{
  "name": "我的训练服务器",
  "host": "203.0.113.10",
  "port": 22,
  "username": "trainer",
  "auth_method": "private_key",
  "host_fingerprint": "替换为可信渠道获得的SHA256主机指纹",
  "work_dir": "/home/trainer/experiments",
  "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n替换为完整私钥内容\n-----END OPENSSH PRIVATE KEY-----\n",
  "passphrase": null
}
```

这里提交的是**私钥文本内容**，不是你电脑上的文件路径，也不是 `.pub` 公钥；JSON 中的换行写成 `\n`。创建时先做格式和长度校验，完整的私钥解析、口令核对以及远端是否接受该密钥，在测试连接时完成。因此“保存成功”不等于“认证成功”。

密码登录时改为 `"auth_method": "password"`，传 `"password": "你的SSH密码"`，不传 `private_key` 和 `passphrase`。不要把真正的凭据复制到聊天、截图或提交到 Git。公开部署通过 HTTPS 传输；`SecretStr` 和数据库加密都不能代替 HTTPS。

保存后复制返回的 `server_id` 到测试接口执行。成功响应包含 `connected: true`、`fingerprint_verified: true`、认证方式、检查时间和耗时。成功只说明当时 SSH 身份与认证通过，尚未检查工作目录存在、CUDA 可用、GPU 空闲或训练脚本能够运行。

## 5. 部署者必须配置允许连接的 IP

普通用户不能通过接口修改后端环境变量。部署者在服务器环境或现有 `config/.env` 里配置允许的训练服务器网段，例如：

```dotenv
SSH_ALLOWED_NETWORKS=203.0.113.10/32
SSH_TEST_TIMEOUT_SECONDS=15
SSH_MAX_CONNECTIONS=4
```

示例 IP 只是文档占位，需换成真实训练服务器 IP。多个网段用英文逗号分隔。IPv4 单主机用 `/32`，IPv6 单主机用 `/128`；较大网段必须是规范 CIDR 网络地址。第一版仅接收 IP，不接收域名。

未设置 `SSH_ALLOWED_NETWORKS` 时允许保存配置，但连接测试返回 403，不向目标发起 SSH。私有地址或回环地址也只有明确在允许范围内才可连接。配置读取后服务对象会缓存，变更后重启进程生效；没有自动改写你的 `.env` 或允许任意公网连接。

超时默认 15 秒，可设 1～60；连接测试名额默认每进程 4 个，可设 1～32。名额满时立即返回 429。它不是 Redis 分布式限流，多个 Uvicorn worker 或 Kubernetes 副本各有自己的名额。

## 6. 如何取得服务器主机指纹

通过可信的服务器控制台或管理员渠道核对主机公钥。例如服务器确实使用 Ed25519 主机密钥时，在服务器控制台执行：

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

复制输出中 `SHA256:...` 这一段，不包括位数、注释和算法名。指纹来自**服务器主机公钥**，不是用户登录用的私钥或其公钥。实际服务器密钥类型、文件路径和协商到的主机密钥必须一致；如果服务端使用其他主机密钥，需要核对对应指纹。不能把未经核实的网络扫描结果直接当成身份依据。[OpenSSH ssh-keygen 官方手册](https://man.openbsd.org/ssh-keygen) 说明了 `-l`、`-f` 和 `-E` 选项。

实现使用独立空信任库及指纹回调，不读取当前电脑 `known_hosts`，也没有使用 `known_hosts=None` 关闭主机验证。连接前明确关闭默认 SSH 配置、默认私钥发现和 agent 回退。这些选项依据 [AsyncSSH 官方 API](https://asyncssh.readthedocs.io/en/stable/api.html) 配置，并有本地握手测试覆盖。

## 7. 一次测试请求内部如何流转

```text
POST /servers/{server_id}/test
  → require_current_user：提取 Bearer Token，查有效登录记录，得到当前 user_id
  → get_server_connection_service：复用仓储、凭据加密器及 SSH 适配器
  → ServerConnectionService.get：校验 ID，在线程中读取本人配置
  → ServerCredentialCipher.decrypt：核对用途、用户和配置绑定，再取得临时明文
  → SSHConnector.test_connection：检查部署允许网段、占用连接名额
      → 私钥模式在线程中解析私钥和口令
      → 异步连接、核对服务器主机指纹、完成认证
      → 关闭连接，finally 释放名额
  → finally 释放临时凭据字典中的引用
  → 再查一次本人配置：避免连接期间已删除却仍声称配置可用
  → 返回明确的连接测试结果
```

前一次查询是为取得配置和校验归属；后一次查询是应对网络等待期间配置被删除，解决的是不同时间点的问题。数据库事务不会跨整个 SSH 网络等待持锁。

`async def` 表示异步函数，调用后需要 `await` 等待其结果。AsyncSSH 的连接本身异步；MySQL 驱动和私钥解析是同步函数，通过 `await asyncio.to_thread(函数, 参数...)` 放入工作线程，避免在事件循环中直接阻塞。

连接及认证等待受超时控制；取消和异常时会关闭已经取得的连接。`to_thread` 的超时不能强行杀掉正在解析私钥的 Python 工作线程，因此这里不能宣称实现了任意 CPU 工作的硬超时或完整生产级抗滥用隔离。

## 8. 数据库和加密具体存什么

| 列 | 含义 |
| --- | --- |
| id | 后端生成的 32 位十六进制 UUID |
| user_id | 所属系统用户，外键关联 users.id |
| name | 用户给配置起的名称 |
| host / port / username | SSH 目标与账号 |
| auth_method | private_key 或 password |
| host_fingerprint | 预先核实的服务器主机 SHA256 指纹 |
| work_dir | Linux 绝对工作目录；当前仅保存并检查格式 |
| encrypted_credentials | 密码或私钥及口令组成的 Fernet 密文 |
| created_at | UTC 创建时间 |

`(user_id, created_at DESC, id DESC)` 索引用于本人的稳定分页；用户外键和端口、认证方式及密文长度 CHECK 提供数据库约束。创建使用参数化 `INSERT ... SELECT users`，用户不存在就不能插入，再用事务提交和回读确认。

凭据密文内部包含版本、独立用途标记、user_id，以及服务器 ID、IP、端口、用户名、认证方式、主机指纹和目录的摘要绑定。换用户、搬到另一条配置或篡改这些连接信息，解密会失败。名称与创建时间不属于连接绑定。

同一部署主密钥现在保护模型 Key 和 SSH 凭据两类记录；不要每次启动生成新的主密钥。将来轮换必须迁移两类现有密文，当前没有自动轮换流程。Python 字符串无法保证安全擦除；清理字典是缩短引用存活时间，不是内存擦除承诺。

## 9. 每个新增模块和包做什么

| 模块 | 职责与主要引用 |
| --- | --- |
| app/models/server_connection.py | dataclasses 定义不可变配置与分页对象；ipaddress 校验 IP/CIDR；uuid 规范 ID；re/base64 校验指纹和账号；PurePosixPath 规范 Linux 路径；hashlib/json 计算配置绑定 |
| app/security/server_credentials.py | cryptography.Fernet 认证加密；json 编解码含用途的载荷；校验密文封装及用户、配置绑定 |
| app/repositories/server_connection_repository.py | pathlib 加载固定 SQL；mysql-connector-python 连接和事务；所有查询、分页、删除限制本人，创建仅存密文 |
| app/services/server_connection_service.py | uuid4 生成 ID，datetime 生成 UTC 时间，asyncio.to_thread 调仓储，组织加密、测试和结果回读 |
| app/services/ssh_connector.py | AsyncSSH 实际连接；hmac 比较指纹；asyncio 控制等待；threading.BoundedSemaphore 限制每进程连接测试；perf_counter 计时 |
| app/api/server_connection_schemas.py | Pydantic BaseModel、Field 和 SecretStr 校验请求，公开响应明确排除凭据；typing.Annotated 描述字段约束 |
| app/api/server_connection_routes.py | FastAPI APIRouter/Depends 挂接五个接口；UUID/Query 校验路径和分页；HTTPException 映射安全错误；不缓存成功响应 |

`bcrypt==5.0.0` 为 AsyncSSH 读取带口令 OpenSSH 私钥提供所需 KDF 支持，不是替换本系统已有的 Argon2 登录密码哈希。`asyncssh==2.24.1` 与它均加入 requirements，当前 langchain 环境已安装。cryptography、FastAPI、MySQL 驱动及其他已有依赖继续沿用。

改动的公共模块：`config.py` 读取 SSH 策略；`dependencies.py` 组装单例协作者；`main.py` 挂路由；`errors.py` 定义业务异常；`logging_config.py` 增加不含原始异常文本的固定原因；`database.py` 增加选择性建表。容量仓储和两条容量 SQL 纳入第十张表。

## 10. 六个 SQL 文件各做什么

| 文件 | 用途 |
| --- | --- |
| sql/create_server_connections.sql | 幂等创建新表、用户外键、索引与 CHECK |
| sql/create_server_connection.sql | 用户存在时保存一条本人配置 |
| sql/get_server_connection.sql | 按 user_id 和 server_id 读内部记录，包含供后端解密的密文 |
| sql/list_server_connections.sql | 本人第一页；不选择密文列 |
| sql/list_server_connections_before.sql | 本人游标下一页；不选择密文列 |
| sql/delete_server_connection.sql | 按 user_id 和 server_id 删除配置 |

SQL 在后端源码里，不需要普通用户输入。所有参数通过驱动占位符绑定，不拼接用户名、IP 或私钥到 SQL 文本。

## 11. 错误与日志

| 状态 | 代表什么 |
| --- | --- |
| 400 | 服务层参数不符合要求，或 SSH 私钥/密码认证失败 |
| 401 | 没登录、Token 无效或已退出 |
| 403 | 目标 IP 不在部署允许范围 |
| 404 | 配置不存在或不属于当前用户 |
| 409 | 服务器主机指纹不匹配 |
| 422 | 请求结构、类型、UUID、额外字段等校验失败 |
| 429 | 本进程连接测试名额已满 |
| 502 | SSH 网络/协议连接失败 |
| 503 | 配置、解密、数据库等内部服务故障，统一提示稍后重试 |
| 504 | SSH 连接或认证等待超时 |

日志记录操作名、来源模块/行号、耗时、请求关联 ID 和安全异常类型链。不会记录私钥、口令、SSH 密码、Token、密文、请求正文或原始驱动异常文本。422 沿用已有安全处理，不把错误输入原样回显给用户。

## 12. 如何重新运行验证

```powershell
python -B -m unittest tests.test_server_connection_core tests.test_ssh_connector tests.test_server_connection_api tests.test_server_connection_repository
python -B -m scripts.verify_ssh_connector
python -B -m scripts.verify_server_connections_mysql
```

前两条不需要你的真实训练服务器。第三条需要本机 MySQL 和已配置的 MYSQL_PASSWORD，会创建并最终删除独立随机临时库，使用临时用户和本地临时 SSH 服务；不清空业务库。所有测试凭据仅在测试中生成，不应复用。

本次结果：新增 41 项单元测试，完整 818 项通过；10 组服务器 HTTP/MySQL/本地 SSH 验证通过，容量 8 组、运行记录 15 组、上下文聊天 16 组回归通过。连接专项验证了三种认证、错误指纹/密码/口令，以及未打开远程命令 session。未调用收费模型，未购买服务。

`scripts/_ssh_test_server.py` 生成临时 SSH 主机和用户密钥，监听本机随机端口；`verify_ssh_connector.py` 测真实握手；`verify_server_connections_mysql.py` 测实际 HTTP/数据库/SSH 全链路；`_chat_api_test_app.py` 增加隔离库和依赖替身，禁止真实模型构造。四个新增 unittest 文件分别检查数据/凭据/业务、适配器、路由、仓储；原容量临时库脚本同步建新表。

## 13. 下一步

**Runner 的运行与服务器绑定、已登记脚本启动、远端任务标识、状态回收和有界日志** 已完成本地验证，真实 Linux 待验收。接着采集结构化指标并生成报告。创建运行记录、SSH 连通、远端进程启动是三个不同状态，需要分别记录，重复请求不得重复训练。之后才能绑定模型训练工具并接入有据建议。

本阶段完整新增与修改文件见 [服务器连接阶段文件变更清单](服务器连接阶段文件变更清单.md)。
