<script setup lang="ts">
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { useAuth } from "../stores/auth";
import { api } from "../api";
import { errorText } from "../api/client";
import { safeReturnPath } from "../lib/format";
import BrandMark from "../components/BrandMark.vue";
const auth = useAuth(),
  route = useRoute(),
  router = useRouter();
const register = ref(false),
  username = ref(""),
  password = ref(""),
  repeat = ref(""),
  busy = ref(false),
  error = ref("");
async function submit() {
  error.value = "";
  if (!username.value.trim()) {
    error.value = "请输入用户名。";
    return;
  }
  if (password.value.length < 8 || password.value.length > 32) {
    error.value = "密码长度应为 8～32 个字符。";
    return;
  }
  if (register.value && password.value !== repeat.value) {
    error.value = "两次输入的密码不一致。";
    return;
  }
  busy.value = true;
  try {
    if (register.value) {
      await api.register({
        username: username.value.trim(),
        password: password.value,
      });
      ElMessage.success("注册成功，请登录。");
      register.value = false;
      password.value = "";
      repeat.value = "";
    } else {
      await auth.login(username.value.trim(), password.value);
      password.value = "";
      await router.replace(safeReturnPath(route.query.return));
    }
  } catch (e) {
    error.value = errorText(e);
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <main class="auth-page">
    <section class="auth-story">
      <div class="eyebrow">EXPERIMENT WORKSPACE</div>
      <BrandMark large />
      <h1>把灵感交给实验，<br />把依据留在每一步。</h1>
      <p>
        连接训练服务器，跟进真实结果。<br />在同一处讨论论文、检查代码，记录每次改进。
      </p>
      <div class="auth-pills">
        <span>实验监控</span><span>有据建议</span><span>完整记录</span>
      </div>
    </section>
    <section class="auth-card">
      <BrandMark class="auth-mobile-brand" />
      <div class="eyebrow">欢迎来到你的工作台</div>
      <h2>{{ register ? "创建账号" : "登录炼丹神器" }}</h2>
      <p class="muted">
        {{ register ? "为你的实验建立独立空间" : "继续你的实验与研究" }}
      </p>
      <el-form label-position="top" @submit.prevent="submit"
        ><el-form-item label="用户名" required
          ><el-input
            v-model="username"
            name="username"
            autocomplete="username"
            maxlength="64"
            size="large"
            :disabled="busy"
            placeholder="请输入用户名" /></el-form-item
        ><el-form-item label="密码" required
          ><el-input
            v-model="password"
            name="password"
            type="password"
            show-password
            :autocomplete="register ? 'new-password' : 'current-password'"
            maxlength="32"
            size="large"
            :disabled="busy"
            placeholder="8～32 个字符" /></el-form-item
        ><el-form-item v-if="register" label="确认密码" required
          ><el-input
            v-model="repeat"
            type="password"
            autocomplete="new-password"
            maxlength="32"
            size="large"
            :disabled="busy"
            placeholder="再次输入密码"
        /></el-form-item>
        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
        <el-button
          native-type="submit"
          type="primary"
          size="large"
          :loading="busy"
          class="full-width"
          >{{ register ? "注册账号" : "登录" }}</el-button
        ></el-form
      >
      <p class="auth-switch">
        {{ register ? "已有账号？" : "还没有账号？" }}
        <button
          class="text-button"
          :disabled="busy"
          @click="
            register = !register;
            error = '';
            password = '';
            repeat = '';
          "
        >
          {{ register ? "去登录" : "创建账号" }}
        </button>
      </p>
      <p class="fine-print">模型 API Key 和训练服务器在登录后单独配置。</p>
    </section>
  </main>
</template>
