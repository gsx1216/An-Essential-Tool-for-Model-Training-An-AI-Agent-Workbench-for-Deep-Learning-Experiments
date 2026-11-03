# PRNet Runner 阶段文件变更清单

日期：2026-10-26。保存目标：`E:\code\LangChian_New`。

本阶段完成真实系统 Runner → Linux/GPU PRNet → MySQL 指标 → 版本化报告 → 新聊天工具查询的验证。检查点 CPU 严格重载成功，原研究代码及旧训练日志保持不变。

## 新增文件（14 个）

| 文件 | 作用 |
| --- | --- |
| `app/runner/prnet_adapter.py` | 远端独立适配模板；固定训练环境、源码快照、GPU 锁、真实指标、检查点及溯源。 |
| `app/models/prnet_deployment.py` | 校验部署 profile，生成带摘要脚本、注册规则、评测及默认参数。 |
| `scripts/prepare_prnet_runner.py` | 管理员生成部署包的 CLI，不连接服务器或训练。 |
| `scripts/verify_prnet_runner_live.py` | 显式真实验收：临时 MySQL、真实 SSH/GPU/Runner、真实工具与模拟模型。 |
| `examples/prnet_deployment.example.json` | 公开部署 profile：路径、六份源码摘要、数据集及资源限制，无密码。 |
| `examples/prnet_server.example.json` | 服务器 API 请求示例，密码仅占位符。 |
| `examples/prnet_experiment.example.json` | 创建 PRNet 实验及评测配置的完整 API 示例。 |
| `examples/prnet_parameters.example.json` | 本轮 epochs、batch_size、learning_rate 的参数示例。 |
| `config/runner_scripts.json` | 登记当前已安装 PRNet 适配脚本的路径、摘要、参数和资源上限。 |
| `config/ssh_allowed_networks.json` | 部署者允许访问的指定服务器单 IP；不包含凭据。 |
| `tests/test_prnet_adapter.py` | 31 项测试，覆盖参数、路径、源码、指标、清理、部署包和允许列表。 |
| `docs/PRNet接入Runner与指标报告说明.md` | 完整流程、模块与包说明、真实验收结果、部署配置及 /docs 操作。 |
| `docs/PRNet_Runner阶段文件变更清单.md` | 本阶段全部新增/修改文件、验证与未实现边界。 |
| `docs/validation/prnet_runner_20261026.json` | 无凭据的真实运行报告、产物清单、来源、检查点和 CPU 复核证据。 |

## 修改文件（5 个）

| 文件 | 变化 |
| --- | --- |
| `app/config.py` | SSH 允许网段支持受限 JSON 文件，显式环境变量继续优先，缺省仍拒绝。 |
| `app/services/ssh_connector.py` | 修复多主机密钥协商：无认证探测匹配算法，仍验证固定指纹；仅缓存公开元数据。 |
| `scripts/_chat_api_test_app.py` | 验收工厂可注入指定 ServerSettings，旧回环默认不变。 |
| `tests/test_ssh_connector.py` | 调整原测试并增加 4 项算法选择、错误指纹、无凭据探测、超时释放测试。 |
| `docs/系统总体架构图与模块分层说明.md` | 更新真实 Linux/GPU 已验收的状态，补充 PRNet 数据流与边界。 |

## 服务器新增文件与产物

- 版本化适配脚本：`/mnt/Data/HOME/gsx/PRNet/assistant_adapters/prnet_130a55b9a456ac99.py`。
- 本次系统作业目录：`/mnt/Data/HOME/gsx/PRNet/.experiment-assistant/79724eec23c54368b5bde11b5f2ec48b`，其中包含独立监督脚本、参数、状态、输出日志、artifacts 源码与预训练副本、TensorBoard、指标、检查点和溯源记录；精确文件清单见验证 JSON。
- GPU 文件锁位于项目 `.experiment-assistant/gpu-locks/gpu-0.lock`，释放锁后保留文件本身是正常现象，不代表 GPU 仍占用。
- 没有覆盖原 PRNet 研究文件、预训练权重或原 `pth/PRNet.log`。

## 验证与边界

- 1013 项单元测试通过。
- HTTP / MySQL / 回环 SSH 验收：Runner 8 组、服务器 10 组、指标报告 10 组、聊天工具 12 组通过。
- 真实服务器一轮训练与测试成功：loss 4.5840，test MAE 0.027058536180191436；2 条有效指标、0 条无效，完整报告版本 1。
- 新会话的实际工具读取真实报告；模型文本生成使用模拟网关，付费模型调用 0 次。
- 本轮检查点 53,361,123 字节，551 个权重条目；CPU strict=True 加载通过，浮点权重有限；原文件摘要不变，GPU 0 已释放。
- 验收库是随机临时库，已删除。远端产物保留，不能直接在日常库中查询验收 ID。
- 未新增依赖、业务表或 SQL 迁移；未修改真实 `.env`、加密主密钥、MySQL 密码。业务表仍为 21 张。
- 普通问答不自动训练，后台定时采集 / Redis Streams、多 Agent 修改闭环、PDF RAG / Skill 尚未接入。

下一步建议：后台 Worker 自动刷新运行并增量采集指标，任务事件通过 Redis Streams 分发，MySQL 保留持久状态与幂等依据；完成或异常时再触发分析。

阅读入口：[PRNet接入Runner与指标报告说明](PRNet接入Runner与指标报告说明.md)。
