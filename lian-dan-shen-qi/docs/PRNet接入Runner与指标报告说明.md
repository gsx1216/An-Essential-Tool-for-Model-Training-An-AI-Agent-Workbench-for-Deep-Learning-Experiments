# PRNet 接入 Runner、指标与新会话查询

2026-10-29 历史记录说明：本文描述的专用 PRNet 适配器、构建脚本和默认登记现已移除，文中的旧部署命令不再适用于当前版本；已有实验、权重与历史记录保留。PRNet 与其他网络今后都通过 [通用训练项目配置](通用训练项目接入与接口说明.md) 接入。


日期：2026-10-26。项目：`E:\code\LangChian_New`。

后续更新：现在已接入通用对话训练 Tool。本文保留 PRNet Runner 阶段记录；聊天启动与多项目选择的当前使用方式见 [对话调用训练Tool与多项目任务说明](对话调用训练Tool与多项目任务说明.md)。

本阶段把 PRNet 接入了已有执行系统，完成真实 Linux / GPU 验收：从 HTTP 接口创建运行、通过 SSH 启动、采集真实训练指标到 MySQL、生成版本化报告，最后从一个新聊天通过工具读取这份报告。

## 1. 先区分三种事情

1. **创建实验**：保存研究任务及评测标准，不执行训练。
2. **创建 run**：冻结这一次的参数和评测配置，也不执行训练。
3. **启动 run**：显式调用启动接口，才会通过 SSH 启动已登记的脚本。

普通问答和“最近一次训练怎么样”仍然是查询，不会再次启动训练。本阶段没有让模型自行执行任意 shell 命令，也没有增加自动修改研究代码的功能。

```mermaid
flowchart TD
    A[登录用户] --> B[创建实验及评测配置]
    B --> C[创建 run：参数和评测快照]
    C --> D[显式 POST /runs/id/start]
    D --> E[登录鉴权、资源归属、脚本及参数校验]
    E --> F[SSH：固定协议与主机指纹验证]
    F --> G[远端监督进程：状态、超时、日志、停止]
    G --> H[已登记 PRNet 适配脚本]
    H --> I[独立源码副本和预训练权重副本]
    I --> J[训练及测试：GPU 0]
    J --> K[JSONL 指标与独立检查点]
    K --> L[显式 metrics/sync：游标采集到 MySQL]
    L --> M[版本化报告]
    N[新聊天询问最近训练] --> O[聊天工具按当前用户查询]
    O --> M
    M --> P[模型结合真实结果回答]
```

## 2. 本轮真实验收结果

| 项目 | 本轮结果 |
| --- | --- |
| 服务器 | `10.61.52.182:2222`，用户名 `gsx` |
| 项目根目录 | `/mnt/Data/HOME/gsx/PRNet` |
| Python | `/mnt/Data/HOME/gsx/miniconda3/envs/mynet/bin/python` |
| 环境 | Python 3.9.21、PyTorch 2.6.0+cu118、CUDA 11.8 |
| GPU | 物理 GPU 0，RTX 3060 |
| 参数 | 实际 1 epoch，batch size 4，384 分辨率，学习率 `0.00005` |
| 数据 | tiny_cod，训练 977 对，测试 649 对 |
| 运行结果 | succeeded，exit_code=0 |
| 平均训练 loss | **4.5840**，来自原训练日志的四位小数 |
| 测试 MAE | **0.027058536180191436**，来自原测试日志 |
| 指标 / 报告 | 有效 2 条，无效 0 条；报告版本 1，schema 2，采集完成，无质量告警 |
| 检查点 | `artifacts/last.pth`，53,361,123 字节 |
| 权重复核 | CPU 上 `weights_only=True` 读取，`strict=True` 加载成功；551 个条目，浮点权重均为有限值 |
| 原文件 | 六个原始源码文件及原 `pth/PRNet.log` 的哈希均保持不变 |
| GPU 结束状态 | GPU 0 显存 8 MiB，利用率 0%（复核时的瞬时值） |
| 新会话查询 | 真实工具查询到了本轮运行和报告；生成文字的模型用模拟网关，付费模型调用 0 次 |

这一轮经系统启动的远端目录：

```text
/mnt/Data/HOME/gsx/PRNet/.experiment-assistant/79724eec23c54368b5bde11b5f2ec48b
```

验收 run_id 为 `536f261de0344760a436d620cbe764af`。**这些数据库记录属于随机临时验收库，验收结束已删除，不能把这个 run_id 直接拿到日常 `/docs` 中查询。** 远端目录和检查点保留，公开验收证据见 `docs/validation/prnet_runner_20261026.json`。

之前直接 SSH 启动检查得到的 MAE 是 0.035638273530397446；本轮是一次新的独立运行。两轮都只跑了一轮，未固定完整随机性，不是受控性能对比，不能据此宣称方法获得改进。

## 3. 先从 /docs 自己跑通

代码保存后，重启后端，使已缓存的服务对象重新读取部署配置。在项目根目录执行原来的 Uvicorn 启动命令，例如：

```powershell
conda activate langchain
cd E:\code\LangChian_New
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000/docs`。

### 3.1 登录和服务器配置

1. 注册或登录，取得自己的 access_token；在 Authorize 输入 Token。
2. 调用 `GET /runner/scripts`，应看到 `prnet_train` 及其参数约束。
3. 调用 `POST /servers`。请求结构见 `examples/prnet_server.example.json`，把 password 占位符替换为你自己的 SSH 密码，不把真实密码写回示例文件。
4. 保存响应的 `server_id`，再调用 `POST /servers/{server_id}/test` 测试连接。

本次使用密码登录。已有私钥登录功能继续支持；需要将 auth_method 改为 private_key，并提供对应用户名、私钥及需要时的口令，不能只删掉 password 就称为免认证。服务器凭据经已有加密模块存储，读取接口不返回凭据。

### 3.2 创建实验和本次运行

调用 `POST /experiments`，请求体直接参考 `examples/prnet_experiment.example.json`。它包含伪装目标检测任务类型、数据集版本和 loss / MAE 的定义。记下响应的 `experiment_id`。

调用 `POST /experiments/{experiment_id}/runs`：

```json
{
  "request_id": "换成一个新的UUID",
  "parameters": {
    "epochs": 1,
    "batch_size": 4,
    "learning_rate": 0.00005
  }
}
```

`request_id` 用于幂等：网络重试同一次操作沿用原值；有意新建另一轮时生成新值。例如本地运行 `python -c "from uuid import uuid4; print(uuid4().hex)"`。上面的中文占位符不能原样提交。

记录响应中 `run.run_id`。调用 `POST /runs/{run_id}/start`：

```json
{
  "server_id": "刚才自己的服务器ID",
  "script_id": "prnet_train"
}
```

这一步才会实际占用 GPU。相同运行重复启动使用已有 job，不创建第二份训练。想做另一组独立实验，应创建新的 run。`202` 表示系统已处理启动请求，不表示整个训练已经完成。

### 3.3 看状态、日志和报告

| 接口 | 作用 | 是否连接 SSH |
| --- | --- | --- |
| `GET /runs/{run_id}/execution` | 读取数据库里最后保存的执行状态 | 否 |
| `POST /runs/{run_id}/refresh` | 向服务器查询实际状态，并写回数据库 | 是 |
| `GET /runs/{run_id}/logs?offset=0&limit=4096` | 按字节偏移读取有限日志 | 是 |
| `POST /runs/{run_id}/metrics/sync?max_pages=8` | 将有限日志页中的指标增量写入 MySQL | 是 |
| `GET /runs/{run_id}/metrics/latest` | 读取已经入库的分组统计 | 否 |
| `POST /runs/{run_id}/report` | 根据已入库事实生成或复用报告 | 否 |
| `GET /runs/{run_id}/report` | 查询已存报告，包含是否过期 | 否 |
| `POST /runs/{run_id}/stop` | 请求停止本轮运行，之后需要刷新确认 | 是 |

状态查询和指标采集目前需要显式调用。训练结束后继续 sync，直到报告显示 `collection_complete=true`；如果单次页数不足，重复采集会从持久化游标继续，不会把已有指标重复算入。

然后创建一个自己的新聊天，问“我最近一次训练的测试结果怎么样”。已有聊天工具可以按用户查最近运行、读取报告；聊天模型需要你自行配置有效的 DeepSeek Key。验收只模拟了模型文字输出，因此还不能把本轮验收等同于真实供应商模型已完成工具选择测试。

## 4. 每个新增核心模块在做什么

### app/models/prnet_deployment.py：部署包生成

输入是部署者维护的 profile，不是普通用户对话。它先校验固定 Python、项目目录、数据路径、GPU 限制和源码哈希，再把这些公开配置嵌入适配脚本。

生成的脚本计算 SHA-256，以摘要前缀作为文件名的一部分；完整摘要写入 Runner 注册表。更改配置或适配源码后会得到新的版本文件。不要直接覆盖已登记脚本，否则会触发摘要不匹配。

`build_bundle()` 返回脚本文本、注册规则、默认参数和标准实验请求。`write_bundle()` 将它们保存到新的目录，目录已存在时拒绝覆盖。合并现有注册表时保留其他脚本；同一个 script_id 指向另一个版本时要求使用新 ID。

使用 `hashlib` 计算摘要、`json` 生成规范配置、`Path` 处理路径；调用已有 `RunnerSettings` 和 `canonical_evaluation` 复用业务校验。

### scripts/prepare_prnet_runner.py：管理员生成命令

这是 CLI 入口，用 `argparse` 读取参数，调用上面的部署模块，不连接服务器，也不开始训练。

```powershell
python -m scripts.prepare_prnet_runner --profile examples/prnet_deployment.example.json --output deployment/prnet_v1
```

输出包括 `bundle.json`、`runner_scripts.json`、`experiment.json` 和已嵌入配置的 Python 文件。当前服务器已经安装了本轮版本。以后重新部署时，先确认源码哈希和路径，生成新目录；需要合并现有登记项时使用脚本的 `--existing-registry` 参数，并给变更后的脚本一个新 script_id。

### app/runner/prnet_adapter.py：远端 PRNet 桥接

这是独立的 Linux 脚本模板。生成后的文件在训练服务器上运行，不需要安装 FastAPI、MySQL 或后端整个项目。

它依次完成：

1. 校验 config 必须属于本项目 `.experiment-assistant/<job_id>/parameters.json`，只接受 epochs、batch_size、learning_rate。
2. 使用指定 Conda Python 启动 worker，设置 GPU 和运行环境。
3. 使用 Linux `fcntl.flock` 锁住本系统对应 GPU，避免本系统的两个作业同时使用 GPU 0；同时检查显存余量。这个文件锁不约束其他人的独立训练程序。
4. 新建本轮 artifacts，检查图像与掩码文件配对。清单摘要覆盖文件名和大小，不是全部数据内容的完整摘要。
5. 将六个研究源码及预训练权重复制到本轮目录，核对源码哈希，写入 provenance。
6. 用 `runpy.run_path` 初始化源码副本的训练环境；复用原 train / test 函数，自己控制实际 epoch 循环。
7. 从原 logging 记录提取真实 epoch loss / test MAE，输出 Runner 能识别的 JSONL，同时保存在 artifacts 中。
8. 确认每轮都有训练和测试指标，保存 `last.pth`、检查点摘要、result.json，并关闭 TensorBoard writer。

原研究文件的入口使用 `range(1, opt.epoch)`，所以内部仍传 `--epoch 2`，而本轮实际训练一次。外部 API 的 `epochs=1` 就是实际一轮，不要求用户理解这个历史写法。

使用的标准库包括 `argparse`、`json`、`hashlib`、`logging`、`pathlib`、`shutil`、`runpy`、`os`；Linux 锁使用 `fcntl`。训练环境继续使用研究项目已经安装的 PyTorch、torchvision、timm、NumPy、Pillow、tensorboardX 等依赖。没有要求后端 Windows 环境安装 CUDA。

### scripts/verify_prnet_runner_live.py：真实环境验收

用 FastAPI TestClient 调用真实路由、真实仓储、真实 MySQL 和 SSH / Runner。数据库使用随机临时库，用户及登录会话由测试创建，退出时删除临时库，远端产物保留。模型网关被明确模拟，避免付费调用。

它先通过 SFTP 安装与摘要一致的版本脚本，再测试启动、重复启动、跨用户隔离、刷新、指标入库、报告和新会话工具查询。SSH 密码由 `getpass` 隐藏输入，不从命令行参数传入，也不保存到文件。脚本必须显式带 `--confirm-real-training`，再次运行会开始另一轮真实训练，日常不用重复执行。

## 5. 现有模块如何配合它

| 层 | 现有模块 | 在这条链路中的职责 |
| --- | --- | --- |
| 路由 | `app/api/runner_routes.py` | 解析请求，Depends 注入服务，映射状态与异常；路径本阶段未改 |
| 登录依赖 | `require_current_user` | 从 Bearer Token 找到当前用户，确认登录有效；用户身份不能由请求体随便指定 |
| 运行服务 | `app/services/runner_service.py` | 检查本人 run / server、注册脚本、参数、不可变启动绑定以及幂等 |
| SSH | `app/services/ssh_connector.py` | 允许 IP、认证、固定主机指纹、超时和连接并发限制 |
| 传输与远端监督 | 既有 RunnerTransport / `app/runner/remote_agent.py` | 固定协议传输，远端独立进程、状态持久化、日志、超时与停止 |
| 指标服务 / 仓储 | 既有 metric 服务与仓储 | 按游标读取有限日志，解析 JSONL，事务保存指标和游标 |
| 报告服务 | 既有报告生成模块 | 根据数据集、split、metric 分组，保存事实与报告版本 |
| 聊天工具 | 既有实验工具执行器 | 新会话不依赖旧聊天文本，按当前用户查询最近运行 / 报告 |

登录校验和资源归属校验不是同一件事。有效 Token 只说明“这是某个已登录用户”，服务和仓储还必须确认“这份 server / run 属于他”。否则别人只要猜到 UUID 就可能读取训练日志。

## 6. 本次发现并修复的 SSH 问题

服务器同时提供 RSA 和 Ed25519 主机密钥，而登记的是 Ed25519 指纹。默认 SSH 协商可能先选 RSA，导致明明登记正确指纹仍被拒绝。之前首轮安装就在这一阶段失败，没有开始训练。

本次保留指纹校验：默认连接遇到主机密钥不匹配后，仅探测强算法对应的公开主机密钥，不进行用户认证；找到与已登记指纹一致的算法后，再以该算法建立正常连接，仍然验证指纹。找不到匹配项就拒绝。

缓存只保存有限数量的“主机、端口、指纹 → 算法”公开元数据，不缓存密码、私钥或长连接。探测和重连仍受总超时与连接并发限制。不使用跳过主机身份校验的办法。

## 7. 配置分别存在哪里

| 文件 | 内容 | 谁维护 |
| --- | --- | --- |
| `config/runner_scripts.json` | 脚本相对路径、完整 SHA-256、允许参数范围、运行和日志上限 | 部署者 |
| `config/ssh_allowed_networks.json` | 本次只允许 `10.61.52.182/32` | 部署者 |
| `examples/prnet_deployment.example.json` | 已核对的项目 / 环境 / 数据路径、源码摘要及 GPU 限制，不含密码 | 部署者 |
| `examples/prnet_server.example.json` | `/servers` 的公开请求示例，密码是占位符 | 用户参考 |
| `examples/prnet_experiment.example.json` | `/experiments` 的实验和评测请求示例 | 用户参考 |
| `examples/prnet_parameters.example.json` | 创建 run 时 parameters 部分的示例 | 用户参考 |

SSH 允许列表的读取顺序：存在 `SSH_ALLOWED_NETWORKS` 环境变量时优先使用它；没有时才读取 JSON 文件。环境变量显式设置为空表示禁用，JSON 不会覆盖它；两者都没有时默认拒绝连接。没有改动原 `config/.env`、MySQL 密码或加密主密钥。

后端依赖包括 FastAPI / Starlette、Pydantic、asyncio、AsyncSSH、mysql-connector-python、cryptography、python-dotenv。`asyncio` 管理异步网络与等待，AsyncSSH 负责连接和 SFTP，MySQL 存永久业务记录，cryptography 复用既有凭据加密。本阶段没有新增 Python 第三方依赖，也没有新增数据库表，业务表仍是 21 张。

## 8. 当前限制与下一步

- 本次登记专用于这台服务器上的 PRNet。物理 GPU 固定 0，实际 epochs 上限 1，batch size 2～4，单次最多 900 秒、日志上限 1 MiB。不是完整长期训练配置；增大轮数时应同步评审超时、日志和资源配额，不能只改一次 API 请求。
- 研究源码有 GPU 变量覆盖及固定 384 特征尺寸，因此暂不开放 GPU / 分辨率参数。换项目、源码版本或数据集时应生成独立 profile / 脚本版本，并使用对应评测协议。
- 数据清单校验不是数据集内容完全冻结；正式可复现实验还需要完整数据版本、依赖版本、随机种子与更完整环境锁定。
- 检查点目前保留在服务器，尚无用户下载 API、对象存储或自动清理策略。
- 采集、刷新和报告生成目前由显式接口推动。真实供应商模型的工具选择与建议质量还需独立验收。
- 下一步适合接后台 Worker + Redis Streams：把定时刷新、指标采集和完成后的报告生成移到普通后台任务；出现异常或完成事件时再触发分析 Agent。MySQL 继续作为事实记录与幂等依据。

## 9. 验证记录

保存前已通过：1013 项单元测试；Runner HTTP / MySQL / 回环 SSH 8 组；服务器接口 10 组；指标 / 报告 10 组；聊天工具 12 组。另有本章记录的真实服务器一次训练 / 测试，以及随后只在 CPU 做的检查点严格重载核对。

测试覆盖了参数边界、源码变更拒绝、指标提取、缺失测试结果拒绝、writer 关闭、部署包确定性、文件允许列表优先级、SSH 多主机密钥选择、错误指纹拒绝、连接超时释放等。本轮真实联调无付费模型调用；这些结果不代表 Kubernetes、多进程后台 Worker 或所有研究任务已经完成生产验收。
