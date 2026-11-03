<script setup lang="ts">
import { computed } from "vue";
import { itemsOf, record } from "../types/domain";
import { number, dateTime, stateLabel } from "../lib/format";
import DataTree from "./DataTree.vue";
const props = defineProps<{ data: Record<string, unknown> }>();
const series = computed(() => itemsOf(props.data.series).map(record));
const warnings: Record<string, string> = {
  NOT_SYNCED: "尚未采集日志",
  NO_METRICS: "未发现有效指标",
  INVALID_LINES: "存在无法解析的指标行",
  OVERSIZED_LINES: "存在过长日志行",
  STEP_REGRESSION: "部分指标 step 回退",
  LOG_TRUNCATED: "日志被截断",
  STALE_SOURCE: "来源已变化",
  SOURCE_INCOMPLETE: "日志尚未采集完整",
  PENDING_TAIL: "日志尾部仍有未完成行",
  EVALUATION_UNSPECIFIED: "未声明评测协议",
  DATASET_UNDECLARED: "数据集未在协议中声明",
  DATASET_UNSPECIFIED: "未指定数据集",
  METRIC_UNDECLARED: "指标未在协议中声明",
  VALUE_OUT_OF_RANGE: "指标超出声明范围",
};
const warningList = computed(() =>
  Array.isArray(props.data.warnings) ? props.data.warnings.map(String) : [],
);
const numeric = (value: unknown) =>
  typeof value === "number" ? number(value) : "—";
</script>
<template>
  <div>
    <div class="metadata-row">
      <span>训练：{{ stateLabel(String(data.status || "")) }}</span
      ><span
        >日志采集：{{ data.collection_complete ? "完整" : "尚未完整" }}</span
      ><span
        >结束：{{
          dateTime(
            typeof data.finished_at === "string" ? data.finished_at : null,
          )
        }}</span
      >
    </div>
    <div v-if="warningList.length" class="notice warning-note">
      <strong>报告适用范围</strong>
      <ul>
        <li v-for="code in warningList" :key="code">
          {{ warnings[code] || code }}
        </li>
      </ul>
    </div>
    <div class="card-grid">
      <div v-for="(item, index) in series" :key="index" class="card">
        <div class="card-title">
          <h3>{{ item.metric_name }}</h3>
          <el-tag v-if="item.is_primary_metric" size="small" effect="plain"
            >主要指标</el-tag
          >
        </div>
        <p class="fine-print">
          {{ item.dataset }} · {{ item.split }} · {{ item.points }} 个采样点
        </p>
        <div class="metric-grid">
          <div class="metric-value">
            <small>末值</small><strong>{{ numeric(item.last_value) }}</strong>
          </div>
          <div class="metric-value">
            <small>协议允许时的观测最优值</small
            ><strong>{{ numeric(item.observed_best_value) }}</strong>
          </div>
        </div>
        <p class="fine-print">
          首值 {{ numeric(item.first_value) }} · 变化 {{ numeric(item.delta)
          }}<br />数值范围 {{ numeric(item.min_value) }} ～
          {{ numeric(item.max_value) }}
        </p>
        <p
          v-for="flag in Array.isArray(item.quality_flags)
            ? item.quality_flags
            : []"
          :key="String(flag)"
          class="hint"
        >
          {{ warnings[String(flag)] || flag }}
        </p>
      </div>
    </div>
    <p v-if="!series.length" class="hint">当前报告没有可显示的指标序列。</p>
    <p class="hint" style="margin-top: 18px">{{ data.interpretation }}</p>
    <details class="tool-record">
      <summary>完整事实报告与评测定义</summary>
      <DataTree :value="data" />
    </details>
  </div>
</template>
