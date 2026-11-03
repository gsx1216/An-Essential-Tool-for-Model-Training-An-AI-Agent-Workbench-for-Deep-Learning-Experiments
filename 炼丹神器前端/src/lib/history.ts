import type { Message } from "../types/domain";
export async function readFullHistory(
  read: (after: number) => Promise<Message[]>,
  onPage?: (count: number) => void,
): Promise<Message[]> {
  const messages: Message[] = [];
  let after = 0;
  while (true) {
    const page = await read(after);
    if (!page.length) break;
    if (
      page.some(
        (message, index) =>
          message.seq <= (index ? page[index - 1].seq : after),
      )
    )
      throw new Error("历史消息顺序不符合接口要求，请重新读取。");
    messages.push(...page);
    after = page[page.length - 1].seq;
    onPage?.(messages.length);
    if (page.length < 100) break;
  }
  return messages;
}
