import { beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus, { ElMessageBox } from "element-plus";
import { api } from "../src/api";
import { useWorkspace } from "../src/stores/workspace";
import LibraryView from "../src/views/LibraryView.vue";
import type { Schema } from "../src/types/domain";

vi.mock("../src/api", () => ({
  api: {
    papers: vi.fn(),
    paper: vi.fn(),
    chunks: vi.fn(),
    skills: vi.fn(),
    createSkill: vi.fn(),
    deletePaper: vi.fn(),
    deleteSkill: vi.fn(),
  },
}));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
const poll = vi.hoisted(() => ({
  operation: null as null | ((signal: AbortSignal) => Promise<void>),
}));
vi.mock("../src/composables/useSerialPoll", () => ({
  useSerialPoll: (_enabled: unknown, operation: typeof poll.operation) => {
    poll.operation = operation;
  },
}));
beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  vi.mocked(api.papers).mockResolvedValue({
    items: [],
    offset: 0,
    next_offset: null,
  });
  vi.mocked(api.skills).mockResolvedValue({ items: [] });
});
it("资料卡片确认删除后调用本人资料删除接口并移除卡片", async () => {
  const paper = {
    paper_id: "d".repeat(32),
    request_id: "b".repeat(32),
    title: "待删除论文",
    status: "ready",
    error_code: null,
    file_sha256: "c".repeat(64),
    file_bytes: 100,
    page_count: 2,
    chunk_count: 2,
    empty_pages: [],
    parser_version: "test",
    created_at: "2026-10-29T00:00:00Z",
    updated_at: "2026-10-29T00:00:00Z",
  } satisfies Schema<"PaperResponse">;
  vi.mocked(api.papers).mockResolvedValueOnce({
    items: [paper],
    offset: 0,
    next_offset: null,
  });
  vi.mocked(api.deletePaper).mockResolvedValue(undefined);
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue(
    "confirm" as unknown as Awaited<ReturnType<typeof ElMessageBox.confirm>>,
  );
  const wrapper = render();
  await flushPromises();
  await wrapper
    .get('button[aria-label="删除资料 待删除论文"]')
    .trigger("click");
  await flushPromises();
  expect(api.deletePaper).toHaveBeenCalledWith(paper.paper_id);
  expect(
    wrapper.find('button[aria-label="删除资料 待删除论文"]').exists(),
  ).toBe(false);
  wrapper.unmount();
});
function render() {
  return mount(LibraryView, {
    global: {
      plugins: [ElementPlus],
      stubs: {
        teleport: true,
        // 本用例覆盖表单与状态更新，隔离依赖真实布局的第三方页签/下拉框。
        ElTabs: { name: "ElTabs", template: "<div><slot /></div>" },
        ElTabPane: true,
        ElSelect: { template: "<div><slot /></div>" },
        ElOption: true,
      },
    },
  });
}
it("个人 Skill 表单保存名称和提示词，并加入本人的选择列表", async () => {
  const created = {
    skill_id: "personal-" + "a".repeat(32),
    title: "论文分析",
    description: "个人工作流",
    version: "1.0.0",
    sha256: "b".repeat(64),
    instructions: "先读取论文方法，再核对代码。",
  };
  vi.mocked(api.createSkill).mockResolvedValue(created);
  const wrapper = render();
  await flushPromises();
  wrapper
    .findComponent({ name: "ElTabs" })
    .vm.$emit("update:modelValue", "skills");
  await flushPromises();
  await wrapper
    .findAll("button")
    .find((b) => b.text().includes("新增个人 Skill"))!
    .trigger("click");
  await flushPromises();
  await wrapper.find('input[maxlength="80"]').setValue("论文分析");
  await wrapper.find("textarea").setValue(created.instructions);
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "保存个人 Skill")!
    .trigger("click");
  await flushPromises();
  expect(api.createSkill).toHaveBeenCalledWith({
    title: "论文分析",
    instructions: created.instructions,
  });
  expect(useWorkspace().skills[0]?.skill_id).toBe(created.skill_id);
  expect(wrapper.text()).toContain("用于新问题");
  wrapper.unmount();
});
it("正在解析的论文刷新为 ready 后自动展示正文索引", async () => {
  const paper = {
    paper_id: "a".repeat(32),
    request_id: "b".repeat(32),
    title: "RDNet",
    status: "processing",
    error_code: null,
    file_sha256: "c".repeat(64),
    file_bytes: 100,
    page_count: 0,
    chunk_count: 0,
    empty_pages: [],
    parser_version: null,
    created_at: "2026-10-29T00:00:00Z",
    updated_at: "2026-10-29T00:00:00Z",
  } satisfies Schema<"PaperResponse">;
  vi.mocked(api.papers).mockResolvedValue({
    items: [paper],
    offset: 0,
    next_offset: null,
  });
  vi.mocked(api.paper).mockResolvedValue({
    ...paper,
    status: "ready",
    page_count: 13,
    chunk_count: 17,
  });
  vi.mocked(api.chunks).mockResolvedValue({
    items: [],
    offset: 0,
    next_offset: null,
  });
  const wrapper = render();
  await flushPromises();
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "查看正文与索引")!
    .trigger("click");
  await flushPromises();
  await poll.operation!(new AbortController().signal);
  await flushPromises();
  expect(api.paper).toHaveBeenCalledWith(paper.paper_id);
  expect(api.chunks).toHaveBeenCalledWith(paper.paper_id, 0);
  expect(wrapper.text()).toContain("13 页");
  wrapper.unmount();
});

it("删除个人 Skill 后移除卡片并清除当前选择，公共模板不提供删除", async () => {
  const personal = {
    skill_id: "personal-" + "a".repeat(32),
    title: "个人流程",
    description: "本人使用",
    version: "1.0.0",
    sha256: "b".repeat(64),
  };
  const publicSkill = {
    ...personal,
    skill_id: "public-workflow",
    title: "公共模板",
  };
  vi.mocked(api.skills).mockResolvedValue({ items: [personal, publicSkill] });
  vi.mocked(api.deleteSkill).mockResolvedValue(undefined);
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue(
    "confirm" as unknown as Awaited<ReturnType<typeof ElMessageBox.confirm>>,
  );
  const wrapper = render();
  await flushPromises();
  useWorkspace().selectedSkill = personal.skill_id;
  wrapper
    .findComponent({ name: "ElTabs" })
    .vm.$emit("update:modelValue", "skills");
  await flushPromises();
  expect(
    wrapper.find('button[aria-label="删除个人 Skill 公共模板"]').exists(),
  ).toBe(false);
  await wrapper
    .get('button[aria-label="删除个人 Skill 个人流程"]')
    .trigger("click");
  await flushPromises();
  expect(api.deleteSkill).toHaveBeenCalledWith(personal.skill_id);
  expect(useWorkspace().selectedSkill).toBeNull();
  expect(useWorkspace().skills.map((item) => item.skill_id)).toEqual([
    publicSkill.skill_id,
  ]);
  wrapper.unmount();
});
