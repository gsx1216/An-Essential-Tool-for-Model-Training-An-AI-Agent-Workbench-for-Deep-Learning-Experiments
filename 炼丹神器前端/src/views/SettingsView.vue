<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import { errorText } from "../api/client";
import { confirmAction, notifyError } from "../lib/ui";
import { useAuth } from "../stores/auth";
import SectionState from "../components/SectionState.vue";
const auth = useAuth();
const configured = ref<boolean | null>(null),
  key = ref(""),
  busy = ref(false),
  loading = ref(false),
  error = ref("");
async function load() {
  loading.value = true;
  error.value = "";
  try {
    configured.value = (await api.credential()).configured;
  } catch (e) {
    error.value = errorText(e);
  } finally {
    loading.value = false;
  }
}
async function saveKey() {
  if (!key.value.trim()) return;
  busy.value = true;
  try {
    await api.saveCredential(key.value.trim());
    configured.value = true;
    key.value = "";
    ElMessage.success("个人模型 Key 已保存");
  } catch (e) {
    notifyError(e);
  } finally {
    busy.value = false;
  }
}
async function removeKey() {
  if (
    !(await confirmAction(
      "删除个人 DeepSeek API Key？之后的新模型请求需要重新配置。",
    ))
  )
    return;
  busy.value = true;
  try {
    await api.deleteCredential();
    configured.value = false;
    key.value = "";
    ElMessage.success("已删除个人模型 Key");
  } catch (e) {
    notifyError(e);
  } finally {
    busy.value = false;
  }
}
onMounted(load);
</script>
<template>
  <div class="page settings-layout">
    <header class="page-header">
      <div>
        <div class="eyebrow">YOUR WORKSPACE</div>
        <h1>设置</h1>
        <p>管理个人模型配置与实验助手偏好。</p>
      </div>
    </header>
    <div class="card">
      <div class="card-title">
        <h2>个人账号</h2>
        <span class="user-avatar">{{ auth.user?.username.slice(0, 1) }}</span>
      </div>
      <strong>{{ auth.user?.username }}</strong>
      <p class="fine-print">用户编号 {{ auth.user?.user_id }}</p>
    </div>
    <SectionState :loading="loading" :error="error" @retry="load" />
    <div class="card">
      <div class="card-title">
        <h2>模型服务</h2>
        <el-tag
          v-if="configured !== null"
          :type="configured ? 'success' : 'info'"
          effect="plain"
          >{{ configured ? "已配置" : "未配置" }}</el-tag
        >
      </div>
      <p class="hint">
        使用你自己的 DeepSeek API Key。已保存的 Key 不会回传到页面。
      </p>
      <el-form label-position="top" @submit.prevent="saveKey"
        ><el-form-item label="DeepSeek API Key"
          ><el-input
            v-model="key"
            type="password"
            show-password
            maxlength="4096"
            autocomplete="new-password"
            placeholder="输入新 Key，留空表示不修改"
            :disabled="busy"
        /></el-form-item>
        <div class="actions">
          <el-button
            type="primary"
            :disabled="!key.trim() || configured === null"
            :loading="busy"
            @click="saveKey"
            >{{ configured ? "更新 Key" : "保存 Key" }}</el-button
          ><el-button
            v-if="configured"
            type="danger"
            text
            :disabled="busy"
            @click="removeKey"
            >删除 Key</el-button
          >
        </div></el-form
      >
    </div>
    <div class="card">
      <h2>实验助手</h2>
      <p>
        分析与建议采用手动触发。请进入运行详情的“分析与建议”，选择主网络代码后点击“开始分析与建议”。
      </p>
      <p class="fine-print">
        聊天、报告分析和代码诊断复用上方保存的个人模型
        Key。训练完成与指标采集不会自动调用模型。
      </p>
    </div>
  </div>
</template>
