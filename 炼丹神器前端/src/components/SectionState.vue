<script setup lang="ts">
defineProps<{
  loading?: boolean;
  error?: string;
  empty?: boolean;
  text?: string;
}>();
defineEmits<{ retry: [] }>();
</script>
<template>
  <div v-if="loading" class="state-box" role="status">
    <span class="loading-ring" />正在读取…
  </div>
  <div v-else-if="error" class="state-box error-box" role="alert">
    <p>{{ error }}</p>
    <el-button @click="$emit('retry')">重新读取</el-button>
  </div>
  <div v-else-if="empty" class="state-box">
    <div class="empty-symbol">◇</div>
    <p>{{ text || "暂无记录" }}</p>
    <slot />
  </div>
</template>
