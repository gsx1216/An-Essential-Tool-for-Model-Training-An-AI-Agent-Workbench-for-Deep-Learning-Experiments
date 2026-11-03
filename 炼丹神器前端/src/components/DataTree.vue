<script setup lang="ts">
import { computed } from "vue";
import { record } from "../types/domain";
defineOptions({ name: "DataTree" });
const props = defineProps<{ value: unknown }>();
const entries = computed(() => Object.entries(record(props.value)));
const labels: Record<string, string> = {
  summary: "摘要",
  conclusions: "结论",
  suggestions: "建议",
  evidence: "依据",
  limitations: "局限",
  notice: "说明",
  status: "状态",
  parameters: "运行参数",
  metrics: "评测指标",
  dataset: "数据集",
  split: "分组",
  outputs: "输出文件",
  title: "标题",
  description: "描述",
  reason: "原因",
  risks: "风险",
  expected_effect: "预期效果",
  experiment: "实验",
  details: "详情",
  warnings: "注意事项",
  next_steps: "下一步",
  content: "内容",
  references: "参考资料",
  url: "来源地址",
  code_evidence: "代码依据",
  checks: "检查结果",
  report_version: "报告版本",
  implementation: "评测实现",
  evaluation: "评测方案",
  generated_at: "生成时间",
  source: "来源",
};
</script>
<template>
  <span v-if="value == null" class="muted">—</span>
  <ol v-else-if="Array.isArray(value)" class="data-list">
    <li v-for="(item, index) in value" :key="index">
      <DataTree :value="item" />
    </li>
    <li v-if="!value.length" class="muted">暂无记录</li>
  </ol>
  <dl v-else-if="typeof value === 'object'" class="data-tree">
    <div v-for="[key, item] in entries" :key="key">
      <dt>{{ labels[key] || key }}</dt>
      <dd><DataTree :value="item" /></dd>
    </div>
  </dl>
  <a
    v-else-if="typeof value === 'string' && /^https?:\/\//i.test(value)"
    :href="value"
    target="_blank"
    rel="noopener noreferrer"
    class="plain-value"
    >{{ value }}</a
  >
  <span v-else class="plain-value">{{
    typeof value === "boolean" ? (value ? "是" : "否") : value
  }}</span>
</template>
