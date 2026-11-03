<script setup lang="ts">
defineProps<{ large?: boolean; compact?: boolean }>();
</script>
<template>
  <div class="brand" :class="{ large, compact }">
    <span class="brand-image"
      ><img src="/brand/yaolao-coldflame-v1.png" alt="骨灵冷火药老"
    /></span>
    <div v-if="!compact">
      <strong>炼丹神器</strong><small>让每一次实验，都有依据</small>
    </div>
  </div>
</template>
