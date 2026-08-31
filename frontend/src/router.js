import { createRouter, createWebHistory } from "vue-router";
import { api } from "./api";
import HomeView from "./views/HomeView.vue";
import ReleaseView from "./views/ReleaseView.vue";
import LoginView from "./views/LoginView.vue";
import AdminView from "./views/AdminView.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: HomeView },
    { path: "/release/:releaseId", component: ReleaseView },
    { path: "/admin/login", component: LoginView },
    { path: "/admin", component: AdminView, meta: { requiresAuth: true } },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
  scrollBehavior: () => ({ top: 0 }),
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
