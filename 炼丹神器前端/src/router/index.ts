import { createRouter, createWebHistory } from "vue-router";
import { useAuth } from "../stores/auth";
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/login",
      component: () => import("../views/AuthView.vue"),
      meta: { public: true, title: "登录" },
    },
    {
      path: "/",
      component: () => import("../layouts/WorkspaceLayout.vue"),
      children: [
        { path: "", redirect: "/chat" },
        {
          path: "chat/:sessionId?",
          component: () => import("../views/ChatView.vue"),
          meta: { title: "聊天工作台" },
        },
        {
          path: "training",
          component: () => import("../views/TrainingView.vue"),
          meta: { title: "实验与训练" },
        },
        {
          path: "runs/:runId",
          component: () => import("../views/RunView.vue"),
          meta: { title: "训练监控" },
        },
        {
          path: "servers",
          component: () => import("../views/ServersView.vue"),
          meta: { title: "训练服务器" },
        },
        {
          path: "library",
          component: () => import("../views/LibraryView.vue"),
          meta: { title: "资料与建议" },
        },
        {
          path: "settings",
          component: () => import("../views/SettingsView.vue"),
          meta: { title: "设置" },
        },
      ],
    },
    {
      path: "/:pathMatch(.*)*",
      component: () => import("../views/NotFoundView.vue"),
      meta: { public: true, title: "页面不存在" },
    },
  ],
});
router.beforeEach(async (to) => {
  const auth = useAuth();
  if (!auth.ready) await auth.restore();
  if (!to.meta.public && !auth.token)
    return { path: "/login", query: { return: to.fullPath } };
  if (to.path === "/login" && auth.user) return "/chat";
  document.title = `${String(to.meta.title || "工作台")} · 炼丹神器`;
});
