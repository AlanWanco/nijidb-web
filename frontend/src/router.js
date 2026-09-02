import { createRouter, createWebHistory } from "vue-router";
import { api } from "./api";

const appTitle = "Nijigasaki DB";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: () => import("./views/ArchiveLandingView.vue"), meta: { title: "选择档案" } },
    { path: "/music", component: () => import("./views/HomeView.vue"), meta: { title: "虹咲音乐档案" } },
    { path: "/release/:releaseId", component: () => import("./views/ReleaseView.vue"), meta: { title: "发行详情" } },
    { path: "/programs", component: () => import("./views/ProgramsView.vue"), meta: { title: "节目档案" } },
    { path: "/programs/archive", component: () => import("./views/ProgramArchiveView.vue"), meta: { title: "已录入节目" } },
    { path: "/programs/archive/:programId", component: () => import("./views/ProgramArchiveView.vue"), meta: { title: "节目详情" } },
    { path: "/admin/login", component: () => import("./views/LoginView.vue"), meta: { title: "管理员登录" } },
    { path: "/admin", component: () => import("./views/AdminView.vue"), meta: { requiresAuth: true, title: "设置" } },
    { path: "/admin/programs", component: () => import("./views/ProgramAdminView.vue"), meta: { requiresAuth: true, title: "节目档案管理" } },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
  scrollBehavior: () => ({ top: 0 }),
});

router.afterEach(to => {
  document.title = to.meta.title ? `${to.meta.title} · ${appTitle}` : appTitle;
});

router.beforeEach(async (to) => {
  if (!to.meta.requiresAuth) return true;
  try {
    const session = await api("/api/auth/session");
    if (session.authenticated) return true;
  } catch {
    // The view will show the request error if the API itself is unavailable.
  }
  return { path: "/admin/login", query: { redirect: to.fullPath } };
});

export default router;
