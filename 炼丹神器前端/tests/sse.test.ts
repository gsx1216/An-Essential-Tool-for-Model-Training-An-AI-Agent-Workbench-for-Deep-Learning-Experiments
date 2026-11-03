import { expect, it } from "vitest";
import { consumeChatStream } from "../src/lib/sse";
function stream(text: string) {
  const bytes = new globalThis.TextEncoder().encode(text);
  return new globalThis.ReadableStream<Uint8Array>({
    start(controller) {
      for (let i = 0; i < bytes.length; i += 3)
        controller.enqueue(bytes.slice(i, i + 3));
      controller.close();
    },
  });
}
it("跨字节分片正确读取中文、心跳和最终持久化结果", async () => {
  const events: string[] = [];
  const result = await consumeChatStream(
    stream(
      ': keep-alive\n\nevent: delta\ndata: {"content":"你好"}\n\nevent: reset\ndata: {}\n\nevent: done\ndata: {"content":"完整结果"}\n\n',
    ),
    (event) => events.push(event.type),
  );
  expect(result).toBe("完整结果");
  expect(events).toEqual(["delta", "reset", "done"]);
});
it("没有 done 的半截响应必须判定为未完成", async () => {
  await expect(
    consumeChatStream(
      stream('event: delta\ndata: {"content":"半截"}\n\n'),
      () => undefined,
    ),
  ).rejects.toThrow("连接已中断");
});
