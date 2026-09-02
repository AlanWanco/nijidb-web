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
const passwordForm = reactive({ current_password: "", new_password: "", confirm_password: "" });
const loading = ref(true);
const saving = ref(false);
const testing = ref(false);
const syncing = ref(false);
const changingPassword = ref(false);
const message = ref("");
const error = ref("");
const passwordMessage = ref("");
const passwordError = ref("");
const backupInput = ref(null);
const backupFile = ref(null);
const backingUp = ref(false);
const restoring = ref(false);
const backupMessage = ref("");
const backupError = ref("");

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

async function changePassword() {
  changingPassword.value = true;
  passwordMessage.value = "";
  passwordError.value = "";
  try {
    const data = await api("/api/admin/password", { method: "PATCH", body: { ...passwordForm } });
    passwordMessage.value = data.message;
    Object.assign(passwordForm, { current_password: "", new_password: "", confirm_password: "" });
  } catch (requestError) {
    if (requestError.status === 401) showError(requestError);
    else passwordError.value = requestError.message || "密码修改失败";
  } finally {
    changingPassword.value = false;
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

function selectBackup(event) {
  backupFile.value = event.target.files?.[0] || null;
  backupMessage.value = "";
  backupError.value = "";
}

async function downloadBackup() {
  backingUp.value = true;
  backupMessage.value = "";
  backupError.value = "";
  try {
    const response = await fetch("/api/admin/backup", {
      headers: { Accept: "application/vnd.sqlite3" },
      credentials: "same-origin",
    });
    if (!response.ok) {
      if (response.status === 401) {
        showError({ status: 401 });
        return;
      }
      throw new Error("数据库备份下载失败");
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = `nijidb-backup-${new Date().toISOString().replace(/[:.]/g, "-")}.sqlite3`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    backupMessage.value = "数据库备份已下载";
  } catch (requestError) {
    backupError.value = requestError.message || "数据库备份下载失败";
  } finally {
    backingUp.value = false;
  }
}

async function restoreBackup() {
  if (!backupFile.value) {
    backupError.value = "请先选择数据库备份文件";
    return;
  }
  if (!window.confirm("还原会覆盖当前数据库中的设置、节目和资料，确定继续吗？")) return;
  restoring.value = true;
  backupMessage.value = "";
  backupError.value = "";
  try {
    const response = await fetch("/api/admin/backup/restore", {
      method: "POST",
      headers: { "Content-Type": "application/vnd.sqlite3" },
      body: backupFile.value,
      credentials: "same-origin",
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : {};
    if (!response.ok) {
      if (response.status === 401) {
        showError({ status: 401 });
        return;
      }
      throw new Error(payload.detail || "数据库还原失败");
    }
    backupMessage.value = payload.message || "数据库还原成功";
    backupFile.value = null;
    if (backupInput.value) backupInput.value.value = "";
  } catch (requestError) {
    backupError.value = requestError.message || "数据库还原失败";
  } finally {
    restoring.value = false;
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
        <div><p class="eyebrow">CONTROL ROOM / 01</p><h1>运行设置</h1><p class="settings-intro">调整同步节奏、通知出口与本地档案的维护方式。</p></div>
        <div class="settings-heading-actions">
          <button class="secondary" type="button" @click="router.push('/admin/programs')">管理节目</button>
          <button class="secondary" type="button" @click="logout">退出登录</button>
        </div>
      </div>
      <p v-if="loading" class="state">正在读取设置……</p>
      <template v-else>
        <p v-if="message" class="success">{{ message }}</p>
        <p v-if="error" class="state error">{{ error }}</p>
        <div class="settings-stack">
          <form class="settings-card" @submit.prevent="saveSettings">
            <div class="form-heading"><span class="form-number">01</span><div><p class="form-kicker">SYNC ENGINE</p><h2>抓取</h2></div></div>
            <label>整页目录检查（分钟）<input v-model="settings.interval_minutes" type="number" min="5" max="60"><small>检查目录顺序、新专辑和封面，范围 5–60 分钟。</small></label>
            <label>异步详情检查（分钟）<input v-model="settings.detail_interval_minutes" type="number" min="1" max="30"><small>检查 cd_detail.php 中的最新专辑详情，范围 1–30 分钟。</small></label>
            <div class="form-heading subsection-heading"><span class="form-number">02</span><div><p class="form-kicker">NOTIFICATION BRIDGE</p><h2>OneBot V11 HTTP</h2></div></div>
            <label>接口地址<input v-model="settings.onebot_url" placeholder="http://127.0.0.1:3000"></label>
            <label>Access Token<input v-model="settings.onebot_token" type="password"></label>
            <label>接收目标<input v-model="settings.onebot_target" placeholder="private:QQ号 或 group:群号"><small>私聊填写 private:QQ号，群组填写 group:群号。</small></label>
            <label>身份备注<input v-model="settings.onebot_profile"></label>
            <div class="actions">
              <button :disabled="saving">{{ saving ? "保存中……" : "保存设置" }}</button>
              <button type="button" class="secondary" :disabled="testing" @click="testOnebot">{{ testing ? "发送中……" : "发送测试消息" }}</button>
            </div>
          </form>
          <form class="settings-card password-form" @submit.prevent="changePassword">
            <div class="form-heading"><span class="form-number">03</span><div><p class="form-kicker">ACCESS CONTROL</p><h2>修改管理员密码</h2></div></div>
            <p class="muted">新密码至少 8 位，修改后会持久化到数据卷。</p>
            <p v-if="passwordMessage" class="success">{{ passwordMessage }}</p>
            <p v-if="passwordError" class="state error">{{ passwordError }}</p>
            <label>当前密码<input v-model="passwordForm.current_password" type="password" autocomplete="current-password" required></label>
            <label>新密码<input v-model="passwordForm.new_password" type="password" autocomplete="new-password" minlength="8" required></label>
            <label>确认新密码<input v-model="passwordForm.confirm_password" type="password" autocomplete="new-password" minlength="8" required></label>
            <button :disabled="changingPassword">{{ changingPassword ? "修改中……" : "修改密码" }}</button>
          </form>
          <form class="settings-card sync" @submit.prevent="syncNow">
            <div class="form-heading"><span class="form-number">04</span><div><p class="form-kicker">MANUAL RUN</p><h2>立即检查</h2></div></div>
            <p>手动执行一次网页检查；首次初始化不会发送通知。</p>
            <button class="secondary" :disabled="syncing">{{ syncing ? "同步中……" : "立即同步" }}</button>
          </form>
          <section class="settings-card backup">
            <div class="form-heading"><span class="form-number">05</span><div><p class="form-kicker">DATA SAFETY</p><h2>数据库备份与还原</h2></div></div>
            <p class="muted">备份包含设置、节目档案、发行资料和同步记录，不包含封面图片。还原后会在下一次同步重新检查封面缓存。</p>
            <p v-if="backupMessage" class="success">{{ backupMessage }}</p>
            <p v-if="backupError" class="state error">{{ backupError }}</p>
            <div class="backup-actions">
              <button type="button" :disabled="backingUp" @click="downloadBackup">{{ backingUp ? "准备中……" : "下载数据库备份" }}</button>
              <label class="backup-file">选择备份文件<input ref="backupInput" type="file" accept=".sqlite3,.sqlite,.db" @change="selectBackup"></label>
              <button type="button" class="secondary" :disabled="restoring || !backupFile" @click="restoreBackup">{{ restoring ? "还原中……" : "还原所选备份" }}</button>
            </div>
            <small v-if="backupFile">已选择：{{ backupFile.name }}</small>
          </section>
        </div>
      </template>
    </section>
  </main>
</template>
