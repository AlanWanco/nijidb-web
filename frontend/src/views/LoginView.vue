<script setup>
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";

const route = useRoute();
const router = useRouter();
const username = ref("");
const password = ref("");
const loading = ref(false);
const error = ref("");

async function login() {
  loading.value = true;
  error.value = "";
  try {
    await api("/api/auth/login", { method: "POST", body: { username: username.value, password: password.value } });
    router.replace(typeof route.query.redirect === "string" ? route.query.redirect : "/admin");
  } catch (requestError) {
    error.value = requestError.message || "账号或密码错误";
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <main class="page narrow-page">
    <section class="login">
      <p class="eyebrow">PRIVATE AREA</p>
      <h1>管理员登录</h1>
      <p v-if="error" class="state error">{{ error }}</p>
      <form @submit.prevent="login">
        <label>账号<input v-model="username" autocomplete="username" required></label>
        <label>密码<input v-model="password" type="password" autocomplete="current-password" required></label>
        <button :disabled="loading">{{ loading ? "登录中……" : "进入设置" }}</button>
      </form>
    </section>
  </main>
</template>
