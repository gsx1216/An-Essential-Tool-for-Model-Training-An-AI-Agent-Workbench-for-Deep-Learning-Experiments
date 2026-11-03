export const shortId = (id: string) => id.replaceAll("-", "").slice(0, 8);
export const safeAssistantText = (text: string) =>
  /<\/?[｜|]+DSML[｜|]+/i.test(text)
    ? "此条历史回复格式异常，不能作为查询已执行的结果。请重新发送原问题。"
    : text;
export const sameId = (left?: string | null, right?: string | null) =>
  !!left &&
  !!right &&
  left.replaceAll("-", "").toLowerCase() ===
    right.replaceAll("-", "").toLowerCase();
export function dateTime(value?: string | null) {
  return value
    ? new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "short",
        timeStyle: "medium",
        hour12: false,
      }).format(new Date(value))
    : "—";
}
export function number(value?: number | null) {
  return value == null
    ? "—"
    : new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 6 }).format(
        value,
      );
}
export const stateLabels: Record<string, string> = {
  created: "待启动",
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已停止",
  scheduled: "监控待调度",
  completed: "监控已结束",
  attention: "需要处理",
  ready: "已就绪",
  processing: "解析中",
  planning: "生成计划中",
  applying: "应用中",
  applied: "已应用",
  checks_failed: "检查失败",
  conflict: "版本冲突",
  pending: "等待分发",
  sending: "发送中",
  sent: "已发送",
  received: "已接收",
  passed: "通过",
  incomplete: "检查不完整",
  not_configured: "未配置",
};
export const stateLabel = (state?: string) =>
  state ? stateLabels[state] || state : "暂无状态";
export const terminal = (state?: string) =>
  ["succeeded", "failed", "cancelled"].includes(state || "");
export const safeReturnPath = (value: unknown) =>
  typeof value === "string" &&
  value.startsWith("/") &&
  !value.startsWith("//") &&
  !value.includes("\\")
    ? value
    : "/chat";
