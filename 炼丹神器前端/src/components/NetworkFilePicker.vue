<script setup lang="ts">
import { ref, watch } from "vue";
import { api } from "../api";
import { errorText } from "../api/client";
import SectionState from "./SectionState.vue";
const props = defineProps<{
  modelValue: string;
  serverId?: string;
  runId?: string;
  disabled?: boolean;
}>();
const emit = defineEmits<{ "update:modelValue": [path: string] }>();
const visible = ref(false),
  busy = ref(false),
  error = ref("");
const directory = ref("."),
  files = ref<{ path: string; kind: string }[]>([]),
  next = ref<number | null>(null);
let epoch = 0;
watch(
  () => [props.serverId, props.runId],
  () => {
    epoch++;
    visible.value = false;
    files.value = [];
    directory.value = ".";
  },
);
async function load(path = ".", more = false) {
  const current = ++epoch;
  busy.value = true;
  error.value = "";
  try {
    const result = props.runId
      ? await api.files(props.runId, path, more ? next.value || 0 : 0)
      : await api.projectFiles(
          props.serverId!,
          path,
          more ? next.value || 0 : 0,
        );
    if (current !== epoch) return;
    directory.value = result.path;
    files.value = more ? [...files.value, ...result.items] : result.items;
    next.value = result.next_offset;
  } catch (e) {
    if (current === epoch) error.value = errorText(e);
  } finally {
    if (current === epoch) busy.value = false;
  }
}
function open() {
  visible.value = true;
  void load(".");
}
function choose(item: { path: string; kind: string }) {
  if (item.kind === "directory") {
    void load(item.path);
    return;
  }
  emit("update:modelValue", item.path);
  visible.value = false;
}
function up() {
  void load(
    directory.value.includes("/")
      ? directory.value.slice(0, directory.value.lastIndexOf("/"))
      : ".",
  );
}
</script>
<template>
  <div>
    <div class="actions">
      <el-input
        :model-value="modelValue"
        readonly
        placeholder="选择实现模型结构和 forward 的 .py 文件"
        aria-label="主网络文件"
      /><el-button :disabled="disabled || (!serverId && !runId)" @click="open"
        >选择主网络文件</el-button
      >
    </div>
    <p class="fine-print">
      选择模型结构所在的源文件，例如
      models/network.py；训练入口用于启动训练，主网络用于代码分析。只读取文件，不执行代码。
    </p>
    <el-dialog
      v-model="visible"
      title="选择主网络 Python 文件"
      width="min(94vw, 720px)"
      append-to-body
    >
      <div class="actions">
        <span>当前目录：{{ directory }}</span
        ><el-button :disabled="directory === '.' || busy" @click="up"
          >上一级</el-button
        >
      </div>
      <SectionState
        :loading="busy"
        :error="error"
        :empty="!busy && !error && !files.length"
        text="此目录没有可读取的源文件"
        @retry="load(directory)"
      />
      <div class="network-files">
        <el-button
          v-for="item in files"
          :key="item.path"
          text
          :disabled="
            busy || (item.kind === 'file' && !item.path.endsWith('.py'))
          "
          @click="choose(item)"
          >{{ item.kind === "directory" ? "📁" : "📄" }}
          {{ item.path.split("/").at(-1) }}</el-button
        >
      </div>
      <el-button
        v-if="next !== null"
        :loading="busy"
        @click="load(directory, true)"
        >加载更多</el-button
      >
    </el-dialog>
  </div>
</template>
<style scoped>
.network-files {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  max-height: 400px;
  overflow: auto;
}
.network-files .el-button {
  justify-content: flex-start;
  margin-left: 0;
}
</style>
