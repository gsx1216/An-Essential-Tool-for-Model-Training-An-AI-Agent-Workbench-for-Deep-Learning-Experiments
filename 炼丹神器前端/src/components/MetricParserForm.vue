<script setup lang="ts">
import { ref, watch } from "vue";
import type { Schema } from "../types/domain";
type Rule = Schema<"MetricRule">;
type FormRule = Rule & { key: number };
const props = defineProps<{ rules: Rule[]; busy: boolean; error: string }>();
const emit = defineEmits<{ save: [body: Schema<"MetricParserRequest">] }>();
const rows = ref<FormRule[]>([]),
  localError = ref("");
let nextKey = 0;
const metricOptions = [
  { value: "loss", label: "损失值" },
  { value: "sal_loss", label: "显著性损失" },
  { value: "learning_rate", label: "学习率" },
  { value: "mae", label: "平均绝对误差" },
  { value: "accuracy", label: "准确率" },
  { value: "iou", label: "交并比" },
  { value: "map", label: "平均精度" },
];
function draft(rule?: Rule): FormRule {
  return {
    key: nextKey++,
    pattern: rule?.pattern || "",
    dataset: rule?.dataset === "unspecified" ? "" : rule?.dataset || "",
    split: rule?.split || "train",
    metrics: [...(rule?.metrics || [])],
  };
}
watch(
  () => props.rules,
  (rules) => {
    rows.value = rules.map(draft);
    localError.value = "";
  },
  { immediate: true },
);
function add() {
  if (rows.value.length < 8) rows.value.push(draft());
}
function example() {
  if (rows.value.length >= 8) return;
  rows.value.push(
    draft({
      pattern: "step=(?P<step>\\d+)\\s+loss=(?P<loss>[-+0-9.eE]+)",
      dataset: "unspecified",
      split: "train",
      metrics: ["loss"],
    }),
  );
}
function save() {
  localError.value = "";
  const incomplete = rows.value.findIndex(
    (row) => !row.pattern.trim() || !row.metrics.length,
  );
  if (incomplete >= 0) {
    localError.value = `请补全第 ${incomplete + 1} 条规则的匹配表达式和提取指标。`;
    return;
  }
  emit("save", {
    rules: rows.value.map((row) => ({
      pattern: row.pattern.trim(),
      dataset: row.dataset.trim().toLowerCase() || "unspecified",
      split: row.split,
      metrics: row.metrics.map((name) => name.trim().toLowerCase()),
    })),
  });
}
</script>
<template>
  <el-form label-position="top" :disabled="busy" @submit.prevent="save">
    <p class="hint">
      保留原来的文本日志，在这里说明“哪些内容代表训练轮数、损失值等指标”。保存后会重新读取历史日志，不重启训练。
    </p>
    <div class="actions">
      <el-button :disabled="rows.length >= 8" @click="add"
        >新增解析规则</el-button
      >
      <el-button plain :disabled="rows.length >= 8" @click="example"
        >填入通用示例</el-button
      >
    </div>
    <p class="fine-print">
      示例只匹配 step=1 loss=0.5
      这样的文本。不同日志请填写各自的匹配方式，最多添加八条。
    </p>
    <p v-if="!rows.length" class="hint">
      尚未设置普通文本规则。已有结构化指标时无需添加。
    </p>
    <section v-for="(row, index) in rows" :key="row.key" class="card">
      <div class="card-title">
        <h3>解析规则 {{ index + 1 }}</h3>
        <el-button
          type="danger"
          text
          :aria-label="`删除第 ${index + 1} 条规则`"
          @click="rows.splice(index, 1)"
          >删除这条规则</el-button
        >
      </div>
      <el-form-item label="日志匹配表达式" required>
        <el-input
          v-model="row.pattern"
          type="textarea"
          :rows="3"
          :maxlength="512"
          :aria-label="`第 ${index + 1} 条日志匹配表达式`"
          placeholder="填写用来识别一行日志的匹配表达式"
        />
      </el-form-item>
      <p class="fine-print">
        表达式像一张“填空模板”。例如 (?P&lt;epoch&gt;\d+)
        取出训练轮数，(?P&lt;loss&gt;[0-9.]+)
        取出损失值；指标名称需与模板中的名称对应，大小写可不同。日志文字匹配也忽略大小写，MAE
        和 mae 视为同一个指标。
      </p>
      <el-form-item label="所属数据集">
        <el-input
          v-model="row.dataset"
          :aria-label="`第 ${index + 1} 条所属数据集`"
          placeholder="可留空，表示未指定数据集"
        />
      </el-form-item>
      <el-form-item label="指标来自哪一组" required>
        <el-select
          v-model="row.split"
          :aria-label="`第 ${index + 1} 条指标分组`"
        >
          <el-option label="训练集" value="train" /><el-option
            label="验证集"
            value="validation"
          /><el-option label="测试集" value="test" />
        </el-select>
      </el-form-item>
      <el-form-item label="要提取的指标" required>
        <el-select
          v-model="row.metrics"
          multiple
          filterable
          allow-create
          default-first-option
          :aria-label="`第 ${index + 1} 条提取指标`"
          placeholder="选择常见指标，或填写自己的指标名称"
        >
          <el-option
            v-for="item in metricOptions"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
      </el-form-item>
      <p class="fine-print">
        可以提取多个数值。同一行命中多条规则时，只采用排在最前面的匹配规则。
      </p>
    </section>
    <details class="tool-record">
      <summary>给训练脚本开发者：结构化日志输出示例</summary>
      <p>
        如果可以修改脚本，每次输出一行以下格式，系统即可直接采集，不需要另写解析规则。
      </p>
      <pre>
{"type":"metric","step":1,"epoch":1,"dataset":"my_dataset","split":"train","loss":0.5}</pre>
      <p class="fine-print">
        步数跨训练轮次递增，指标值必须是数值。可以用 print(json.dumps(record),
        flush=True) 及时写出每一行。
      </p>
    </details>
    <p v-if="localError || error" class="inline-error">
      {{ localError || error }}
    </p>
    <el-button type="primary" :loading="busy" native-type="submit"
      >保存并补采指标</el-button
    >
  </el-form>
</template>
