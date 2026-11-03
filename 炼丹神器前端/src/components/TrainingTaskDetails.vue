<script setup lang="ts">
import { computed } from "vue";
import type { TrainingTask } from "../types/domain";
const props = defineProps<{
  task: TrainingTask;
  experimentName: string;
  serverName: string;
}>();
const project = computed(() => props.task.project);
const parameters = computed(() => [
  ...new Set([
    ...Object.keys(props.task.default_parameters || {}),
    ...Object.keys(props.task.parameter_rules || {}),
  ]),
]);
const labels: Record<string, string> = {
  epochs: "训练轮数",
  epoch: "训练轮数",
  batch_size: "每批样本数",
  learning_rate: "学习率",
  lr: "学习率",
  weight_decay: "权重衰减",
  seed: "随机种子",
  image_size: "图像尺寸",
  input_size: "输入尺寸",
  num_workers: "数据加载进程数",
  momentum: "动量",
  patience: "提前停止等待轮数",
};
const parameterName = (key: string) => labels[key] || `自定义参数：${key}`;
const parameterValue = (key: string) => {
  const value = props.task.default_parameters?.[key];
  return value === undefined
    ? "使用脚本默认值"
    : typeof value === "boolean"
      ? value
        ? "是"
        : "否"
      : String(value);
};
const gpu = computed(() => {
  const value = project.value?.cuda_visible_devices;
  return value == null
    ? "沿用服务器环境"
    : value === ""
      ? "不使用显卡"
      : `显卡编号：${value}`;
});
const environment = computed(() => {
  const python = project.value?.python;
  if (!python) return "由训练入口管理";
  const conda = python.match(/\/envs\/([^/]+)\//);
  if (conda) return `${conda[1]} 环境`;
  if (/\/(?:\.venv|venv)\//.test(python)) return "项目虚拟环境";
  return python.includes("/") ? "已选择的服务器运行环境" : python;
});
</script>
<template>
  <dl class="data-tree">
    <div>
      <dt>任务名称</dt>
      <dd>{{ task.name }}</dd>
    </div>
    <div>
      <dt>所属实验</dt>
      <dd>{{ experimentName || "实验名称暂时无法读取" }}</dd>
    </div>
    <div>
      <dt>训练服务器</dt>
      <dd>{{ serverName || "服务器已删除或暂时无法读取" }}</dd>
    </div>
    <div>
      <dt>训练文件</dt>
      <dd>{{ project?.entrypoint || "使用已登记的训练入口" }}</dd>
    </div>
    <div>
      <dt>主网络代码</dt>
      <dd>{{ project?.main_network || "尚未选择，可在运行的分析页补选" }}</dd>
    </div>
    <div>
      <dt>运行环境</dt>
      <dd>{{ environment }}</dd>
    </div>
    <div v-if="project">
      <dt>使用显卡</dt>
      <dd>{{ gpu }}</dd>
    </div>
    <div v-if="project?.arguments?.length">
      <dt>附加启动参数</dt>
      <dd>{{ project.arguments.join(" ") }}</dd>
    </div>
    <div v-if="project?.max_runtime_seconds">
      <dt>最长运行时间</dt>
      <dd>{{ project.max_runtime_seconds }} 秒</dd>
    </div>
    <div>
      <dt>日志指标解析</dt>
      <dd>
        {{
          project?.metric_rules?.length
            ? `已配置 ${project.metric_rules.length} 条解析规则`
            : "未配置普通文本规则；结构化指标可直接采集"
        }}
      </dd>
    </div>
  </dl>
  <h3>默认训练参数</h3>
  <dl v-if="parameters.length" class="data-tree">
    <div v-for="key in parameters" :key="key">
      <dt>{{ parameterName(key) }}</dt>
      <dd>{{ parameterValue(key) }}</dd>
    </div>
  </dl>
  <p v-else class="hint">未单独设置参数，将使用训练脚本的默认值。</p>
  <p class="fine-print">
    保存配置不会启动训练。开始训练后，每次运行分别记录状态、日志和结果。
  </p>
</template>
