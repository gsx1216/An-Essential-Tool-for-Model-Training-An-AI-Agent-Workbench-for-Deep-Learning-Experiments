<script setup lang="ts">
import { stateLabel } from "../lib/format";
defineProps<{ state?: string; label?: string }>();
</script>
<template>
  <span class="status" :class="state"
    ><span class="status-dot" />{{ label || stateLabel(state) }}</span
  >
</template>
