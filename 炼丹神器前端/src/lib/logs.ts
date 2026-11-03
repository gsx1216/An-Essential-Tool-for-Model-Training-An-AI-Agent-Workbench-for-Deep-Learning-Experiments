import { record } from "../types/domain";
export class LogBuffer {
  offset = 0;
  text = "";
  clipped = false;
  hasMore = false;
  private decoder = new TextDecoder();
  reset() {
    this.offset = 0;
    this.text = "";
    this.clipped = false;
    this.hasMore = false;
    this.decoder = new TextDecoder();
  }
  append(value: unknown) {
    const data = record(value);
    if (data.source_changed)
      throw new Error("远端日志发生变化，请清空当前日志后重新读取。");
    if (data.status === "missing") throw new Error("远端尚未发现日志文件。");
    if (
      typeof data.next_offset !== "number" ||
      !Number.isInteger(data.next_offset) ||
      data.next_offset < this.offset
    )
      throw new Error("日志游标不符合接口要求。");
    let text = typeof data.content === "string" ? data.content : "";
    if (typeof data.raw_base64 === "string") {
      const bytes = Uint8Array.from(atob(data.raw_base64), (char) =>
        char.charCodeAt(0),
      );
      text = this.decoder.decode(bytes, { stream: true });
    }
    this.text += text;
    this.offset = data.next_offset;
    this.hasMore = data.has_more === true;
    if (this.text.length > 262144) {
      this.text = this.text.slice(-262144);
      this.clipped = true;
    }
  }
}
