export interface ChatStreamEvent {
  type: "status" | "reset" | "delta" | "done" | "error";
  content?: string;
  detail?: string;
  status?: number;
}
export async function consumeChatStream(
  stream: ReadableStream<Uint8Array>,
  receive: (event: ChatStreamEvent) => void,
): Promise<string> {
  const reader = stream.getReader(),
    decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const part = await reader.read();
      buffer += decoder.decode(part.value, { stream: !part.done });
      buffer = buffer.replace(/\r\n/g, "\n");
      let end: number;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const lines = frame.split("\n");
        const type = lines
          .find((line) => line.startsWith("event:"))
          ?.slice(6)
          .trim();
        if (
          !type ||
          !["status", "reset", "delta", "done", "error"].includes(type)
        )
          continue;
        const raw = lines
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        const value = JSON.parse(raw) as Record<string, unknown>;
        if (
          !value ||
          typeof value !== "object" ||
          ((type === "delta" || type === "done") &&
            typeof value.content !== "string")
        )
          throw new Error("流式响应格式不正确，请重试同一轮问题。");
        const event: ChatStreamEvent = {
          type: type as ChatStreamEvent["type"],
          content:
            typeof value.content === "string" ? value.content : undefined,
          detail: typeof value.detail === "string" ? value.detail : undefined,
          status: typeof value.status === "number" ? value.status : undefined,
        };
        receive(event);
        if (type === "error") throw new Error("本轮回复未完成。");
        if (type === "done") return event.content!;
      }
      if (buffer.length > 2 * 1024 * 1024)
        throw new Error("流式响应超出限制。");
      if (part.done) throw new Error("回复连接已中断，请重试同一轮问题。");
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
