# Runner 执行、状态与日志说明

日期：2026-10-25；后续进展更新于 2026-10-26。项目：`E:\code\LangChian_New`。后续指标与事实报告已完成，见 [训练指标采集与事实报告说明](训练指标采集与事实报告说明.md)。本文中本阶段测试数字记录 Runner 当时验收。本阶段从“可以测试 SSH 认证”推进到“可以通过受控接口启动已登记的 Python 脚本，查询结果、停止并读取有界日志”。

## 1. 已实现与验证范围

新增运行与服务器的持久化执行绑定、部署脚本注册表、固定 SSH 执行协议、独立远端监督进程及六个登录接口。创建运行仍然不自动训练；用户显式调用 start，后续模型工具也必须调用同一个业务层。

完整 854 项单元测试通过，本阶段新增 36 项；另有 8 组真实 HTTP、临时 MySQL、本地 SSH 和微型子进程验证。旧服务器 10 组、运行记录 15 组、容量 8 组、上下文聊天 16 组回归通过。微型脚本只输出演示信息，不运行 GPU 训练或收费模型。

本机是 Windows，没有可用的 Linux 执行环境，因此监督器的 Windows 兼容分支经过真实进程验证；**Linux 的 flock、会话脱离及进程组终止分支尚需在真实 Linux 服务器验收**。当前没有真实训练服务器连接配置，也没有启动用户的真实训练任务。这一范围不能冒充生产服务器已经验收。

## 2. 总体调用流程

```text
注册/登录得到 Token
  → 创建实验定义
  → 创建运行记录（固定参数快照，status=created）
  → 保存自己的 SSH 连接配置并测试认证
  → POST /runs/{run_id}/start，提交 server_id 和 script_id
      → Token 校验 → 本人运行 → 本人服务器 → 已登记脚本及参数规则
      → 数据库短事务固定执行绑定、启动快照和 job_id，运行改 queued
      → 解密本人的 SSH 凭据 → 核验目标网段及服务器指纹
      → SSH 固定 Python bootstrap，JSON 通过 stdin 传入
      → 远端持久化 manifest/state，启动独立监督进程，立即返回
      → 监督器核对脚本路径及 SHA256，使用 argv 启动 Python
      → 采集 stdout+stderr、心跳、退出码、取消和超时
  → refresh/logs/stop 查询真实远端信息，按规则更新 MySQL
  → 原有运行详情、列表和最近训练接口显示最新已持久化状态
```

HTTP 请求只等待短暂的 SSH 管理操作，不等待整次训练。SSH 连接关闭后，监督进程仍继续工作。当前没有后台定时轮询 worker：前端/调用方需要定期调用 refresh，例如 3～5 秒一次；不应把数据库中的旧状态当作实时观测。

## 3. 六个接口

| 接口 | 作用 |
| --- | --- |
| `GET /runner/scripts` | 当前部署登记的脚本编号、参数规则和最大时长；需要登录 |
| `POST /runs/{run_id}/start` | 首次绑定并启动，或针对同一目标重试；202 表示请求已处理，不代表训练成功 |
| `GET /runs/{run_id}/execution` | 只读本人数据库执行记录，不连接 SSH |
| `POST /runs/{run_id}/refresh` | 查询远端并更新数据库，返回 execution 与 observation |
| `POST /runs/{run_id}/stop` | 写入停止标记，返回 202；需要 refresh 确认最终 cancelled/其他终态 |
| `GET /runs/{run_id}/logs?offset=0&limit=4096` | 按字节读取本人日志，并携带当前状态观测 |

start 请求仅包含：

```json
{
  "server_id": "替换为本人服务器UUID",
  "script_id": "runner_smoke"
}
```

训练参数来自创建 run 时已保存的 parameters，不在 start 请求里重新提交。不能传 user_id、shell、command、任意脚本路径或任意解释器路径。不同服务器或脚本复用同一 run 会返回 409；想使用其他参数，创建新的 run。

execution 只公开 run_id、server_id、script_id、已知状态、退出码、安全错误码、绑定时间及最近检查时间；不公开内部 job_id、远端路径或启动快照。observation 是这次 SSH 的真实观测，包含 started、stale、log_bytes 等；只有进程已创建才标记 started。启动前取消、脚本缺失/哈希不符不会伪造 started_at。

## 4. 部署者怎样登记脚本

继续沿用原有 MYSQL_PASSWORD、MODEL_CREDENTIAL_ENCRYPTION_KEY 和 SSH_ALLOWED_NETWORKS，不改写现有 `.env`。新增配置由部署者设置，普通用户不能通过接口修改：

```dotenv
RUNNER_SCRIPTS_FILE=E:/code/LangChian_New/config/runner_scripts.json
RUNNER_PYTHON_COMMAND=python3
```

RUNNER_PYTHON_COMMAND 是**远端 Linux** 的 Python 程序名或绝对路径，例如 `/home/trainer/miniconda3/envs/train/bin/python`，不是本机 Windows 解释器。指定的解释器同时运行监督器和训练脚本，须包含训练所需依赖。远端监督器只依赖 Python 标准库，建议 Python 3.10 或以上。

未配置注册表时，默认查找 `config/runner_scripts.json`；文件不存在则脚本列表为空，拒绝启动。显式配置的文件不存在、格式错误或超出 128 KiB 时返回通用 503。配置对象缓存，修改注册表后重启后端进程生效。

项目提供 `config/runner_scripts.example.json`，它是**非活动示例**，默认不会启用。内容格式：

```json
{
  "runner_smoke": {
    "path": "runner_smoke.py",
    "sha256": "替换为实际脚本文件的64位小写SHA256",
    "parameters": {
      "epochs": {"type": "int", "min": 1, "max": 5},
      "delay_seconds": {"type": "float", "min": 0, "max": 1}
    },
    "max_runtime_seconds": 60,
    "max_log_bytes": 65536
  }
}
```

样例文件内已经填写本次 `examples/runner_smoke.py` 的实际字节哈希；拷贝到服务器时不要改变换行或内容。服务器用 `sha256sum runner_smoke.py` 重新核对。自己写的训练脚本需要登记自己的哈希，更新脚本后同步更新注册表；已有运行继续使用旧启动快照，哈希不匹配会安全失败。

参数规则当前支持 int、float、str、bool，支持 required、数值范围和文本最大长度；拒绝未登记参数。最大运行时长 1～604800 秒，日志总上限 1～10485760 字节。注册脚本只能是服务器 work_dir 内的相对 `.py` 路径，不得包含上级目录；解析符号链接后仍检查脚本位于工作目录内，脚本本身最大 10 MiB。

训练脚本统一接收：

```text
固定Python解释器 -u 已登记脚本 --config 监督器生成的parameters.json
```

脚本自行用 argparse 读取 --config，再用 json 读取参数。没有 `shell=True`，用户参数不会拼成 shell 字符串。SSH 入口的命令只包含部署者选定解释器与固定 bootstrap；主机、凭据和运行参数不会被当作命令文本拼接。

## 5. /docs 手工验收步骤

1. 在真实 Linux 服务器准备一个可写工作目录，将 `examples/runner_smoke.py` 复制到该目录根部；先只用演示脚本测试。
2. 部署者复制示例注册表为活动注册表，核对脚本实际哈希、远端 Python 和允许网段；重启后端。
3. 项目根目录运行 `python -m app.database --runner-schema`。仅增量补两个归属父键和执行表，不清空数据，不运行旧聊天回填。
4. 运行 Uvicorn，在 /docs 登录并 Authorize。创建本人 SSH 配置，填写可信主机指纹及刚才目录，测试连接。
5. 创建 training/script 实验，再创建一次 run，parameters 可用 `{"epochs": 3, "delay_seconds": 0.2}`。
6. 用 run_id 调 start，选择自己的 server_id 和 runner_smoke。再调用 refresh，直到 succeeded；读取 logs，应看到“no model training”和演示步骤。
7. 重复 start，不应产生第二次执行。再创建一个持续较久的新 run，测试 stop，然后 refresh 确认取消。

本次未自动完成第 1～2 步，也未激活示例注册表，因为没有你的真实服务器和脚本位置。现有 `.env`、凭据和历史记录保持原样。

## 6. 为什么重复请求不会直接重复训练

第一层是数据库：对本人 run 行加短锁，一个 run 仅允许一条 run_executions；先提交固定 job_id、server_id、script_id、脚本/参数快照，然后才发起 SSH。并发 start 读取同一执行绑定。脚本注册配置更新后，已绑定运行仍读取原快照；移除脚本会阻止新的 start/retry，查询和停止仍允许。

第二层是远端：固定 job_id 目录，request.lock 串行管理请求；manifest 绑定完整启动内容，不一致即拒绝。state 在启动监督器前写入；监督器先写 launch_claimed，再创建训练进程。重复 start 看见已有状态就只返回，不再派生训练进程。

这采用保守的**至多一次启动**，不能宣称覆盖所有崩溃窗口的 exactly-once。若进程在“已持久化启动意图、尚未 Popen”之间崩溃，运行可能停在 queued/stale，系统不会冒险自动重跑。若监督器在训练仍存活时异常退出，同样不能仅凭心跳过期就判定训练失败或自动再启动。

status=missing 或 stale=true 是“当前无法可靠确认”，保留数据库已有状态。先人工核对服务器和任务；不要删除 job 目录或为同一次任务重新生成 job_id。新 run 是新的执行意图，仍需确认旧任务没有继续运行。

## 7. 状态、时间与停止

MySQL 记录 created → queued → running → succeeded/failed/cancelled。终态不会被迟到的 queued/running 覆盖，running 不会回退 queued。exit_code=0 才允许报告成功；错误脚本、哈希变化、超过运行时长及监督器故障有固定 error_code。

数据库 started_at、finished_at 是后端首次观测并持久化的 UTC 时间，不是精确远端训练起止时间。后台自动同步还未接入，轮询间隔会带来时间差。未真正创建训练子进程的取消运行，started_at 保持空值。

stop 不接受客户端提供 PID。它在固定任务目录写 cancel 标记，由持有训练子进程对象的监督器处理。Linux 路径先终止该次训练的进程组，等待后仍未退出再强制终止；没有使用任意用户输入 PID 执行 kill。Windows 本机验收使用进程 terminate/kill，不能替代 Linux 子进程组行为验收。

停止响应可能仍是 running，因为只是提交请求。若训练已经自然结束，stop 保留原终态。首次 SSH 启动失败而远端尚无目录时，stop 会建立 cancelled 记录，之后 start 重试也不会执行。

## 8. 日志、心跳和恢复的边界

监督器把 stdout 和 stderr 合并采集，有界队列控制内存，日志达到上限后继续排空输出但不继续增加日志文件。日志中可能包含脚本主动打印的数据，部署脚本不要输出凭据。后端请求日志只记录操作和安全错误，不记录这些训练正文。

日志默认每次 4096 字节，最大 16384，offset 为字节游标，下一次使用 next_offset。超出总日志上限会标记 truncated。显示按 UTF-8 解码，非文本字节或在多字节字符中截断时使用替换字符；当前不是支持终端控制码的完整终端。接口读取当前远端日志，不把整个训练日志写进 MySQL，也没有接入对象存储归档。指标阶段新增 raw_base64、log_size、anchor_sha256、source_changed，供按原始字节采集；content 和既有游标仍保留。

心跳由监督器在采集循环更新，30 秒未更新标记 stale。重新建立后端 Service/Repository 可以从 MySQL 和远端文件继续查询，无需内存任务表；本次有重建协作者的联调验证。服务器重启后自动重启任务、崩溃后接管旧进程、远程目录灾备、跨主机迁移及分布式调度尚未实现。

每个用户仍应使用其有权使用的服务器账号。路径检查和脚本注册不是 OS 沙箱；同一远端账号能改写该账号的文件，恶意脚本也有该账号本身的权限。本阶段不宣称实现不可信代码执行隔离或 GPU 调度。训练脚本版本在启动前核验，后续文件管理需部署者维持可信权限。

## 9. 每个模块和引用包的作用

| 模块 | 职责与引用 |
| --- | --- |
| app/models/runner.py | dataclass 封装部署设置；json 复制快照；hashlib 绑定内容；PurePosixPath/re 校验路径和 ID；math 拒绝非有限参数；校验远端状态 |
| app/repositories/runner_repository.py | pathlib 加载固定 SQL；复用 MySQL 驱动，事务 claim/get/observe；uuid4 生成内部 job_id；校验快照和归属 |
| app/services/runner_service.py | logged_operation 记录操作；asyncio.to_thread 包装同步仓储；读取本人服务器、短暂解密、远端操作后保存观测；公开字段筛选 |
| app/services/runner_transport.py | shlex 只引用固定程序与 bootstrap；AsyncSSH 创建命令通道；asyncio 并发有界读取输出；json 校验协议；不记录 stderr 原文 |
| app/runner/remote_agent.py | 独立标准库程序；subprocess 启动训练，queue/threading 有界采集，time 心跳与预算，signal 终止本次进程组，fcntl/msvcrt 锁，os.replace 原子元数据 |
| app/api/runner_routes.py | FastAPI APIRouter/Depends/Query 处理六个入口；Pydantic 请求/响应约束；UUID 路径、HTTPException 和安全日志 |
| app/services/ssh_connector.py | 抽出共用异步连接上下文，继续核验固定主机指纹、部署网段、认证、超时和名额；旧 test_connection 仍不开远程 session |
| app/api/dependencies.py | lru_cache 复用固定服务对象；不缓存某个用户身份、SSH 明文或任务状态 |
| app/config.py | 读取部署注册表及远端解释器；不提供用户改写全局环境变量的接口 |
| app/database.py | 选择性父键补齐和新表迁移；不启动脚本 |
| app/errors.py / app/logging_config.py | 新增固定异常含义和安全原因，原始远端输出不进通用日志 |
| app/repositories/server_connection_repository.py / app/api/server_connection_routes.py | 数据库拒绝删除已绑定服务器时，转为明确 409 |

没有新增第三方依赖，沿用 AsyncSSH、FastAPI、Pydantic、mysql-connector-python、cryptography 和 python-dotenv。SSH 与监督进程相关选项参考 [AsyncSSH 官方 API](https://asyncssh.readthedocs.io/en/stable/api.html)、[Python subprocess](https://docs.python.org/3/library/subprocess.html) 和 [fcntl](https://docs.python.org/3/library/fcntl.html)。

## 10. 数据库和 SQL

新增第十一张表 run_executions，存 run_id、server_id、user_id、script_id、随机 job_id、启动 JSON 和摘要、创建/检查时间。启动快照包含工作目录、服务器配置绑定、脚本哈希/规则和参数；不包含 SSH 密码、私钥或口令。

两个复合外键分别关联 experiment_runs(id,user_id) 与 server_connections(id,user_id)，因此数据库也拒绝跨用户绑定；两个父表补充相应唯一索引。已关联服务器删除受 RESTRICT 保护，返回 409。现阶段没有执行历史清理/服务器归档功能，不能直接删掉配置破坏后续查询。

| SQL | 作用 |
| --- | --- |
| create_run_executions.sql | 新执行表、复合归属外键及作业唯一键 |
| runner_parent_indexes.sql | 检查两个归属父键的实际列序和唯一性 |
| add_runner_run_owner_index.sql | 旧运行表补 (id,user_id) 唯一键 |
| add_runner_server_owner_index.sql | 旧服务器表补 (id,user_id) 唯一键 |
| lock_runner_run.sql | 锁住本人的单次运行，串行首次绑定 |
| get_run_execution.sql | 联表按当前身份读内部执行记录 |
| create_run_execution.sql | 本人运行与本人服务器存在时创建执行绑定 |
| queue_execution_run.sql | 首次绑定将 created 改 queued |
| observe_execution_run.sql | 更新已确认状态，保留终态并拒绝 running 回退 queued |
| touch_run_execution.sql | 更新最近检查时间 |

create_experiment_runs.sql、create_server_connections.sql 同步包含新父键，首次部署不需额外手动 ALTER；capacity_tables.sql、capacity_indexes.sql 和容量仓储在本阶段跟踪十一张表，指标阶段扩展为十四张。

## 11. 复验与故障排查

```powershell
python -B -m unittest tests.test_runner_core tests.test_remote_runner tests.test_runner_api tests.test_runner_transport tests.test_runner_migration
python -B -m scripts.verify_runner_mysql
```

第一条含真实微型子进程测试，第二条使用已有本机 MySQL、新建随机临时库及真实回环 SSH 通道，结束后只删除本次测试库和临时目录。不会连接你的真实训练服务器，不调用 DeepSeek 或其他收费模型。

400 表示参数不满足登记规则；401 未登录；403 未登记脚本或 SSH 网段不允许；404 本人运行/服务器/执行绑定不存在；409 重绑或服务器已使用；422 请求结构错误；429 SSH 名额已满；502 连接失败；503 内部存储/协议错误统一稍后重试；504 管理连接超时。出现连接错误先查询 execution，再用相同目标重试或 refresh，不能自行改 run_id 当作无重复保证。

## 12. 下一步与 Skill/RAG

结构化指标和事实报告已完成基础，接着接入受控模型工具，支持“查询最近训练并追问”。Skill 负责按需加载工作流程；RAG 负责检索论文和其他资料；二者都通过 Runner 受控工具操作训练。具体规划见 [Skill与论文RAG接入规划](Skill与论文RAG接入规划.md)，本阶段未安装外部 Skill、向量库或自动下载论文。

全部新增/修改文件见 [Runner阶段文件变更清单](Runner阶段文件变更清单.md)。
