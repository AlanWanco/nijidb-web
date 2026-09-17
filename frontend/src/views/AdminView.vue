<script setup>
import { computed, onMounted, reactive, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import "../settingsLayout.css";
import { api } from "../api";
import { formatLocalDateTime } from "../datetime";
import { t } from "../i18n";

const router = useRouter();
const route = useRoute();
const sections = [
  { id: "music", label: "音乐抓取设置" },
  { id: "news", label: "新闻抓取设置" },
  { id: "source", label: "手动源代码" },
  { id: "bot", label: "Bot 设置" },
  { id: "api", label: "外部 API" },
  { id: "database", label: "数据库" },
  { id: "account", label: "账号安全" },
];
const userRole = ref("admin");
const editorMode = computed(() => userRole.value === "editor");
const visibleSections = computed(() =>
  editorMode.value ? sections.filter((item) => item.id === "database") : sections,
);
const section = computed(() => {
  if (editorMode.value) return "database";
  return sections.some((item) => item.id === route.query.section)
    ? route.query.section
    : "music";
});
const logCategory = ref("");
const logPage = ref(1);
const newsLastSync = ref(null);
const newsSlowRefresh = ref(null);
const refreshingLogs = ref(false);
const sourceUrls = reactive({
  music: "https://www.lovelive-anime.jp/nijigasaki/cd.php",
  news: "https://www.lovelive-anime.jp/nijigasaki/topics.php",
});
const manualSourceType = ref("music");
const manualSourceUrl = ref("");
const manualSourceHtml = ref("");
const queryingSource = ref(false);
const submittingSource = ref(false);
const settings = reactive({
  interval_minutes: "10",
  detail_interval_minutes: "5",
  music_auto_sync: "1",
  news_interval_minutes: "30",
  news_auto_sync: "1",
  news_slow_refresh_enabled: "0",
  news_slow_refresh_delay_seconds: "10",
  onebot_url: "",
  onebot_token: "",
  onebot_target: "",
  onebot_profile: "bot",
});
const passwordForm = reactive({
  current_password: "",
  new_password: "",
  confirm_password: "",
});
const editorPasswordForm = reactive({
  current_password: "",
  new_password: "",
  confirm_password: "",
});
const loading = ref(true);
const saving = ref(false);
const testing = ref(false);
const syncing = ref(false);
const newsSyncing = ref(false);
const newsSlowRefreshing = ref(false);
const newsSlowRetrying = ref(false);
const externalApiDocs = ref(null);
const externalApiLoading = ref(false);
const externalApiPage = ref(0);
const externalApiKeyGenerating = ref(false);
const externalApiGeneratedKey = ref("");
const externalApiCopyStatus = ref("");
let externalApiCopyTimer = null;
const activityLogs = ref([]);
const changingPassword = ref(false);
const changingEditorPassword = ref(false);
const message = ref("");
const error = ref("");
const passwordMessage = ref("");
const passwordError = ref("");
const editorPasswordMessage = ref("");
const editorPasswordError = ref("");
const backingUp = ref(false);
const databaseBackups = ref([]);
const backupsLoading = ref(false);
const backupMessage = ref("");
const backupError = ref("");
const filteredLogs = computed(() =>
  activityLogs.value.filter(
    (log) => !logCategory.value || log.category === logCategory.value,
  ),
);
const logPages = computed(() =>
  Math.max(1, Math.ceil(filteredLogs.value.length / 15)),
);
const visibleLogs = computed(() =>
  filteredLogs.value.slice((logPage.value - 1) * 15, logPage.value * 15),
);
const externalApiPages = computed(() => externalApiDocs.value?.resources?.length || 0);
const externalApiResource = computed(
  () => externalApiDocs.value?.resources?.[externalApiPage.value] || null,
);
watch(logCategory, () => {
  logPage.value = 1;
});
watch(logPages, (value) => {
  logPage.value = Math.min(logPage.value, value);
});
watch(externalApiPages, (value) => {
  externalApiPage.value = Math.min(externalApiPage.value, Math.max(0, value - 1));
});
watch(externalApiPage, () => {
  externalApiGeneratedKey.value = "";
  externalApiCopyStatus.value = "";
});
watch(section, (value) => {
  if (value !== "api") {
    externalApiGeneratedKey.value = "";
    externalApiCopyStatus.value = "";
  }
  if (value === "database" && !loading.value) {
    loadBackups();
    refreshActivity();
  }
  if (value === "api" && !loading.value) loadExternalApiDocs();
});
const deviceTimeZone =
  Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

function showError(requestError) {
  if (requestError.status === 401) {
    router.replace({
      path: "/admin/login",
      query: { redirect: route.fullPath },
    });
    return;
  }
  error.value = requestError.message || t("请求失败");
}

function setSettings(values) {
  Object.assign(settings, values);
}

const manualSourceTargetUrl = computed(() =>
  manualSourceType.value === "music"
    ? sourceUrls.music
    : manualSourceUrl.value.trim() || sourceUrls.news,
);

function officialSourceUrl(value) {
  try {
    const parsed = new URL(value);
    return (
      parsed.protocol === "https:" &&
      ["www.lovelive-anime.jp", "lovelive-anime.jp"].includes(parsed.hostname) &&
      (!parsed.port || parsed.port === "443")
    );
  } catch {
    return false;
  }
}

function activityLogSummary(log) {
  if (log.error)
    return log.category === "news"
      ? t("新闻监控检查失败")
      : t("歌曲监控检查失败");
  const category = t(
    {
      program: "节目档案",
      news: "官网新闻",
      collabo: "联动立绘档案",
      music: "歌曲监控",
    }[log.category] || log.category,
  );
  return `${category} · ${log.summary}`;
}

async function loadSettings() {
  try {
    const data = await api("/api/admin/settings");
    userRole.value = data.role || "admin";
    setSettings(data.settings);
    Object.assign(sourceUrls, data.source_urls || {});
    activityLogs.value = data.activity_logs || [];
    newsLastSync.value = data.news_last_sync;
    newsSlowRefresh.value = data.news_slow_refresh || null;
    if (section.value === "database") await loadBackups();
    if (section.value === "api" && !editorMode.value) await loadExternalApiDocs();
  } catch (requestError) {
    showError(requestError);
  } finally {
    loading.value = false;
  }
}

async function loadExternalApiDocs() {
  if (externalApiLoading.value || externalApiDocs.value) return;
  externalApiLoading.value = true;
  try {
    externalApiDocs.value = await api("/api/admin/external-api-docs");
  } catch (requestError) {
    showError(requestError);
  } finally {
    externalApiLoading.value = false;
  }
}

function formatExternalApiExample(example) {
  return JSON.stringify(example || {}, null, 2);
}

function externalApiKeySourceLabel(resource) {
  if (resource.key_source === "database") return t("管理员生成");
  if (resource.key_source === "environment") return t("环境变量");
  return t("未配置");
}

function externalApiKeyUpdatedLabel(resource) {
  return resource.key_updated_at ? formatLocalDateTime(resource.key_updated_at) : "";
}

function updateExternalApiResource(resourceId, changes) {
  externalApiDocs.value = {
    ...externalApiDocs.value,
    resources: externalApiDocs.value.resources.map((resource) =>
      resource.id === resourceId ? { ...resource, ...changes } : resource,
    ),
  };
}

async function copyExternalApiText(value, target) {
  if (!value) return;
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(value);
  } catch {
    const fallback = document.createElement("textarea");
    fallback.value = value;
    fallback.style.position = "fixed";
    fallback.style.opacity = "0";
    document.body.appendChild(fallback);
    fallback.focus();
    fallback.select();
    const copied = document.execCommand("copy");
    fallback.remove();
    if (!copied) {
      error.value = t("复制失败");
      return;
    }
  }
  externalApiCopyStatus.value = target;
  if (externalApiCopyTimer) window.clearTimeout(externalApiCopyTimer);
  externalApiCopyTimer = window.setTimeout(() => {
    externalApiCopyStatus.value = "";
  }, 1800);
}

function copyExternalApiExample() {
  return copyExternalApiText(
    formatExternalApiExample(externalApiResource.value?.example),
    "example",
  );
}

function copyExternalApiKey() {
  return copyExternalApiText(externalApiGeneratedKey.value, "key");
}

async function generateExternalApiKey() {
  const resource = externalApiResource.value;
  if (!resource || externalApiKeyGenerating.value) return;
  if (
    resource.configured &&
    !window.confirm(t("轮换将立即使现有密钥失效，继续吗？"))
  ) {
    return;
  }
  externalApiKeyGenerating.value = true;
  message.value = "";
  error.value = "";
  try {
    const response = await api(
      `/api/admin/external-api-keys/${encodeURIComponent(resource.id)}`,
      { method: "POST" },
    );
    externalApiGeneratedKey.value = response.key || "";
    externalApiCopyStatus.value = "";
    updateExternalApiResource(resource.id, {
      configured: true,
      key_source: "database",
      key_updated_at: response.updated_at,
    });
    message.value = t("密钥已生成，请立即复制");
  } catch (requestError) {
    showError(requestError);
  } finally {
    externalApiKeyGenerating.value = false;
  }
}

async function refreshActivity() {
  refreshingLogs.value = true;
  try {
    const data = await api("/api/admin/settings");
    activityLogs.value = data.activity_logs || [];
    newsLastSync.value = data.news_last_sync;
    newsSlowRefresh.value = data.news_slow_refresh || newsSlowRefresh.value;
  } catch (requestError) {
    showError(requestError);
  } finally {
    refreshingLogs.value = false;
  }
}

async function loadBackups() {
  backupsLoading.value = true;
  try {
    const data = await api("/api/admin/backups");
    databaseBackups.value = data.backups || [];
  } catch (requestError) {
    showError(requestError);
  } finally {
    backupsLoading.value = false;
  }
}

function backupReasonLabel(reason) {
  if (reason === "before-json-import") return t("JSON 导入前自动备份");
  if (reason === "before-restore") return t("还原前自动备份");
  if (reason === "daily") return t("每日自动备份");
  return t("手动备份");
}

function formatBackupSize(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function backupDownloadPath(filename) {
  return `/api/admin/backups/${encodeURIComponent(filename)}/download`;
}

async function saveSettings() {
  saving.value = true;
  message.value = "";
  error.value = "";
  try {
    const keys =
      section.value === "news"
        ? [
            "news_auto_sync",
            "news_interval_minutes",
            "news_slow_refresh_enabled",
            "news_slow_refresh_delay_seconds",
          ]
        : section.value === "bot"
          ? ["onebot_url", "onebot_token", "onebot_target", "onebot_profile"]
          : ["music_auto_sync", "interval_minutes", "detail_interval_minutes"];
    const data = await api("/api/admin/settings", {
      method: "PATCH",
      body: Object.fromEntries(keys.map((key) => [key, settings[key]])),
    });
    setSettings(data.settings);
    message.value = t("设置已保存");
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
    await api("/api/admin/test-onebot", {
      method: "POST",
      body: { ...settings },
    });
    message.value = t("测试消息已发送");
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
    const data = await api("/api/admin/password", {
      method: "PATCH",
      body: { ...passwordForm },
    });
    passwordMessage.value = data.message;
    Object.assign(passwordForm, {
      current_password: "",
      new_password: "",
      confirm_password: "",
    });
  } catch (requestError) {
    if (requestError.status === 401) showError(requestError);
    else passwordError.value = requestError.message || t("密码修改失败");
  } finally {
    changingPassword.value = false;
  }
}

async function changeEditorPassword() {
  changingEditorPassword.value = true;
  editorPasswordMessage.value = "";
  editorPasswordError.value = "";
  try {
    const data = await api("/api/admin/editor-password", {
      method: "PATCH",
      body: { ...editorPasswordForm },
    });
    editorPasswordMessage.value = data.message;
    Object.assign(editorPasswordForm, {
      current_password: "",
      new_password: "",
      confirm_password: "",
    });
  } catch (requestError) {
    if (requestError.status === 401) showError(requestError);
    else editorPasswordError.value = requestError.message || t("编辑者密码修改失败");
  } finally {
    changingEditorPassword.value = false;
  }
}

async function fetchManualSourceFromBrowser() {
  message.value = "";
  error.value = "";
  const sourceUrl =
    manualSourceType.value === "news"
      ? manualSourceUrl.value.trim()
      : sourceUrls.music;
  if (!sourceUrl) {
    error.value = t("请输入具体新闻详情地址");
    return;
  }
  if (!officialSourceUrl(sourceUrl)) {
    error.value = t("请输入官网 HTTPS 地址");
    return;
  }
  queryingSource.value = true;
  try {
    const response = await fetch(sourceUrl, {
      credentials: "include",
      redirect: "follow",
      headers: { Accept: "text/html,application/xhtml+xml" },
    });
    if (!response.ok) throw new Error(t("官网返回 HTTP {status}", { status: response.status }));
    const html = await response.text();
    if (!html.trim()) throw new Error(t("官网没有返回网页源代码"));
    if (new Blob([html]).size > 8 * 1024 * 1024) {
      throw new Error(t("网页源代码不能超过 8 MB"));
    }
    manualSourceHtml.value = html;
    message.value = t("已从浏览器读取官网源代码");
  } catch (requestError) {
    error.value =
      requestError instanceof TypeError
        ? t("浏览器无法读取官网源代码，可能是跨域 CORS 限制；请改用 HTML 文件或粘贴。")
        : requestError.message || t("浏览器读取官网源代码失败");
  } finally {
    queryingSource.value = false;
  }
}

async function loadManualSourceFile(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  message.value = "";
  error.value = "";
  try {
    if (file.size > 8 * 1024 * 1024) throw new Error(t("网页源代码不能超过 8 MB"));
    manualSourceHtml.value = await file.text();
    if (!manualSourceHtml.value.trim()) throw new Error(t("网页源代码不能为空"));
    message.value = t("已载入 HTML 源代码");
  } catch (requestError) {
    error.value = requestError.message || t("读取 HTML 文件失败");
  }
}

async function submitManualSource() {
  message.value = "";
  error.value = "";
  if (!manualSourceHtml.value.trim()) {
    error.value = t("网页源代码不能为空");
    return;
  }
  const sourceUrl = manualSourceType.value === "news" ? manualSourceUrl.value.trim() : "";
  if (manualSourceType.value === "news" && !sourceUrl) {
    error.value = t("请输入具体新闻详情地址");
    return;
  }
  if (manualSourceType.value === "news" && !officialSourceUrl(sourceUrl)) {
    error.value = t("请输入官网 HTTPS 地址");
    return;
  }
  submittingSource.value = true;
  try {
    const data = await api("/api/admin/source-html", {
      method: "POST",
      body: {
        source: manualSourceType.value,
        source_url: sourceUrl,
        html: manualSourceHtml.value,
      },
    });
    activityLogs.value = data.activity_logs || activityLogs.value;
    message.value = t("源代码解析完成：{parsed} 项，发现 {changed} 项变化", {
      parsed: data.parsed_count || 0,
      changed: data.changed_count || 0,
    });
  } catch (requestError) {
    showError(requestError);
  } finally {
    submittingSource.value = false;
  }
}

async function syncNewsNow() {
  newsSyncing.value = true;
  message.value = "";
  error.value = "";
  try {
    const data = await api("/api/admin/news/sync", { method: "POST" });
    activityLogs.value = data.activity_logs || activityLogs.value;
    newsLastSync.value = data.last_sync;
    if (data.error) error.value = `${t("新闻同步失败")}: ${data.error}`;
    else
      message.value = data.changed_count
        ? t("新闻监控发现 {count} 项变化", { count: data.changed_count })
        : t("Topics 没有新变化");
  } catch (requestError) {
    showError(requestError);
  } finally {
    newsSyncing.value = false;
  }
}

async function runNewsSlowRefresh() {
  if (newsSlowRefreshing.value) return;
  newsSlowRefreshing.value = true;
  message.value = "";
  error.value = "";
  try {
    const data = await api("/api/admin/news/slow-refresh/run", { method: "POST" });
    newsSlowRefresh.value = data.news_slow_refresh || newsSlowRefresh.value;
    if (data.status === "failed") error.value = `${t("慢速刷新失败")}: ${data.error}`;
    else if (data.status === "skipped") error.value = `${t("慢速刷新跳过")}: ${data.error}`;
    else if (data.processed) message.value = t("慢速刷新已处理一篇新闻");
    else message.value = t("慢速刷新队列暂时没有待处理新闻");
  } catch (requestError) {
    showError(requestError);
  } finally {
    newsSlowRefreshing.value = false;
  }
}

async function refreshNewsSlowStatus() {
  try {
    const data = await api("/api/admin/settings");
    newsSlowRefresh.value = data.news_slow_refresh || newsSlowRefresh.value;
  } catch (requestError) {
    showError(requestError);
  }
}

async function retryNewsSlowRefresh() {
  if (newsSlowRetrying.value) return;
  newsSlowRetrying.value = true;
  message.value = "";
  error.value = "";
  try {
    const data = await api("/api/admin/news/slow-refresh/retry", { method: "POST" });
    newsSlowRefresh.value = data.news_slow_refresh || newsSlowRefresh.value;
    message.value = t("已重新排队 {count} 个失败页面", { count: data.reset_count || 0 });
  } catch (requestError) {
    showError(requestError);
  } finally {
    newsSlowRetrying.value = false;
  }
}

async function syncNow() {
  syncing.value = true;
  message.value = "";
  error.value = "";
  try {
    const data = await api("/api/admin/sync", { method: "POST" });
    activityLogs.value = data.activity_logs || activityLogs.value;
    if (data.error) error.value = `${t("同步失败")}: ${data.error}`;
    else
      message.value = data.changed_count
        ? t("歌曲监控发现 {count} 项变化", { count: data.changed_count })
        : "";
  } catch (requestError) {
    showError(requestError);
  } finally {
    syncing.value = false;
  }
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
      throw new Error(t("数据库备份下载失败"));
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
    backupMessage.value = t("数据库备份已下载");
    await loadBackups();
  } catch (requestError) {
    backupError.value = requestError.message || t("数据库备份下载失败");
  } finally {
    backingUp.value = false;
  }
}

async function logout() {
  await api("/api/auth/logout", { method: "POST" });
  router.replace("/");
}

onMounted(loadSettings);
</script>

<template>
  <main class="page settings-page">
    <section class="settings">
      <div class="settings-heading">
        <div>
          <p class="eyebrow">CONTROL ROOM / 01</p>
          <h1>{{ editorMode ? t("编辑者控制台") : t("运行设置") }}</h1>
          <p class="settings-intro">
            {{
              editorMode
                ? t("编辑者只能管理节目和联动，数据库仅支持下载。")
                : t("调整同步节奏、通知出口与本地档案的维护方式。")
            }}
          </p>
        </div>
        <div class="settings-heading-actions">
          <button class="secondary" type="button" @click="logout">
            {{ t("退出登录") }}
          </button>
        </div>
      </div>
      <p v-if="loading" class="state">{{ t("正在读取设置……") }}</p>
      <template v-else>
        <p v-if="message" class="success">{{ message }}</p>
        <p v-if="error" class="state error">{{ error }}</p>
        <div class="settings-workspace">
          <nav class="settings-directory" :aria-label="t('设置目录')">
            <RouterLink
              v-for="item in visibleSections"
              :key="item.id"
              :to="{
                path: '/admin',
                query: { ...route.query, section: item.id },
              }"
              :class="{ selected: section === item.id }"
              :aria-current="section === item.id ? 'page' : undefined"
              >{{ t(item.label) }}</RouterLink
            >
          </nav>
          <div class="settings-stack">
            <div class="settings-quick-links">
              <button
                class="secondary settings-programs-button"
                type="button"
                @click="router.push('/admin/programs')"
              >
                {{ t("管理节目") }}
              </button>
              <button
                class="secondary settings-programs-button"
                type="button"
                @click="router.push('/admin/collabo')"
              >
                {{ t("管理联动") }}
              </button>
            </div>
            <form
              v-if="!editorMode"
              v-show="section === 'music'"
              class="settings-card"
              @submit.prevent="saveSettings"
            >
              <div class="form-heading">
                <span class="form-number">01</span>
                <div>
                  <p class="form-kicker">SYNC ENGINE</p>
                  <h2>{{ t("音乐抓取设置") }}</h2>
                </div>
              </div>
              <label class="settings-checkbox"
                ><input
                  type="checkbox"
                  :checked="settings.music_auto_sync === '1'"
                  @change="
                    settings.music_auto_sync = $event.target.checked ? '1' : '0'
                  "
                /><span>{{ t("启用音乐自动检查") }}</span></label
              >
              <label
                >{{ t("整页目录检查（分钟）")
                }}<input
                  v-model="settings.interval_minutes"
                  type="number"
                  min="5"
                  max="300"
                /><small>{{
                  t("检查目录顺序、新专辑和封面，范围 5–300 分钟（最多 5 小时）。")
                }}</small></label
              >
              <label
                >{{ t("异步详情检查（分钟）")
                }}<input
                  v-model="settings.detail_interval_minutes"
                  type="number"
                  min="1"
                  max="300"
                /><small>{{
                  t("检查 cd_detail.php 中的最新专辑详情，范围 1–300 分钟（最多 5 小时）。")
                }}</small></label
              >
              <div class="actions">
                <button type="submit" :disabled="saving">
                  {{ saving ? t("保存中……") : t("保存设置") }}
                </button>
                <button
                  type="button"
                  class="secondary"
                  :disabled="syncing"
                  @click="syncNow"
                >
                  {{ syncing ? t("同步中……") : t("立即检查") }}
                </button>
              </div>
            </form>
            <form
              v-if="!editorMode"
              v-show="section === 'source'"
              class="settings-card manual-source-card"
              @submit.prevent="submitManualSource"
            >
              <div class="form-heading">
                <span class="form-number">03</span>
                <div>
                  <p class="form-kicker">BROWSER SOURCE BRIDGE</p>
                  <h2>{{ t("手动提交官网源代码") }}</h2>
                </div>
              </div>
              <p class="muted">
                {{
                  t(
                    "优先由当前浏览器直接读取官网，再把源代码提交给服务器解析；服务器不会代理官网请求。",
                  )
                }}
              </p>
              <p class="muted">
                {{
                  t(
                    "若浏览器提示跨域 CORS，说明官网允许打开但不允许管理页读取，请保存或复制 HTML 后在这里导入。",
                  )
                }}
              </p>
              <label
                >{{ t("源代码类型") }}<select v-model="manualSourceType">
                  <option value="music">{{ t("音乐目录") }}</option>
                  <option value="news">{{ t("新闻详情") }}</option>
                </select></label
              >
              <label v-if="manualSourceType === 'news'"
                >{{ t("官网新闻地址") }}<input
                  v-model="manualSourceUrl"
                  type="url"
                  :placeholder="sourceUrls.news"
                  autocomplete="off"
                /><small>{{ t("必须是 lovelive-anime.jp 的 HTTPS 新闻地址。") }}</small></label
              >
              <label
                >{{ t("网页源代码") }}<textarea
                  v-model="manualSourceHtml"
                  class="manual-source-textarea"
                  rows="16"
                  spellcheck="false"
                  :placeholder="t('可粘贴 view-source 或保存的 HTML 文件内容。')"
                ></textarea><small>{{ t("单次最多 8 MB；解析失败不会覆盖已有资料。") }}</small></label
              >
              <div class="actions">
                <button type="button" :disabled="queryingSource" @click="fetchManualSourceFromBrowser">
                  {{ queryingSource ? t("读取中……") : t("当前浏览器读取官网") }}
                </button>
                <label class="manual-source-file-button">
                  <span class="secondary">{{ t("选择 HTML 文件") }}</span>
                  <input type="file" accept=".html,.htm,text/html" @change="loadManualSourceFile" />
                </label>
                <a
                  class="manual-source-open"
                  :href="manualSourceTargetUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                  >{{ t("打开官网页面") }}</a
                >
                <button type="submit" class="secondary" :disabled="submittingSource">
                  {{ submittingSource ? t("解析中……") : t("提交并解析") }}
                </button>
              </div>
            </form>
            <form
              v-if="!editorMode"
              v-show="section === 'bot'"
              class="settings-card bot-settings-card"
              @submit.prevent="saveSettings"
            >
              <div class="form-heading">
                <span class="form-number">03</span>
                <div>
                  <p class="form-kicker">NOTIFICATION BRIDGE</p>
                  <h2>OneBot V11 HTTP</h2>
                </div>
              </div>
              <label
                >{{ t("接口地址")
                }}<input
                  v-model="settings.onebot_url"
                  placeholder="http://127.0.0.1:3000"
              /></label>
              <label
                >{{ t("访问令牌")
                }}<input v-model="settings.onebot_token" type="password"
              /></label>
              <label
                >{{ t("接收目标")
                }}<input
                  v-model="settings.onebot_target"
                  :placeholder="t('private:QQ号 或 group:群号')"
                /><small>{{
                  t("私聊填写 private:QQ号，群组填写 group:群号。")
                }}</small></label
              >
              <label
                >{{ t("身份备注") }}<input v-model="settings.onebot_profile"
              /></label>
              <div class="actions">
                <button :disabled="saving">
                  {{ saving ? t("保存中……") : t("保存设置") }}
                </button>
                <button
                  type="button"
                  class="secondary"
                  :disabled="testing"
                  @click="testOnebot"
                >
                  {{ testing ? t("发送中……") : t("发送测试消息") }}
                </button>
              </div>
            </form>
            <section
              v-if="!editorMode"
              v-show="section === 'news'"
              class="settings-card news-monitor-card"
            >
              <div class="form-heading">
                <span class="form-number">02</span>
                <div>
                  <p class="form-kicker">OFFICIAL SITE NEWS</p>
                  <h2>{{ t("新闻抓取设置") }}</h2>
                </div>
              </div>
              <p class="muted">
                {{
                  t(
                    "只自动检查官网 Topics；历史 news 与 as_news 仅作为本地归档展示。",
                  )
                }}
              </p>
              <p class="muted">
                {{
                  t(
                    "每次检查最近四页 Topics 及详情变化，失败自动重试，手动修改不会被覆盖。",
                  )
                }}
              </p>
              <p v-if="newsLastSync" class="muted">
                {{ t("上次检查") }}：{{
                  formatLocalDateTime(newsLastSync.checked_at)
                }}
                · {{ newsLastSync.discovered_count }} /
                {{ newsLastSync.changed_count
                }}<span v-if="newsLastSync.error" class="error">
                  · {{ newsLastSync.error }}</span
                >
              </p>
              <label class="settings-checkbox"
                ><input
                  type="checkbox"
                  :checked="settings.news_auto_sync === '1'"
                  @change="
                    settings.news_auto_sync = $event.target.checked ? '1' : '0'
                  "
                /><span>{{ t("启用 Topics 自动检查") }}</span></label
              >
              <label
                >{{ t("Topics 检查间隔（分钟）")
                }}<input
                  v-model="settings.news_interval_minutes"
                  type="number"
                  min="10"
                  max="300"
                /><small>{{
                  t("范围 10–300 分钟（最多 5 小时）；图片可在页面内后续补录。")
                }}</small></label
              >
              <label class="settings-checkbox"
                ><input
                  type="checkbox"
                  :checked="settings.news_slow_refresh_enabled === '1'"
                  @change="
                    settings.news_slow_refresh_enabled = $event.target.checked
                      ? '1'
                      : '0'
                  "
                /><span>{{ t("启用慢速官方图床刷新") }}</span></label
              >
              <label
                >{{ t("单篇刷新间隔（秒）")
                }}<input
                  v-model="settings.news_slow_refresh_delay_seconds"
                  type="number"
                  min="5"
                  max="60"
                /><small>{{
                  t("每篇新闻之间至少等待 5–60 秒；失败页面会记录并延迟重试。")
                }}</small></label
              >
              <div v-if="newsSlowRefresh" class="news-slow-refresh-status">
                <div class="news-slow-refresh-heading">
                  <strong>{{ t("慢速官方图床刷新") }}</strong>
                  <span>
                    {{ newsSlowRefresh.enabled ? t("已开启") : t("已关闭") }} ·
                    {{ newsSlowRefresh.completed }} / {{ newsSlowRefresh.total }}
                  </span>
                </div>
                <p>
                  {{ t("待处理") }} {{ newsSlowRefresh.remaining }} ·
                  {{ t("间隔") }} {{ newsSlowRefresh.delay_seconds }}s ·
                  {{ t("已跳过") }} {{ newsSlowRefresh.skipped }} ·
                  {{ t("失败") }} {{ newsSlowRefresh.failed }}
                  <span v-if="newsSlowRefresh.risk_failed"
                    >· {{ t("风控失败") }} {{ newsSlowRefresh.risk_failed }}</span
                  >
                </p>
                <p v-if="newsSlowRefresh.last_page" class="muted">
                  {{ t("最近处理") }}：{{ newsSlowRefresh.last_page.title || newsSlowRefresh.last_page.id }}
                </p>
                <ol v-if="newsSlowRefresh.failed_pages?.length" class="news-slow-failure-list">
                  <li v-for="failedPage in newsSlowRefresh.failed_pages.slice(0, 8)" :key="failedPage.id">
                    <RouterLink :to="`/news/${failedPage.id}`">{{ failedPage.title || failedPage.id }}</RouterLink>
                    <small>{{ failedPage.error }}</small>
                  </li>
                </ol>
              </div>
              <div class="actions">
                <button
                  type="button"
                  :disabled="newsSyncing"
                  @click="syncNewsNow"
                >
                  {{
                    newsSyncing ? t("检查中……") : t("立即检查 Topics")
                  }}</button
                ><button
                  type="button"
                  class="secondary"
                  :disabled="saving"
                  @click="saveSettings"
                >
                  {{ saving ? t("保存中……") : t("保存新闻设置") }}</button>
                <button
                  type="button"
                  class="secondary"
                  :disabled="newsSlowRefreshing"
                  @click="runNewsSlowRefresh"
                >
                  {{ newsSlowRefreshing ? t("刷新中……") : t("处理下一篇") }}
                </button>
                <button
                  type="button"
                  class="secondary"
                  @click="refreshNewsSlowStatus"
                >
                  {{ t("刷新状态") }}
                </button>
                <button
                  v-if="newsSlowRefresh?.failed"
                  type="button"
                  class="secondary"
                  :disabled="newsSlowRetrying"
                  @click="retryNewsSlowRefresh"
                >
                  {{ newsSlowRetrying ? t("排队中……") : t("重试失败页面") }}
                </button>
                <button
                  type="button"
                  class="secondary"
                  @click="router.push('/news')"
                >
                  {{ t("查看新闻页") }}
                </button>
              </div>
            </section>
            <section
              v-if="!editorMode"
              v-show="section === 'api'"
              class="settings-card external-api-card"
            >
              <div class="form-heading">
                <span class="form-number">07</span>
                <div>
                  <p class="form-kicker">EXTERNAL INGEST API</p>
                  <h2>{{ t("外部 API 文档") }}</h2>
                </div>
                <span v-if="externalApiPages" class="section-count"
                  >{{ externalApiPage + 1 }} / {{ externalApiPages }}</span
                >
              </div>
              <p class="muted">
                {{
                  t(
                    "供独立的外部更新器使用。四类资源分别使用独立 API Key；管理员生成的密钥会持久化到数据卷，明文只显示一次。",
                  )
                }}
              </p>
              <p v-if="externalApiDocs" class="external-api-key">
                {{ t("请求头") }}：<code>{{ externalApiDocs.header }}</code> ·
                {{ t("内容类型") }}：<code>{{ externalApiDocs.content_type }}</code>
              </p>
              <ul v-if="externalApiDocs?.notes?.length" class="external-api-notes">
                <li v-for="note in externalApiDocs.notes" :key="note">{{ note }}</li>
              </ul>
              <p v-if="externalApiLoading" class="state">
                {{ t("正在读取 API 文档……") }}
              </p>
              <template v-else-if="externalApiResource">
                <nav class="external-api-pagination" :aria-label="t('API 文档分页')">
                  <button
                    type="button"
                    class="secondary"
                    :disabled="externalApiPage <= 0"
                    @click="externalApiPage--"
                  >
                    ← {{ t("上一页") }}
                  </button>
                  <div class="external-api-page-tabs" role="tablist">
                    <button
                      v-for="(resource, index) in externalApiDocs.resources"
                      :key="resource.id"
                      type="button"
                      :class="{ selected: externalApiPage === index }"
                      role="tab"
                      :aria-selected="externalApiPage === index"
                      @click="externalApiPage = index"
                    >
                      {{ resource.label }}
                    </button>
                  </div>
                  <button
                    type="button"
                    class="secondary"
                    :disabled="externalApiPage >= externalApiPages - 1"
                    @click="externalApiPage++"
                  >
                    {{ t("下一页") }} →
                  </button>
                </nav>
                <article class="external-api-resource">
                  <div class="external-api-endpoint">
                    <span class="external-api-method">{{ externalApiResource.method }}</span>
                    <code>{{ externalApiResource.path }}</code>
                    <span
                      class="external-api-status"
                      :class="{ configured: externalApiResource.configured }"
                    >
                      {{
                        externalApiResource.configured
                          ? t("密钥已配置")
                          : t("密钥未配置")
                      }}
                    </span>
                  </div>
                  <p class="external-api-description">
                    {{ externalApiResource.description }}
                  </p>
                  <div class="external-api-key-management">
                    <div>
                      <span class="external-api-key-label">{{ t("密钥状态") }}</span>
                      <strong>{{ externalApiKeySourceLabel(externalApiResource) }}</strong>
                      <small v-if="externalApiKeyUpdatedLabel(externalApiResource)">
                        {{ t("最近更新") }}：{{ externalApiKeyUpdatedLabel(externalApiResource) }}
                      </small>
                    </div>
                    <button
                      type="button"
                      class="secondary"
                      :disabled="externalApiKeyGenerating"
                      @click="generateExternalApiKey"
                    >
                      {{
                        externalApiKeyGenerating
                          ? t("正在生成……")
                          : externalApiResource.configured
                            ? t("轮换密钥")
                            : t("生成密钥")
                      }}
                    </button>
                  </div>
                  <p class="external-api-key">
                    {{ t("密钥环境变量") }}：<code>{{ externalApiResource.key_env }}</code>
                  </p>
                  <div v-if="externalApiGeneratedKey" class="external-api-secret">
                    <div class="external-api-secret-heading">
                      <strong>{{ t("新密钥（仅显示一次）") }}</strong>
                      <button
                        type="button"
                        class="secondary external-api-copy-key"
                        @click="copyExternalApiKey"
                      >
                        {{ externalApiCopyStatus === "key" ? t("已复制") : t("复制密钥") }}
                      </button>
                    </div>
                    <code>{{ externalApiGeneratedKey }}</code>
                    <small>{{ t("请立即复制；刷新或切换页面后不能再次读取。") }}</small>
                  </div>
                  <h3>{{ t("请求字段") }}</h3>
                  <dl class="external-api-fields">
                    <div
                      v-for="field in externalApiResource.fields"
                      :key="field.name"
                    >
                      <dt>
                        <code>{{ field.name }}</code>
                        <strong v-if="field.required">{{ t("必填") }}</strong>
                      </dt>
                      <dd>{{ field.description }}</dd>
                    </div>
                  </dl>
                  <h3>{{ t("请求示例") }}</h3>
                  <div class="external-api-code">
                    <button
                      type="button"
                      class="secondary external-api-copy"
                      @click="copyExternalApiExample"
                    >
                      {{ externalApiCopyStatus === "example" ? t("已复制") : t("复制示例") }}
                    </button>
                    <pre class="external-api-example"><code>{{
                      formatExternalApiExample(externalApiResource.example)
                    }}</code></pre>
                  </div>
                </article>
              </template>
            </section>
            <form
              v-if="!editorMode"
              v-show="section === 'account'"
              class="settings-card password-form"
              @submit.prevent="changePassword"
            >
              <div class="form-heading">
                <span class="form-number">04</span>
                <div>
                  <p class="form-kicker">ACCESS CONTROL</p>
                  <h2>{{ t("修改管理员密码") }}</h2>
                </div>
              </div>
              <p class="muted">
                {{ t("新密码至少 8 位，修改后会持久化到数据卷。") }}
              </p>
              <p v-if="passwordMessage" class="success">
                {{ passwordMessage }}
              </p>
              <p v-if="passwordError" class="state error">
                {{ passwordError }}
              </p>
              <label
                >{{ t("当前密码")
                }}<input
                  v-model="passwordForm.current_password"
                  type="password"
                  autocomplete="current-password"
                  required
              /></label>
              <label
                >{{ t("新密码")
                }}<input
                  v-model="passwordForm.new_password"
                  type="password"
                  autocomplete="new-password"
                  minlength="8"
                  required
              /></label>
              <label
                >{{ t("确认新密码")
                }}<input
                  v-model="passwordForm.confirm_password"
                  type="password"
                  autocomplete="new-password"
                  minlength="8"
                  required
              /></label>
              <button :disabled="changingPassword">
                {{ changingPassword ? t("修改中……") : t("修改密码") }}
              </button>
            </form>
            <form
              v-if="!editorMode"
              v-show="section === 'account'"
              class="settings-card password-form"
              @submit.prevent="changeEditorPassword"
            >
              <div class="form-heading">
                <span class="form-number">04B</span>
                <div>
                  <p class="form-kicker">EDITOR ACCESS</p>
                  <h2>{{ t("修改编辑者密码") }}</h2>
                </div>
              </div>
              <p class="muted">
                {{ t("为 editor 账户设置新密码，旧的编辑者登录会立即失效。") }}
              </p>
              <p v-if="editorPasswordMessage" class="success">
                {{ editorPasswordMessage }}
              </p>
              <p v-if="editorPasswordError" class="state error">
                {{ editorPasswordError }}
              </p>
              <label
                >{{ t("当前管理员密码")
                }}<input
                  v-model="editorPasswordForm.current_password"
                  type="password"
                  autocomplete="current-password"
                  required
              /></label>
              <label
                >{{ t("新密码")
                }}<input
                  v-model="editorPasswordForm.new_password"
                  type="password"
                  autocomplete="new-password"
                  minlength="8"
                  required
              /></label>
              <label
                >{{ t("确认新密码")
                }}<input
                  v-model="editorPasswordForm.confirm_password"
                  type="password"
                  autocomplete="new-password"
                  minlength="8"
                  required
              /></label>
              <button :disabled="changingEditorPassword">
                {{ changingEditorPassword ? t("修改中……") : t("更新编辑者密码") }}
              </button>
            </form>
            <section
              v-show="section === 'database'"
              class="settings-card sync-log-card"
            >
              <div class="form-heading">
                <span class="form-number">05</span>
                <div>
                  <p class="form-kicker">DATABASE ACTIVITY</p>
                  <h2>{{ t("数据库变化记录") }}</h2>
                </div>
                <span class="section-count">{{ activityLogs.length }}</span>
              </div>
              <p class="sync-log-intro">
                {{
                  t(
                    "显示音乐、节目、新闻和联动的实际变化；时间按设备时区显示（{timezone}）。",
                    { timezone: deviceTimeZone },
                  )
                }}
              </p>
              <div class="settings-log-toolbar">
                <label class="settings-log-filter"
                  >{{ t("筛选记录") }}<span class="settings-select-control"
                    ><select v-model="logCategory">
                      <option value="">{{ t("全部") }}</option>
                      <option value="music">{{ t("歌曲监控") }}</option>
                      <option value="program">{{ t("节目档案") }}</option>
                      <option value="news">{{ t("官网新闻") }}</option>
                      <option value="collabo">{{ t("联动立绘档案") }}</option>
                    </select></span></label
                ><button
                  type="button"
                  class="secondary"
                  :disabled="refreshingLogs"
                  @click="refreshActivity"
                >
                  {{ t("刷新列表") }}
                </button>
              </div>
              <ol v-if="visibleLogs.length" class="sync-log-list">
                <li
                  v-for="log in visibleLogs"
                  :key="log.id"
                  :class="{ failed: log.error }"
                >
                  <time
                    :datetime="log.checked_at"
                    :title="
                      t('设备时区：{timezone}', { timezone: deviceTimeZone })
                    "
                    >{{ formatLocalDateTime(log.checked_at) }}</time
                  >
                  <div>
                    <strong>{{ activityLogSummary(log) }}</strong
                    ><small v-if="log.error">{{ log.error }}</small>
                  </div>
                </li>
              </ol>
              <p v-else class="muted">{{ t("暂时没有数据库变化记录。") }}</p>
              <nav
                v-if="logPages > 1"
                class="settings-log-pages"
                :aria-label="t('数据库变化记录')"
              >
                <button
                  type="button"
                  class="secondary"
                  :disabled="logPage <= 1"
                  @click="logPage--"
                >
                  ←</button
                ><span>{{ logPage }} / {{ logPages }}</span
                ><button
                  type="button"
                  class="secondary"
                  :disabled="logPage >= logPages"
                  @click="logPage++"
                >
                  →
                </button>
              </nav>
            </section>
            <section
              v-show="section === 'database'"
              class="settings-card backup"
            >
              <div class="form-heading">
                <span class="form-number">06</span>
                <div>
                  <p class="form-kicker">DATA SAFETY</p>
                  <h2>{{ t("数据库备份") }}</h2>
                </div>
              </div>
              <p class="muted">
                {{
                  t(
                    "数据库仅支持下载，不支持上传覆盖；备份包含设置、节目、音乐、新闻、联动及其图片路径，不包含图片文件。每天自动备份 SQLite，最多保留 30 份。",
                  )
                }}
              </p>
              <p v-if="backupMessage" class="success">{{ backupMessage }}</p>
              <p v-if="backupError" class="state error">{{ backupError }}</p>
              <div class="backup-actions">
                <button
                  type="button"
                  :disabled="backingUp"
                  @click="downloadBackup"
                >
                  {{ backingUp ? t("准备中……") : t("下载数据库备份") }}
                </button>
              </div>
              <div class="backup-list-heading">
                <div>
                  <strong>{{ t("已保存的数据库备份") }}</strong
                  ><small>{{
                    t(
                      "自动备份和手动备份都会保留在列表中，最多保留最近 30 份。",
                    )
                  }}</small>
                </div>
                <button
                  type="button"
                  class="secondary backup-refresh-button"
                  :disabled="backupsLoading"
                  @click="loadBackups"
                >
                  {{ backupsLoading ? t("读取中……") : t("刷新列表") }}
                </button>
              </div>
              <p v-if="backupsLoading" class="muted">
                {{ t("正在读取数据库备份……") }}
              </p>
              <p v-else-if="!databaseBackups.length" class="muted">
                {{ t("还没有保存的数据库备份。") }}
              </p>
              <ol v-else class="backup-list">
                <li v-for="backup in databaseBackups" :key="backup.filename">
                  <div>
                    <strong>{{ backupReasonLabel(backup.reason) }}</strong
                    ><small
                      >{{ formatLocalDateTime(backup.created_at) }} ·
                      {{ formatBackupSize(backup.size) }}</small
                    ><code>{{ backup.filename }}</code>
                  </div>
                  <div class="backup-list-actions">
                    <a
                      class="secondary backup-list-button"
                      :href="backupDownloadPath(backup.filename)"
                      >{{ t("下载") }}</a
                    >
                  </div>
                </li>
              </ol>
            </section>
          </div>
        </div>
      </template>
    </section>
  </main>
</template>
