import { expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import ElementPlus from "element-plus";
import ExperimentForm from "../src/components/ExperimentForm.vue";

function render() {
  return mount(ExperimentForm, {
    props: { busy: false, error: "" },
    global: {
      plugins: [ElementPlus],
      stubs: {
        ElSelect: { name: "ElSelect", template: "<div><slot /></div>" },
        ElOption: true,
      },
    },
  });
}
async function fill(wrapper: ReturnType<typeof render>) {
  async function input(label: string, text: string) {
    const item = wrapper
      .findAll(".el-form-item")
      .find((row) => row.find(".el-form-item__label").text() === label)!;
    await item.get("input").setValue(text);
  }
  await input("实验标题", "大小写测试");
  await input("方案标识", "COD-Evaluation");
  await input("方案版本", "V1");
  await input("数据集 ID", "Tiny_COD");
  await input("版本与划分", "官方验证集");
  await input("指标名（与脚本上报一致）", "MAE");
  await input("单位", "ratio");
  const names = wrapper
    .findAll(".el-form-item")
    .filter((row) => row.find(".el-form-item__label").text() === "显示名称");
  await names[0]!.get("input").setValue("Tiny COD 数据集");
  await names[1]!.get("input").setValue("MAE 平均绝对误差");
}
async function submit(wrapper: ReturnType<typeof render>) {
  await wrapper
    .findAll("button")
    .find((button) => button.text() === "创建实验")!
    .trigger("click");
}
it("评测标识可填大写，主要指标选项与提交统一小写，显示名称保持原样", async () => {
  const wrapper = render();
  await fill(wrapper);
  const primary = wrapper
    .findAll(".el-form-item")
    .find((row) => row.find(".el-form-item__label").text() === "主要指标")!;
  expect(primary.findComponent({ name: "ElOption" }).props("value")).toBe(
    "mae",
  );
  primary
    .findComponent({ name: "ElSelect" })
    .vm.$emit("update:modelValue", "MAE");
  await submit(wrapper);
  const body = wrapper.emitted("save")![0]![0] as {
    evaluation: Record<string, unknown>;
  };
  expect(body.evaluation).toMatchObject({
    protocol_id: "cod-evaluation",
    protocol_version: "V1",
    primary_metric: "mae",
    datasets: [{ id: "tiny_cod", display_name: "Tiny COD 数据集" }],
    metrics: [{ name: "mae", display_name: "MAE 平均绝对误差" }],
  });
  wrapper.unmount();
});
it("忽略大小写后重复的指标仍不能提交", async () => {
  const wrapper = render();
  await fill(wrapper);
  await wrapper
    .findAll("button")
    .find((button) => button.text().includes("添加指标"))!
    .trigger("click");
  const definitions = wrapper.findAll(".metric-definition");
  const inputs = definitions[1]!.findAll("input");
  await inputs[0]!.setValue("mae");
  await inputs[1]!.setValue("重复指标");
  await inputs[2]!.setValue("ratio");
  await submit(wrapper);
  expect(wrapper.emitted("save")).toBeUndefined();
  expect(wrapper.text()).toContain("数据集 ID 或指标名不能重复");
  wrapper.unmount();
});
