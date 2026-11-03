import { describe, expect, it } from "vitest";
import { renderMarkdown } from "../src/lib/markdown";
import { readFullHistory } from "../src/lib/history";
import { LogBuffer } from "../src/lib/logs";
import { safeReturnPath, sameId, safeAssistantText } from "../src/lib/format";
describe("不可信模型内容的展示", () => {
  it("历史助手协议异常改为中文提示，正常分析正文保留", () => {
    const text = safeAssistantText("<｜｜DSML｜｜ calls>原始工具标记");
    expect(text).not.toContain("DSML");
    expect(text).toContain("格式异常");
    expect(safeAssistantText("训练损失正在下降。")).toBe("训练损失正在下降。");
  });
  it("保留正常 Markdown，禁用原始 HTML", () => {
    const html = renderMarkdown("**结果**\n<script>alert(1)</script>");
    expect(html).toContain("<strong>结果</strong>");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });
  it("阻止 javascript、data 等执行链接", () => {
    const html = renderMarkdown(
      "[执行](javascript:alert(1)) [载荷](data:text/html;base64,AAAA)",
    );
    expect(html).not.toContain('href="javascript:');
    expect(html).not.toContain('href="data:');
  });
  it("外链隔离新窗口，图片不发起远端请求", () => {
    const html = renderMarkdown(
      "[论文](https://example.com) ![远程图](https://example.com/pixel.png)",
    );
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).not.toContain("<img");
  });
});
describe("历史分页", () => {
  it("按后端 seq 读取全部页面，允许缺口，不重新编号", async () => {
    const pages = [
      Array.from({ length: 100 }, (_, i) => ({
        seq: i + 1,
        role: i % 2 ? "ai" : "human",
        content: String(i),
      })),
      [{ seq: 104, role: "human", content: "尾部" }],
    ];
    const after: number[] = [];
    const result = await readFullHistory(async (seq) => {
      after.push(seq);
      return pages.shift() || [];
    });
    expect(after).toEqual([0, 100]);
    expect(result).toHaveLength(101);
    expect(result.at(-1)?.seq).toBe(104);
  });
  it("分页回退时中止读取，避免无限循环", async () => {
    await expect(
      readFullHistory(async () => [{ seq: 0, role: "ai", content: "" }]),
    ).rejects.toThrow("顺序");
  });
  it("读取失败时不把部分历史伪装成完整历史", async () => {
    await expect(
      readFullHistory(async () => {
        throw new Error("503");
      }),
    ).rejects.toThrow("503");
  });
});
describe("日志字节游标", () => {
  it("使用服务器偏移，跨页 UTF-8 字符不会损坏", () => {
    const log = new LogBuffer();
    const bytes = new TextEncoder().encode("训练");
    log.append({
      status: "running",
      raw_base64: btoa(String.fromCharCode(...bytes.slice(0, 2))),
      next_offset: 2,
      has_more: true,
    });
    log.append({
      status: "running",
      raw_base64: btoa(String.fromCharCode(...bytes.slice(2))),
      next_offset: 6,
      has_more: false,
    });
    expect(log.offset).toBe(6);
    expect(log.text).toBe("训练");
    expect(log.hasMore).toBe(false);
  });
  it("拒绝被替换或缩短的日志，不覆盖原片段", () => {
    const log = new LogBuffer();
    log.append({ content: "abc", next_offset: 3 });
    expect(() => log.append({ source_changed: true, next_offset: 0 })).toThrow(
      "变化",
    );
    expect(log.text).toBe("abc");
  });
  it("有界保留页面日志，可显式从头读取", () => {
    const log = new LogBuffer();
    log.append({ content: "x".repeat(300000), next_offset: 300000 });
    expect(log.text.length).toBe(262144);
    expect(log.clipped).toBe(true);
    log.reset();
    expect(log.offset).toBe(0);
    expect(log.text).toBe("");
  });
});
it("UUID 展示格式不同仍能匹配，登录返回路径不允许外站", () => {
  expect(
    sameId(
      "3fa85f6457174562b3fc2c963f66afa6",
      "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    ),
  ).toBe(true);
  for (const path of ["https://evil.test", "//evil.test", "/\\evil.test"])
    expect(safeReturnPath(path)).toBe("/chat");
  expect(safeReturnPath("/runs/123?tab=logs")).toBe("/runs/123?tab=logs");
});
