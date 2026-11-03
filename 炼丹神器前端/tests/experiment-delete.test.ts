import { beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus, { ElMessageBox } from "element-plus";
import TrainingView from "../src/views/TrainingView.vue";
import { api } from "../src/api";
import type { Schema } from "../src/types/domain";

vi.mock("../src/api", () => ({
  api: {
    experiments: vi.fn(),
    experiment: vi.fn(),
    runs: vi.fn(),
    tasks: vi.fn(),
    servers: vi.fn(),
    scripts: vi.fn(),
    deleteExperiment: vi.fn(),
    deleteRun: vi.fn(),
  },
}));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
const stamp = "2026-10-30T00:00:00Z";
const experiment = {
  experiment_id: "a".repeat(32),
  title: "删除验收",
  kind: "training",
  description: "测试实验",
  created_at: stamp,
  updated_at: stamp,
} satisfies Schema<"ExperimentResponse">;
const run = {
  run_id: "b".repeat(32),
  experiment_id: experiment.experiment_id,
  experiment_title: experiment.title,
  experiment_kind: "training",
  status: "cancelled",
  created_at: stamp,
  updated_at: stamp,
  started_at: stamp,
  finished_at: stamp,
  exit_code: null,
  error_code: null,
  has_logs: true,
  has_result: true,
  result_version: 1,
} satisfies Schema<"RunListItem">;
const confirmed = "confirm" as unknown as Awaited<
  ReturnType<typeof ElMessageBox.confirm>
>;
beforeEach(() => {
  vi.resetAllMocks();
  setActivePinia(createPinia());
  vi.mocked(api.experiments).mockResolvedValue({
    items: [experiment],
    next_cursor: null,
  });
  vi.mocked(api.experiment).mockResolvedValue(experiment);
  vi.mocked(api.runs).mockResolvedValue({ items: [run], next_cursor: null });
  vi.mocked(api.tasks).mockResolvedValue({ items: [], next_offset: null });
  vi.mocked(api.servers).mockResolvedValue({ items: [], next_cursor: null });
  vi.mocked(api.scripts).mockResolvedValue({ items: [] });
});
async function render() {
  const wrapper = mount(TrainingView, {
    global: {
      plugins: [ElementPlus],
      stubs: {
        RouterLink: { template: "<span><slot /></span>" },
        ElSelect: { template: "<div><slot /></div>" },
        ElOption: true,
        ElTabs: { template: "<div><slot /></div>" },
        ElTabPane: true,
        ExperimentForm: true,
        TrainingTaskForm: true,
      },
    },
  });
  await flushPromises();
  return wrapper;
}
it("删除实验明确数据库范围，成功后移除实验及运行卡片", async () => {
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue(confirmed);
  const wrapper = await render();
  vi.mocked(api.experiments).mockResolvedValue({
    items: [],
    next_cursor: null,
  });
  await wrapper
    .get('button[aria-label="永久删除实验 删除验收"]')
    .trigger("click");
  await flushPromises();
  expect(api.deleteExperiment).toHaveBeenCalledWith(experiment.experiment_id);
  expect(ElMessageBox.confirm).toHaveBeenCalledWith(
    expect.stringContaining("数据库记录将被实际删除"),
    expect.anything(),
    expect.anything(),
  );
  expect(
    wrapper.find('button[aria-label="永久删除运行 删除验收"]').exists(),
  ).toBe(false);
  wrapper.unmount();
});
it("只删除一条运行，实验继续保留", async () => {
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue(confirmed);
  const wrapper = await render();
  vi.mocked(api.runs).mockResolvedValue({ items: [], next_cursor: null });
  await wrapper
    .get('button[aria-label="永久删除运行 删除验收"]')
    .trigger("click");
  await flushPromises();
  expect(api.deleteRun).toHaveBeenCalledWith(run.run_id);
  expect(api.deleteExperiment).not.toHaveBeenCalled();
  expect(
    wrapper.find('button[aria-label="永久删除实验 删除验收"]').exists(),
  ).toBe(true);
  wrapper.unmount();
});
it("取消删除不发送请求，服务失败保留卡片", async () => {
  vi.spyOn(ElMessageBox, "confirm")
    .mockRejectedValueOnce("cancel")
    .mockResolvedValue(confirmed);
  const wrapper = await render();
  const button = wrapper.get('button[aria-label="永久删除运行 删除验收"]');
  await button.trigger("click");
  await flushPromises();
  expect(api.deleteRun).not.toHaveBeenCalled();
  vi.mocked(api.deleteRun).mockRejectedValue(new Error("请稍后重试。"));
  await button.trigger("click");
  await flushPromises();
  expect(
    wrapper.find('button[aria-label="永久删除运行 删除验收"]').exists(),
  ).toBe(true);
  wrapper.unmount();
});
it("运行中的训练不提供可点击的永久删除", async () => {
  vi.mocked(api.runs).mockResolvedValue({
    items: [{ ...run, status: "running" }],
    next_cursor: null,
  });
  const wrapper = await render();
  expect(
    wrapper
      .get('button[aria-label="永久删除运行 删除验收"]')
      .attributes("disabled"),
  ).toBeDefined();
  wrapper.unmount();
});
