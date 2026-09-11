import { createRouter, createWebHistory } from "vue-router";
import { watch } from "vue";
import { api } from "./api";
import { locale, t } from "./i18n";

const appTitle = "Nijigasaki DB";
const collaboScrollPositions = new Map();

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: () => import("./views/ArchiveLandingView.vue"), meta: { title: "选择档案" } },
    { path: "/music", component: () => import("./views/HomeView.vue"), meta: { title: "虹咲音乐档案" } },
    { path: "/news", component: () => import("./views/NewsView.vue"), meta: { title: "官网新闻" } },
    { path: "/news/:newsId", component: () => import("./views/NewsDetailView.vue"), meta: { title: "新闻详情" } },
    { path: "/release/:releaseId", component: () => import("./views/ReleaseView.vue"), meta: { title: "发行详情" } },
    { path: "/programs", component: () => import("./views/ProgramsView.vue"), meta: { title: "节目档案" } },
    {
      path: "/programs/:month(\\d{6})",
      component: () => import("./views/ProgramsView.vue"),
      meta: { title: "节目档案" },
    },
    {
      path: "/programs/archive",
      component: () => import("./views/ProgramArchiveView.vue"),
      meta: { title: "已录入节目" },
    },
    {
      path: "/programs/archive/:programId",
      component: () => import("./views/ProgramArchiveView.vue"),
      meta: { title: "节目详情" },
    },
    {
      path: "/illustrations",
      component: () => import("./views/CollaborationIllustrationsView.vue"),
      meta: { title: "联动立绘" },
    },
    { path: "/collabo", component: () => import("./views/CollaboView.vue"), meta: { title: "联动立绘" } },
    {
      path: "/collabo/:slug(\\d{8}-[a-f0-9]{6})",
      component: () => import("./views/CollaboDetailView.vue"),
      meta: { title: "联动立绘" },
    },
    {
      path: "/admin/collabo/:id?",
      component: () => import("./views/CollaboAdminView.vue"),
      meta: { requiresAuth: true, title: "联动立绘" },
    },
    { path: "/admin/login", component: () => import("./views/LoginView.vue"), meta: { title: "管理员登录" } },
    { path: "/admin", component: () => import("./views/AdminView.vue"), meta: { requiresAuth: true, title: "设置" } },
    {
      path: "/admin/programs",
      component: () => import("./views/ProgramAdminView.vue"),
      meta: { requiresAuth: true, title: "节目档案管理" },
    },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
  scrollBehavior(to, from, savedPosition) {
    if (to.path === "/collabo" && (from.path === "/collabo" || from.path.startsWith("/collabo/"))) {
      const key = from.path === "/collabo" ? from.fullPath : to.fullPath;
      const position = collaboScrollPositions.get(key);
      if (position !== undefined) return { top: position };
    }
    if (to.path === "/collabo" && savedPosition) return savedPosition;
    return { top: 0 };
  },
});

function updateDocumentTitle(to = router.currentRoute.value) {
  document.title = to.meta.title ? `${t(to.meta.title)} · ${appTitle}` : appTitle;
}

router.afterEach(updateDocumentTitle);
watch(locale, () => updateDocumentTitle());

router.beforeEach(async (to, from) => {
  if (from.path === "/collabo") {
    collaboScrollPositions.set(from.fullPath, window.scrollY);
    if (collaboScrollPositions.size > 30) collaboScrollPositions.delete(collaboScrollPositions.keys().next().value);
  }
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
