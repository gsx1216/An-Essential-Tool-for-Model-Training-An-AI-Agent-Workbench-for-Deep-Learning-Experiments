import { beforeEach, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import ElementPlus from "element-plus";
import RunAgents from "../src/components/RunAgents.vue";
import CodeFindingCard from "../src/components/CodeFindingCard.vue";
import NetworkFilePicker from "../src/components/NetworkFilePicker.vue";
import { api } from "../src/api";
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("../src/api", () => ({
  api: {
    agentTasks: vi.fn(),
    events: vi.fn(),
    analyses: vi.fn(),
    suggestions: vi.fn(),
    analysisReadiness: vi.fn(),
    startAnalysis: vi.fn(),
    saveMainNetwork: vi.fn(),
    files: vi.fn(),
    projectFiles: vi.fn(),
  },
}));
const ready = {
  report_ready: true,
  main_network: { path: "models/net.py" },
  processing: false,
  can_start: true,
  message: "可以手动提交",
  accepted: false,
};
beforeEach(() => {
  vi.resetAllMocks();
  for (const method of [
    api.agentTasks,
    api.events,
    api.analyses,
    api.suggestions,
  ])
    vi.mocked(method).mockResolvedValue([]);
  vi.mocked(api.analysisReadiness).mockResolvedValue(ready);
  vi.mocked(api.startAnalysis).mockResolvedValue({
    ...ready,
    accepted: true,
    message: "已提交",
  });
});
it("打开分析页只查询，点击按钮才手动触发，处理时阻止重复点击", async () => {
  const w = mount(RunAgents, {
    props: { runId: "a".repeat(32) },
    global: { plugins: [ElementPlus], stubs: { NetworkFilePicker: true } },
  });
  await flushPromises();
  expect(api.startAnalysis).not.toHaveBeenCalled();
  expect(w.text()).toContain("主网络代码与建议");
  const button = w
    .findAll("button")
    .find((item) => item.text() === "生成代码建议")!;
  vi.mocked(api.analysisReadiness).mockResolvedValue({
    ...ready,
    processing: true,
    can_start: false,
  });
  await button.trigger("click");
  await flushPromises();
  expect(api.startAnalysis).toHaveBeenCalledExactlyOnceWith("a".repeat(32));
  expect(button.attributes("disabled")).toBeDefined();
  w.unmount();
});
it("没有选择主网络时不能提交模型分析", async () => {
  vi.mocked(api.analysisReadiness).mockResolvedValue({
    ...ready,
    main_network: { path: null },
    can_start: false,
    message: "请先选择主网络 Python 文件。",
  });
  const w = mount(RunAgents, {
    props: { runId: "a".repeat(32) },
    global: { plugins: [ElementPlus], stubs: { NetworkFilePicker: true } },
  });
  await flushPromises();
  expect(w.text()).toContain("请先选择主网络");
  const button = w
    .findAll("button")
    .find((item) => item.text() === "生成代码建议")!;
  expect(button.attributes("disabled")).toBeDefined();
  expect(api.startAnalysis).not.toHaveBeenCalled();
  w.unmount();
});
it("主网络选择读取当前运行目录，并返回所选 py 文件", async () => {
  vi.mocked(api.files).mockResolvedValue({
    run_id: "a".repeat(32),
    retrieved_at: "2026-10-30T00:00:00Z",
    entry_path: "train.py",
    path: ".",
    offset: 0,
    next_offset: null,
    items: [{ path: "net.py", kind: "file" }],
  });
  const w = mount(NetworkFilePicker, {
    props: { runId: "a".repeat(32), modelValue: "" },
    global: {
      plugins: [ElementPlus],
      stubs: { ElDialog: { template: "<div><slot /></div>" } },
    },
  });
  await w
    .findAll("button")
    .find((item) => item.text() === "选择主网络文件")!
    .trigger("click");
  await flushPromises();
  expect(api.files).toHaveBeenCalledWith("a".repeat(32), ".", 0);
  await w
    .findAll("button")
    .find((item) => item.text().includes("net.py"))!
    .trigger("click");
  expect(w.emitted("update:modelValue")![0]).toEqual(["net.py"]);
  w.unmount();
});
it("代码诊断显示实际引用行和修改方向，不暴露数据库任务字段", () => {
  const finding = {
    problem: "待验证问题",
    hypothesis: "需实验验证",
    change: "检查连接",
    conditions: "固定数据",
    validation: "对照实验",
    assessment: "hypothesis" as const,
    evidence_paths: ["/status"],
    code: [{ evidence_id: "code_1", start_line: 2, end_line: 2 }],
  };
  const evidence = {
    evidence_id: "code_1",
    path: "models/net.py",
    file_sha256: "f".repeat(64),
    file_bytes: 30,
    modified_ns: 1,
    lines: [
      { number: 1, text: "import torch" },
      { number: 2, text: "return x + residual" },
    ],
    total_lines: 2,
    start_line: 1,
    end_line: 2,
    next_line: null,
    retrieved_at: "2026-10-30T00:00:00Z",
    version_scope: "current_file" as const,
    training_entry_match: null,
  };
  const w = mount(CodeFindingCard, {
    props: { finding, evidence: [evidence], index: 0 },
    global: { plugins: [ElementPlus] },
  });
  expect(w.text()).toContain("models/net.py");
  expect(w.text()).toContain("return x + residual");
  expect(w.text()).not.toContain("import torch");
  expect(w.text()).toContain("待验证推测");
  expect(w.text()).not.toContain("lease_token");
  w.unmount();
});

it("直接渲染普通 Markdown 建议，不读取旧报告分析或展示内部字段", async () => {
  vi.mocked(api.suggestions).mockResolvedValue([
    {
      task_id: "a".repeat(32),
      report_version: 1,
      is_stale: false,
      created_at: "2026-10-30T00:00:00Z",
      content: {
        direct_advice:
          "## 检查残差连接\n\n1. 在 `models/net.py` 第 2 行检查维度。\n\n```python\nreturn x + residual\n```\n\n用不同分辨率验证输出。",
        direct_input: { log_clipped: true, network_sha256: "INTERNAL_HASH" },
        suggestions: [],
        code_findings: [],
        limitations: [],
        evidence_status: "supported",
        source: { event_id: "INTERNAL_EVENT_ID" },
        facts: [{ path: "/status", value: "succeeded" }],
      },
    },
  ] as never);
  const w = mount(RunAgents, {
    props: { runId: "a".repeat(32) },
    global: { plugins: [ElementPlus], stubs: { NetworkFilePicker: true } },
  });
  await flushPromises();
  expect(w.find("h2").exists()).toBe(true);
  expect(w.find("pre code").text()).toContain("return x + residual");
  expect(w.text()).toContain("用不同分辨率验证输出");
  expect(w.text()).toContain("未提供中间部分");
  expect(api.analyses).not.toHaveBeenCalled();
  expect(api.startAnalysis).not.toHaveBeenCalled();
  for (const internal of [
    "evidence_paths",
    "INTERNAL_EVENT_ID",
    "/status",
    "INTERNAL_HASH",
    "已取得引用依据",
  ])
    expect(w.text()).not.toContain(internal);
  w.unmount();
});
