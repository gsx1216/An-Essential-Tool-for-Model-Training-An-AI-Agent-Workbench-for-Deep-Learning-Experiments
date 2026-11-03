import { beforeEach, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus, { ElMessageBox } from "element-plus";
import WorkspaceLayout from "../src/layouts/WorkspaceLayout.vue";
import ServerGpuInfo from "../src/components/ServerGpuInfo.vue";
import { api } from "../src/api";
vi.mock("../src/api", () => ({
  api: {
    sessions: vi.fn(),
    skills: vi.fn(),
    createSession: vi.fn(),
    serverResources: vi.fn(),
  },
}));
vi.mock("../src/stores/auth", () => ({
  useAuth: () => ({ user: { user_id: 7, username: "test" }, token: "fake" }),
}));
vi.mock("../src/stores/chat", () => ({ useChat: () => ({ clear: vi.fn() }) }));
vi.mock("vue-router", () => ({
  useRoute: () => ({ path: "/chat", fullPath: "/chat", params: {}, meta: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
beforeEach(() => {
  vi.restoreAllMocks();
  setActivePinia(createPinia());
  vi.mocked(api.sessions).mockResolvedValue([]);
  vi.mocked(api.skills).mockResolvedValue({ items: [] });
  vi.mocked(api.createSession).mockResolvedValue({
    session_id: "a".repeat(32),
  });
});
it("新聊天提交用户填写的标题", async () => {
  vi.spyOn(ElMessageBox, "prompt").mockResolvedValue({
    value: "我的论文讨论",
    action: "confirm",
  } as unknown as Awaited<ReturnType<typeof ElMessageBox.prompt>>);
  const wrapper = mount(WorkspaceLayout, {
    global: {
      plugins: [ElementPlus],
      stubs: { RouterLink: true, RouterView: true },
    },
  });
  await flushPromises();
  await wrapper.get("button.new-chat").trigger("click");
  await flushPromises();
  expect(api.createSession).toHaveBeenCalledWith("我的论文讨论");
  wrapper.unmount();
});
it("GPU 面板显示实际数量、显存和利用率", async () => {
  vi.mocked(api.serverResources).mockResolvedValue({
    server_id: "a".repeat(32),
    work_dir: "/project",
    gpu_status: "available",
    gpus: [
      {
        index: 0,
        name: "RTX 4090",
        memory_total_mb: 24564,
        memory_used_mb: 1000,
        utilization_percent: 10,
      },
    ],
  });
  const wrapper = mount(ServerGpuInfo, {
    props: { serverId: "a".repeat(32) },
    global: { plugins: [ElementPlus] },
  });
  await flushPromises();
  expect(wrapper.text()).toContain("检测到 1 张 NVIDIA GPU");
  expect(wrapper.text()).toContain("1000 / 24564 MiB");
  expect(wrapper.text()).toContain("10%");
  wrapper.unmount();
});
