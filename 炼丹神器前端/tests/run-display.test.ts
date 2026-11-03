import { beforeEach, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import ElementPlus, { ElSelect } from "element-plus";
import { api } from "../src/api";
import { ApiError } from "../src/api/client";
import type { Experiment, Run } from "../src/types/domain";
import RunSummary from "../src/components/RunSummary.vue";
import TrainingView from "../src/views/TrainingView.vue";
vi.mock("../src/api", () => ({
  api: {
    run: vi.fn(),
    execution: vi.fn(),
    metrics: vi.fn(),
    metricLatest: vi.fn(),
    experiments: vi.fn(),
    experiment: vi.fn(),
    runs: vi.fn(),
    tasks: vi.fn(),
    scripts: vi.fn(),
    servers: vi.fn(),
  },
}));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
beforeEach(() => vi.resetAllMocks());
it("右侧指标末值取自最新指标接口，而不是历史首批图表数据", async () => {
  vi.mocked(api.run).mockResolvedValue({
    run_id: "run",
    experiment_title: "运行",
    status: "succeeded",
  } as Run);
  vi.mocked(api.execution).mockRejectedValue(new ApiError("未启动", 404));
  vi.mocked(api.metrics).mockResolvedValue({
    items: [
      {
        source_offset: 1,
        step: 1,
        dataset: "test",
        split: "validation",
        values: { loss: 999 },
      },
    ],
    next_offset: 500,
  });
  vi.mocked(api.metricLatest).mockResolvedValue({
    items: [
      {
        dataset: "test",
        split: "validation",
        metric_name: "loss",
        first_value: 999,
        last_value: 0.125,
        min_value: 0.125,
        max_value: 999,
      },
    ],
  });
  const wrapper = mount(RunSummary, {
    props: { runId: "run" },
    global: {
      plugins: [ElementPlus],
      stubs: { MetricChart: true, RouterLink: true },
    },
  });
  await flushPromises();
  expect(wrapper.find(".metric-value strong").text()).toBe("0.125");
  expect(api.metricLatest).toHaveBeenCalledWith("run", undefined);
  wrapper.unmount();
});
it("切换实验后，迟到的旧实验详情不覆盖新选择", async () => {
  const first = {
    experiment_id: "first",
    title: "旧实验详情",
    kind: "training",
    description: "",
    created_at: "2026-10-29T00:00:00Z",
    updated_at: "2026-10-29T00:00:00Z",
  } as Experiment;
  const second = { ...first, experiment_id: "second", title: "新实验详情" };
  let finish: (value: Experiment) => void = () => undefined;
  vi.mocked(api.experiments).mockResolvedValue({
    items: [first, second],
    next_cursor: null,
  });
  vi.mocked(api.experiment).mockImplementation((id) =>
    id === "first"
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : Promise.resolve(second),
  );
  vi.mocked(api.runs).mockResolvedValue({ items: [], next_cursor: null });
  vi.mocked(api.tasks).mockResolvedValue({ items: [], next_offset: null });
  vi.mocked(api.scripts).mockResolvedValue({ items: [] });
  vi.mocked(api.servers).mockResolvedValue({ items: [], next_cursor: null });
  const wrapper = mount(TrainingView, {
    global: {
      plugins: [ElementPlus],
      stubs: { RouterLink: true, ExperimentForm: true, ParameterFields: true },
    },
  });
  await flushPromises();
  wrapper.findComponent(ElSelect).vm.$emit("update:modelValue", "second");
  await flushPromises();
  finish(first);
  await flushPromises();
  expect(wrapper.get(".card h2").text()).toBe("新实验详情");
  wrapper.unmount();
});
