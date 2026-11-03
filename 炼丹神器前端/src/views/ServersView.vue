<script setup lang="ts">
import { onMounted, reactive, ref, watch } from "vue";
import { Plus, Connection, Delete, Monitor } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import { errorText } from "../api/client";
import { notifyError, confirmAction } from "../lib/ui";
import type { Server, Schema } from "../types/domain";
import { dateTime } from "../lib/format";
import StatusBadge from "../components/StatusBadge.vue";
import SectionState from "../components/SectionState.vue";
import ServerGpuInfo from "../components/ServerGpuInfo.vue";
const servers = ref<Server[]>([]),
  cursor = ref<string | null>(null),
  loading = ref(false),
  error = ref(""),
  dialog = ref(false),
  saving = ref(false),
  formError = ref("");
const testing = ref(""),
  results = reactive<Record<string, Schema<"ServerTestResponse">>>({});
const form = reactive({
  name: "",
  host: "",
  port: 22,
  username: "",
  auth_method: "password" as "password" | "private_key",
  work_dir: "",
  host_fingerprint: "",
  password: "",
  private_key: "",
  passphrase: "",
});
function clearSecrets() {
  form.password = "";
  form.private_key = "";
  form.passphrase = "";
}
watch(() => form.auth_method, clearSecrets);
watch(dialog, (value) => {
  if (!value) clearSecrets();
});
async function load(more = false) {
  loading.value = true;
  error.value = "";
  try {
    const result = await api.servers(
      more ? cursor.value || undefined : undefined,
    );
    servers.value = more ? [...servers.value, ...result.items] : result.items;
    cursor.value = result.next_cursor;
  } catch (e) {
    error.value = errorText(e);
  } finally {
    loading.value = false;
  }
}
async function save() {
  formError.value = "";
  if (
    !form.name.trim() ||
    !form.host.trim() ||
    !form.username.trim() ||
    !form.work_dir.trim()
  ) {
    formError.value = "请填写服务器名称、IP 地址、用户名和工作目录。";
    return;
  }
  if (!/^SHA256:[A-Za-z0-9+/]{43}$/.test(form.host_fingerprint.trim())) {
    formError.value =
      "请填写完整的 SHA256 主机指纹，例如 SHA256: 后面 43 个字符。";
    return;
  }
  if (
    (form.auth_method === "password" && !form.password) ||
    (form.auth_method === "private_key" && !form.private_key.trim())
  ) {
    formError.value =
      form.auth_method === "password"
        ? "请输入 SSH 登录密码。"
        : "请粘贴完整私钥。";
    return;
  }
  saving.value = true;
  try {
    await api.createServer({
      name: form.name.trim(),
      host: form.host.trim(),
      port: form.port,
      username: form.username.trim(),
      auth_method: form.auth_method,
      work_dir: form.work_dir.trim(),
      host_fingerprint: form.host_fingerprint.trim(),
      ...(form.auth_method === "password"
        ? { password: form.password }
        : {
            private_key: form.private_key,
            ...(form.passphrase ? { passphrase: form.passphrase } : {}),
          }),
    });
    clearSecrets();
    dialog.value = false;
    ElMessage.success("服务器已保存");
    await load();
  } catch (e) {
    formError.value = errorText(e);
  } finally {
    saving.value = false;
  }
}
async function test(server: Server) {
  testing.value = server.server_id;
  try {
    results[server.server_id] = await api.testServer(server.server_id);
    ElMessage.success("连接成功，主机指纹已验证");
  } catch (e) {
    delete results[server.server_id];
    notifyError(e);
  } finally {
    testing.value = "";
  }
}
async function remove(server: Server) {
  if (
    !(await confirmAction(
      `删除“${server.name}”将永久清除这条配置的连接凭据。历史运行保留，但无法再通过它读取远端日志或源码；服务器上的项目文件不会删除。有排队中或运行中的任务时需先停止并同步状态。`,
      "删除服务器配置",
    ))
  )
    return;
  try {
    await api.deleteServer(server.server_id);
    ElMessage.success("连接凭据已清除，历史运行记录已保留");
    await load();
  } catch (e) {
    notifyError(e);
  }
}
onMounted(() => load());
</script>
<template>
  <div class="page">
    <header class="page-header">
      <div>
        <div class="eyebrow">COMPUTE & CONNECTION</div>
        <h1>训练服务器</h1>
        <p>连接你的计算资源，让实验在熟悉的环境运行。</p>
      </div>
      <el-button
        type="primary"
        :icon="Plus"
        @click="
          dialog = true;
          formError = '';
        "
        >添加服务器</el-button
      >
    </header>
    <SectionState
      :loading="loading && !servers.length"
      :error="error"
      :empty="!loading && !error && !servers.length"
      text="还没有配置训练服务器"
      @retry="load()"
    />
    <div class="card-grid">
      <article v-for="server in servers" :key="server.server_id" class="card">
        <div class="card-title">
          <el-icon :size="23" color="#168e8c"><Monitor /></el-icon
          ><StatusBadge
            v-if="results[server.server_id]"
            state="succeeded"
            label="连接已验证"
          /><span v-else class="hint">尚未测试</span>
        </div>
        <h2>{{ server.name }}</h2>
        <div class="server-address">
          {{ server.username }}@{{ server.host }}:{{ server.port }}
        </div>
        <dl class="data-tree">
          <div>
            <dt>工作目录</dt>
            <dd>{{ server.work_dir }}</dd>
          </div>
          <div>
            <dt>认证方式</dt>
            <dd>
              {{ server.auth_method === "password" ? "密码登录" : "SSH 私钥" }}
            </dd>
          </div>
          <div>
            <dt>主机指纹</dt>
            <dd class="fine-print">{{ server.host_fingerprint }}</dd>
          </div>
        </dl>
        <p v-if="results[server.server_id]" class="fine-print">
          测试于 {{ dateTime(results[server.server_id].checked_at) }} ·
          {{ results[server.server_id].duration_ms }}ms
        </p>
        <ServerGpuInfo :server-id="server.server_id" />
        <div class="server-footer">
          <el-button
            :icon="Connection"
            :loading="testing === server.server_id"
            :disabled="!!testing"
            @click="test(server)"
            >测试连接</el-button
          ><el-button text type="danger" :icon="Delete" @click="remove(server)"
            >删除配置</el-button
          >
        </div>
      </article>
    </div>
    <div v-if="cursor" class="pagination-actions">
      <el-button :loading="loading" @click="load(true)"
        >加载更多服务器</el-button
      >
    </div>
    <el-dialog
      v-model="dialog"
      title="添加训练服务器"
      width="650px"
      :close-on-click-modal="!saving"
      :show-close="!saving"
      destroy-on-close
      ><el-form label-position="top" :disabled="saving" @submit.prevent="save"
        ><div class="form-grid">
          <el-form-item label="服务器名称" required
            ><el-input
              v-model="form.name"
              maxlength="100"
              placeholder="例如：实验室 GPU 服务器" /></el-form-item
          ><el-form-item label="IP 地址" required
            ><el-input
              v-model="form.host"
              placeholder="IPv4 或 IPv6 地址" /></el-form-item
          ><el-form-item label="SSH 端口" required
            ><el-input-number
              v-model="form.port"
              :min="1"
              :max="65535"
              :precision="0" /></el-form-item
          ><el-form-item label="SSH 用户名" required
            ><el-input
              v-model="form.username"
              maxlength="64"
              autocomplete="off" /></el-form-item
          ><el-form-item label="项目工作目录" required class="wide"
            ><el-input
              v-model="form.work_dir"
              maxlength="1024"
              placeholder="Linux 绝对路径，例如 /home/user/project" /></el-form-item
          ><el-form-item label="主机指纹" required class="wide"
            ><el-input
              v-model="form.host_fingerprint"
              maxlength="64"
              placeholder="SHA256:..."
            />
            <div class="credentials-help">
              在已信任的服务器终端运行
              <code
                >ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E
                sha256</code
              >，复制其中完整的 SHA256:… 字段。主机指纹与登录私钥是两件事。
            </div></el-form-item
          ><el-form-item label="登录方式" class="wide"
            ><el-radio-group v-model="form.auth_method"
              ><el-radio-button value="password">用户名与密码</el-radio-button
              ><el-radio-button value="private_key"
                >SSH 私钥</el-radio-button
              ></el-radio-group
            ></el-form-item
          ><el-form-item
            v-if="form.auth_method === 'password'"
            label="SSH 密码"
            required
            class="wide"
            ><el-input
              v-model="form.password"
              type="password"
              show-password
              autocomplete="new-password"
              maxlength="4096" /></el-form-item
          ><template v-else
            ><el-form-item label="SSH 私钥内容" required class="wide"
              ><el-input
                v-model="form.private_key"
                type="textarea"
                :rows="5"
                maxlength="32768"
                autocomplete="off"
                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----..." /></el-form-item
            ><el-form-item label="私钥口令（私钥未加密时留空）" class="wide"
              ><el-input
                v-model="form.passphrase"
                type="password"
                show-password
                autocomplete="new-password"
                maxlength="4096" /></el-form-item
          ></template>
        </div>
        <p v-if="formError" class="inline-error" role="alert">
          {{ formError }}
        </p>
        <p class="fine-print">
          凭据由后端加密保存；此表单关闭或保存成功后会清空密码与私钥。
        </p></el-form
      ><template #footer
        ><el-button :disabled="saving" @click="dialog = false">取消</el-button
        ><el-button type="primary" :loading="saving" @click="save"
          >保存服务器</el-button
        ></template
      ></el-dialog
    >
  </div>
</template>
