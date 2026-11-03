import { beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import ElementPlus, { ElSelect } from "element-plus";
import TrainingTaskForm from "../src/components/TrainingTaskForm.vue";
import { api } from "../src/api";
import type { Server } from "../src/types/domain";
vi.mock("../src/api", () => ({
  api: {
    projectEnvironments: vi.fn(),
    projectFiles: vi.fn(),
    serverResources: vi.fn(),
  },
}));
const server = {
  server_id: "a".repeat(32),
  name: "My server",
  host: "127.0.0.1",
  work_dir: "/project",
} as Server;
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.serverResources).mockResolvedValue({
    server_id: server.server_id,
    work_dir: "/project",
    gpu_status: "available",
    gpus: [],
  });
  vi.mocked(api.projectEnvironments).mockResolvedValue({
    server_id: server.server_id,
    work_dir: "/project",
    items: [
      {
        name: "mynet",
        python: "/opt/conda/envs/mynet/bin/python",
        kind: "conda",
      },
    ],
    truncated: false,
  });
  vi.mocked(api.projectFiles).mockResolvedValue({
    server_id: server.server_id,
    work_dir: "/project",
    path: ".",
    items: [{ path: "train.py", kind: "file" }],
    next_offset: null,
  });
});
function render() {
  return mount(TrainingTaskForm, {
    props: {
      experiments: [],
      servers: [server, { ...server, server_id: "b".repeat(32) }],
      scripts: [],
      initialExperiment: "e".repeat(32),
      busy: false,
      error: "",
    },
    global: {
      plugins: [ElementPlus],
      stubs: {
        ElSelect: { name: "ElSelect", template: "<div><slot /></div>" },
        ElOption: true,
        SectionState: true,
      },
    },
  });
}
it("选择服务器读取真实资源并将选定文件和环境提交为个人项目", async () => {
  const wrapper = render();
  wrapper
    .findAllComponents(ElSelect)[1]!
    .vm.$emit("update:modelValue", server.server_id);
  await flushPromises();
  expect(api.projectEnvironments).toHaveBeenCalledWith(server.server_id);
  expect(api.projectFiles).toHaveBeenCalledWith(server.server_id, ".", 0);
  await wrapper.find('input[maxlength="80"]').setValue("我的训练");
  wrapper
    .findAllComponents(ElSelect)[2]!
    .vm.$emit("update:modelValue", "/opt/conda/envs/mynet/bin/python");
  await wrapper
    .findAll("button")
    .find((b) => b.text().includes("train.py"))!
    .trigger("click");
  await wrapper.find("form").trigger("submit");
  await flushPromises();
  const body = wrapper.emitted("submit")![0]![0] as Record<string, unknown>;
  expect(body).toMatchObject({
    name: "我的训练",
    server_id: server.server_id,
    project: {
      entrypoint: "train.py",
      python: "/opt/conda/envs/mynet/bin/python",
    },
  });
  expect(body).not.toHaveProperty("script_id");
  wrapper.unmount();
});
it("切换服务器后迟到的环境结果不能覆盖新服务器", async () => {
  let finish!: (
    value: Awaited<ReturnType<typeof api.projectEnvironments>>,
  ) => void;
  vi.mocked(api.projectEnvironments).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const wrapper = render();
  wrapper
    .findAllComponents(ElSelect)[1]!
    .vm.$emit("update:modelValue", server.server_id);
  await flushPromises();
  wrapper
    .findAllComponents(ElSelect)[1]!
    .vm.$emit("update:modelValue", "b".repeat(32));
  await flushPromises();
  finish({
    server_id: server.server_id,
    work_dir: "/old",
    items: [
      { name: "old-environment", python: "/old/bin/python", kind: "venv" },
    ],
    truncated: false,
  });
  await flushPromises();
  expect(wrapper.html()).not.toContain("old-environment");
  wrapper.unmount();
});
