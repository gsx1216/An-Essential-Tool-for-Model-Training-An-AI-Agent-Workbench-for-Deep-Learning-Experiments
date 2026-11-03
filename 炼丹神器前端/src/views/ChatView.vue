<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  Promotion,
  RefreshRight,
  Document,
  DataAnalysis,
  ArrowRight,
  CopyDocument,
} from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import {
  ApiError,
  errorText,
  identityGeneration,
  isCancelled,
} from "../api/client";
import { useWorkspace } from "../stores/workspace";
import { useChat } from "../stores/chat";
import { readFullHistory } from "../lib/history";
import { sameId, shortId, dateTime, safeAssistantText } from "../lib/format";
import { notifyError } from "../lib/ui";
import type { Message, ToolCall, Schema } from "../types/domain";
import BrandMark from "../components/BrandMark.vue";
import MarkdownText from "../components/MarkdownText.vue";
import DataTree from "../components/DataTree.vue";
import SectionState from "../components/SectionState.vue";
import RunSummary from "../components/RunSummary.vue";
const route = useRoute(),
  router = useRouter(),
  workspace = useWorkspace(),
  chat = useChat();
const id = computed(() => String(route.params.sessionId || ""));
const title = computed(
  () =>
    workspace.sessions.find((s) => sameId(s.session_id, id.value))?.title ||
    "新聊天",
);
const turn = computed(() => chat.turn(id.value || "new"));
const draft = computed({
  get: () => chat.drafts[id.value || "new"] || "",
  set: (value) => {
    chat.drafts[id.value || "new"] = value;
  },
});
const messages = ref<Message[]>([]),
  loading = ref(false),
  loadedCount = ref(0),
  loadError = ref(""),
  sendingNew = ref(false);
const calls = ref<ToolCall[]>([]),
  activeRun = ref<string | null>(null),
  showDetails = ref(false),
  showRun = ref(false),
  detailsError = ref(""),
  plan = ref<unknown>(null),
  audit = ref<Schema<"TurnSkillResponse"> | null>(null);
const scroller = ref<HTMLElement>(),
  follow = ref(true);
let readController: AbortController | undefined,
  epoch = 0;
const unresolvedHuman = computed(() => messages.value.at(-1)?.role === "human");
const cannotNew = computed(
  () =>
    turn.value.busy ||
    loading.value ||
    !!loadError.value ||
    sendingNew.value ||
    !turn.value.resolved ||
    unresolvedHuman.value,
);
async function bottom(force = false) {
  await nextTick();
  if ((force || follow.value) && scroller.value)
    scroller.value.scrollTop = scroller.value.scrollHeight;
}
function trackScroll() {
  const el = scroller.value;
  if (el) follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
}
async function loadHistory(forceBottom = false) {
  const target = id.value,
    current = ++epoch;
  readController?.abort();
  readController = new AbortController();
  const signal = readController.signal;
  if (!target) {
    messages.value = [];
    loadError.value = "";
    loading.value = false;
    return;
  }
  loading.value = true;
  loadError.value = "";
  loadedCount.value = 0;
  try {
    const data = await readFullHistory(
      (after) => api.history(target, after, signal),
      (count) => {
        if (current === epoch) loadedCount.value = count;
      },
    );
    if (current !== epoch || target !== id.value) return;
    messages.value = data;
    await bottom(forceBottom);
  } catch (e) {
    if (current === epoch && !isCancelled(e)) loadError.value = errorText(e);
  } finally {
    if (current === epoch) loading.value = false;
  }
}
async function loadDetails() {
  const target = id.value;
  if (!target) return;
  detailsError.value = "";
  try {
    const data = await api.calls(target);
    if (target !== id.value) return;
    calls.value = data.items;
    activeRun.value = data.active_run_id;
  } catch (e) {
    if (target === id.value && !isCancelled(e))
      detailsError.value = errorText(e);
  }
}
async function moreCalls() {
  const target = id.value;
  try {
    const data = await api.calls(target, calls.value.at(-1)?.id || 0);
    if (target === id.value) {
      calls.value.push(...data.items);
      activeRun.value = data.active_run_id;
    }
  } catch (e) {
    notifyError(e);
  }
}
async function send(replay = false, retrySaved = false) {
  if (turn.value.busy || sendingNew.value || loading.value) return;
  let target = id.value;
  const content = draft.value.trim();
  if (!retrySaved && !replay && (!content || cannotNew.value)) return;
  if (!target) {
    sendingNew.value = true;
    try {
      const created = await api.createSession(content.slice(0, 24) || "新聊天");
      target = created.session_id;
      chat.drafts[target] = content;
      delete chat.drafts.new;
      await workspace.loadSessions();
      await router.replace(`/chat/${target}`);
    } catch (e) {
      notifyError(e);
      return;
    } finally {
      sendingNew.value = false;
    }
  }
  const state = chat.turn(target);
  if (!replay && !retrySaved)
    state.request = {
      content,
      request_id: crypto.randomUUID(),
      skill_id: workspace.selectedSkill,
    };
  if (!retrySaved && !state.request) return;
  state.busy = true;
  state.error = "";
  state.partial = "";
  state.resolved = false;
  const identity = identityGeneration();
  if (!replay && !retrySaved) chat.drafts[target] = "";
  follow.value = true;
  void bottom(true);
  try {
    const receive = (event: import("../lib/sse").ChatStreamEvent) => {
      if (identity !== identityGeneration()) return;
      if (event.type === "reset") state.partial = "";
      if (event.type === "delta") state.partial += event.content || "";
      if (target === id.value) void bottom();
    };
    if (retrySaved) await api.retryStream(target, receive);
    else await api.sendStream(target, state.request!, receive);
    if (identity !== identityGeneration()) return;
    state.resolved = true;
    state.partial = "";
    state.request = null;
    if (target === id.value) {
      await loadHistory(true);
      await loadDetails();
    }
  } catch (e) {
    if (identity !== identityGeneration() || isCancelled(e)) return;
    state.error = errorText(e);
    if (target === id.value) {
      await loadHistory();
      await loadDetails();
    }
    if (
      target === id.value &&
      !loadError.value &&
      e instanceof ApiError &&
      [400, 413, 422].includes(e.status) &&
      !unresolvedHuman.value
    ) {
      chat.drafts[target] = state.request?.content || content;
      state.resolved = true;
      state.request = null;
    }
  } finally {
    state.busy = false;
  }
}
async function showPlan() {
  if (!id.value) return;
  try {
    plan.value = await api.plan(id.value);
    showDetails.value = true;
  } catch (e) {
    notifyError(e);
  }
}
async function showAudit(call: ToolCall) {
  try {
    audit.value = await api.skillAudit(id.value, call.turn_id);
  } catch (e) {
    notifyError(e);
  }
}
async function copy(content: string) {
  try {
    await navigator.clipboard.writeText(content);
    ElMessage.success("已复制");
  } catch {
    ElMessage.warning("浏览器未允许复制，请手动选择文字。");
  }
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void send();
  }
}
watch(
  id,
  () => {
    messages.value = [];
    calls.value = [];
    activeRun.value = null;
    audit.value = null;
    plan.value = null;
    follow.value = true;
    void loadHistory(true);
    void loadDetails();
  },
  { immediate: true },
);
watch(
  () => route.query.prompt,
  (value) => {
    if (typeof value === "string") draft.value = value;
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  epoch++;
  readController?.abort();
});
</script>
<template>
  <div class="chat-workspace">
    <section class="chat-main">
      <header class="chat-header">
        <div>
          <div class="eyebrow">RESEARCH COMPANION</div>
          <h1>{{ title }}</h1>
        </div>
        <div class="header-actions">
          <el-button
            text
            :icon="Document"
            @click="
              showDetails = true;
              loadDetails();
            "
            >工具记录</el-button
          ><el-button
            class="run-drawer-toggle"
            :icon="DataAnalysis"
            @click="showRun = true"
            >运行</el-button
          >
        </div>
      </header>
      <div ref="scroller" class="message-scroll" @scroll.passive="trackScroll">
        <div class="messages-inner">
          <SectionState
            :loading="loading"
            :error="loadError"
            @retry="loadHistory"
            ><span v-if="loadedCount"
              >已读取 {{ loadedCount }} 条</span
            ></SectionState
          >
          <div
            v-if="!loading && !loadError && !messages.length && !turn.busy"
            class="chat-welcome"
          >
            <BrandMark large compact />
            <div class="eyebrow">你的实验，从一个问题开始</div>
            <h2>今天，想验证什么想法？</h2>
            <p>一起分析训练结果、阅读论文，或从真实代码寻找改进方向。</p>
            <div class="prompt-grid">
              <button
                @click="
                  draft = '最近一次训练结果怎么样？请查看真实报告并分析。'
                "
              >
                <DataAnalysis />分析训练结果<ArrowRight /></button
              ><button @click="draft = '请列出我可用的训练任务，先不要启动。'">
                <Promotion />查看训练任务<ArrowRight /></button
              ><button @click="router.push('/library')">
                <Document />阅读论文资料<ArrowRight />
              </button>
            </div>
          </div>
          <article
            v-for="message in messages"
            :key="message.seq"
            class="message"
            :class="message.role"
          >
            <div class="message-avatar" :class="message.role">
              {{
                message.role === "human"
                  ? "你"
                  : message.role === "ai"
                    ? "炉"
                    : "系"
              }}
            </div>
            <div class="message-content">
              <div class="message-label">
                {{
                  message.role === "human"
                    ? "你"
                    : message.role === "ai"
                      ? "炼丹助手"
                      : "系统消息"
                }}<span>#{{ message.seq }}</span>
              </div>
              <div v-if="message.role === 'human'" class="user-bubble">
                {{ message.content }}
              </div>
              <MarkdownText
                v-else
                :content="safeAssistantText(message.content)"
              /><button
                class="message-copy text-button"
                aria-label="复制消息"
                @click="
                  copy(
                    message.role === 'ai'
                      ? safeAssistantText(message.content)
                      : message.content,
                  )
                "
              >
                <el-icon><CopyDocument /></el-icon> 复制
              </button>
            </div>
          </article>
          <article
            v-if="
              turn.request &&
              !turn.resolved &&
              !messages.some(
                (m) =>
                  m.role === 'human' &&
                  m.content === turn.request?.content &&
                  m.seq === messages.at(-1)?.seq,
              )
            "
            class="message human"
          >
            <div class="message-avatar human">你</div>
            <div class="message-content">
              <div class="message-label">本轮问题</div>
              <div class="user-bubble">{{ turn.request.content }}</div>
            </div>
          </article>
          <article v-if="turn.partial" class="message ai">
            <div class="message-avatar ai">炉</div>
            <div class="message-content">
              <div class="message-label">
                {{
                  turn.busy ? "正在生成，尚未保存" : "未完成的回复片段，未保存"
                }}
              </div>
              <MarkdownText :content="safeAssistantText(turn.partial)" />
            </div>
          </article>
          <div v-if="turn.busy" class="thinking" role="status">
            <span
              class="loading-ring"
            />正在处理本轮问题，可能会读取报告或调用工具…
          </div>
          <div v-if="turn.error" class="turn-error" role="alert">
            <strong>本轮暂未取得回复</strong>
            <p>{{ turn.error }}</p>
            <small v-if="turn.request"
              >请求 {{ shortId(turn.request.request_id) }} · 重试沿用原问题与
              Skill</small
            ><el-button
              v-if="turn.request"
              :icon="RefreshRight"
              :loading="turn.busy"
              @click="send(true)"
              >重试本轮</el-button
            >
          </div>
          <div
            v-if="!turn.busy && unresolvedHuman && !turn.request && !loadError"
            class="turn-error"
          >
            <p>最后一个问题尚无回复，可以继续完成这一轮。</p>
            <el-button :loading="turn.busy" @click="send(false, true)"
              >重试最后一轮</el-button
            >
          </div>
        </div>
      </div>
      <form class="composer-wrap" @submit.prevent="send()">
        <div class="composer">
          <el-input
            v-model="draft"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 7 }"
            resize="none"
            placeholder="描述你的问题，或明确说“启动 任务名称”…"
            :disabled="cannotNew"
            aria-label="输入消息"
            @keydown="keydown"
          />
          <div class="composer-bottom">
            <el-select
              v-model="workspace.selectedSkill"
              clearable
              placeholder="不使用 Skill"
              aria-label="本轮 Skill"
              :disabled="turn.busy"
              ><el-option
                v-for="skill in workspace.skills"
                :key="skill.skill_id"
                :label="skill.title"
                :value="skill.skill_id" /></el-select
            ><span class="composer-tip">Enter 发送 · Shift + Enter 换行</span
            ><el-button
              type="primary"
              native-type="submit"
              :icon="Promotion"
              :disabled="!draft.trim() || cannotNew"
              :loading="turn.busy || sendingNew"
              >发送</el-button
            >
          </div>
        </div>
        <p class="composer-note">
          启动训练需要明确指令。报告、代码与资料来源可在工具记录中核对。
        </p>
      </form>
    </section>
    <aside class="chat-run-aside"><RunSummary :run-id="activeRun" /></aside>
    <el-drawer
      v-model="showRun"
      title="当前关联运行"
      size="min(92vw, 420px)"
      destroy-on-close
      ><RunSummary :run-id="activeRun"
    /></el-drawer>
    <el-drawer
      v-model="showDetails"
      title="本会话的工具与依据"
      size="min(94vw, 680px)"
      ><p class="hint">这里显示实际调用记录，不包含模型内部推理。</p>
      <el-button :disabled="!id" @click="loadDetails">刷新记录</el-button
      ><el-button :disabled="!id" @click="showPlan">查看最近训练计划</el-button>
      <p v-if="detailsError" class="inline-error">{{ detailsError }}</p>
      <div v-if="activeRun" class="notice">
        关联运行
        <router-link :to="`/runs/${activeRun}`"
          >{{ shortId(activeRun) }} →</router-link
        >
      </div>
      <DataTree v-if="plan" :value="plan" /><SectionState
        :empty="!calls.length"
        text="此会话暂无工具记录" />
      <details v-for="call in calls" :key="call.id" class="tool-record">
        <summary>
          {{ call.tool_name }}
          <span class="muted"
            >{{ call.outcome }} · {{ call.duration_ms }}ms</span
          >
        </summary>
        <p class="fine-print">
          {{ dateTime(call.created_at) }} · 本轮 {{ shortId(call.turn_id) }}
        </p>
        <DataTree
          :value="{ arguments: call.arguments, result: call.result_metadata }"
        /><el-button text @click="showAudit(call)"
          >查看本轮 Skill 快照</el-button
        >
      </details>
      <el-button v-if="calls.length >= 100" @click="moreCalls"
        >加载后续工具记录</el-button
      ><DataTree v-if="audit" :value="audit"
    /></el-drawer>
  </div>
</template>
