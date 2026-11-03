# Redis 事件通信接口与验收说明

日期：2026-10-27。三个接口全部需要 Authorization: Bearer <access_token>。user_id 来自登录身份，不接收客户端指定身份；只访问 MySQL，不连接 Redis 或调用模型。

## 1. GET /runs/{run_id}/events

用途：查看本人某次运行的报告事件及投递进度。

- path：run_id，已有运行 UUID。
- query：limit 默认 20，范围 1～50；offset 默认 0，范围 0～10000。按 created_at、id 升序。
- 请求体：无。
- 成功：200，JSON 数组；本人的运行尚无事件时返回 []。
- Cache-Control: no-store。

响应示例，ID/时间仅示意：

```json
[
  {
    "event_id": "11111111-1111-4111-8111-111111111111",
    "run_id": "cbadeeed-4085-4c8e-8337-a880b52db299",
    "event_type": "training.report_ready",
    "report_version": 1,
    "status": "received",
    "attempts": 1,
    "failure_count": 0,
    "last_error_code": null,
    "next_attempt_at": "2026-10-27T12:00:00Z",
    "sent_at": "2026-10-27T11:59:00Z",
    "lease_active": false,
    "created_at": "2026-10-27T11:59:00Z"
  }
]
```

pending=等待投递；sending=有投递者领取；sent=Redis 已确认写入但尚未确认 Inbox；received=分析任务已提交到 MySQL；attention=需要处理。sent_at 是首次记录的投递/接收确认时间。received 后 next_attempt_at 不表示还在等待新的投递。

attempts 是总领取次数，failure_count 是连续失败次数。lease_active 仅表示数据库租约有效，不证明操作系统进程一定活着。错误码包括 REDIS_UNAVAILABLE、INTERNAL_ERROR、PAYLOAD_INVALID，不暴露 Redis URL、驱动异常正文或快照。

## 2. GET /runs/{run_id}/agent-tasks

path、分页和缓存约定与 events 相同，无请求体，成功 200。

```json
[
  {
    "task_id": "22222222-2222-4222-8222-222222222222",
    "event_id": "11111111-1111-4111-8111-111111111111",
    "run_id": "cbadeeed-4085-4c8e-8337-a880b52db299",
    "agent_name": "analysis",
    "report_version": 1,
    "status": "queued",
    "created_at": "2026-10-27T11:59:00Z"
  }
]
```

queued 表示等待真正的分析执行器。本阶段任务不会自动调用大模型；不能显示“分析已完成”。schema 中的 running/succeeded/failed/attention 为后续执行层预留。

这两个查询仅能访问本人运行，运行不存在和属于他人均返回 404。新响应的 UUID 输出为带连字符格式，可直接复制回路径参数。

## 3. POST /runs/{run_id}/events/{event_id}/retry

用途：将本人原事件重新排队，不重训、不新建报告、不创建第二条同事件分析任务。

- path：本人的 run_id 与该运行的 event_id，均为 UUID。
- query / 请求体：无，不需要新 request_id。
- 成功：202。

```json
{
  "event_id": "11111111-1111-4111-8111-111111111111",
  "status": "pending"
}
```

202 表示重排已接受，后续状态可能很快推进，需重新 GET events。failure_count 清零，总 attempts 不清零。现有分析任务不会被删除、重建或重置。

若存在有效 sending 租约返回 409，避免抢占投递者；失效租约可重新排队。重复请求目标为同一事件，Inbox 唯一约束负责接收去重。

## 4. 错误约定

| HTTP | 含义 |
| --- | --- |
| 401 | 未登录或 Token 已失效 |
| 404 | 事件/运行不存在或不可访问 |
| 409 | retry 时仍有有效投递租约 |
| 422 | UUID 或分页格式不符合要求 |
| 503 | 请稍后重试；维护者通过安全日志定位 |

固定安全响应示例：{"detail":"请稍后重试。"}。校验错误继续使用项目自定义的安全字段错误列表，不返回原始输入或凭据。

## 5. 在 /docs 验证

1. 重启 API、监控 Worker；登录并 Authorize。
2. 新训练完成且完整报告生成后，GET /runs/{run_id}/events 应看到 pending 事件。已有完整报告可显式 POST /runs/{run_id}/report，复用报告时补齐原事件，不需要重训。
3. 在两个终端分别启动 event_relay_worker 和 analysis_event_worker，或分别使用 --once。
4. 再查 events，应进入 received；查 agent-tasks，应看到一条 queued 分析任务。
5. 同一事件调用 retry，再投递/接收，agent-tasks 仍为一条。
6. 暂停接收者，发送后不确认的消息会在恢复后按 pending idle 阈值重领；无需新建训练。
7. 不要在正常环境中删除 Streams、停止现有 Redis 或清空数据库来试故障；这些情况已通过独立临时 Redis/临时库验证。

前端后续只需读取这些结构化状态，不靠聊天文字猜测是否投递、接收或分析完成。
