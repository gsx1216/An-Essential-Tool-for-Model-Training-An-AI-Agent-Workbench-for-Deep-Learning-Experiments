<script setup lang="ts">
import { computed } from "vue";
import { renderMarkdown } from "../lib/markdown";
const props = defineProps<{ content: string }>();
const rendered = computed(() => renderMarkdown(props.content));
</script>
<template>
  <!-- HTML comes only from lib/markdown, with raw HTML disabled. -->
  <!-- eslint-disable-next-line vue/no-v-html -->
  <div class="markdown" v-html="rendered" />
</template>
