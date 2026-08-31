<script setup>
import { onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api";

const router = useRouter();
const settings = reactive({
  interval_minutes: "10",
  detail_interval_minutes: "5",
  onebot_url: "",
  onebot_token: "",
  onebot_target: "",
  onebot_profile: "bot",
});
const loading = ref(true);
const saving = ref(false);
const testing = ref(false);
const syncing = ref(false);
const message = ref("");
const error = ref("");

function showError(requestError) {
  if (requestError.status === 401) {
    router.replace({ path: "/admin/login", query: { redirect: "/admin" } });
    return;
  }
  error.value = requestError.message || "请求失败";
}

function setSettings(values) {
  Object.assign(settings, values);
}

async function loadSettings() {
  try {
    const data = await api("/api/admin/settings");
    setSettings(data.settings);
  } catch (requestError) {
    showError(requestError);
  } finally {
    loading.value = false;
  }
}

async function saveSettings() {
  saving.value = true;
  message.value = "";
  error.value = "";
  try {
    const data = await api("/api/admin/settings", { method: "PATCH", body: { ...settings } });
    setSettings(data.settings);
    message.value = "设置已保存";
  } catch (requestError) {
    showError(requestError);
  } finally {
    saving.value = false;
  }
}

async function testOnebot() {
  testing.value = true;
  message.value = "";
  error.value = "";
  try {
    await api("/api/admin/test-onebot", { method: "POST", body: { ...settings } });
    message.value = "测试消息已发送";
  } catch (requestError) {
    showError(requestError);
  } finally {
    testing.value = false;
  }
}

async function syncNow() {
  syncing.value = true;
  message.value = "";
  error.value = "";
  try {
    const data = await api("/api/admin/sync", { method: "POST" });
    if (data.error) error.value = `同步失败：${data.error}`;
    else message.value = `同步完成，检测到 ${data.changed_count} 项变化`;
  } catch (requestError) {
    showError(requestError);
  } finally {
    syncing.value = false;
  }
}

async function logout() {
  await api("/api/auth/logout", { method: "POST" });
  router.replace("/");
}

onMounted(loadSettings);
</script>

<template>
  <main class="page narrow-page">
    <section class="settings">
      <div class="settings-heading">
        <div><p class="eyebrow">CONTROL ROOM</p><h1>运行设置</h1></div>
        <button class="secondary" type="button" @click="logout">退出登录</button>
      </div>
      <p v-if="loading" class="state">正在读取设置……</p>
      <template v-else>
        <p v-if="message" class="success">{{ message }}</p>
        <p v-if="error" class="state error">{{ error }}</p>
        <form @submit.prevent="saveSettings">
          <h2>抓取</h2>
          <label>整页目录检查（分钟）<input v-model="settings.interval_minutes" type="number" min="5" max="60"><small>检查目录顺序、新专辑和封面，范围 5–60 分钟。</small></label>
          <label>异步详情检查（分钟）<input v-model="settings.detail_interval_minutes" type="number" min="1" max="30"><small>检查 cd_detail.php 中的最新专辑详情，范围 1–30 分钟。</small></label>
          <h2>OneBot V11 HTTP</h2>
          <label>接口地址<input v-model="settings.onebot_url" placeholder="http://127.0.0.1:3000"></label>
          <label>Access Token<input v-model="settings.onebot_token" type="password"></label>
          <label>接收目标<input v-model="settings.onebot_target" placeholder="private:QQ号 或群号"></label>
          <label>身份备注<input v-model="settings.onebot_profile"></label>
          <div class="actions">
            <button :disabled="saving">{{ saving ? "保存中……" : "保存设置" }}</button>
            <button type="button" class="secondary" :disabled="testing" @click="testOnebot">{{ testing ? "发送中……" : "发送测试消息" }}</button>
          </div>
        </form>
        <form class="sync" @submit.prevent="syncNow">
          <h2>立即检查</h2>
          <p>手动执行一次网页检查；首次初始化不会发送通知。</p>
          <button class="secondary" :disabled="syncing">{{ syncing ? "同步中……" : "立即同步" }}</button>
        </form>
      </template>
    </section>
  </main>
</template>
