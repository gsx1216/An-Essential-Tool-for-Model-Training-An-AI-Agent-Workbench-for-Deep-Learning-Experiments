import { expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import TrainingTaskDetails from "../src/components/TrainingTaskDetails.vue";
import type { TrainingTask } from "../src/types/domain";

it("只展示中文业务字段，额外数据库字段和编号不会被展开", () => {
  const task = {
    task_id: "PRIVATE_TASK_ID",
    experiment_id: "PRIVATE_EXPERIMENT_ID",
    server_id: "PRIVATE_SERVER_ID",
    script_id: "project_PRIVATE_SCRIPT_ID",
    name: "点云训练",
    snapshot_json: "PRIVATE_STORED_JSON",
    configuration_sha256: "PRIVATE_HASH",
    default_parameters: { epochs: 20, learning_rate: 0.00005 },
    project: {
      entrypoint: "tools/train.py",
      python: "/opt/conda/envs/pointcloud/bin/python",
      cuda_visible_devices: "0,1",
      entry_sha256: "PRIVATE_SOURCE_HASH",
      max_runtime_seconds: 86400,
      max_log_bytes: 10485760,
    },
  } as TrainingTask;
  const wrapper = mount(TrainingTaskDetails, {
    props: { task, experimentName: "点云分类实验", serverName: "实验室服务器" },
  });
  const text = wrapper.text();
  for (const expected of [
    "任务名称",
    "所属实验",
    "训练服务器",
    "点云分类实验",
    "实验室服务器",
    "tools/train.py",
    "pointcloud 环境",
    "训练轮数",
    "学习率",
    "0.00005",
  ])
    expect(text).toContain(expected);
  for (const hidden of [
    "PRIVATE_",
    "task_id",
    "experiment_id",
    "server_id",
    "script_id",
    "snapshot_json",
    "sha256",
    "/opt/conda/",
  ])
    expect(text).not.toContain(hidden);
  wrapper.unmount();
});
it("关联名称不可读取时不退回显示数据库编号", () => {
  const task = {
    task_id: "a".repeat(32),
    experiment_id: "b".repeat(32),
    server_id: "c".repeat(32),
    script_id: "registered",
    name: "已保存任务",
  };
  const wrapper = mount(TrainingTaskDetails, {
    props: { task, experimentName: "", serverName: "" },
  });
  expect(wrapper.text()).toContain("服务器已删除或暂时无法读取");
  expect(wrapper.text()).toContain("使用训练脚本的默认值");
  expect(wrapper.text()).not.toContain(task.experiment_id);
  expect(wrapper.text()).not.toContain(task.server_id);
  wrapper.unmount();
});
