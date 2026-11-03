<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { api } from "../api";
import { errorText } from "../api/client";
import type { Schema } from "../types/domain";
const props = defineProps<{
  serverId: string;
  selectable?: boolean;
  disabled?: boolean;
}>();
const selected = defineModel<string[]>("selected", { default: () => [] });
const data = ref<Schema<"ProjectResourceResponse"> | null>(null),
  busy = ref(false),
  error = ref("");
let epoch = 0;
onBeforeUnmount(() => {
  epoch++;
});
async function load() {
  if (!props.serverId) return;
  const current = ++epoch;
  busy.value = true;
  error.value = "";
  try {
    const result = await api.serverResources(props.serverId);
    if (current === epoch) data.value = result;
  } catch (e) {
    if (current === epoch) error.value = errorText(e);
  } finally {
    if (current === epoch) busy.value = false;
  }
}
watch(
  () => props.serverId,
  () => {
    epoch++;
    data.value = null;
    selected.value = [];
    void load();
  },
  { immediate: true },
);
</script>
<template>
  <div class="gpu-resources">
    <div class="actions">
      <strong>{{
        data?.gpu_status === "available"
          ? `检测到 ${data.gpus.length} 张 NVIDIA GPU`
          : "GPU 资源"
      }}</strong
      ><el-button text :loading="busy" :disabled="disabled" @click="load">{{
        data ? "刷新 GPU" : "读取 GPU"
      }}</el-button>
    </div>
    <p v-if="error" class="inline-error">{{ error }}</p>
    <p v-else-if="data?.gpu_status === 'not_installed'" class="fine-print">
      服务器未发现 nvidia-smi；可能没有 NVIDIA GPU，或工具不在 SSH 环境 PATH
      中。
    </p>
    <p v-else-if="data?.gpu_status === 'unavailable'" class="fine-print">
      当前无法读取 GPU 状态，请检查服务器驱动或查询权限。
    </p>
    <template v-else-if="data?.gpu_status === 'available'">
      <el-checkbox-group
        v-if="selectable"
        v-model="selected"
        :disabled="disabled"
      >
        <el-checkbox
          v-for="gpu in data.gpus"
          :key="gpu.index"
          :value="String(gpu.index)"
          >GPU {{ gpu.index }} · {{ gpu.name }}</el-checkbox
        >
      </el-checkbox-group>
      <div v-for="gpu in data.gpus" :key="gpu.index" class="gpu-row">
        <strong>GPU {{ gpu.index }} · {{ gpu.name }}</strong>
        <p class="fine-print">
          显存 {{ gpu.memory_used_mb }} / {{ gpu.memory_total_mb }} MiB · 剩余
          {{
            Math.max(0, gpu.memory_total_mb - gpu.memory_used_mb)
          }}
          MiB<br />利用率
          {{
            gpu.utilization_percent === null
              ? "未知"
              : gpu.utilization_percent + "%"
          }}
        </p>
      </div>
      <p class="fine-print">
        这是本次读取的占用快照；有空闲显存不代表一定足够运行当前模型。
      </p>
    </template>
    <p v-if="selectable" class="fine-print">
      勾选训练使用的 GPU；不勾选时继承服务器环境。当前选择：{{
        selected.join(",") || "继承服务器环境"
      }}。
    </p>
  </div>
</template>
<style scoped>
.gpu-resources {
  margin: 12px 0;
}
.gpu-row {
  border-top: 1px solid var(--el-border-color-lighter);
  padding-top: 8px;
  margin-top: 8px;
  overflow-wrap: anywhere;
}
.el-checkbox {
  height: auto;
  white-space: normal;
}
.el-checkbox-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
</style>
