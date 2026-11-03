import { expect, it } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import ElementPlus from "element-plus";
import MetricParserForm from "../src/components/MetricParserForm.vue";
const existing = {
  pattern: "Epoch: (?P<epoch>\\d+) MAE: (?P<mae>[0-9.]+)",
  dataset: "custom",
  split: "validation",
  metrics: ["mae"],
};
function render(rules = [] as (typeof existing)[]) {
  return mount(MetricParserForm, {
    props: { rules, busy: false, error: "" },
    global: { plugins: [ElementPlus] },
  });
}
it("中文表单保留已有规则，提交时不包含界面内部键", async () => {
  const wrapper = render([existing]);
  await flushPromises();
  expect(wrapper.text()).toContain("日志匹配表达式");
  expect(wrapper.text()).toContain("验证集");
  expect(wrapper.find('textarea[aria-label="指标解析规则"]').exists()).toBe(
    false,
  );
  await wrapper.get("form").trigger("submit");
  expect(wrapper.emitted("save")![0]![0]).toEqual({ rules: [existing] });
  wrapper.unmount();
});
it("缺少匹配表达式和指标时指出具体规则，不发送配置", async () => {
  const wrapper = render();
  await wrapper
    .findAll("button")
    .find((button) => button.text() === "新增解析规则")!
    .trigger("click");
  await wrapper.get("form").trigger("submit");
  expect(wrapper.emitted("save")).toBeUndefined();
  expect(wrapper.text()).toContain("第 1 条规则");
  wrapper.unmount();
});
it("通用示例可提交，留空数据集使用默认值，规则可删除", async () => {
  const wrapper = render();
  await wrapper
    .findAll("button")
    .find((button) => button.text() === "填入通用示例")!
    .trigger("click");
  await flushPromises();
  await wrapper.get("form").trigger("submit");
  const body = wrapper.emitted("save")![0]![0] as {
    rules: (typeof existing)[];
  };
  expect(body.rules[0]!.pattern).toContain("(?P<step>");
  expect(body.rules[0]!.dataset).toBe("unspecified");
  expect(wrapper.text()).not.toContain("PRNet");
  await wrapper.get('button[aria-label="删除第 1 条规则"]').trigger("click");
  await wrapper.get("form").trigger("submit");
  expect(wrapper.emitted("save")![1]![0]).toEqual({ rules: [] });
  wrapper.unmount();
});

it("忽略指标和数据集大小写，保持正则表达式原样", async () => {
  const rule = {
    ...existing,
    pattern: "Epoch: (?P<EPOCH>\\d+) MAE: (?P<MAE>[0-9.]+)",
    dataset: "Tiny_COD",
    metrics: ["MAE"],
  };
  const wrapper = render([rule]);
  await wrapper.get("form").trigger("submit");
  expect(wrapper.emitted("save")![0]![0]).toEqual({
    rules: [{ ...rule, dataset: "tiny_cod", metrics: ["mae"] }],
  });
  expect(wrapper.text()).toContain("大小写可不同");
  wrapper.unmount();
});
