import { afterEach, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { defineComponent, h, ref } from "vue";
import { useSerialPoll } from "../src/composables/useSerialPoll";
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});
it("慢请求未完成时不叠加轮询，卸载后不再读取", async () => {
  vi.useFakeTimers();
  let finish: () => void = () => undefined;
  const read = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
  );
  const wrapper = mount(
    defineComponent({
      setup() {
        useSerialPoll(ref(true), read, 7000);
        return () => h("div");
      },
    }),
  );
  await vi.advanceTimersByTimeAsync(30000);
  expect(read).toHaveBeenCalledOnce();
  finish();
  await flushPromises();
  await vi.advanceTimersByTimeAsync(7000);
  expect(read).toHaveBeenCalledTimes(2);
  wrapper.unmount();
  finish();
  await flushPromises();
  await vi.advanceTimersByTimeAsync(30000);
  expect(read).toHaveBeenCalledTimes(2);
});
it("页面隐藏时中断当前读取，恢复可见后继续", async () => {
  vi.useFakeTimers();
  let hidden = false;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  const read = vi.fn(async (_signal: AbortSignal) => undefined);
  const wrapper = mount(
    defineComponent({
      setup() {
        useSerialPoll(ref(true), read, 7000);
        return () => h("div");
      },
    }),
  );
  await flushPromises();
  hidden = true;
  document.dispatchEvent(new Event("visibilitychange"));
  expect(read.mock.calls[0][0].aborted).toBe(true);
  await vi.advanceTimersByTimeAsync(30000);
  expect(read).toHaveBeenCalledOnce();
  hidden = false;
  document.dispatchEvent(new Event("visibilitychange"));
  await flushPromises();
  expect(read).toHaveBeenCalledTimes(2);
  wrapper.unmount();
});
