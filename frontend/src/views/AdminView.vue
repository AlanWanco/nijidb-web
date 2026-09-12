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
  { id: "bot", label: "Bot 设置" },
  { id: "database", label: "数据库" },
  { id: "account", label: "账号安全" },
];
const section = computed(() =>
  sections.some((item) => item.id === route.query.section)
    ? route.query.section
    : "music",
);
const logCategory = ref("");
const logPage = ref(1);
const newsLastSync = ref(null);
const newsSlowRefresh = ref(null);
const refreshingLogs = ref(false);
const settings = reactive({
  interval_minutes: "10",
  detail_interval_minutes: "5",
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
const loading = ref(true);
const saving = ref(false);
const testing = ref(false);
const syncing = ref(false);
const newsSyncing = ref(false);
const newsSlowRefreshing = ref(false);
const newsSlowRetrying = ref(false);
const activityLogs = ref([]);
const changingPassword = ref(false);
const message = ref("");
const error = ref("");
const passwordMessage = ref("");
const passwordError = ref("");
const backupInput = ref(null);
const backupFile = ref(null);
const backingUp = ref(false);
const restoring = ref(false);
const databaseBackups = ref([]);
const backupsLoading = ref(false);
const restoringBackupName = ref("");
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
watch(logCategory, () => {
  logPage.value = 1;
});
watch(logPages, (value) => {
  logPage.value = Math.min(logPage.value, value);
});
watch(section, (value) => {
  if (value === "database" && !loading.value) {
    loadBackups();
    refreshActivity();
  }
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
    setSettings(data.settings);
    activityLogs.value = data.activity_logs || [];
    newsLastSync.value = data.news_last_sync;
    newsSlowRefresh.value = data.news_slow_refresh || null;
    if (section.value === "database") await loadBackups();
  } catch (requestError) {
    showError(requestError);
  } finally {
    loading.value = false;
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
          : ["interval_minutes", "detail_interval_minutes"];
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

async function restoreBackup() {
  if (!backupFile.value) {
    backupError.value = t("请先选择数据库备份文件");
    return;
  }
  if (
    !window.confirm(
      t("第一次确认：还原会覆盖当前数据库中的设置、节目和资料，确定继续吗？"),
    )
  )
    return;
  if (!window.confirm(t("第二次确认：还原后当前数据库会被替换，继续执行吗？")))
    return;
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
    const payload = contentType.includes("application/json")
      ? await response.json()
      : {};
    if (!response.ok) {
      if (response.status === 401) {
        showError({ status: 401 });
        return;
      }
      throw new Error(payload.detail || t("数据库还原失败"));
    }
    backupMessage.value = payload.message || t("数据库还原成功");
    backupFile.value = null;
    if (backupInput.value) backupInput.value.value = "";
    await loadSettings();
  } catch (requestError) {
    backupError.value = requestError.message || t("数据库还原失败");
  } finally {
    restoring.value = false;
  }
}

async function restoreStoredBackup(backup) {
  if (restoringBackupName.value) return;
  if (
    !window.confirm(
      t("第一次确认：确定还原备份「{filename}」吗？当前数据库会被替换。", {
        filename: backup.filename,
      }),
    )
  )
    return;
  if (
    !window.confirm(
      t("第二次确认：还原「{filename}」不可自动撤销，继续执行吗？", {
        filename: backup.filename,
      }),
    )
  )
    return;
  restoringBackupName.value = backup.filename;
  backupMessage.value = "";
  backupError.value = "";
  try {
    const data = await api(
      `/api/admin/backups/${encodeURIComponent(backup.filename)}/restore`,
      { method: "POST" },
    );
    backupMessage.value = data.message || t("数据库还原成功");
    await loadSettings();
  } catch (requestError) {
    if (requestError.status === 401) showError(requestError);
    else backupError.value = requestError.message || t("数据库还原失败");
  } finally {
    restoringBackupName.value = "";
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
          <h1>{{ t("运行设置") }}</h1>
          <p class="settings-intro">
            {{ t("调整同步节奏、通知出口与本地档案的维护方式。") }}
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
              v-for="item in sections"
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
              <label
                >{{ t("整页目录检查（分钟）")
                }}<input
                  v-model="settings.interval_minutes"
                  type="number"
                  min="5"
                  max="60"
                /><small>{{
                  t("检查目录顺序、新专辑和封面，范围 5–60 分钟。")
                }}</small></label
              >
              <label
                >{{ t("异步详情检查（分钟）")
                }}<input
                  v-model="settings.detail_interval_minutes"
                  type="number"
                  min="1"
                  max="30"
                /><small>{{
                  t("检查 cd_detail.php 中的最新专辑详情，范围 1–30 分钟。")
                }}</small></label
              >
              <div class="actions">
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
              <p class="muted">{{ t("Bot 设置独立于音乐与新闻抓取，便于后续接入更多通知。") }}</p>
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
                  max="1440"
                /><small>{{
                  t("范围 10–1440 分钟；图片可在页面内后续补录。")
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
            <form
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
                  <h2>{{ t("数据库备份与还原") }}</h2>
                </div>
              </div>
              <p class="muted">
                {{
                  t(
                    "备份包含设置、节目、音乐、新闻、联动及其图片路径，不包含图片文件。每天自动备份 SQLite，最多保留 30 份；不会备份整卷图库。",
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
                <label class="backup-file"
                  >{{ t("选择备份文件")
                  }}<input
                    ref="backupInput"
                    type="file"
                    accept=".sqlite3,.sqlite,.db"
                    @change="selectBackup"
                /></label>
                <button
                  type="button"
                  class="secondary"
                  :disabled="restoring || !backupFile"
                  @click="restoreBackup"
                >
                  {{ restoring ? t("还原中……") : t("还原所选备份") }}
                </button>
              </div>
              <small v-if="backupFile"
                >{{ t("已选择：") }}{{ backupFile.name }}</small
              >
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
                    ><button
                      type="button"
                      class="danger backup-list-button"
                      :disabled="restoringBackupName === backup.filename"
                      @click="restoreStoredBackup(backup)"
                    >
                      {{
                        restoringBackupName === backup.filename
                          ? t("还原中……")
                          : t("还原")
                      }}
                    </button>
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
