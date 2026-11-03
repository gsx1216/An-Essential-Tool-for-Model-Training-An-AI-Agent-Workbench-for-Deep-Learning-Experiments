<script setup lang="ts">
import { reactive, ref } from "vue";
import type { Schema } from "../types/domain";
defineProps<{ busy: boolean; error: string }>();
const emit = defineEmits<{
  save: [value: Schema<"CreateExperimentRequest">];
}>();
const form = reactive({
  title: "",
  kind: "training" as "training" | "script",
  description: "",
});
const withEvaluation = ref(true),
  localError = ref("");
const evaluation = reactive<Schema<"EvaluationSpec">>({
  task_type: "camouflaged_object_detection",
  protocol_id: "",
  protocol_version: "",
  implementation: "",
  primary_metric: null,
  datasets: [{ id: "", display_name: "", version: "" }],
  metrics: [
    {
      name: "",
      display_name: "",
      unit: "",
      direction: "none",
      definition: "",
      min_value: null,
      max_value: null,
    },
  ],
});
const taskTypes = {
  camouflaged_object_detection: "伪装目标检测",
  object_detection: "目标检测",
  object_tracking: "目标跟踪",
  point_cloud_classification: "点云分类",
  point_cloud_segmentation: "点云分割",
  point_cloud_completion: "点云补全",
  point_cloud_detection: "点云检测",
  custom: "其他任务",
};
function submit() {
  localError.value = "";
  if (!form.title.trim()) {
    localError.value = "请填写实验标题。";
    return;
  }
  if (withEvaluation.value) {
    evaluation.protocol_id = evaluation.protocol_id.trim().toLowerCase();
    evaluation.datasets.forEach((dataset) => {
      dataset.id = dataset.id.trim().toLowerCase();
    });
    evaluation.metrics.forEach((metric) => {
      metric.name = metric.name.trim().toLowerCase();
    });
    evaluation.primary_metric =
      evaluation.primary_metric?.toLowerCase() || null;
    if (
      !evaluation.protocol_id ||
      !evaluation.protocol_version ||
      evaluation.datasets.some((d) => !d.id || !d.display_name || !d.version) ||
      evaluation.metrics.some((m) => !m.name || !m.display_name || !m.unit)
    ) {
      localError.value = "请填写评测方案、数据集版本和指标定义中的必填项。";
      return;
    }
    const identifier = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
    if (
      !identifier.test(evaluation.protocol_id) ||
      evaluation.datasets.some((d) => !identifier.test(d.id)) ||
      evaluation.metrics.some((m) => !/^[a-z][a-z0-9_]{0,63}$/.test(m.name))
    ) {
      localError.value =
        "方案与数据集 ID 使用英文、数字、_ . -；指标名须以英文字母开头，仅含英文、数字和下划线。大小写不影响识别，保存时统一为小写。";
      return;
    }
    if (
      new Set(evaluation.datasets.map((d) => d.id)).size !==
        evaluation.datasets.length ||
      new Set(evaluation.metrics.map((m) => m.name)).size !==
        evaluation.metrics.length
    ) {
      localError.value = "数据集 ID 或指标名不能重复。";
      return;
    }
    if (
      evaluation.metrics.some(
        (m) =>
          m.min_value != null &&
          m.max_value != null &&
          m.min_value > m.max_value,
      )
    ) {
      localError.value = "指标下限不能大于上限。";
      return;
    }
  }
  emit("save", {
    ...form,
    title: form.title.trim(),
    evaluation: withEvaluation.value
      ? JSON.parse(JSON.stringify(evaluation))
      : null,
  });
}
</script>
<template>
  <el-form label-position="top" :disabled="busy" @submit.prevent
    ><div class="form-grid">
      <el-form-item label="实验标题" required
        ><el-input
          v-model="form.title"
          maxlength="200"
          placeholder="为这组实验起一个名字" /></el-form-item
      ><el-form-item label="实验类型"
        ><el-select v-model="form.kind"
          ><el-option label="模型训练" value="training" /><el-option
            label="脚本实验"
            value="script" /></el-select></el-form-item
      ><el-form-item label="实验说明" class="wide"
        ><el-input
          v-model="form.description"
          type="textarea"
          :rows="2"
          maxlength="10000"
      /></el-form-item>
    </div>
    <div class="settings-item">
      <div>
        <strong>记录评测方案</strong>
        <p>定义数据集、指标与计算方式，供后续报告和结果对比使用。</p>
      </div>
      <el-switch v-model="withEvaluation" aria-label="记录评测方案" />
    </div>
    <template v-if="withEvaluation"
      ><div class="notice">
        评测方案会随运行冻结。请填写实际采用的版本和计算方式。标识和指标名忽略大小写，显示名称保留原样。
      </div>
      <div class="form-grid">
        <el-form-item label="研究任务" required
          ><el-select v-model="evaluation.task_type"
            ><el-option
              v-for="(label, value) in taskTypes"
              :key="value"
              :label="label"
              :value="value" /></el-select></el-form-item
        ><el-form-item label="方案标识" required
          ><el-input
            v-model="evaluation.protocol_id"
            placeholder="例如 cod-evaluation"
            maxlength="64" /></el-form-item
        ><el-form-item label="方案版本" required
          ><el-input
            v-model="evaluation.protocol_version"
            placeholder="例如 v1（按你的实际版本填写）"
            maxlength="200" /></el-form-item
        ><el-form-item label="主要指标"
          ><el-select
            v-model="evaluation.primary_metric"
            clearable
            placeholder="可不指定"
            ><el-option
              v-for="metric in evaluation.metrics.filter((m) => m.name)"
              :key="metric.name"
              :label="metric.display_name || metric.name"
              :value="metric.name.toLowerCase()" /></el-select></el-form-item
        ><el-form-item label="评测实现说明" class="wide"
          ><el-input
            v-model="evaluation.implementation"
            type="textarea"
            :rows="2"
            maxlength="1000"
            placeholder="评测代码、预处理、阈值及计算约定"
        /></el-form-item>
      </div>
      <div class="split-heading">
        <h3>数据集</h3>
        <el-button
          text
          :disabled="evaluation.datasets.length >= 16"
          @click="
            evaluation.datasets.push({ id: '', display_name: '', version: '' })
          "
          >＋ 添加数据集</el-button
        >
      </div>
      <div
        v-for="(dataset, index) in evaluation.datasets"
        :key="index"
        class="dataset-row"
      >
        <div class="form-grid">
          <el-form-item label="数据集 ID" required
            ><el-input
              v-model="dataset.id"
              maxlength="64"
              placeholder="例如 cod10k" /></el-form-item
          ><el-form-item label="显示名称" required
            ><el-input
              v-model="dataset.display_name"
              maxlength="200"
              placeholder="例如 COD10K" /></el-form-item
          ><el-form-item label="版本与划分" required
            ><el-input
              v-model="dataset.version"
              maxlength="200"
              placeholder="实际版本、测试集划分" /></el-form-item
          ><el-form-item label="操作"
            ><el-button
              text
              type="danger"
              :disabled="evaluation.datasets.length === 1"
              @click="evaluation.datasets.splice(index, 1)"
              >移除此数据集</el-button
            ></el-form-item
          >
        </div>
      </div>
      <div class="split-heading">
        <h3>指标定义</h3>
        <el-button
          text
          :disabled="evaluation.metrics.length >= 64"
          @click="
            evaluation.metrics.push({
              name: '',
              display_name: '',
              unit: '',
              direction: 'none',
              min_value: null,
              max_value: null,
              definition: '',
            })
          "
          >＋ 添加指标</el-button
        >
      </div>
      <div
        v-for="(metric, index) in evaluation.metrics"
        :key="index"
        class="metric-definition"
      >
        <div class="form-grid">
          <el-form-item label="指标名（与脚本上报一致）" required
            ><el-input
              v-model="metric.name"
              maxlength="64"
              placeholder="例如 mae、sm、map" /></el-form-item
          ><el-form-item label="显示名称" required
            ><el-input
              v-model="metric.display_name"
              maxlength="200" /></el-form-item
          ><el-form-item label="单位" required
            ><el-input
              v-model="metric.unit"
              maxlength="200"
              placeholder="例如 ratio 或 %" /></el-form-item
          ><el-form-item label="更优方向" required
            ><el-select v-model="metric.direction"
              ><el-option label="越小越好" value="minimize" /><el-option
                label="越大越好"
                value="maximize" /><el-option
                label="不判定方向"
                value="none" /></el-select></el-form-item
          ><el-form-item label="下限（可留空）"
            ><el-input-number
              v-model="metric.min_value"
              :controls="false" /></el-form-item
          ><el-form-item label="上限（可留空）"
            ><el-input-number
              v-model="metric.max_value"
              :controls="false" /></el-form-item
          ><el-form-item label="计算约定" class="wide"
            ><el-input
              v-model="metric.definition"
              type="textarea"
              maxlength="1000"
              placeholder="例如阈值取均值还是最大值、是否对每张图求均值等"
          /></el-form-item>
        </div>
        <el-button
          text
          type="danger"
          :disabled="evaluation.metrics.length === 1"
          @click="evaluation.metrics.splice(index, 1)"
          >移除此指标</el-button
        >
      </div></template
    >
    <p v-if="error || localError" class="inline-error" role="alert">
      {{ error || localError }}
    </p>
    <div class="actions">
      <el-button type="primary" :loading="busy" @click="submit"
        >创建实验</el-button
      >
    </div></el-form
  >
</template>
