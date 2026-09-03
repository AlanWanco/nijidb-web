<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { NIJIGASAKI_CAST, castColorSegments } from "../programCast";
import { occurrenceLinkItems, programAdminPath, relatedLinkItem } from "../programLinks";

const weekdayNames = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const timezoneLabels = {
  "Asia/Tokyo": "东京时间",
  "Asia/Shanghai": "中国标准时间",
  "Asia/Seoul": "韩国时间",
  UTC: "协调世界时",
  "America/Los_Angeles": "美国太平洋时间",
  "America/New_York": "美国东部时间",
};
const route = useRoute();
const router = useRouter();
const programs = ref([]);
const occurrences = ref([]);
const keyword = ref("");
const castFilter = ref([]);
const castFilterDetails = ref(null);
const castFilterOpen = ref(false);
const loading = ref(true);
const occurrenceLoading = ref(false);
const error = ref("");
const adminAuthenticated = ref(false);
const editAccessNotice = ref("");
const editAccessMessage = "需要登录管理员后才能编辑；未来将开放编辑审核。";

const programId = computed(() => String(route.params.programId || ""));
const detailMode = computed(() => Boolean(programId.value));
const selectedProgram = computed(() => programs.value.find(program => program.id === programId.value) || null);
const selectedRelatedLink = computed(() => relatedLinkItem(selectedProgram.value));
const selectedAdminProgramPath = computed(() => programId.value ? programAdminPath(programId.value) : "/admin/programs");
const selectedAdminOccurrencePath = computed(() => programId.value
  ? programAdminPath(programId.value, { panel: "occurrences" })
  : "/admin/programs");
const newAdminProgramPath = "/admin/programs?new=1";
const allCastSelected = computed(() => castFilter.value.length === NIJIGASAKI_CAST.length);
const castFilterLabel = computed(() => {
  if (!castFilter.value.length || allCastSelected.value) return "全部 Cast";
  return `已选 ${castFilter.value.length} 位`;
});
const filteredPrograms = computed(() => programs.value.filter(programMatchesFilters));
const visibleOccurrences = computed(() => occurrences.value.filter(row => row.status !== "deleted"));

function programType(program) {
  return program.category === "official" ? "官方节目" : "个人节目";
}

function programStatus(program) {
  return program.status === "completed" ? "已完结" : "进行中";
}

function formatLabel(program) {
  return `${program.format === "radio" ? "广播" : "有画面"} · ${program.platform === "tv" ? "电视台" : "网络"} · ${program.delivery === "live" ? "直播" : "录播"}`;
}

function periodScheduleLabel(period) {
  const time = period.schedule_time ? ` ${period.schedule_time}` : "";
  if (period.frequency === "single") return `单次${time}`;
  if (period.frequency === "individual") return `月更 · 逐期设置${time}`;
  const weekday = weekdayNames[period.weekday] || "";
  if (period.frequency === "monthly") {
    const direction = period.week_direction || (period.week_index < 0 ? "last" : "first");
    const number = period.week_number || Math.abs(period.week_index) || 1;
    const week = direction === "last" ? `倒数第${number}周` : `第${number}周`;
    return `每月${week}${weekday}${time}`;
  }
  const interval = period.week_interval > 1 ? `每${period.week_interval}周` : "每周";
  return `${interval}${weekday}${time}`;
}

function scheduleLabel(program) {
  const periods = program.periods || [];
  if (periods.length > 1) return `${periods.length} 个分段时期`;
  if (!periods.length) return "未设置排期";
  return periodScheduleLabel(periods[0]);
}

function timezoneLabel(value) {
  return timezoneLabels[value] || value || "东京时间";
}

function dateLabel(value) {
  if (!value) return "未设置日期";
  const parsed = new Date(`${value}T12:00:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "short" }).format(parsed);
}

function episodeLabel(row) {
  return row.special === "EX" ? "EX 特别节目" : `第 ${row.episode} 期`;
}

function occurrenceStatus(row) {
  if (row.status === "cancelled") return "已取消";
  const airStatus = row.aired ? "已播出" : "未播出";
  return row.status === "rescheduled" ? `已改期 · ${airStatus}` : airStatus;
}

function occurrenceStateClass(row) {
  if (row.status === "cancelled") return "status-cancelled";
  return row.aired ? "status-aired" : "status-upcoming";
}

function occurrenceSourceLabel(row) {
  if (row.status === "cancelled") return "保留在排期中，标记为已取消";
  if (row.adjusted_date) return `原定 ${dateLabel(row.original_date)} · 已改期`;
  return row.generated ? "自动生成" : row.materialized ? "已播出并保存" : "已单独录入";
}

function programCast(program) {
  return castColorSegments(program.people || []);
}

function programMatchesCast(program) {
  if (!castFilter.value.length || allCastSelected.value) return true;
  const people = program.people || [];
  return castFilter.value.some(selectedName => {
    const member = NIJIGASAKI_CAST.find(item => item.name === selectedName);
    return Boolean(member && [member.name, ...member.aliases].some(name => people.includes(name)));
  });
}

function programMatchesFilters(program) {
  const query = keyword.value.trim().toLocaleLowerCase();
  if (query) {
    const text = [program.title, program.subprogram_name, program.description, ...(program.people || [])]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase();
    if (!text.includes(query)) return false;
  }
  return programMatchesCast(program);
}

function toggleCastFilter(name) {
  castFilter.value = castFilter.value.includes(name)
    ? castFilter.value.filter(item => item !== name)
    : [...castFilter.value, name];
}

function selectAllCast() {
  castFilter.value = NIJIGASAKI_CAST.map(member => member.name);
}

function clearCastFilter() {
  castFilter.value = [];
}

function closeCastFilter(event) {
  if (!castFilterDetails.value?.open || castFilterDetails.value.contains(event.target)) return;
  castFilterDetails.value.open = false;
  castFilterOpen.value = false;
}

function syncCastFilter(event) {
  castFilterOpen.value = event.target.open;
}

async function loadOccurrences() {
  const program = selectedProgram.value;
  if (!program) {
    occurrences.value = [];
    return;
  }
  occurrenceLoading.value = true;
  try {
    const params = new URLSearchParams();
    if (program.start_date) params.set("start", program.start_date);
    if (program.end_date) params.set("end", program.end_date);
    const query = params.toString();
    const data = await api(`/api/programs/${encodeURIComponent(program.id)}/occurrences${query ? `?${query}` : ""}`);
    occurrences.value = data.occurrences || [];
  } catch (requestError) {
    error.value = requestError.message || "单集列表加载失败";
  } finally {
    occurrenceLoading.value = false;
  }
}

async function loadPrograms() {
  loading.value = true;
  error.value = "";
  try {
    const data = await api("/api/programs");
    programs.value = data.programs || [];
    if (detailMode.value && !selectedProgram.value) error.value = "节目不存在或已被删除";
    await loadOccurrences();
  } catch (requestError) {
    error.value = requestError.message || "节目列表加载失败";
  } finally {
    loading.value = false;
  }
}

function openProgram(program) {
  router.push(`/programs/archive/${encodeURIComponent(program.id)}`);
}

function returnToList() {
  router.push("/programs/archive");
}

async function checkAdminSession() {
  try {
    const session = await api("/api/auth/session");
    adminAuthenticated.value = Boolean(session.authenticated);
  } catch {
    adminAuthenticated.value = false;
  }
  return adminAuthenticated.value;
}

async function openAdminEditor(path) {
  editAccessNotice.value = "";
  if (await checkAdminSession()) {
    router.push(path);
    return;
  }
  editAccessNotice.value = editAccessMessage;
}

watch(programId, () => {
  if (programs.value.length) {
    error.value = "";
    loadOccurrences();
  }
});

onMounted(() => {
  loadPrograms();
  checkAdminSession();
  document.addEventListener("click", closeCastFilter);
});

onUnmounted(() => document.removeEventListener("click", closeCastFilter));
</script>

<template>
  <main class="page programs-page program-archive-page">
    <div class="programs-topline">
      <div>
        <p class="eyebrow">PROGRAM ARCHIVE / READ ONLY</p>
        <h1>{{ detailMode ? "节目详情" : "已录入节目" }}</h1>
        <p class="programs-intro">查看节目资料、排期时期和单集记录；编辑操作需要管理员登录。</p>
      </div>
      <RouterLink class="back" to="/programs">← 返回播出日历</RouterLink>
    </div>

    <p v-if="error" class="state error">{{ error }}</p>
    <p v-if="editAccessNotice" class="program-edit-access-notice" role="status">
      {{ editAccessNotice }}
    </p>

    <template v-if="!detailMode">
      <section class="program-calendar-card program-readonly-list-card" :class="{ 'program-cast-filter-open': castFilterOpen }">
        <div class="section-heading">
          <div><p class="eyebrow">CURRENT ENTRIES</p><h2>已录入节目</h2></div>
          <div class="program-readonly-list-actions">
            <span class="section-count">{{ filteredPrograms.length }}<small v-if="keyword || castFilter.length"> / {{ programs.length }}</small></span>
            <button type="button" class="secondary program-action-button" @click="openAdminEditor(newAdminProgramPath)">新建节目</button>
          </div>
        </div>
        <div class="program-readonly-tools">
          <label class="program-readonly-search">
            <span>关键词</span>
            <input v-model="keyword" type="search" placeholder="搜索节目、子节目或成员" aria-label="搜索节目、子节目或成员">
          </label>
          <div class="program-calendar-filter program-cast-filter program-readonly-cast-filter">
            <span class="program-calendar-filter-label">按 Cast 筛选</span>
            <details ref="castFilterDetails" class="program-cast-filter-details" @toggle="syncCastFilter">
              <summary class="program-cast-filter-summary"><strong>{{ castFilterLabel }}</strong><b>⌄</b></summary>
              <div class="program-cast-filter-panel">
                <div class="program-cast-filter-actions">
                  <button type="button" class="secondary program-action-button" @click="selectAllCast">全选</button>
                  <button type="button" class="secondary program-action-button" @click="clearCastFilter">清空</button>
                </div>
                <div class="program-cast-tags">
                  <button v-for="member in NIJIGASAKI_CAST" :key="member.name" type="button" class="program-cast-tag" :class="{ selected: castFilter.includes(member.name) }" :aria-pressed="castFilter.includes(member.name)" @click="toggleCastFilter(member.name)"><i class="program-cast-dot" :style="{ backgroundColor: member.color }"></i>{{ member.name }}</button>
                </div>
              </div>
            </details>
          </div>
        </div>
        <p v-if="loading" class="state">正在读取节目……</p>
        <p v-else-if="!programs.length" class="muted">还没有录入节目。</p>
        <p v-else-if="!filteredPrograms.length" class="muted">当前搜索和 Cast 筛选没有匹配的节目。</p>
        <div v-else class="program-readonly-list">
          <RouterLink v-for="program in filteredPrograms" :key="program.id" class="program-readonly-item" :to="`/programs/archive/${encodeURIComponent(program.id)}`">
            <span v-if="programCast(program).length" class="program-admin-cast-line" aria-label="固定参与成员"><i v-for="member in programCast(program)" :key="member.name" :style="{ '--cast-color': member.color }"></i></span>
            <div>
              <div class="program-admin-tags"><span class="program-kind" :class="`program-kind-${program.category}`">{{ programType(program) }}</span><span class="program-subprogram-key">{{ program.subprogram_name || "主节目" }}</span><span class="program-status" :class="`status-${program.status}`">{{ programStatus(program) }}</span></div>
              <h3>{{ program.title }}</h3>
              <p>{{ scheduleLabel(program) }} · 已播 {{ program.episode_count }} 期</p>
            </div>
            <span class="program-readonly-arrow" aria-hidden="true">→</span>
          </RouterLink>
        </div>
      </section>
    </template>

    <template v-else-if="selectedProgram">
      <section class="program-calendar-card program-readonly-detail-card">
        <div class="section-heading">
          <div><p class="eyebrow">PROGRAM PROFILE</p><h2>{{ selectedProgram.title }}</h2></div>
          <div class="program-readonly-actions">
            <button type="button" class="secondary program-action-button" @click="openAdminEditor(selectedAdminProgramPath)">编辑节目</button>
            <button type="button" class="secondary program-action-button" @click="returnToList">← 已录入节目</button>
          </div>
        </div>
        <div class="program-readonly-detail-grid">
          <div>
            <div class="program-admin-tags"><span class="program-kind" :class="`program-kind-${selectedProgram.category}`">{{ programType(selectedProgram) }}</span><span class="program-subprogram-key">{{ selectedProgram.subprogram_name || "主节目" }}</span><span class="program-status" :class="`status-${selectedProgram.status}`">{{ programStatus(selectedProgram) }}</span></div>
            <p v-if="selectedProgram.description" class="program-description">{{ selectedProgram.description }}</p>
            <dl class="program-meta">
              <dt>节目属性</dt><dd>{{ formatLabel(selectedProgram) }}</dd>
              <dt>已播期数</dt><dd>{{ selectedProgram.episode_count }} 期</dd>
              <dt>首集编号</dt><dd>第 {{ selectedProgram.episode_start }} 期</dd>
              <dt>固定成员</dt>
               <dd>
                 <div v-if="selectedProgram.people?.length" class="program-people-tags"><span v-for="person in selectedProgram.people" :key="person">{{ person }}</span></div>
                 <span v-else class="muted">未填写</span>
               </dd>
                <template v-if="selectedRelatedLink">
                  <dt>{{ selectedRelatedLink.label }}</dt>
                  <dd><a class="program-meta-link" :href="selectedRelatedLink.href" target="_blank" rel="noopener noreferrer">{{ selectedRelatedLink.display }} ↗</a></dd>
                </template>
               <dt>档案区间</dt><dd>{{ selectedProgram.start_date }}{{ selectedProgram.end_date ? ` → ${selectedProgram.end_date}` : " → 至今" }}</dd>
            </dl>
          </div>
          <div class="program-readonly-periods">
            <p class="eyebrow">BROADCAST PERIODS</p>
            <ul class="program-period-summary">
              <li v-for="(period, index) in selectedProgram.periods" :key="period.id || `${period.start_date}-${index}`">
                <strong>{{ periodScheduleLabel(period) }}</strong>
                <small>{{ period.start_date }}{{ period.end_date ? ` → ${period.end_date}` : " → 至今" }} · {{ timezoneLabel(period.timezone) }}</small>
              </li>
            </ul>
          </div>
        </div>
         <section class="program-readonly-occurrences">
           <div class="section-heading">
             <div><p class="eyebrow">EPISODE INDEX</p><h3>单集节目列表</h3></div>
             <div class="program-readonly-occurrence-actions">
               <button type="button" class="secondary program-action-button" @click="openAdminEditor(selectedAdminOccurrencePath)">编辑单集</button>
               <span class="section-count">{{ visibleOccurrences.length }}</span>
             </div>
           </div>
          <p v-if="occurrenceLoading" class="state">正在读取单集排期……</p>
          <p v-else-if="!visibleOccurrences.length" class="muted">当前没有可公开显示的单集记录。</p>
          <div v-else class="program-readonly-episodes">
            <article v-for="row in visibleOccurrences" :key="`${row.id || 'generated'}-${row.original_date}-${row.original_time || 'all-day'}`" class="program-readonly-episode" :class="{ cancelled: row.status === 'cancelled' }">
               <div class="program-readonly-episode-heading"><strong>{{ episodeLabel(row) }}</strong><span class="program-status" :class="occurrenceStateClass(row)">{{ occurrenceStatus(row) }}</span><strong v-if="row.title" class="program-readonly-episode-title">{{ row.title }}</strong></div>
                <time>{{ dateLabel(row.date) }}{{ row.time ? ` · ${row.time}` : " · 全天" }}</time>
                <small>{{ occurrenceSourceLabel(row) }} · {{ row.delivery === "live" ? "直播" : "录播" }}{{ row.guests?.length ? ` · 嘉宾 ${row.guests.length} 人` : "" }}</small>
               <div v-if="occurrenceLinkItems(row).length" class="program-episode-links">
                 <template v-for="link in occurrenceLinkItems(row)" :key="link.key">
                   <a v-if="link.href" class="program-meta-link" :href="link.href" target="_blank" rel="noopener noreferrer">{{ link.label }}：{{ link.display }} ↗</a>
                   <span v-else>{{ link.label }}：{{ link.display }}</span>
                 </template>
               </div>
               <p v-if="row.note">{{ row.note }}</p>
            </article>
          </div>
        </section>
      </section>
    </template>
  </main>
</template>
