<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api } from "../api";
import { ApiError, errorText, isCancelled } from "../api/client";
import type { MetricEvent, MetricSummary, Run, Schema } from "../types/domain";
import { shortId, number, terminal } from "../lib/format";
import { useSerialPoll } from "../composables/useSerialPoll";
import SectionState from "./SectionState.vue";
import StatusBadge from "./StatusBadge.vue";
import MetricChart from "./MetricChart.vue";
const props = defineProps<{ runId: string | null }>();
const run = ref<Run | null>(null),
  execution = ref<Schema<"ExecutionResponse"> | null>(null),
  events = ref<MetricEvent[]>([]),
  summaries = ref<MetricSummary[]>([]),
  error = ref(""),
  loading = ref(false),
  tab = ref("overview");
let epoch = 0;
async function read(signal?: AbortSignal) {
  const id = props.runId,
    current = epoch;
  if (!id) return;
  try {
    const data = await api.run(id, signal);
    if (current !== epoch) return;
    run.value = data;
    error.value = "";
    try {
      const value = await api.execution(id, signal);
      if (current === epoch) execution.value = value;
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 404)) throw e;
    }
    const [metrics, latest] = await Promise.all([
      api.metrics(id, -1, signal),
      api.metricLatest(id, signal),
    ]);
    if (current === epoch) {
      events.value = metrics.items;
      summaries.value = latest.items;
    }
  } catch (e) {
    if (current === epoch && !isCancelled(e)) error.value = errorText(e);
  } finally {
    if (current === epoch) loading.value = false;
  }
}
watch(
  () => props.runId,
  () => {
    epoch++;
    run.value = null;
    execution.value = null;
    events.value = [];
    summaries.value = [];
    error.value = "";
    loading.value = !!props.runId;
    void read();
  },
  { immediate: true },
);
useSerialPoll(
  computed(() => !!props.runId && !!run.value && !terminal(run.value.status)),
  read,
);
const recent = computed(() =>
  summaries.value
    .slice(0, 4)
    .map((item) => ({
      name: item.metric_name,
      value: item.last_value,
      group: `${item.dataset} · ${item.split}`,
    })),
);
</script>
<template>
  <section class="run-summary">
    <div class="summary-header">
      <h2>当前关联运行</h2>
      <span class="eyebrow">LIVE CONTEXT</span>
    </div>
    <SectionState :loading="loading && !run" :error="error" @retry="read()" />
    <div v-if="!runId" class="state-box run-empty">
      <div class="empty-symbol">◇</div>
      <h3>让实验与对话连起来</h3>
      <p>查看或启动训练后，相关运行会出现在这里。</p>
      <router-link to="/training"
        ><el-button>查看实验与训练</el-button></router-link
      >
    </div>
    <template v-if="run"
      ><el-tabs v-model="tab" stretch
        ><el-tab-pane label="概览" name="overview" /><el-tab-pane
          label="报告"
          name="report"
      /></el-tabs>
      <div v-if="tab === 'overview'">
        <div class="card">
          <StatusBadge :state="run.status" />
          <div class="run-title">{{ run.experiment_title }}</div>
          <p class="fine-print">运行 #{{ shortId(run.run_id) }}</p>
          <div class="metric-grid">
            <div
              v-for="item in recent"
              :key="item.name + item.group"
              class="metric-value"
            >
              <small>{{ item.name }}</small
              ><strong>{{ number(item.value) }}</strong>
              <div class="fine-print">{{ item.group }}</div>
            </div>
          </div>
          <p v-if="!recent.length" class="hint">尚无已采集指标</p>
        </div>
        <div class="card">
          <h3>训练曲线</h3>
          <MetricChart :events="events" compact />
          <p class="fine-print">
            显示首批最多 500 个已采集事件；完整指标请打开监控。
          </p>
        </div>
        <div v-if="execution?.output_directory" class="card">
          <h3>输出目录</h3>
          <p class="fine-print">{{ execution.output_directory }}</p>
          <small class="muted">目录内实际产物请在训练监控中刷新确认。</small>
        </div>
      </div>
      <div v-else class="card">
        <h3>事实报告与改进建议</h3>
        <p class="hint">
          报告基于已采集结果；分析和建议由后台任务生成，训练完成不代表建议已经就绪。
        </p>
        <router-link :to="`/runs/${run.run_id}?tab=report`"
          ><el-button type="primary" plain>打开报告</el-button></router-link
        >
      </div>
      <div class="run-links">
        <router-link :to="`/runs/${run.run_id}`"
          >训练监控与日志 <span>→</span></router-link
        ><router-link :to="`/runs/${run.run_id}?tab=agents`"
          >分析与有据建议 <span>→</span></router-link
        ><router-link :to="`/runs/${run.run_id}?tab=code`"
          >源码与修改记录 <span>→</span></router-link
        >
      </div></template
    >
  </section>
</template>
