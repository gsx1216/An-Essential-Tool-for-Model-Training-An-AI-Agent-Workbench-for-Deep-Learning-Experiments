<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  ChatDotRound,
  DataAnalysis,
  Monitor,
  Collection,
  Setting,
  Plus,
  Fold,
  Delete,
  SwitchButton,
} from "@element-plus/icons-vue";
import { useAuth } from "../stores/auth";
import { useWorkspace } from "../stores/workspace";
import { useChat } from "../stores/chat";
import { api } from "../api";
import { errorText, isCancelled } from "../api/client";
import { confirmAction, notifyError } from "../lib/ui";
import { sameId } from "../lib/format";
import { ElMessageBox } from "element-plus";
import BrandMark from "../components/BrandMark.vue";
const auth = useAuth(),
  workspace = useWorkspace(),
  chat = useChat(),
  route = useRoute(),
  router = useRouter();
const drawer = ref(false),
  creating = ref(false);
const nav = [
  { path: "/chat", title: "聊天工作台", mobile: "聊天", icon: ChatDotRound },
  {
    path: "/training",
    title: "实验与训练",
    mobile: "训练",
    icon: DataAnalysis,
  },
  { path: "/servers", title: "训练服务器", mobile: "服务器", icon: Monitor },
  { path: "/library", title: "资料与建议", mobile: "资料", icon: Collection },
  { path: "/settings", title: "设置", mobile: "我的", icon: Setting },
];
const active = computed(() =>
  route.path.startsWith("/runs")
    ? "/training"
    : nav.find((item) => route.path.startsWith(item.path))?.path,
);
async function sessions() {
  try {
    workspace.sessionError = "";
    await workspace.loadSessions();
  } catch (e) {
    if (!isCancelled(e)) workspace.sessionError = errorText(e);
  }
}
async function newChat() {
  if (creating.value) return;
  creating.value = true;
  try {
    const { value } = await ElMessageBox.prompt(
      "输入聊天标题，方便之后查找。",
      "新建聊天",
      {
        inputValue: "新聊天",
        confirmButtonText: "创建聊天",
        cancelButtonText: "取消",
        inputValidator: (value) =>
          (!!value?.trim() && value.trim().length <= 200) ||
          "请输入 1～200 个字符的标题。",
        closeOnClickModal: false,
      },
    );
    const data = await api.createSession(value.trim());
    await sessions();
    drawer.value = false;
    await router.push(`/chat/${data.session_id}`);
  } catch (e) {
    if (e === "cancel" || e === "close") return;
    notifyError(e);
  } finally {
    creating.value = false;
  }
}
async function deleteChat(id: string, title: string) {
  if (
    !(await confirmAction(
      `删除“${title}”及其中的聊天记录？已启动的训练会继续运行。`,
      "删除聊天",
    ))
  )
    return;
  try {
    await api.deleteSession(id);
    await sessions();
    if (sameId(String(route.params.sessionId || ""), id))
      await router.push("/chat");
  } catch (e) {
    notifyError(e);
  }
}
async function logout() {
  if (!(await confirmAction("退出当前账号？", "退出登录"))) return;
  try {
    await auth.logout();
  } catch (e) {
    notifyError(e);
  } finally {
    workspace.clear();
    await router.replace("/login");
  }
}
watch(
  () => auth.user?.user_id,
  (value) => {
    workspace.clear();
    chat.clear();
    if (value) {
      void sessions();
      void workspace.loadSkills().catch(notifyError);
    }
  },
  { immediate: true },
);
watch(
  () => auth.token,
  (value) => {
    if (!value) {
      workspace.clear();
      void router.replace({
        path: "/login",
        query: { return: route.fullPath },
      });
    }
  },
);
watch(
  () => route.fullPath,
  () => {
    drawer.value = false;
  },
);
</script>
<template>
  <div class="app-layout">
    <aside class="sidebar" :class="{ open: drawer }" aria-label="主导航">
      <BrandMark /><el-button
        class="new-chat"
        type="primary"
        :icon="Plus"
        :loading="creating"
        @click="newChat"
        >新聊天</el-button
      >
      <nav class="primary-nav">
        <router-link
          v-for="item in nav"
          :key="item.path"
          :to="item.path"
          :class="{ active: active === item.path }"
          ><el-icon><component :is="item.icon" /></el-icon
          >{{ item.title }}</router-link
        >
      </nav>
      <div class="session-label">
        <span>最近的聊天</span
        ><button class="text-button" @click="sessions">刷新</button>
      </div>
      <div class="session-list">
        <p v-if="workspace.sessionError" class="inline-error">
          {{ workspace.sessionError }}
        </p>
        <p v-else-if="!workspace.sessions.length" class="hint">
          从一个新问题开始
        </p>
        <div
          v-for="session in workspace.sessions"
          :key="session.session_id"
          class="session-row"
          :class="{
            active: sameId(
              String(route.params.sessionId || ''),
              session.session_id,
            ),
          }"
        >
          <router-link
            :to="`/chat/${session.session_id}`"
            :title="session.title"
            ><el-icon><ChatDotRound /></el-icon
            ><span>{{ session.title }}</span></router-link
          ><el-button
            text
            :icon="Delete"
            :aria-label="`删除聊天 ${session.title}`"
            @click="deleteChat(session.session_id, session.title)"
          />
        </div>
      </div>
      <div class="sidebar-user">
        <span class="user-avatar">{{
          auth.user?.username.slice(0, 1).toUpperCase()
        }}</span>
        <div>
          <strong>{{ auth.user?.username }}</strong
          ><small>个人实验工作台</small>
        </div>
        <el-button
          text
          :icon="SwitchButton"
          aria-label="退出登录"
          @click="logout"
        />
      </div>
    </aside>
    <div v-if="drawer" class="sidebar-backdrop" @click="drawer = false" />
    <main class="main-shell">
      <header class="mobile-header">
        <el-button
          text
          :icon="Fold"
          aria-label="展开导航和历史聊天"
          @click="drawer = true"
        /><BrandMark compact /><strong>{{ route.meta.title }}</strong
        ><router-link to="/settings" class="mobile-user">{{
          auth.user?.username.slice(0, 1)
        }}</router-link>
      </header>
      <div v-if="auth.restoreError" class="restore-state">
        <h2>暂时无法读取账号</h2>
        <p>{{ auth.restoreError }}</p>
        <el-button type="primary" @click="auth.restore">重新连接</el-button
        ><el-button @click="logout">退出当前登录</el-button>
      </div>
      <router-view v-else-if="auth.user" />
    </main>
    <nav class="bottom-nav" aria-label="手机导航">
      <router-link
        v-for="item in nav"
        :key="item.path"
        :to="item.path"
        :class="{ active: active === item.path }"
        ><el-icon><component :is="item.icon" /></el-icon
        ><span>{{ item.mobile }}</span></router-link
      >
    </nav>
  </div>
</template>
