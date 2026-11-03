<script setup lang="ts">
import type { ParameterRule, Parameters } from "../types/domain";
defineProps<{ rules: Record<string, ParameterRule>; disabled?: boolean }>();
const model = defineModel<Parameters>({ required: true });
const numericValue = (key: string) =>
  typeof model.value[key] === "number"
    ? (model.value[key] as number)
    : undefined;
const textValue = (key: string) =>
  typeof model.value[key] === "string" ? (model.value[key] as string) : "";
function change(key: string, value: unknown) {
  if (value === undefined || value === "" || value === null)
    delete model.value[key];
  else model.value[key] = value as string | number | boolean;
}
</script>
<template>
  <div v-if="!Object.keys(rules).length" class="hint">
    此入口没有可配置参数。
  </div>
  <div class="form-grid">
    <el-form-item
      v-for="(rule, key) in rules"
      :key="key"
      :label="String(key)"
      :required="rule.required"
      ><el-select
        v-if="rule.type === 'bool'"
        :model-value="model[String(key)]"
        clearable
        :disabled="disabled"
        placeholder="未指定，使用脚本默认值"
        @update:model-value="change(String(key), $event)"
        ><el-option label="是 (true)" :value="true" /><el-option
          label="否 (false)"
          :value="false" /></el-select
      ><el-input-number
        v-else-if="rule.type === 'int' || rule.type === 'float'"
        :model-value="numericValue(String(key))"
        :precision="rule.type === 'int' ? 0 : undefined"
        :min="rule.min"
        :max="rule.max"
        :step="rule.type === 'int' ? 1 : 0.00001"
        :disabled="disabled"
        controls-position="right"
        placeholder="未指定"
        @update:model-value="change(String(key), $event)"
      /><el-input
        v-else
        :model-value="textValue(String(key))"
        :maxlength="rule.max_length || 1024"
        :disabled="disabled"
        placeholder="未指定，使用脚本默认值"
        @update:model-value="change(String(key), $event)"
      /><small v-if="rule.min != null || rule.max != null" class="muted"
        >范围 {{ rule.min ?? "不限" }} ～ {{ rule.max ?? "不限" }}</small
      ></el-form-item
    >
  </div>
</template>
