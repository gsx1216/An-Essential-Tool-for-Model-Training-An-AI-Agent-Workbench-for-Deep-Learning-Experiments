<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  Refresh,
  VideoPlay,
  VideoPause,
  ChatDotRound,
} from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import { ApiError, errorText, isCancelled } from "../api/client";
import { confirmAction, notifyError } from "../lib/ui";
import { dateTime, number, shortId, terminal } from "../lib/format";
import { LogBuffer } from "../lib/logs";
import { useSerialPoll } from "../composables/useSerialPoll";
import type {
  Run,
  Server,
  Script,
  Schema,
  MetricEvent,
  MetricSummary,
} from "../types/domain";
import StatusBadge from "../components/StatusBadge.vue";
import SectionState from "../components/SectionState.vue";
import MetricChart from "../components/MetricChart.vue";
import MetricParserForm from "../components/MetricParserForm.vue";
import DataTree from "../components/DataTree.vue";
import RunAgents from "../components/RunAgents.vue";
import RunCode from "../components/RunCode.vue";
import ReportSummary from "../components/ReportSummary.vue";
const route = useRoute(),
  router = useRouter();
const id = computed(() => String(route.params.runId));
const comparisonLabels = {
  not_compared: "未比较",
  equal: "数值相同",
  direction_unspecified: "未指定方向",
  improved: "按协议方向改善",
  worsened: "按协议方向下降",
};
const validTabs = [
  "overview",
  "logs",
  "report",
  "agents",
  "code",
  "comparison",
];
const tab = ref(
  validTabs.includes(String(route.query.tab))
    ? String(route.query.tab)
    : "overview",
);
const run = ref<Run | null>(null),
  execution = ref<Schema<"ExecutionResponse"> | null>(null),
  monitor = ref<Schema<"MonitorResponse"> | null>(null),
  revision = ref<Schema<"RevisionResponse"> | null>(null),
  report = ref<Schema<"ReportResponse"> | null>(null),
  comparison = ref<Schema<"ComparisonResponse"> | null>(null);
const metrics = ref<MetricEvent[]>([]),
  summaries = ref<MetricSummary[]>([]),
  nextMetric = ref<number | null>(null),
  logs = reactive(new LogBuffer()),
  observation = ref<unknown>(null);
const reportBusy = ref(false),
  comparisonBusy = ref(false);
const parserDialog = ref(false),
  parserBusy = ref(false),
  parserError = ref("");
const parserRules = ref<Schema<"MetricRule">[]>([]);
const metricNotice = ref("");
async function openParser() {
  const target = id.value,
    current = epoch;
  parserDialog.value = true;
  parserBusy.value = true;
  parserError.value = "";
  try {
    const result = await api.metricParser(target);
    if (current === epoch) parserRules.value = result.rules || [];
  } catch (error) {
    if (current === epoch) parserError.value = errorText(error);
  } finally {
    if (current === epoch) parserBusy.value = false;
  }
}
async function saveParser(body: Schema<"MetricParserRequest">) {
  const target = id.value,
    current = epoch;
  parserBusy.value = true;
  parserError.value = "";
  let saved = false;
  try {
    await api.saveMetricParser(target, body);
    saved = true;
    const result = await api.syncMetrics(target);
    if (current !== epoch) return;
    metricNotice.value = result.new_events
      ? ""
      : "规则已保存，但本批日志仍没有匹配到指标，请核对日志格式。";
    await loadMetrics();
    await read();
    if (current === epoch) {
      parserDialog.value = false;
      ElMessage.success(`规则已保存，补采 ${result.new_events} 条指标。`);
    }
  } catch (error) {
    if (current === epoch)
      parserError.value =
        (saved ? "规则已保存，补采未完成：" : "") + errorText(error);
  } finally {
    if (current === epoch) parserBusy.value = false;
  }
}
const loading = ref(true),
  error = ref(""),
  executionError = ref(""),
  metricError = ref(""),
  logError = ref(""),
  reportError = ref(""),
  compareError = ref(""),
  monitorError = ref(""),
  action = ref(""),
  focusChange = ref("");
const logElement = ref<HTMLElement>(),
  logFollow = ref(true),
  logBusy = ref(false);
const servers = ref<Server[]>([]),
  scripts = ref<Script[]>([]),
  startDialog = ref(false),
  startForm = reactive({ server_id: "", script_id: "" }),
  startError = ref("");
let epoch = 0,
  readController: AbortController | undefined;
const hasPendingMonitor = computed(
  () =>
    !!monitor.value &&
    !["completed", "attention"].includes(monitor.value.status),
);
const shouldPoll = computed(
  () =>
    !action.value &&
    !!run.value &&
    (!terminal(run.value.status) || hasPendingMonitor.value),
);
async function read(signal?: AbortSignal) {
  const target = id.value,
    current = epoch;
  const results = await Promise.allSettled([
    api.run(target, signal),
    api.execution(target, signal),
    api.monitor(target, signal),
    api.metricLatest(target, signal),
  ]);
  if (current !== epoch || signal?.aborted) return;
  error.value = "";
  executionError.value = "";
  monitorError.value = "";
  results.forEach((result, index) => {
    if (result.status === "rejected") {
      if (isCancelled(result.reason)) return;
      if (
        result.reason instanceof ApiError &&
        result.reason.status === 404 &&
        index > 0
      )
        return;
      const text = errorText(result.reason);
      if (index === 0) error.value = text;
      else if (index === 1) executionError.value = text;
      else if (index === 2) monitorError.value = text;
      else metricError.value = text;
      return;
    }
    if (index === 0) run.value = result.value as Run;
    if (index === 1)
      execution.value = result.value as Schema<"ExecutionResponse">;
    if (index === 2) monitor.value = result.value as Schema<"MonitorResponse">;
    if (index === 3) {
      summaries.value = (result.value as { items: MetricSummary[] }).items;
      metricError.value = "";
    }
  });
  loading.value = false;
}
async function loadMetrics(signal?: AbortSignal, more = false) {
  const target = id.value,
    current = epoch;
  try {
    const after = more
      ? (nextMetric.value ?? metrics.value.at(-1)?.source_offset ?? -1)
      : (metrics.value.at(-1)?.source_offset ?? -1);
    const page = await api.metrics(target, after, signal);
    if (current !== epoch || signal?.aborted) return;
    const seen = new Set(metrics.value.map((m) => m.source_offset));
    metrics.value.push(...page.items.filter((m) => !seen.has(m.source_offset)));
    nextMetric.value = page.next_offset;
    metricError.value = "";
  } catch (e) {
    if (current === epoch && !isCancelled(e)) metricError.value = errorText(e);
  }
}
async function readLogs(signal?: AbortSignal) {
  if (logBusy.value) return;
  const current = epoch;
  logBusy.value = true;
  try {
    const result = await api.logs(id.value, logs.offset, signal);
    if (current !== epoch || signal?.aborted) return;
    logs.append(result.observation);
    execution.value = result.execution;
    logError.value = "";
    await nextTick();
    if (logFollow.value && logElement.value)
      logElement.value.scrollTop = logElement.value.scrollHeight;
  } catch (e) {
    if (current === epoch && !isCancelled(e)) logError.value = errorText(e);
  } finally {
    if (current === epoch) logBusy.value = false;
  }
}
async function readReport() {
  const current = epoch;
  reportBusy.value = true;
  reportError.value = "";
  try {
    const data = await api.report(id.value);
    if (current === epoch) report.value = data;
  } catch (e) {
    if (current === epoch) {
      if (e instanceof ApiError && e.status === 404) report.value = null;
      else reportError.value = errorText(e);
    }
  } finally {
    if (current === epoch) reportBusy.value = false;
  }
}
async function readComparison() {
  const current = epoch;
  comparisonBusy.value = true;
  compareError.value = "";
  try {
    const data = await api.comparison(id.value);
    if (current === epoch) comparison.value = data;
  } catch (e) {
    if (current === epoch) compareError.value = errorText(e);
  } finally {
    if (current === epoch) comparisonBusy.value = false;
  }
}
async function readRevision() {
  const current = epoch;
  try {
    const data = await api.revision(id.value);
    if (current === epoch) revision.value = data;
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 404) && !isCancelled(e))
      notifyError(e);
  }
}
async function initialize() {
  epoch++;
  parserDialog.value = false;
  parserBusy.value = false;
  parserRules.value = [];
  parserError.value = "";
  metricNotice.value = "";
  readController?.abort();
  readController = new AbortController();
  run.value = null;
  execution.value = null;
  monitor.value = null;
  revision.value = null;
  report.value = null;
  comparison.value = null;
  metrics.value = [];
  summaries.value = [];
  nextMetric.value = null;
  observation.value = null;
  logs.reset();
  loading.value = true;
  logBusy.value = false;
  error.value = "";
  logError.value = "";
  metricError.value = "";
  reportError.value = "";
  compareError.value = "";
  focusChange.value = "";
  await read(readController.signal);
  if (run.value) {
    void loadMetrics(readController.signal);
    void readRevision();
    if (tab.value === "report") void readReport();
    if (tab.value === "comparison") void readComparison();
    if (tab.value === "logs") void readLogs(readController.signal);
  }
}
watch(id, initialize, { immediate: true });
watch(tab, (value) => {
  void router.replace({ query: { ...route.query, tab: value } });
  if (value === "report") void readReport();
  if (value === "comparison") void readComparison();
  if (value === "logs" && execution.value) void readLogs();
});
watch(
  () => route.query.tab,
  (value) => {
    if (validTabs.includes(String(value))) tab.value = String(value);
  },
);
useSerialPoll(shouldPoll, async (signal) => {
  await read(signal);
  if (signal.aborted) return;
  if (tab.value === "overview") await loadMetrics(signal);
  if (tab.value === "logs" && execution.value) await readLogs(signal);
});
async function operate(
  name: "refresh" | "stop" | "sync" | "report" | "monitor",
) {
  const target = id.value;
  if (
    name === "stop" &&
    !(await confirmAction(
      `停止运行 #${shortId(target)}？这会向该训练进程发送停止请求，已有日志与结果会保留。`,
      "停止训练",
    ))
  )
    return;
  action.value = name;
  try {
    if (name === "refresh" || name === "stop") {
      const result = await api[name](target);
      if (target !== id.value) return;
      execution.value = result.execution;
      observation.value = result.observation;
      ElMessage.success(
        name === "stop"
          ? "停止请求已提交，请以查询到的状态为准"
          : "已刷新远端状态",
      );
    }
    if (name === "sync") {
      const result = await api.syncMetrics(target);
      if (target !== id.value) return;
      await loadMetrics();
      metricNotice.value =
        !result.stream.valid && result.stream.ignored
          ? "日志已读取，但没有识别到指标。请配置普通日志解析规则，或让脚本输出结构化指标。"
          : "";
      if (result.new_events)
        ElMessage.success(`已采集 ${result.new_events} 条新指标`);
      else if (metricNotice.value) ElMessage.warning(metricNotice.value);
      else ElMessage.info("暂时没有新增指标。");
    }
    if (name === "report") {
      const result = await api.generateReport(target);
      if (target !== id.value) return;
      report.value = result;
      ElMessage.success("事实报告已就绪");
    }
    if (name === "monitor") {
      await api.retryMonitor(target);
      ElMessage.success("已提交监控恢复请求");
    }
    if (target === id.value) await read();
  } catch (e) {
    notifyError(e);
  } finally {
    action.value = "";
  }
}
async function openStart() {
  startError.value = "";
  if (revision.value) {
    if (
      !(await confirmAction(
        `启动修改后运行 #${shortId(id.value)}？将使用冻结副本及当前显示的参数。`,
        "启动修改后训练",
      ))
    )
      return;
    action.value = "start";
    try {
      const result = await api.startRevision(id.value);
      execution.value = result.execution;
      observation.value = result.observation;
      await read();
      ElMessage.success("已提交启动请求");
    } catch (e) {
      notifyError(e);
    } finally {
      action.value = "";
    }
    return;
  }
  startDialog.value = true;
  try {
    const registered = await api.scripts();
    scripts.value = registered.items;
    let before: string | undefined;
    const rows: Server[] = [];
    do {
      const page = await api.servers(before);
      rows.push(...page.items);
      before = page.next_cursor || undefined;
    } while (before);
    servers.value = rows;
    if (execution.value) {
      startForm.server_id = execution.value.server_id;
      startForm.script_id = execution.value.script_id;
    } else if (scripts.value.some((s) => s.script_id === route.query.script))
      startForm.script_id = String(route.query.script);
  } catch (e) {
    startError.value = errorText(e);
  }
}
async function start() {
  if (!startForm.server_id || !startForm.script_id) {
    startError.value = "请选择服务器和已登记脚本入口。";
    return;
  }
  const server = servers.value.find((s) => s.server_id === startForm.server_id);
  if (
    !(await confirmAction(
      `在“${server?.name || "已选服务器"}”上执行 ${startForm.script_id}？运行参数已冻结，请核对后启动。`,
      "启动真实训练",
    ))
  )
    return;
  action.value = "start";
  startError.value = "";
  try {
    const result = await api.start(id.value, { ...startForm });
    execution.value = result.execution;
    observation.value = result.observation;
    startDialog.value = false;
    await read();
    ElMessage.success("启动请求已提交，请继续查看运行状态");
  } catch (e) {
    startError.value = errorText(e);
  } finally {
    action.value = "";
  }
}
function logScroll() {
  const el = logElement.value;
  if (el)
    logFollow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
}
function selectChange(change: string) {
  focusChange.value = change;
  tab.value = "code";
}
onBeforeUnmount(() => {
  epoch++;
  readController?.abort();
});
</script>
<template>
  <div class="page">
    <header class="page-header">
      <div>
        <div class="eyebrow">TRAINING OBSERVATORY</div>
        <h1>{{ run?.experiment_title || "训练监控" }}</h1>
        <p>运行 #{{ shortId(id) }} <span v-if="revision">· 修改后训练</span></p>
      </div>
      <div class="header-actions">
        <el-button
          :icon="ChatDotRound"
          @click="
            router.push({
              path: '/chat',
              query: {
                prompt: `请查看运行 ${id} 的真实训练结果并分析，先不要启动新训练或修改代码。`,
              },
            })
          "
          >围绕此运行提问</el-button
        ><el-button
          :icon="Refresh"
          :loading="action === 'refresh'"
          :disabled="!execution || !!action"
          @click="operate('refresh')"
          >刷新远端状态</el-button
        >
      </div>
    </header>
    <SectionState :loading="loading" :error="error" @retry="initialize" />
    <template v-if="run"
      ><section class="card run-status-card">
        <div>
          <StatusBadge :state="run.status" />
          <h2>{{ run.experiment_title }}</h2>
          <div class="run-status-info">
            <span>创建 {{ dateTime(run.created_at) }}</span
            ><span>开始 {{ dateTime(run.started_at) }}</span
            ><span>结束 {{ dateTime(run.finished_at) }}</span>
          </div>
          <p
            v-if="run.error_code || execution?.error_code"
            class="inline-error"
          >
            {{ run.error_code || execution?.error_code }}
          </p>
        </div>
        <div class="actions">
          <el-button
            v-if="
              run.status === 'created' || (!execution && !terminal(run.status))
            "
            type="primary"
            :icon="VideoPlay"
            :loading="action === 'start'"
            :disabled="!!action || !!executionError"
            @click="openStart"
            >启动训练</el-button
          ><el-button
            v-if="execution && !terminal(execution.status)"
            type="danger"
            plain
            :icon="VideoPause"
            :loading="action === 'stop'"
            :disabled="!!action"
            @click="operate('stop')"
            >停止训练</el-button
          >
        </div>
      </section>
      <p v-if="executionError" class="inline-error">
        执行信息：{{ executionError }}
      </p>
      <el-tabs v-model="tab" class="run-detail-tabs"
        ><el-tab-pane label="概览" name="overview" /><el-tab-pane
          label="训练日志"
          name="logs" /><el-tab-pane
          label="事实报告"
          name="report" /><el-tab-pane
          label="分析与建议"
          name="agents" /><el-tab-pane
          label="代码与修改"
          name="code" /><el-tab-pane label="结果对比" name="comparison"
      /></el-tabs>
      <section v-if="tab === 'overview'" class="run-columns">
        <div>
          <div class="card">
            <div class="card-title">
              <h3>指标趋势</h3>
              <el-button
                v-if="!metrics.length && execution"
                size="small"
                :disabled="!!action"
                @click="openParser"
                >日志格式与采集规则</el-button
              >
              <el-button
                size="small"
                :disabled="!!action || !execution"
                :loading="action === 'sync'"
                @click="operate('sync')"
                >手动采集指标</el-button
              >
            </div>
            <MetricChart :events="metrics" />
            <p v-if="metricNotice" class="notice warning-note">
              {{ metricNotice }}
            </p>
            <p class="fine-print">
              已加载
              {{ metrics.length }} 条真实指标事件，按数据集和分组分别绘制。
            </p>
            <el-button
              v-if="nextMetric !== null"
              @click="loadMetrics(undefined, true)"
              >继续加载指标</el-button
            >
            <p v-if="metricError" class="inline-error">{{ metricError }}</p>
          </div>
          <div class="card">
            <h3>已采集指标末值</h3>
            <div class="metric-grid">
              <div
                v-for="item in summaries"
                :key="`${item.dataset}/${item.split}/${item.metric_name}`"
                class="metric-value"
              >
                <small>{{ item.metric_name }}</small
                ><strong>{{ number(item.last_value) }}</strong>
                <div class="fine-print">
                  {{ item.dataset }} · {{ item.split }}
                </div>
                <div class="fine-print">
                  范围 {{ number(item.min_value) }}～{{
                    number(item.max_value)
                  }}
                </div>
              </div>
            </div>
            <SectionState :empty="!summaries.length" text="尚无可显示的指标" />
          </div>
        </div>
        <div>
          <div class="card">
            <h3>后台监控</h3>
            <template v-if="monitor"
              ><StatusBadge :state="monitor.status" />
              <p class="fine-print">
                上次成功 {{ dateTime(monitor.last_success_at) }}
              </p>
              <p v-if="monitor.last_error_code" class="inline-error">
                {{ monitor.last_error_code }}
              </p>
              <el-button
                v-if="monitor.status === 'attention'"
                :loading="action === 'monitor'"
                @click="operate('monitor')"
                >恢复监控</el-button
              ></template
            >
            <p v-else class="hint">尚无监控记录</p>
            <p v-if="monitorError" class="inline-error">{{ monitorError }}</p>
            <p class="fine-print">运行状态、采集完成和报告就绪分别判断。</p>
          </div>
          <div class="card">
            <h3>运行参数</h3>
            <DataTree :value="run.parameters" />
          </div>
          <div v-if="execution?.output_directory" class="card">
            <h3>输出目录</h3>
            <p class="plain-value fine-print">
              {{ execution.output_directory }}
            </p>
            <p class="hint">点击“刷新远端状态”查看实际发现的产物。</p>
          </div>
          <details v-if="run.evaluation" class="card">
            <summary>评测协议</summary>
            <DataTree :value="run.evaluation" />
          </details>
          <div v-if="revision" class="card">
            <h3>修改来源</h3>
            <p class="hint">{{ revision.notice }}</p>
            <router-link :to="`/runs/${revision.original_run_id}`"
              >查看原运行 #{{
                shortId(revision.original_run_id)
              }}
              →</router-link
            >
            <p class="fine-print">
              冻结副本 {{ revision.candidate_directory }}
            </p>
            <p class="fine-print">{{ revision.source_digest }}</p>
          </div>
        </div>
      </section>
      <section v-else-if="tab === 'logs'">
        <div class="card">
          <div class="card-title">
            <h3>远端训练日志</h3>
            <div class="actions">
              <el-checkbox v-model="logFollow">跟随末尾</el-checkbox
              ><el-button
                :disabled="!execution || logBusy"
                :loading="logBusy"
                @click="readLogs()"
                >{{ logs.hasMore ? "读取后续日志" : "读取最新日志" }}</el-button
              ><el-button
                :disabled="logBusy"
                @click="
                  logs.reset();
                  readLogs();
                "
                >从头重新读取</el-button
              >
            </div>
          </div>
          <p class="fine-print">
            当前字节偏移 {{ logs.offset
            }}<span v-if="logs.clipped">
              · 页面仅保留最近 256K 字符，完整文件保留在服务器</span
            >
          </p>
          <p v-if="logError" class="inline-error">{{ logError }}</p>
          <pre ref="logElement" class="log-view" @scroll.passive="logScroll">{{
            logs.text || "尚未读取到日志。"
          }}</pre>
        </div>
      </section>
      <section v-else-if="tab === 'report'">
        <div class="card">
          <div class="card-title">
            <h2>事实报告</h2>
            <div class="actions">
              <el-button @click="readReport">读取已存报告</el-button
              ><el-button
                type="primary"
                plain
                :loading="action === 'report'"
                :disabled="!!action"
                @click="operate('report')"
                >生成或更新事实报告</el-button
              >
            </div>
          </div>
          <p class="hint">
            事实报告汇总真实指标，不调用模型。运行结束且日志采集完整后，开启的自动分析会解释报告，随后生成有来源的建议；这里的“运行结束”也包括停止或失败。
          </p>
          <SectionState
            :loading="reportBusy"
            :error="reportError"
            :empty="!reportBusy && !report && !reportError"
            text="尚未生成事实报告"
            @retry="readReport"
          /><template v-if="report"
            ><div class="metadata-row">
              <span>版本 v{{ report.version }}</span
              ><span>{{ dateTime(report.generated_at) }}</span
              ><el-tag v-if="report.is_stale" type="warning"
                >已有新数据，报告待更新</el-tag
              >
            </div>
            <ReportSummary :data="report.data"
          /></template>
        </div>
      </section>
      <RunAgents
        v-else-if="tab === 'agents'"
        :key="id"
        :run-id="id"
        @change="selectChange" /><RunCode
        v-else-if="tab === 'code'"
        :key="id"
        :run-id="id"
        :focus-change="focusChange" />
      <section v-else-if="tab === 'comparison'">
        <div class="card">
          <div class="card-title">
            <h2>修改前后结果对比</h2>
            <el-button @click="readComparison">刷新对比</el-button>
          </div>
          <p class="hint">
            仅在评测声明兼容、两侧报告具备条件时比较数值。数值变化不直接证明修改有效。
          </p>
          <SectionState
            :loading="comparisonBusy"
            :error="compareError"
            :empty="!comparisonBusy && !comparison && !compareError"
            text="此运行暂无可比较结果"
            @retry="readComparison"
          /><template v-if="comparison"
            ><div
              class="notice"
              :class="{ 'warning-note': !comparison.comparable }"
            >
              {{
                comparison.comparable ? "具备可比较条件" : "当前不可直接比较"
              }}
              <ul>
                <li v-for="reason in comparison.reasons" :key="reason">
                  {{ reason }}
                </li>
              </ul>
            </div>
            <p class="fine-print">
              原报告 v{{ comparison.original_report_version ?? "未就绪" }} ·
              新报告 v{{ comparison.candidate_report_version ?? "未就绪" }}
            </p>
            <div class="card-grid">
              <article
                v-for="item in comparison.series"
                :key="`${item.dataset}/${item.split}/${item.metric_name}`"
                class="card"
              >
                <div class="card-title">
                  <h3>{{ item.metric_name }}</h3>
                  <el-tag
                    :type="
                      item.outcome === 'improved'
                        ? 'success'
                        : item.outcome === 'worsened'
                          ? 'warning'
                          : 'info'
                    "
                    >{{ comparisonLabels[item.outcome] }}</el-tag
                  >
                </div>
                <p class="fine-print">{{ item.dataset }} · {{ item.split }}</p>
                <div class="metric-grid">
                  <div class="metric-value">
                    <small>原运行末值</small
                    ><strong>{{ number(item.original_last) }}</strong>
                  </div>
                  <div class="metric-value">
                    <small>新运行末值</small
                    ><strong>{{ number(item.candidate_last) }}</strong>
                  </div>
                </div>
                <p class="hint">变化量 {{ number(item.delta) }}</p>
                <p v-if="item.reason" class="fine-print">{{ item.reason }}</p>
                <p class="fine-print">
                  方向：{{
                    item.direction === "minimize"
                      ? "越小越好"
                      : item.direction === "maximize"
                        ? "越大越好"
                        : "未指定"
                  }}
                </p>
              </article>
            </div>
            <details class="tool-record">
              <summary>参数变化</summary>
              <DataTree :value="comparison.parameter_changes" />
            </details>
            <p class="hint">{{ comparison.notice }}</p>
            <router-link :to="`/runs/${comparison.original_run_id}`"
              >查看原运行 →</router-link
            ></template
          >
        </div>
      </section>
      <details v-if="observation" class="card">
        <summary>最近一次远端操作确认的信息与实际输出</summary>
        <DataTree :value="observation" /></details
    ></template>
    <el-dialog
      v-model="parserDialog"
      title="通用日志格式与采集规则"
      width="720px"
      :close-on-click-modal="!parserBusy"
      :show-close="!parserBusy"
    >
      <MetricParserForm
        :rules="parserRules"
        :busy="parserBusy"
        :error="parserError"
        @save="saveParser"
      />
    </el-dialog>
    <el-dialog
      v-model="startDialog"
      title="启动训练"
      width="640px"
      :close-on-click-modal="!action"
      ><p class="notice warning-note">
        这是实际训练操作。请选择服务器及已登记入口，并核对冻结参数。
      </p>
      <el-form label-position="top" :disabled="!!action" @submit.prevent
        ><el-form-item label="训练服务器" required
          ><el-select v-model="startForm.server_id" filterable
            ><el-option
              v-for="server in servers"
              :key="server.server_id"
              :label="`${server.name} · ${server.host}:${server.port}`"
              :value="server.server_id" /></el-select></el-form-item
        ><el-form-item label="已登记脚本入口" required
          ><el-select v-model="startForm.script_id"
            ><el-option
              v-for="script in scripts"
              :key="script.script_id"
              :label="script.script_id"
              :value="script.script_id" /></el-select></el-form-item
        ><DataTree :value="run?.parameters"
      /></el-form>
      <p v-if="startError" class="inline-error">{{ startError }}</p>
      <template #footer
        ><el-button :disabled="!!action" @click="startDialog = false"
          >取消</el-button
        ><el-button type="primary" :loading="action === 'start'" @click="start"
          >确认启动此运行</el-button
        ></template
      ></el-dialog
    >
  </div>
</template>
