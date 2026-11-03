import { beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus, { ElMessageBox } from "element-plus";
import { api } from "../src/api";
import TrainingView from "../src/views/TrainingView.vue";

vi.mock("../src/api", () => ({
  api: {
    experiments: vi.fn(),
    tasks: vi.fn(),
    servers: vi.fn(),
    scripts: vi.fn(),
    deleteTask: vi.fn(),
  },
}));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
const task = {
  task_id: "a".repeat(32),
  name: "删除测试",
  script_id: "test_train",
  experiment_id: "b".repeat(32),
  server_id: "c".repeat(32),
};
// Element Plus 的声明将弹窗两种返回形式写成了交叉类型；confirm 实际返回 action 字符串。
const confirmed = "confirm" as unknown as Awaited<
  ReturnType<typeof ElMessageBox.confirm>
>;
beforeEach(() => {
  vi.resetAllMocks();
  setActivePinia(createPinia());
  vi.mocked(api.experiments).mockResolvedValue({
    items: [],
    next_cursor: null,
  });
  vi.mocked(api.tasks).mockResolvedValue({ items: [task], next_offset: null });
  vi.mocked(api.servers).mockResolvedValue({ items: [], next_cursor: null });
  vi.mocked(api.scripts).mockResolvedValue({ items: [] });
});
async function render() {
  const wrapper = mount(TrainingView, {
    global: {
      plugins: [ElementPlus],
      stubs: {
        RouterLink: { template: "<span><slot /></span>" },
        ElTabs: { name: "ElTabs", template: "<div><slot /></div>" },
        ElTabPane: true,
        ExperimentForm: true,
        TrainingTaskForm: true,
      },
    },
  });
  await flushPromises();
  wrapper
    .findComponent({ name: "ElTabs" })
    .vm.$emit("update:modelValue", "tasks");
  await flushPromises();
  return wrapper;
}
it("确认删除后调用配置删除接口并重新加载列表", async () => {
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue(confirmed);
  const wrapper = await render();
  vi.mocked(api.tasks).mockResolvedValue({ items: [], next_offset: null });
  await wrapper
    .get('button[aria-label="删除训练配置 删除测试"]')
    .trigger("click");
  await flushPromises();
  expect(api.deleteTask).toHaveBeenCalledWith(task.task_id);
  expect(
    wrapper.find('button[aria-label="删除训练配置 删除测试"]').exists(),
  ).toBe(false);
  wrapper.unmount();
});
it("取消确认不发送删除请求", async () => {
  vi.spyOn(ElMessageBox, "confirm").mockRejectedValue("cancel");
  const wrapper = await render();
  await wrapper
    .get('button[aria-label="删除训练配置 删除测试"]')
    .trigger("click");
  await flushPromises();
  expect(api.deleteTask).not.toHaveBeenCalled();
  expect(
    wrapper.find('button[aria-label="删除训练配置 删除测试"]').exists(),
  ).toBe(true);
  wrapper.unmount();
});
it("删除失败保留卡片", async () => {
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue(confirmed);
  vi.mocked(api.deleteTask).mockRejectedValue(new Error("请稍后重试。"));
  const wrapper = await render();
  await wrapper
    .get('button[aria-label="删除训练配置 删除测试"]')
    .trigger("click");
  await flushPromises();
  expect(
    wrapper.find('button[aria-label="删除训练配置 删除测试"]').exists(),
  ).toBe(true);
  wrapper.unmount();
});
