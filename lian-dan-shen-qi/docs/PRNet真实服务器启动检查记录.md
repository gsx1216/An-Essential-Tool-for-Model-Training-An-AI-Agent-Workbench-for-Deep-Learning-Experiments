# PRNet 真实服务器训练启动验收记录

日期：2026-10-26，时区 Asia/Shanghai。当前状态：**已成功完成一轮真实 GPU 训练和测试**。

## 1. 结果

| 项目 | 实测结果 |
| --- | --- |
| 开始时间 | 2026-10-26 19:17:27 |
| 结束时间 | 2026-10-26 19:19:17 |
| 总耗时 | 109.65 秒，含模型初始化、数据读取、训练、测试和保存 |
| 最终状态 | succeeded |
| 退出码 | 0 |
| 实际训练轮数 | 1 |
| 训练 batch 数 | 244 |
| 日志中的平均训练 loss | 4.4609 |
| 本轮测试 MAE | 0.03563827353039745 |
| 模型参数量 | 13,290,440 |
| 训练记录的峰值分配显存 | 4023 MiB，来自 torch.cuda.max_memory_allocated |
| 训练后 GPU 0 | 8 MiB，利用率 0%，已释放训练显存 |
| 权重文件 | startup_epoch_1.pth，53,399,356 字节 |
| 权重校验 | CPU 重新加载成功，strict=True 无缺失/多余键；所有浮点 Tensor 为有限值 |
| 原文件检查 | 原训练代码和原 pth/PRNet.log 的已记录 SHA256 均未变化 |

这是一轮启动验收，验证能加载权重、读取真实数据、执行前向/反向和优化、计算测试指标、保存及重新加载检查点。它不代表模型已经充分训练，也不把历史最佳结果当作本轮结果。

## 2. 服务器与环境

| 项目 | 信息 |
| --- | --- |
| SSH | 10.61.52.182:2222，用户 gsx，密码登录；密码未保存在脚本或报告中 |
| 主机身份 | ED25519 指纹 SHA256:R860QwfpoN01weEh9e0XFREpdkg9o/Lt482B+9lIiVk，与本机已有 known_hosts 匹配 |
| PRNet | /mnt/Data/HOME/gsx/PRNet |
| Python | /mnt/Data/HOME/gsx/miniconda3/envs/mynet/bin/python，3.9.21 |
| PyTorch / CUDA | 2.6.0+cu118 / 11.8 |
| torchvision / timm | 0.21.0+cu118 / 1.0.15 |
| 其他依赖 | tensorboardX、thop、PIL 导入成功 |
| GPU | 三张 RTX 3060，每张 12288 MiB；本轮只使用 GPU 0 |

## 3. 实际训练配置

- GPU 0，batch size 4，trainsize 384，学习率 0.00005。
- 加载原有 smt_tiny.pth，日志已确认加载成功。
- 训练数据：tiny_COD/train/Imgs2 与 GT2，各 977 个；测试数据：tiny_COD/test/Imgs2 与 GT2，各 649 个。
- 全部图像/掩码的文件名和尺寸配对检查通过。
- 原数据加载器设置 drop_last=True，因此本轮处理 244×4=976 个训练样本，最后不足一批的 1 个样本按原逻辑跳过。
- 原代码使用 range(1, opt.epoch)，所以传入 --epoch 2 实际执行 1 轮；日志中的 [001/002] 不表示执行了两轮。
- 模型包含固定尺寸特征融合，本次保持 384，没有缩小输入尺寸。

## 4. 本次运行目录与查看方法

远端运行目录：

```text
/mnt/Data/HOME/gsx/PRNet/runs/startup_check_20261026_111727_3ebb68
```

登录服务器后查看：

```bash
cat /mnt/Data/HOME/gsx/PRNet/runs/startup_check_20261026_111727_3ebb68/status.json
tail -n 30 /mnt/Data/HOME/gsx/PRNet/runs/startup_check_20261026_111727_3ebb68/console.log
cat /mnt/Data/HOME/gsx/PRNet/runs/startup_check_20261026_111727_3ebb68/metrics.jsonl
cat /mnt/Data/HOME/gsx/PRNet/runs/startup_check_20261026_111727_3ebb68/verification.json
```

原训练源码和旧权重目录未修改。单独创建的启动包装器在原训练脚本正常返回后，关闭 TensorBoard writer、额外保存本轮检查点并输出 JSONL 指标，避免原代码第一轮不保存权重的问题。

## 5. 监督和验证逻辑

1. 创建带时间和随机后缀的独立目录，提前准备日志保存位置。
2. manifest.json 记录环境、参数、真实轮数和原文件 SHA256。
3. 独立 supervisor.py 运行训练，SSH 断开不影响本轮；最大执行时长为 900 秒。
4. train_once.py 按原参数调用 PRNet_train_cod.py，训练结束后保存 startup_epoch_1.pth 和 metrics.jsonl。
5. supervisor.py 等待子进程退出，将退出码、结束时间及 succeeded/failed 状态写入 status.json。
6. 本轮成功后在 CPU 上以 weights_only=True 重新加载，创建 PRNet 并 strict=True 加载状态，检查全部浮点 Tensor。
7. 核对原源码和原日志的哈希，再查看 GPU 状态；本轮均通过。

监督进程 PID 为 164766，训练 PID 为 164767，两者用于本次记录；运行已结束，不应把这些 PID 当成长期有效的任务标识。

检查点 SHA256：

```text
e8eeb9cd71560780e9bfec15812bba0bc51370c51edf9e82ce90240fba731f18
```

## 6. 远端新增文件

以下文件全部位于本次独立运行目录内：

| 相对路径 | 字节数 | 用途 |
| --- | ---: | --- |
| PRNet.log | 1,258 | 原训练代码的训练及测试日志 |
| console.log | 1,663 | 训练标准输出和警告 |
| manifest.json | 1,605 | 本轮配置及原文件哈希 |
| metrics.jsonl | 99 | 本轮真实测试 MAE |
| startup_epoch_1.pth | 53,399,356 | 本轮模型权重 |
| status.json | 203 | 最终状态及进程/时间信息 |
| summary/events.out.tfevents.1793013453.hbu-System-Product-Name | 1,255,138 | TensorBoard 训练事件记录 |
| supervisor.log | 0 | 监督进程输出，本次为空 |
| supervisor.py | 1,290 | 独立监督、超时和退出码记录 |
| train_once.py | 670 | 一轮训练及额外保存包装器 |
| verification.json | 796 | CPU 权重校验与原文件哈希检查结果 |

## 7. 本地文件记录

项目中修改：`E:\code\LangChian_New\docs\PRNet真实服务器启动检查记录.md`，由“检查通过、等待启动”更新为本次真实成功结果。实验助手业务 Python、SQL 和真实 .env 未修改。

本次 Codex 工作目录保留：

| 文件 | 用途 |
| --- | --- |
| prnet_ssh_probe.py | 主机身份检查 |
| prnet_startup_20261026/ssh_session.py | 临时 SSH 会话工具，密码仅在内存中使用 |
| prnet_startup_20261026/launch_prnet_startup.py | 本轮已获授权的启动器 |
| prnet_startup_20261026/operations.jsonl | 只读检查、启动及结果读取记录 |
| prnet_startup_20261026/result.json | 本轮参数、完整文件清单及校验结果 |
| prnet_startup_20261026/console.log | 本轮控制台日志副本 |
| prnet_startup_20261026/PRNet.log | 本轮原训练日志副本 |
| prnet_startup_20261026/complete_prnet_report.py | 根据实测证据生成本报告，不执行训练 |
| prnet_startup_20261026/report_update_manifest.json | 报告保存前后的文件哈希 |
| prnet_startup_20261026/PRNet真实服务器启动检查记录.md | 本报告的工作副本 |

## 8. 当前边界与下一步

这次完成了真实 SSH 和真实 GPU 的 PRNet 启动验收。它通过直接 SSH 执行，尚未登记到实验助手的正式 runs、Runner 脚本注册表或模型聊天链路，也没有把本次指标写入业务库。

下一步应把这条已验证的训练命令封装为系统允许的训练适配器，接入现有 Runner，再验证状态查询、JSONL 指标入库、事实报告和聊天查询。之后才能说系统端到端链路通过。

日志里有 timm 导入路径和 F.upsample 的弃用警告，本轮未因此失败；这次没有为消除警告而修改原研究模型。

此前启动因自动审批仍使用验收阶段的真实 GPU 限制而被拦截；用户明确解除限制并批准上述一轮配置后才执行。没有绕过拦截，没有追加训练轮次。
