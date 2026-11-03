<script setup lang="ts">
import { computed, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { api } from "../api";
import { errorText, isCancelled } from "../api/client";
import { confirmAction, notifyError } from "../lib/ui";
import { dateTime } from "../lib/format";
import type { Schema } from "../types/domain";
import { useSerialPoll } from "../composables/useSerialPoll";
import StatusBadge from "./StatusBadge.vue";
import SectionState from "./SectionState.vue";
import NetworkFilePicker from "./NetworkFilePicker.vue";
import MarkdownText from "./MarkdownText.vue";
import CodeFindingCard from "./CodeFindingCard.vue";
const props = defineProps<{ runId: string }>();
const emit = defineEmits<{ change: [id: string] }>();
const router = useRouter();
const tasks = ref<Schema<"AgentTaskResponse">[]>([]),
  suggestions = ref<Schema<"SuggestionResponse">[]>([]),
  loading = ref(true),
  error = ref(""),
  busy = ref("");
const readiness = ref<Schema<"ManualAnalysisResponse"> | null>(null),
  manualBusy = ref(false),
  networkBusy = ref(false);
const requestIds = new Map<string, string>();
const suggestionOffset = ref(0),
  taskOffset = ref(0);
const moreSuggestions = ref(false),
  moreTasks = ref(false);
async function load(signal?: AbortSignal) {
  if (signal?.aborted) return;
  const result = await Promise.allSettled([
    api.agentTasks(props.runId),
    api.suggestions(props.runId),
    api.analysisReadiness(props.runId),
  ]);
  if (signal?.aborted) return;
  error.value = "";
  result.forEach((item, index) => {
    if (item.status === "rejected") {
      if (!isCancelled(item.reason)) error.value ||= errorText(item.reason);
      return;
    }
    if (index === 2) {
      readiness.value = item.value as Schema<"ManualAnalysisResponse">;
      return;
    }
    const length = Array.isArray(item.value) ? item.value.length : 0;
    if (index === 0) {
      tasks.value = item.value as Schema<"AgentTaskResponse">[];
      moreTasks.value = length === 20;
      taskOffset.value = length;
    }
    if (index === 1) {
      suggestions.value = item.value as Schema<"SuggestionResponse">[];
      moreSuggestions.value = length === 20;
      suggestionOffset.value = length;
    }
  });
  loading.value = false;
}
const needsPoll = computed(
  () =>
    loading.value ||
    tasks.value.some((t) => ["queued", "running"].includes(t.status)) ||
    readiness.value?.processing ||
    (readiness.value !== null && !readiness.value.report_ready),
);
useSerialPoll(needsPoll, load, 10000);
async function more(kind: "suggestions" | "tasks") {
  try {
    if (kind === "suggestions") {
      const page = await api.suggestions(props.runId, suggestionOffset.value);
      suggestions.value.push(...page);
      suggestionOffset.value += page.length;
      moreSuggestions.value = page.length === 20;
    } else {
      const page = await api.agentTasks(props.runId, taskOffset.value);
      tasks.value.push(...page);
      taskOffset.value += page.length;
      moreTasks.value = page.length === 20;
    }
  } catch (e) {
    notifyError(e);
  }
}
async function saveNetwork(path: string) {
  networkBusy.value = true;
  try {
    await api.saveMainNetwork(props.runId, path);
    ElMessage.success("已选择主网络代码");
    await load();
  } catch (e) {
    notifyError(e);
  } finally {
    networkBusy.value = false;
  }
}
async function startManual() {
  manualBusy.value = true;
  try {
    const result = await api.startAnalysis(props.runId);
    ElMessage.success(result.message);
    await load();
  } catch (e) {
    notifyError(e);
  } finally {
    manualBusy.value = false;
  }
}
const taskError = (code: string) =>
  ({
    MODEL_UNAVAILABLE: "模型连接失败，请检查网络后重新提交。",
    MODEL_TIMEOUT: "模型请求超时，可重新提交。",
    KEY_UNAVAILABLE: "请在设置中配置有效的模型 Key。",
    INPUT_TOO_LARGE: "主网络文件过大，请选择更具体的网络模块后重新提交。",
    EMPTY_MODEL_OUTPUT: "模型请求已返回，但没有给出建议正文，请重新提交。",
    MODEL_OUTPUT_TOO_LARGE:
      "模型回复超过保存长度，请重新提交生成更简短的建议。",
    INVALID_MODEL_OUTPUT: "模型回复的正文格式异常，请重新提交。",
  })[code] || "本次处理未完成，请重新提交；详细原因已记录到后端日志。";
async function prepare(
  suggestion: Schema<"SuggestionResponse">,
  index: number,
) {
  if (
    !(await confirmAction(
      `基于第 ${index + 1} 条建议生成修改计划？此步骤会调用你的模型，但不会写入远端代码。`,
      "生成修改计划",
    ))
  )
    return;
  const key = `${suggestion.task_id}:${index}`;
  busy.value = key;
  if (!requestIds.has(key)) requestIds.set(key, crypto.randomUUID());
  try {
    const change = await api.prepareChange(props.runId, {
      suggestion_task_id: suggestion.task_id,
      suggestion_index: index + 1,
      request_id: requestIds.get(key)!,
    });
    emit("change", change.change_id);
    ElMessage.success("修改计划记录已保存");
  } catch (e) {
    notifyError(e);
  } finally {
    busy.value = "";
  }
}
</script>
<template>
  <div>
    <div class="card-title">
      <h2>分析与有据建议</h2>
      <el-button :loading="loading" @click="load()">刷新</el-button>
    </div>
    <SectionState :loading="loading" :error="error" @retry="load()" />
    <div class="notice">
      后端直接读取主网络代码和原始文本日志，点击按钮后生成代码建议。使用已保存的模型
      Key，可能产生模型服务费用；不会重启训练或修改代码。
    </div>
    <div class="card">
      <h3>主网络代码与建议</h3>
      <NetworkFilePicker
        :model-value="readiness?.main_network.path || ''"
        :run-id="runId"
        :disabled="networkBusy || manualBusy || readiness?.processing"
        @update:model-value="saveNetwork"
      />
      <p class="hint">
        {{ readiness?.message || "正在检查事实报告与主网络选择…" }}
      </p>
      <el-button
        type="primary"
        :loading="manualBusy"
        :disabled="!readiness?.can_start || networkBusy"
        @click="startManual"
        >生成代码建议</el-button
      >
    </div>
    <div class="card">
      <h3>建议生成进度</h3>
      <SectionState
        :empty="!tasks.length"
        text="尚未手动提交分析。事实报告就绪并选好主网络后，点击上方“生成代码建议”。"
      />
      <div v-for="task in tasks" :key="task.task_id" class="tool-record">
        <div class="card-title">
          <strong
            >{{
              task.agent_name === "analysis" ? "分析材料准备" : "主网络代码建议"
            }}
            · 报告 v{{ task.report_version }}</strong
          ><StatusBadge :state="task.status" />
        </div>
        <p v-if="task.last_error_code" class="inline-error">
          {{ taskError(task.last_error_code) }}
        </p>
      </div>
      <el-button v-if="moreTasks" text @click="more('tasks')"
        >更多处理记录</el-button
      >
    </div>
    <div class="split-heading">
      <h3>网络代码建议</h3>
      <span class="hint">{{ suggestions.length }} 份已加载</span>
    </div>
    <SectionState
      :empty="!suggestions.length && !loading"
      text="建议尚未就绪"
    />
    <article v-for="group in suggestions" :key="group.task_id" class="card">
      <div class="card-title">
        <h3>建议 · 报告 v{{ group.report_version }}</h3>
        <el-tag v-if="group.is_stale" type="warning">基于旧报告</el-tag>
      </div>
      <p class="fine-print">
        {{ dateTime(group.created_at) }}
        <template v-if="!group.content.direct_advice"
          >· 证据状态
          {{
            group.content.evidence_status === "supported"
              ? "已取得引用依据"
              : "依据不足"
          }}</template
        >
      </p>
      <MarkdownText
        v-if="group.content.direct_advice"
        :content="group.content.direct_advice"
      />
      <p v-if="group.content.direct_input?.log_clipped" class="fine-print">
        日志较长，本次结合了日志开头和末尾；未提供中间部分。
      </p>
      <p
        v-if="
          group.content.direct_input &&
          !group.content.direct_input.log_available
        "
        class="fine-print"
      >
        本次未取得原始文本日志，仅依据主网络代码给出建议。
      </p>
      <h3 v-if="group.content.code_findings?.length">主网络代码诊断</h3>
      <CodeFindingCard
        v-for="(finding, index) in group.content.code_findings || []"
        :key="index"
        :finding="finding"
        :index="index"
        :evidence="group.content.code_evidence"
      />
      <div
        v-for="(suggestion, index) in group.content.suggestions"
        :key="index"
        class="card"
      >
        <CodeFindingCard
          :finding="suggestion"
          :evidence="group.content.code_evidence"
          :index="index"
        />
        <el-button
          type="primary"
          plain
          :loading="busy === `${group.task_id}:${index}`"
          @click="prepare(group, index)"
          >生成修改计划</el-button
        >
      </div>
      <p
        v-if="
          !group.content.direct_advice &&
          !group.content.suggestions.length &&
          !group.content.code_findings?.length
        "
        class="notice warning-note"
      >
        当前证据不足，尚无可应用的修改建议。
      </p>
      <h4 v-if="group.content.limitations.length">建议说明</h4>
      <ul>
        <li v-for="(item, index) in group.content.limitations" :key="index">
          {{ item }}
        </li>
      </ul>
      <div class="actions">
        <el-button
          text
          @click="
            router.push({
              path: '/chat',
              query: {
                prompt: `请结合运行 ${runId} 的报告和建议解释适用条件，先不要修改代码。`,
              },
            })
          "
          >在聊天中讨论</el-button
        >
      </div>
    </article>
    <el-button v-if="moreSuggestions" @click="more('suggestions')"
      >更多建议</el-button
    >
  </div>
</template>
