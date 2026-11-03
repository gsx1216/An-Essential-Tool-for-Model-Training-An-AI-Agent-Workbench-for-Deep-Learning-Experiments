<script setup lang="ts">
import { onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { api } from "../api";
import { errorText } from "../api/client";
import { confirmAction, notifyError } from "../lib/ui";
import { dateTime, shortId } from "../lib/format";
import type { Schema, Parameters, ParameterRule } from "../types/domain";
import SectionState from "./SectionState.vue";
import StatusBadge from "./StatusBadge.vue";
import DataTree from "./DataTree.vue";
import ParameterFields from "./ParameterFields.vue";
const props = defineProps<{ runId: string; focusChange?: string }>();
const router = useRouter();
const subtab = ref("source"),
  directory = ref<Schema<"DirectoryResponse"> | null>(null),
  source = ref<Schema<"FileResponse"> | null>(null),
  loading = ref(false),
  error = ref(""),
  sourceError = ref(""),
  changes = ref<Schema<"ChangeResponse">[]>([]),
  moreChanges = ref(false),
  offset = ref(0),
  selected = ref<Schema<"ChangeResponse"> | null>(null),
  revisions = ref<Schema<"RevisionResponse">[]>([]),
  busy = ref("");
const deriveOpen = ref(false),
  parameters = ref<Parameters>({}),
  parameterRules = ref<Record<string, ParameterRule>>({}),
  deriveError = ref("");
const deriveIds = reactive<Record<string, string>>({});
const deriveParameters: Record<string, Parameters> = {};
let sourceEpoch = 0;
async function listFiles(path = ".", more = false) {
  loading.value = true;
  sourceError.value = "";
  try {
    const result = await api.files(
      props.runId,
      path,
      more ? directory.value?.next_offset || 0 : 0,
    );
    directory.value =
      more && directory.value
        ? { ...result, items: [...directory.value.items, ...result.items] }
        : result;
  } catch (e) {
    sourceError.value = errorText(e);
  } finally {
    loading.value = false;
  }
}
async function readFile(path?: string, line = 1, sha?: string) {
  const current = ++sourceEpoch;
  loading.value = true;
  sourceError.value = "";
  try {
    const result = await api.file(props.runId, path, line, sha);
    if (current === sourceEpoch) source.value = result;
  } catch (e) {
    if (current === sourceEpoch) sourceError.value = errorText(e);
  } finally {
    if (current === sourceEpoch) loading.value = false;
  }
}
async function loadChanges(more = false) {
  error.value = "";
  try {
    const page = await api.changes(props.runId, more ? offset.value : 0);
    changes.value = more ? [...changes.value, ...page] : page;
    offset.value = changes.value.length;
    moreChanges.value = page.length === 20;
  } catch (e) {
    error.value = errorText(e);
  }
}
async function openChange(id: string) {
  try {
    selected.value = await api.change(id);
    subtab.value = "changes";
    revisions.value = await api.revisions(id);
  } catch (e) {
    notifyError(e);
  }
}
async function apply() {
  const item = selected.value;
  if (
    !item ||
    !(await confirmAction(
      `将此计划应用到独立副本并运行登记检查？原项目保留，暂不启动训练。修改编号 ${shortId(item.change_id)}。`,
      "应用代码修改",
    ))
  )
    return;
  busy.value = "apply";
  try {
    selected.value = await api.applyChange(item.change_id);
    await loadChanges();
    ElMessage.success("已读取应用结果，请检查差异与检查状态");
  } catch (e) {
    notifyError(e);
  } finally {
    busy.value = "";
  }
}
async function openDerive() {
  const item = selected.value;
  if (!item) return;
  deriveError.value = "";
  parameters.value = { ...(deriveParameters[item.change_id] || {}) };
  parameterRules.value = {};
  try {
    const execution = await api.execution(props.runId);
    const scripts = await api.scripts();
    parameterRules.value =
      scripts.items.find((s) => s.script_id === execution.script_id)
        ?.parameters || {};
    deriveOpen.value = true;
  } catch (e) {
    notifyError(e);
  }
}
async function derive() {
  const item = selected.value;
  if (!item) return;
  if (
    !(await confirmAction(
      "预检修改副本并创建一个新的运行记录？此步骤不启动训练。",
      "准备修改后训练",
    ))
  )
    return;
  busy.value = "derive";
  deriveError.value = "";
  deriveIds[item.change_id] ||= crypto.randomUUID();
  deriveParameters[item.change_id] ||= { ...parameters.value };
  try {
    const result = await api.prepareRevision(item.change_id, {
      request_id: deriveIds[item.change_id],
      parameters: deriveParameters[item.change_id],
    });
    deriveOpen.value = false;
    await router.push(`/runs/${result.run_id}`);
  } catch (e) {
    deriveError.value = errorText(e);
  } finally {
    busy.value = "";
  }
}
watch(
  () => props.focusChange,
  (value) => {
    if (value) void openChange(value);
  },
  { immediate: true },
);
onMounted(() => {
  void listFiles();
  void loadChanges();
});
</script>
<template>
  <div>
    <el-tabs v-model="subtab"
      ><el-tab-pane label="真实源码" name="source" /><el-tab-pane
        label="修改记录"
        name="changes"
    /></el-tabs>
    <div v-if="subtab === 'source'">
      <div class="notice">
        读取当前版本的源码片段。路径、行号与 SHA256
        可用于核对建议；这不等于完整训练时快照。
      </div>
      <div class="actions" style="margin-bottom: 16px">
        <el-button :disabled="loading" @click="readFile()"
          >读取登记的训练入口</el-button
        ><el-button :disabled="loading" @click="listFiles()"
          >返回项目根目录</el-button
        >
      </div>
      <SectionState :error="sourceError" @retry="listFiles()" />
      <div class="source-browser">
        <div class="file-list">
          <p class="fine-print">{{ directory?.path || "." }}</p>
          <button
            v-if="directory?.path && directory.path !== '.'"
            @click="
              listFiles(directory.path.split('/').slice(0, -1).join('/') || '.')
            "
          >
            ← 上级目录</button
          ><button
            v-for="entry in directory?.items || []"
            :key="entry.path"
            :disabled="loading"
            @click="
              entry.kind === 'directory'
                ? listFiles(entry.path)
                : readFile(entry.path)
            "
          >
            {{ entry.kind === "directory" ? "▸" : "·" }}
            {{ entry.path.split("/").at(-1) }}</button
          ><el-button
            v-if="directory?.next_offset !== null && directory"
            text
            :loading="loading"
            @click="listFiles(directory.path, true)"
            >更多文件</el-button
          >
        </div>
        <div class="source-content">
          <template v-if="source"
            ><h3>{{ source.path }}</h3>
            <p class="fine-print">
              SHA256 {{ source.file_sha256 }} · 读取于
              {{ dateTime(source.retrieved_at) }}
            </p>
            <p class="fine-print">
              与登记训练入口一致：{{
                source.training_entry_match === null
                  ? "无此依据"
                  : source.training_entry_match
                    ? "是"
                    : "否"
              }}<template v-if="source.frozen_source_match != null">
                · 与冻结副本一致：{{
                  source.frozen_source_match ? "是" : "否"
                }}</template
              >
            </p>
            <pre
              class="code-block"
            ><span v-for="line in source.lines" :key="line.number" class="source-line"><span class="line-number">{{ line.number }}</span>{{ line.text }}</span></pre>
            <div class="actions">
              <el-button
                :disabled="loading || source.start_line <= 1"
                @click="
                  readFile(
                    source.path,
                    Math.max(1, source.start_line - 60),
                    source.file_sha256,
                  )
                "
                >上一段</el-button
              ><span class="hint"
                >{{ source.start_line }}～{{ source.end_line ?? "—" }} /
                {{ source.total_lines }} 行</span
              ><el-button
                :disabled="loading || !source.next_line"
                @click="
                  readFile(
                    source.path,
                    source.next_line || 1,
                    source.file_sha256,
                  )
                "
                >下一段</el-button
              ><el-button
                text
                :disabled="loading"
                @click="readFile(source.path)"
                >重新读取最新版本</el-button
              >
            </div></template
          ><SectionState
            v-else
            :loading="loading"
            :empty="!loading"
            text="选择一个文件，读取源码与版本信息"
          />
        </div>
      </div>
    </div>
    <div v-else>
      <div class="card-title">
        <h3>修改计划与实际结果</h3>
        <el-button @click="loadChanges()">刷新记录</el-button>
      </div>
      <SectionState
        :error="error"
        :empty="!error && !changes.length"
        text="尚无修改记录，请从有据建议中生成计划"
        @retry="loadChanges()"
      />
      <div class="record-stack">
        <article v-for="item in changes" :key="item.change_id" class="card">
          <div class="card-title">
            <strong
              >建议 {{ item.suggestion_index }} · 修改 #{{
                shortId(item.change_id)
              }}</strong
            ><StatusBadge :state="item.status" />
          </div>
          <p class="fine-print">{{ dateTime(item.created_at) }}</p>
          <p class="hint">
            {{
              item.plan?.explanation ||
              item.error_code ||
              "计划处理中，请刷新查看。"
            }}
          </p>
          <el-button @click="openChange(item.change_id)"
            >查看计划、差异与检查</el-button
          >
        </article>
      </div>
      <div v-if="moreChanges" class="pagination-actions">
        <el-button @click="loadChanges(true)">更多修改记录</el-button>
      </div>
    </div>
    <el-drawer
      :model-value="!!selected"
      title="修改详情"
      size="min(96vw, 900px)"
      @close="selected = null"
      ><template v-if="selected"
        ><div class="card-title">
          <h2>修改 #{{ shortId(selected.change_id) }}</h2>
          <StatusBadge :state="selected.status" />
        </div>
        <div class="actions">
          <el-button @click="openChange(selected.change_id)"
            >刷新此记录</el-button
          ><el-button
            v-if="
              selected.plan &&
              !selected.lease_active &&
              !selected.result &&
              !['conflict', 'planning'].includes(selected.status)
            "
            type="primary"
            :loading="busy === 'apply'"
            @click="apply"
            >应用到独立副本</el-button
          ><el-button
            v-if="
              selected.result &&
              ['passed', 'incomplete'].includes(selected.result.check_status)
            "
            type="primary"
            plain
            @click="openDerive"
            >准备修改后训练</el-button
          >
        </div>
        <p v-if="selected.error_code" class="inline-error">
          {{ selected.error_code }}
        </p>
        <template v-if="selected.plan"
          ><h3 style="margin-top: 24px">修改说明</h3>
          <p>{{ selected.plan.explanation }}</p>
          <div
            v-for="(edit, index) in selected.plan.edits"
            :key="index"
            class="card"
          >
            <h4>{{ edit.path }}</h4>
            <p class="hint">{{ edit.reason }}</p>
            <div class="diff-pair">
              <div>
                <p class="fine-print">替换前</p>
                <pre class="code-block diff-old">{{ edit.old_text }}</pre>
              </div>
              <div>
                <p class="fine-print">替换后</p>
                <pre class="code-block diff-new">{{ edit.new_text }}</pre>
              </div>
            </div>
          </div>
          <details class="tool-record">
            <summary>计划绑定的文件版本和登记检查</summary>
            <DataTree
              :value="{
                files: selected.plan.files,
                project_checks: selected.plan.project_checks,
                excluded_directories: selected.plan.excluded_directories,
              }"
            /></details></template
        ><template v-if="selected.result"
          ><h3>实际应用结果</h3>
          <p class="notice">
            副本目录：{{ selected.result.workspace_directory }}
          </p>
          <StatusBadge :state="selected.result.check_status" /><DataTree
            :value="selected.result.checks"
          />
          <div v-for="file in selected.result.files" :key="file.path">
            <h4>{{ file.path }}</h4>
            <p class="fine-print">
              {{ file.before_sha256 }} → {{ file.after_sha256 }}
            </p>
            <pre class="code-block">{{ file.diff }}</pre>
          </div>
          <p class="fine-print">{{ selected.result.notice }}</p></template
        >
        <h3 style="margin-top: 24px">派生的训练记录</h3>
        <SectionState :empty="!revisions.length" text="暂无派生训练" />
        <div
          v-for="revision in revisions"
          :key="revision.run_id"
          class="tool-record"
        >
          <StatusBadge :state="revision.status" /><router-link
            :to="`/runs/${revision.run_id}`"
            style="margin-left: 10px"
            >查看运行 #{{ shortId(revision.run_id) }} →</router-link
          >
        </div></template
      ></el-drawer
    >
    <el-dialog
      v-model="deriveOpen"
      title="准备修改后训练"
      width="620px"
      append-to-body
      :close-on-click-modal="!busy"
      ><p class="hint">
        默认保留原训练参数；只填写需要覆盖的参数。原运行与修改记录会绑定到新运行。
      </p>
      <el-form
        label-position="top"
        :disabled="!!busy || !!(selected && deriveIds[selected.change_id])"
        @submit.prevent
        ><ParameterFields v-model="parameters" :rules="parameterRules"
      /></el-form>
      <p v-if="deriveError" class="inline-error">{{ deriveError }}</p>
      <template #footer
        ><el-button :disabled="!!busy" @click="deriveOpen = false"
          >取消</el-button
        ><el-button type="primary" :loading="busy === 'derive'" @click="derive"
          >保存派生运行，不启动</el-button
        ></template
      ></el-dialog
    >
  </div>
</template>
