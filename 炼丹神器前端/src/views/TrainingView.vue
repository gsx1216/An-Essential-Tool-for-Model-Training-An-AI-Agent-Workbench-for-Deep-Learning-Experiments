<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { Plus, ArrowRight, Refresh, Delete } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import { errorText } from "../api/client";
import { confirmAction, notifyError } from "../lib/ui";
import { dateTime } from "../lib/format";
import type {
  Schema,
  Script,
  Server,
  TrainingTask,
  Parameters,
} from "../types/domain";
import StatusBadge from "../components/StatusBadge.vue";
import SectionState from "../components/SectionState.vue";
import ExperimentForm from "../components/ExperimentForm.vue";
import ParameterFields from "../components/ParameterFields.vue";
import DataTree from "../components/DataTree.vue";
import TrainingTaskForm from "../components/TrainingTaskForm.vue";
import TrainingTaskDetails from "../components/TrainingTaskDetails.vue";
const router = useRouter();
const tab = ref("experiments"),
  experiments = ref<Schema<"ExperimentListItem">[]>([]),
  selected = ref(""),
  cursor = ref<string | null>(null),
  runs = ref<Schema<"RunListItem">[]>([]),
  runCursor = ref<string | null>(null);
const loading = ref(false),
  runLoading = ref(false),
  error = ref(""),
  runError = ref(""),
  experimentDetail = ref<Schema<"ExperimentResponse"> | null>(null);
const experimentDialog = ref(false),
  taskDialog = ref(false),
  runDialog = ref(false),
  saving = ref(false),
  formError = ref(""),
  optionsError = ref("");
const servers = ref<Server[]>([]),
  scripts = ref<Script[]>([]),
  taskResults = ref<TrainingTask[]>([]),
  taskQuery = ref(""),
  taskOffset = ref<number | null>(null),
  taskLoading = ref(false),
  taskError = ref(""),
  taskDetail = ref<TrainingTask | null>(null);
const deletingTasks = reactive(new Set<string>());
const deletingExperiment = ref("");
const deletingRuns = reactive(new Set<string>());
const detailExperimentName = ref(""),
  detailServerName = ref("");
const runForm = reactive({
  experiment_id: "",
  script_id: "",
  parameters: {} as Parameters,
  request_id: "",
});
const runRules = computed(
  () =>
    scripts.value.find((s) => s.script_id === runForm.script_id)?.parameters ||
    {},
);
const trainingExperiments = computed(() =>
  experiments.value.filter((e) => e.kind === "training"),
);
let runEpoch = 0,
  taskEpoch = 0,
  taskDetailEpoch = 0;
watch(
  () => runForm.script_id,
  () => {
    runForm.parameters = {};
    runForm.request_id = "";
  },
);
async function load(more = false) {
  loading.value = true;
  error.value = "";
  try {
    const result = await api.experiments(
      more ? cursor.value || undefined : undefined,
    );
    experiments.value = more
      ? [...experiments.value, ...result.items]
      : result.items;
    cursor.value = result.next_cursor;
    if (!selected.value && experiments.value[0])
      selected.value = experiments.value[0].experiment_id;
  } catch (e) {
    error.value = errorText(e);
  } finally {
    loading.value = false;
  }
}
async function loadOptions() {
  optionsError.value = "";
  void api
    .scripts()
    .then((result) => {
      scripts.value = result.items;
    })
    .catch(() => {
      scripts.value = [];
    });
  try {
    let before: string | undefined;
    const rows: Server[] = [];
    do {
      const page = await api.servers(before);
      rows.push(...page.items);
      before = page.next_cursor || undefined;
    } while (before);
    servers.value = rows;
  } catch (e) {
    optionsError.value = errorText(e);
  }
}
async function loadRuns(more = false) {
  const id = selected.value,
    current = ++runEpoch;
  if (!id) return;
  runLoading.value = true;
  runError.value = "";
  try {
    const result = await api.runs(
      id,
      more ? runCursor.value || undefined : undefined,
    );
    if (current !== runEpoch) return;
    runs.value = more ? [...runs.value, ...result.items] : result.items;
    runCursor.value = result.next_cursor;
    if (!more) {
      const detail = await api.experiment(id);
      if (current === runEpoch) experimentDetail.value = detail;
    }
  } catch (e) {
    if (current === runEpoch) runError.value = errorText(e);
  } finally {
    if (current === runEpoch) runLoading.value = false;
  }
}
watch(selected, () => {
  runs.value = [];
  runCursor.value = null;
  experimentDetail.value = null;
  void loadRuns();
});
async function createExperiment(body: Schema<"CreateExperimentRequest">) {
  saving.value = true;
  formError.value = "";
  try {
    const result = await api.createExperiment(body);
    experimentDialog.value = false;
    await load();
    selected.value = result.experiment_id;
    ElMessage.success("实验已创建");
  } catch (e) {
    formError.value = errorText(e);
  } finally {
    saving.value = false;
  }
}
async function listTasks(more = false) {
  const current = ++taskEpoch;
  taskLoading.value = true;
  taskError.value = "";
  try {
    const result = await api.tasks(
      taskQuery.value,
      more ? taskOffset.value || 0 : 0,
    );
    if (current !== taskEpoch) return;
    taskResults.value = more
      ? [...taskResults.value, ...result.items]
      : result.items;
    taskOffset.value = result.next_offset;
  } catch (e) {
    if (current === taskEpoch) taskError.value = errorText(e);
  } finally {
    if (current === taskEpoch) taskLoading.value = false;
  }
}
async function createTask(body: Schema<"TaskRequest">) {
  saving.value = true;
  formError.value = "";
  try {
    await api.createTask(body);
    taskDialog.value = false;
    await listTasks();
    tab.value = "tasks";
    ElMessage.success("任务已配置，可以在聊天中按名称启动");
  } catch (e) {
    formError.value = errorText(e);
  } finally {
    saving.value = false;
  }
}
async function createRun() {
  if (!selected.value || !runForm.script_id) {
    formError.value = "请选择登记的脚本入口以填写参数。";
    return;
  }
  saving.value = true;
  formError.value = "";
  if (!runForm.request_id) {
    runForm.request_id = crypto.randomUUID();
    runForm.experiment_id = selected.value;
  }
  try {
    const result = await api.createRun(runForm.experiment_id, {
      request_id: runForm.request_id,
      parameters: runForm.parameters,
    });
    runDialog.value = false;
    runForm.request_id = "";
    await router.push(
      `/runs/${result.run.run_id}?script=${encodeURIComponent(runForm.script_id)}`,
    );
  } catch (e) {
    formError.value = errorText(e);
  } finally {
    saving.value = false;
  }
}
async function inspectTask(task: TrainingTask) {
  const current = ++taskDetailEpoch;
  try {
    const detail = await api.task(task.task_id);
    if (current !== taskDetailEpoch) return;
    taskDetail.value = detail;
    detailExperimentName.value =
      experiments.value.find(
        (item) => item.experiment_id === detail.experiment_id,
      )?.title || "";
    detailServerName.value =
      servers.value.find((item) => item.server_id === detail.server_id)?.name ||
      "";
    const [experiment, server] = await Promise.allSettled([
      detailExperimentName.value
        ? Promise.resolve(null)
        : api.experiment(detail.experiment_id),
      detailServerName.value
        ? Promise.resolve(null)
        : api.server(detail.server_id),
    ]);
    if (current !== taskDetailEpoch || !taskDetail.value) return;
    if (experiment.status === "fulfilled" && experiment.value)
      detailExperimentName.value = experiment.value.title;
    if (server.status === "fulfilled" && server.value)
      detailServerName.value = server.value.name;
  } catch (e) {
    notifyError(e);
  }
}
function closeTaskDetail() {
  taskDetailEpoch++;
  taskDetail.value = null;
}
async function deleteExperiment() {
  const experiment = experimentDetail.value;
  if (!experiment || deletingExperiment.value) return;
  if (
    !(await confirmAction(
      `永久删除实验“${experiment.title}”及其全部运行、指标、报告、分析、建议和所属训练配置？数据库记录将被实际删除，无法恢复。聊天原文、服务器代码和权重文件保留。`,
      "永久删除实验",
    ))
  )
    return;
  deletingExperiment.value = experiment.experiment_id;
  try {
    await api.deleteExperiment(experiment.experiment_id);
    if (selected.value === experiment.experiment_id) {
      runEpoch++;
      selected.value = "";
      experimentDetail.value = null;
      runs.value = [];
      runError.value = "";
      runLoading.value = false;
    }
    experiments.value = experiments.value.filter(
      (item) => item.experiment_id !== experiment.experiment_id,
    );
    if (taskDetail.value?.experiment_id === experiment.experiment_id)
      closeTaskDetail();
    await Promise.all([load(), listTasks()]);
    ElMessage.success("实验及数据库关联记录已永久删除");
  } catch (e) {
    notifyError(e);
  } finally {
    deletingExperiment.value = "";
  }
}
async function deleteRun(run: Schema<"RunListItem">) {
  if (deletingRuns.has(run.run_id)) return;
  if (
    !(await confirmAction(
      `永久删除这条“${run.experiment_title}”运行记录及其指标、报告、分析、建议和修改记录？数据库记录将被实际删除，无法恢复。实验定义、其他运行、聊天原文和服务器文件保留。`,
      "永久删除运行记录",
    ))
  )
    return;
  deletingRuns.add(run.run_id);
  try {
    await api.deleteRun(run.run_id);
    runs.value = runs.value.filter((item) => item.run_id !== run.run_id);
    if (selected.value === run.experiment_id) await loadRuns();
    ElMessage.success("运行及数据库关联记录已永久删除");
  } catch (e) {
    notifyError(e);
  } finally {
    deletingRuns.delete(run.run_id);
  }
}
async function deleteTask(task: TrainingTask) {
  if (deletingTasks.has(task.task_id)) return;
  if (
    !(await confirmAction(
      `删除训练配置“${task.name}”？历史计划和运行记录将保留，已经运行的训练不会停止。`,
    ))
  )
    return;
  deletingTasks.add(task.task_id);
  try {
    await api.deleteTask(task.task_id);
    if (taskDetail.value?.task_id === task.task_id) closeTaskDetail();
    ElMessage.success("训练配置已删除");
    await listTasks();
  } catch (e) {
    notifyError(e);
  } finally {
    deletingTasks.delete(task.task_id);
  }
}
function openTask() {
  formError.value = "";
  taskDialog.value = true;
  void loadOptions();
}
onMounted(() => {
  void load();
  void listTasks();
  void loadOptions();
});
</script>
<template>
  <div class="page">
    <header class="page-header">
      <div>
        <div class="eyebrow">实验与训练管理</div>
        <h1>实验与训练</h1>
        <p>从实验配置到训练结果，保留每一步可追溯的记录。</p>
      </div>
      <div class="header-actions">
        <el-button
          :icon="Refresh"
          aria-label="刷新实验"
          @click="
            load();
            listTasks();
          "
        /><el-button
          type="primary"
          :icon="Plus"
          @click="
            experimentDialog = true;
            formError = '';
          "
          >新建实验</el-button
        >
      </div>
    </header>
    <el-tabs v-model="tab"
      ><el-tab-pane label="实验与运行" name="experiments" /><el-tab-pane
        label="对话训练任务"
        name="tasks"
    /></el-tabs>
    <section v-if="tab === 'experiments'">
      <SectionState
        :loading="loading && !experiments.length"
        :error="error"
        :empty="!loading && !error && !experiments.length"
        text="创建第一个实验，开始记录训练"
        @retry="load()"
      />
      <div v-if="experiments.length" class="toolbar">
        <el-select v-model="selected" filterable aria-label="选择实验"
          ><el-option
            v-for="exp in experiments"
            :key="exp.experiment_id"
            :label="exp.title"
            :value="exp.experiment_id" /></el-select
        ><el-button v-if="cursor" :loading="loading" @click="load(true)"
          >加载更多实验</el-button
        ><el-button
          type="primary"
          plain
          :disabled="!selected || !!optionsError"
          @click="
            runDialog = true;
            formError = '';
          "
          >创建运行记录</el-button
        ><el-button text @click="openTask">配置对话训练任务</el-button>
      </div>
      <div v-if="experimentDetail" class="card">
        <div class="card-title">
          <h2>{{ experimentDetail.title }}</h2>
          <el-tag effect="plain">{{
            experimentDetail.kind === "training" ? "模型训练" : "脚本实验"
          }}</el-tag>
          <el-button
            type="danger"
            plain
            :icon="Delete"
            :aria-label="`永久删除实验 ${experimentDetail.title}`"
            :loading="deletingExperiment === experimentDetail.experiment_id"
            @click="deleteExperiment"
            >删除实验</el-button
          >
        </div>
        <p class="hint">{{ experimentDetail.description || "暂无实验说明" }}</p>
        <details v-if="experimentDetail.evaluation">
          <summary class="text-button">评测方案与指标定义</summary>
          <DataTree :value="experimentDetail.evaluation" />
        </details>
        <p v-else class="fine-print">
          此实验未声明评测方案，结果对比可能受到限制。
        </p>
      </div>
      <SectionState
        :loading="runLoading"
        :error="runError"
        :empty="!!selected && !runLoading && !runError && !runs.length"
        text="这个实验还没有运行记录"
        @retry="loadRuns()"
      />
      <div class="card-grid">
        <article v-for="run in runs" :key="run.run_id" class="card">
          <div class="card-title">
            <StatusBadge :state="run.status" /><span class="fine-print"
              >训练运行</span
            >
          </div>
          <h3>{{ run.experiment_title }}</h3>
          <p class="card-meta">创建于 {{ dateTime(run.created_at) }}</p>
          <p v-if="run.error_code" class="inline-error">{{ run.error_code }}</p>
          <div class="actions">
            <router-link :to="`/runs/${run.run_id}`"
              ><el-button :icon="ArrowRight"
                >查看训练监控</el-button
              ></router-link
            ><span class="hint">{{
              run.has_result ? `报告 v${run.result_version}` : "报告尚未就绪"
            }}</span>
            <el-button
              type="danger"
              plain
              :icon="Delete"
              :aria-label="`永久删除运行 ${run.experiment_title}`"
              :disabled="
                ['queued', 'running'].includes(run.status) ||
                !!deletingExperiment
              "
              :loading="deletingRuns.has(run.run_id)"
              @click="deleteRun(run)"
              >删除运行</el-button
            >
          </div>
        </article>
      </div>
      <div v-if="runCursor" class="pagination-actions">
        <el-button :loading="runLoading" @click="loadRuns(true)"
          >加载更多运行</el-button
        >
      </div>
    </section>
    <section v-else>
      <div class="toolbar">
        <el-input
          v-model="taskQuery"
          maxlength="80"
          clearable
          placeholder="按任务名称搜索"
          @keyup.enter="listTasks()"
        /><el-button :loading="taskLoading" @click="listTasks()">查询</el-button
        ><el-button type="primary" :icon="Plus" @click="openTask"
          >配置训练任务</el-button
        >
      </div>
      <p class="hint">
        将实验、服务器、脚本入口与默认参数保存为任务。配置后，在聊天中明确说“启动
        任务名称”。
      </p>
      <SectionState
        :loading="taskLoading"
        :error="taskError"
        :empty="!taskLoading && !taskError && !taskResults.length"
        text="暂无符合条件的训练任务"
        @retry="listTasks()"
      />
      <div class="card-grid">
        <article v-for="task in taskResults" :key="task.task_id" class="card">
          <h2>{{ task.name }}</h2>
          <p class="hint">已保存训练配置，可在详情中核对文件与参数。</p>
          <div class="actions">
            <el-button @click="inspectTask(task)">配置详情</el-button
            ><router-link
              :to="{
                path: '/chat',
                query: {
                  prompt: `请查询训练任务 ${task.name}，先展示配置，不要启动。`,
                },
              }"
              ><el-button type="primary" plain
                >在聊天中查看</el-button
              ></router-link
            >
            <el-button
              type="danger"
              plain
              :icon="Delete"
              :aria-label="`删除训练配置 ${task.name}`"
              :loading="deletingTasks.has(task.task_id)"
              @click="deleteTask(task)"
              >删除配置</el-button
            >
          </div>
        </article>
      </div>
      <div v-if="taskOffset !== null" class="pagination-actions">
        <el-button :loading="taskLoading" @click="listTasks(true)"
          >加载更多任务</el-button
        >
      </div>
    </section>
    <el-dialog
      v-model="experimentDialog"
      title="新建实验"
      width="780px"
      :close-on-click-modal="!saving"
      :show-close="!saving"
      destroy-on-close
      ><ExperimentForm
        :busy="saving"
        :error="formError"
        @save="createExperiment"
    /></el-dialog>
    <el-dialog
      v-model="taskDialog"
      title="配置对话训练任务"
      width="min(96vw, 820px)"
      :close-on-click-modal="!saving"
      :show-close="!saving"
    >
      <SectionState :error="optionsError" @retry="loadOptions" />
      <TrainingTaskForm
        v-if="taskDialog"
        :experiments="trainingExperiments"
        :servers="servers"
        :scripts="scripts"
        :initial-experiment="
          trainingExperiments.some((e) => e.experiment_id === selected)
            ? selected
            : ''
        "
        :busy="saving"
        :error="formError"
        @submit="createTask"
      />
    </el-dialog>
    <el-dialog
      v-model="runDialog"
      title="创建运行记录"
      width="620px"
      :close-on-click-modal="!saving"
      :show-close="!saving"
      ><p class="hint">创建记录后，选择服务器并确认启动。此处只保存参数。</p>
      <el-form
        label-position="top"
        :disabled="saving || !!runForm.request_id"
        @submit.prevent
        ><el-form-item label="参数对应的脚本入口"
          ><el-select v-model="runForm.script_id"
            ><el-option
              v-for="script in scripts"
              :key="script.script_id"
              :label="script.script_id"
              :value="script.script_id" /></el-select></el-form-item
        ><ParameterFields v-model="runForm.parameters" :rules="runRules" />
        <p v-if="formError" class="inline-error">{{ formError }}</p>
        <p v-if="runForm.request_id" class="fine-print">
          原创建请求已发出；再次点击会查询或复用同一运行，避免重复创建。
        </p></el-form
      ><template #footer
        ><el-button :disabled="saving" @click="runDialog = false"
          >关闭</el-button
        ><el-button type="primary" :loading="saving" @click="createRun">{{
          runForm.request_id ? "重试原创建请求" : "创建记录，不启动"
        }}</el-button></template
      ></el-dialog
    >
    <el-drawer
      :model-value="!!taskDetail"
      title="训练任务配置"
      size="min(94vw, 620px)"
      @close="closeTaskDetail"
      ><TrainingTaskDetails
        v-if="taskDetail"
        :task="taskDetail"
        :experiment-name="detailExperimentName"
        :server-name="detailServerName"
    /></el-drawer>
  </div>
</template>
