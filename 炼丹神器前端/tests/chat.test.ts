import { beforeEach, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { reactive } from "vue";
import ElementPlus from "element-plus";
import ChatView from "../src/views/ChatView.vue";
import { api } from "../src/api";
import { useWorkspace } from "../src/stores/workspace";
import { ApiError } from "../src/api/client";
const state = vi.hoisted(() => ({
  route: null as unknown as {
    params: { sessionId: string };
    query: Record<string, string>;
  },
}));
vi.mock("vue-router", () => ({
  useRoute: () => state.route,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));
vi.mock("../src/api", () => ({
  api: {
    history: vi.fn(),
    calls: vi.fn(),
    sendStream: vi.fn(),
    retryStream: vi.fn(),
  },
}));
let pinia: ReturnType<typeof createPinia>;
beforeEach(() => {
  vi.resetAllMocks();
  pinia = createPinia();
  setActivePinia(pinia);
  state.route = reactive({ params: { sessionId: "first" }, query: {} });
  vi.mocked(api.history).mockResolvedValue([]);
  vi.mocked(api.calls).mockResolvedValue({
    active_run_id: null,
    items: [],
    next_after_id: null,
  });
});
function render() {
  return mount(ChatView, {
    global: {
      plugins: [pinia, ElementPlus],
      stubs: {
        RunSummary: true,
        DataTree: true,
        BrandMark: true,
        RouterLink: true,
      },
    },
  });
}
it("网络失败重试保留同一问题、UUID 和 Skill；不偷偷新建训练请求", async () => {
  useWorkspace().selectedSkill = "cod-experiment-analysis";
  vi.mocked(api.sendStream)
    .mockRejectedValueOnce(new ApiError("连接中断", 0))
    .mockResolvedValueOnce("ok");
  const wrapper = render();
  await flushPromises();
  await wrapper
    .get('textarea[aria-label="输入消息"]')
    .setValue("请执行我选择的任务");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  const first = vi.mocked(api.sendStream).mock.calls[0]!;
  expect(first[0]).toBe("first");
  expect(first[1].skill_id).toBe("cod-experiment-analysis");
  const retry = wrapper
    .findAll("button")
    .find((button) => button.text().includes("重试本轮"))!;
  await retry.trigger("click");
  await flushPromises();
  expect(vi.mocked(api.sendStream).mock.calls[1]?.slice(0, 2)).toEqual(
    first.slice(0, 2),
  );
  expect(api.retryStream).not.toHaveBeenCalled();
  wrapper.unmount();
});
it("切换会话后，迟到的发送结果不会添加到新会话", async () => {
  vi.mocked(api.history).mockImplementation(async (id) =>
    id === "second"
      ? [
          { seq: 1, role: "human", content: "第二个会话" },
          { seq: 2, role: "ai", content: "属于第二个会话的回复" },
        ]
      : [],
  );
  let finish: (value: string) => void = () => undefined;
  vi.mocked(api.sendStream).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const wrapper = render();
  await flushPromises();
  await wrapper
    .get('textarea[aria-label="输入消息"]')
    .setValue("第一个会话的问题");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  state.route.params.sessionId = "second";
  await flushPromises();
  finish("第一个会话的迟到回复");
  await flushPromises();
  expect(wrapper.text()).toContain("属于第二个会话的回复");
  expect(wrapper.text()).not.toContain("第一个会话的迟到回复");
  expect(wrapper.text()).not.toContain("第一个会话的问题");
  wrapper.unmount();
});
it("重新打开末条为 human 的会话，提供继续本轮而不是追加伪造回复", async () => {
  vi.mocked(api.history).mockResolvedValue([
    { seq: 5, role: "human", content: "尚未完成的问题" },
  ]);
  const wrapper = render();
  await flushPromises();
  expect(wrapper.text()).toContain("重试最后一轮");
  expect(wrapper.findAll("article")).toHaveLength(1);
  expect(wrapper.get("textarea").attributes("disabled")).toBeDefined();
  wrapper.unmount();
});
