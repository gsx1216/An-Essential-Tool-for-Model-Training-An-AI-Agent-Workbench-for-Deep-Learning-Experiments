<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { api } from "../api";
import { errorText } from "../api/client";
import type {
  Schema,
  Server,
  Script,
  Parameters,
  ParameterRule,
} from "../types/domain";
import NetworkFilePicker from "./NetworkFilePicker.vue";
import ParameterFields from "./ParameterFields.vue";
import SectionState from "./SectionState.vue";
import ServerGpuInfo from "./ServerGpuInfo.vue";
const props = defineProps<{
  experiments: Schema<"ExperimentListItem">[];
  servers: Server[];
  scripts: Script[];
  initialExperiment: string;
  busy: boolean;
  error: string;
}>();
const emit = defineEmits<{ submit: [body: Schema<"TaskRequest">] }>();
const form = reactive({
  name: "",
  experiment_id: props.initialExperiment,
  server_id: "",
  script_id: "",
  python: "",
  entrypoint: "",
  main_network: "",
  arguments: "",
});
const mode = ref("project");
const selectedGpus = ref<string[]>([]);
const parameters = ref<Parameters>({});
const rows = ref<
  { name: string; flag: string; type: "int" | "float" | "str"; value: string }[]
>([]);
const environments = ref<Schema<"PythonEnvironment">[]>([]),
  environmentBusy = ref(false),
  environmentError = ref(""),
  truncated = ref(false);
const directory = ref("."),
  files = ref<{ path: string; kind: "directory" | "file" }[]>([]),
  nextOffset = ref<number | null>(null),
  fileBusy = ref(false),
  fileError = ref(""),
  localError = ref("");
const server = computed(() =>
  props.servers.find((s) => s.server_id === form.server_id),
);
const rules = computed(
  () =>
    props.scripts.find((s) => s.script_id === form.script_id)?.parameters || {},
);
let serverEpoch = 0,
  directoryEpoch = 0;
onBeforeUnmount(() => {
  serverEpoch++;
  directoryEpoch++;
});
async function loadEnvironments() {
  if (!form.server_id) return;
  const target = form.server_id,
    epoch = serverEpoch;
  environmentBusy.value = true;
  environmentError.value = "";
  try {
    const result = await api.projectEnvironments(target);
    if (epoch !== serverEpoch) return;
    environments.value = result.items;
    truncated.value = result.truncated;
  } catch (e) {
    if (epoch === serverEpoch) environmentError.value = errorText(e);
  } finally {
    if (epoch === serverEpoch) environmentBusy.value = false;
  }
}
async function loadFiles(path = directory.value, more = false) {
  if (!form.server_id) return;
  const epoch = ++directoryEpoch,
    selectedServer = serverEpoch;
  fileBusy.value = true;
  fileError.value = "";
  try {
    const result = await api.projectFiles(
      form.server_id,
      path,
      more ? nextOffset.value || 0 : 0,
    );
    if (epoch !== directoryEpoch || selectedServer !== serverEpoch) return;
    directory.value = result.path;
    files.value = more ? [...files.value, ...result.items] : result.items;
    nextOffset.value = result.next_offset;
  } catch (e) {
    if (epoch === directoryEpoch && selectedServer === serverEpoch)
      fileError.value = errorText(e);
  } finally {
    if (epoch === directoryEpoch && selectedServer === serverEpoch)
      fileBusy.value = false;
  }
}
function up() {
  void loadFiles(
    directory.value.includes("/")
      ? directory.value.slice(0, directory.value.lastIndexOf("/"))
      : ".",
  );
}
watch(
  () => form.server_id,
  () => {
    serverEpoch++;
    directoryEpoch++;
    form.python = "";
    form.entrypoint = "";
    form.main_network = "";
    environments.value = [];
    files.value = [];
    directory.value = ".";
    nextOffset.value = null;
    fileError.value = "";
    environmentError.value = "";
    if (form.server_id) {
      void loadEnvironments();
      void loadFiles(".");
    }
  },
);
watch(
  () => form.script_id,
  () => {
    parameters.value = {};
  },
);
function submit() {
  if (props.busy) return;
  localError.value = "";
  try {
    if (!form.name.trim() || !form.experiment_id || !form.server_id)
      throw new Error("请填写任务名称并选择实验、服务器。");
    const common = {
      name: form.name.trim(),
      experiment_id: form.experiment_id,
      server_id: form.server_id,
    };
    if (mode.value === "registered") {
      if (!form.script_id) throw new Error("请选择快捷模板。");
      emit("submit", {
        ...common,
        script_id: form.script_id,
        default_parameters: parameters.value,
      });
      return;
    }
    if (!form.python || !form.entrypoint)
      throw new Error("请选择 Python 环境和训练入口文件。");
    const argumentsList = form.arguments
      .split(/\r?\n/)
      .map((a) => a.trim())
      .filter(Boolean);
    const parameterRules: Record<string, ParameterRule> = {},
      defaults: Parameters = {};
    for (const row of rows.value) {
      const key = row.name.trim(),
        flag = row.flag.trim();
      if (
        !/^[a-z][a-z0-9_]{0,63}$/.test(key) ||
        key in parameterRules ||
        ["source_root", "input_root", "artifact_dir", "config_file"].includes(
          key,
        )
      )
        throw new Error(
          "参数名须为小写字母开头的英文/数字/下划线，不能重复或使用系统路径名称。",
        );
      if (!/^--?[A-Za-z][A-Za-z0-9_-]*$/.test(flag))
        throw new Error("参数选项应为 --epochs 或 -b 这样的格式。");
      if (!row.value.trim()) throw new Error(`请填写参数 ${key} 的默认值。`);
      const value = row.type === "str" ? row.value : Number(row.value);
      if (
        typeof value === "number" &&
        (!Number.isFinite(value) ||
          (row.type === "int" && !Number.isInteger(value)))
      )
        throw new Error(`参数 ${key} 的数值格式不正确。`);
      parameterRules[key] = { type: row.type, required: true };
      defaults[key] = value;
      argumentsList.push(flag, `{${key}}`);
    }
    emit("submit", {
      ...common,
      project: {
        entrypoint: form.entrypoint,
        main_network: form.main_network || null,
        python: form.python,
        arguments: argumentsList,
        parameter_rules: parameterRules,
        cuda_visible_devices: selectedGpus.value.length
          ? selectedGpus.value.join(",")
          : null,
        max_runtime_seconds: 86400,
        max_log_bytes: 10485760,
      },
      default_parameters: defaults,
    });
  } catch (e) {
    localError.value = errorText(e);
  }
}
</script>
<template>
  <el-form label-position="top" :disabled="busy" @submit.prevent="submit">
    <div class="form-grid">
      <el-form-item label="任务名称" required class="wide"
        ><el-input
          v-model="form.name"
          maxlength="80"
          placeholder="例如：我的 PRNet 训练"
      /></el-form-item>
      <el-form-item label="训练实验" required
        ><el-select
          v-model="form.experiment_id"
          filterable
          placeholder="选择训练实验"
          ><el-option
            v-for="exp in experiments"
            :key="exp.experiment_id"
            :label="exp.title"
            :value="exp.experiment_id" /></el-select
      ></el-form-item>
      <el-form-item label="服务器" required
        ><el-select
          v-model="form.server_id"
          filterable
          placeholder="选择自己的服务器"
          ><el-option
            v-for="item in servers"
            :key="item.server_id"
            :label="`${item.name} · ${item.host}`"
            :value="item.server_id" /></el-select
      ></el-form-item>
    </div>
    <el-form-item label="配置方式"
      ><el-radio-group v-model="mode"
        ><el-radio-button value="project">选择我的项目</el-radio-button
        ><el-radio-button value="registered"
          >快捷模板（可选）</el-radio-button
        ></el-radio-group
      ></el-form-item
    >
    <template v-if="mode === 'project'">
      <p v-if="!server" class="hint">
        先选择服务器，系统再读取该服务器的 Python 环境和项目文件。
      </p>
      <template v-else>
        <el-form-item label="Python 环境" required>
          <el-select
            v-model="form.python"
            filterable
            allow-create
            default-first-option
            :loading="environmentBusy"
            placeholder="选择环境，或输入 Python 绝对路径"
            ><el-option
              v-for="item in environments"
              :key="item.python"
              :label="`${item.name} · ${item.python}`"
              :value="item.python"
          /></el-select>
          <el-button text :loading="environmentBusy" @click="loadEnvironments"
            >重新读取环境</el-button
          >
        </el-form-item>
        <p class="fine-print">
          只检查解释器文件是否存在且可执行，不安装依赖、不运行训练。没有列出的环境可填写完整
          Python 路径。
        </p>
        <p v-if="truncated" class="notice warning-note">
          环境较多，当前仅展示部分发现结果。
        </p>
        <SectionState :error="environmentError" @retry="loadEnvironments" />
        <el-form-item label="训练入口文件" required
          ><el-input
            :model-value="form.entrypoint"
            readonly
            placeholder="从下方项目文件中选择 .py 文件"
        /></el-form-item>
        <div class="card project-file-picker">
          <div class="actions">
            <strong>项目文件</strong
            ><el-button
              text
              :disabled="directory === '.' || fileBusy"
              @click="up"
              >上一级</el-button
            ><el-button text :loading="fileBusy" @click="loadFiles()"
              >刷新文件</el-button
            >
          </div>
          <p class="fine-print plain-value">
            工作目录：{{ server.work_dir }}<br />当前目录：{{ directory }}
          </p>
          <SectionState
            :loading="fileBusy && !files.length"
            :error="fileError"
            :empty="!fileBusy && !fileError && !files.length"
            text="当前目录没有可选择的源码文件"
            @retry="loadFiles()"
          />
          <div class="project-file-list">
            <el-button
              v-for="item in files"
              :key="item.path"
              text
              :type="form.entrypoint === item.path ? 'primary' : 'default'"
              :disabled="
                fileBusy || (item.kind === 'file' && !item.path.endsWith('.py'))
              "
              @click="
                item.kind === 'directory'
                  ? loadFiles(item.path)
                  : (form.entrypoint = item.path)
              "
              >{{ item.kind === "directory" ? "📁" : "📄" }}
              {{ item.path.split("/").at(-1) }}</el-button
            >
          </div>
          <el-button
            v-if="nextOffset !== null"
            :loading="fileBusy"
            @click="loadFiles(directory, true)"
            >加载更多文件</el-button
          >
        </div>
        <el-form-item label="主网络代码（用于分析）">
          <NetworkFilePicker
            v-model="form.main_network"
            :server-id="form.server_id"
            :disabled="busy"
          />
        </el-form-item>
        <el-form-item label="额外启动参数（可选，每行一个 argv 参数）"
          ><el-input
            v-model="form.arguments"
            type="textarea"
            :rows="4"
            placeholder="例如：&#10;--data&#10;/mnt/datasets/COD10K&#10;--output&#10;{artifact_dir}"
        /></el-form-item>
        <p class="fine-print">
          直接传给训练脚本，不填写 python/train.py，不使用 shell
          命令。{artifact_dir}
          是本次运行独立输出目录；仅在脚本支持对应参数时填写。
        </p>
        <div class="actions">
          <strong>可在聊天中调整的参数（可选）</strong
          ><el-button
            text
            @click="rows.push({ name: '', flag: '', type: 'int', value: '' })"
            >添加参数</el-button
          >
        </div>
        <div v-for="(row, index) in rows" :key="index" class="parameter-row">
          <el-input
            v-model="row.name"
            placeholder="名称，如 epochs"
            aria-label="参数名称"
          />
          <el-input
            v-model="row.flag"
            placeholder="选项，如 --epochs"
            aria-label="脚本选项"
          />
          <el-select v-model="row.type" aria-label="参数类型"
            ><el-option label="整数" value="int" /><el-option
              label="小数"
              value="float" /><el-option label="文本" value="str"
          /></el-select>
          <el-input
            v-model="row.value"
            placeholder="默认值"
            aria-label="参数默认值"
          /><el-button text type="danger" @click="rows.splice(index, 1)"
            >移除</el-button
          >
        </div>
        <ServerGpuInfo
          v-model:selected="selectedGpus"
          :server-id="form.server_id"
          selectable
          :disabled="busy"
        />
      </template>
    </template>
    <template v-else
      ><el-form-item label="已登记快捷模板" required
        ><el-select v-model="form.script_id" placeholder="选择已有模板"
          ><el-option
            v-for="script in scripts"
            :key="script.script_id"
            :label="script.script_id"
            :value="script.script_id" /></el-select></el-form-item
      ><ParameterFields v-model="parameters" :rules="rules"
    /></template>
    <p class="fine-print">
      保存配置只读取和校验入口，不开始训练。之后在聊天中按任务名称明确要求启动。
    </p>
    <p v-if="localError || error" class="inline-error">
      {{ localError || error }}
    </p>
    <div class="pagination-actions">
      <el-button type="primary" native-type="submit" :loading="busy"
        >保存任务配置</el-button
      >
    </div>
  </el-form>
</template>
<style scoped>
.project-file-list {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  max-height: 250px;
  overflow: auto;
}
.project-file-list .el-button {
  justify-content: flex-start;
  margin-left: 0;
  white-space: normal;
  text-align: left;
  word-break: break-all;
  height: auto;
  min-height: 32px;
}
.parameter-row {
  display: grid;
  grid-template-columns: 1fr 1fr 90px 1fr auto;
  gap: 8px;
  margin: 12px 0;
}
@media (max-width: 640px) {
  .parameter-row {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
