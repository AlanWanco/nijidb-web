<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { locale, localeTag, t } from "../i18n";
import { NIJIGASAKI_CAST, castColorSegments } from "../programCast";
import { occurrenceLinkItems, programAdminPath, relatedLinkItem } from "../programLinks";

const weekdayNames = computed(() => locale.value === "en"
  ? ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
  : locale.value === "ja" ? ["月", "火", "水", "木", "金", "土", "日"] : ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]);
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
const editAccessMessage = computed(() => t("需要登录管理员后才能编辑；未来将开放编辑审核。"));

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
  if (!castFilter.value.length || allCastSelected.value) return t("全部 Cast");
  return t("已选 {count} 位", { count: castFilter.value.length });
});
const filteredPrograms = computed(() => programs.value.filter(programMatchesCast));
const visibleOccurrences = computed(() => occurrences.value.filter(row => row.status !== "deleted"));
let programSearchTimer;
let programSearchRequest = 0;

function programType(program) {
  return program.category === "official" ? t("官方节目") : t("个人节目");
}

function programStatus(program) {
  return program.status === "completed" ? t("已完结") : t("进行中");
}

function formatLabel(program) {
  return `${program.format === "radio" ? t("广播") : t("有画面")} · ${program.platform === "tv" ? t("电视台") : t("网络")} · ${program.delivery === "live" ? t("直播") : t("录播")}`;
}

function periodScheduleLabel(period) {
  const time = period.schedule_time ? ` ${period.schedule_time}` : "";
  if (period.frequency === "single") return `${t("单次")}${time}`;
  if (period.frequency === "individual") return `${t("月更 · 逐期设置")}${time}`;
  const weekday = weekdayNames.value[period.weekday] || "";
  if (period.frequency === "monthly") {
    const direction = period.week_direction || (period.week_index < 0 ? "last" : "first");
    const number = period.week_number || Math.abs(period.week_index) || 1;
    const week = direction === "last" ? t("倒数第{count}周", { count: number }) : t("第{count}周", { count: number });
    return `${t("每月")}${week}${weekday}${time}`;
  }
  const interval = period.week_interval > 1 ? t("每{count}周", { count: period.week_interval }) : t("每周");
  return `${interval}${weekday}${time}`;
}

function scheduleLabel(program) {
  const periods = program.periods || [];
  if (periods.length > 1) return t("{count} 个分段时期", { count: periods.length });
  if (!periods.length) return t("未设置排期");
  return periodScheduleLabel(periods[0]);
}

function timezoneLabel(value) {
  return value ? t(timezoneLabels[value] || value) : t("东京时间");
}

function dateLabel(value) {
  if (!value) return t("未设置日期");
  const parsed = new Date(`${value}T12:00:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(localeTag(), { year: "numeric", month: "long", day: "numeric", weekday: "short" }).format(parsed);
}

function episodeLabel(row) {
  return row.special === "EX" ? t("EX 特别节目") : t("第 {count} 期", { count: row.episode });
}

function occurrenceStatus(row) {
  if (row.status === "cancelled") return t("已取消");
  const airStatus = row.aired ? t("已播出") : t("未播出");
  return row.status === "rescheduled" ? `${t("已改期")} · ${airStatus}` : airStatus;
}

function occurrenceStateClass(row) {
  if (row.status === "cancelled") return "status-cancelled";
  return row.aired ? "status-aired" : "status-upcoming";
}

function occurrenceSourceLabel(row) {
  if (row.status === "cancelled") return t("保留在排期中，标记为已取消");
  if (row.adjusted_date) return `${t("原定")} ${dateLabel(row.original_date)} · ${t("已改期")}`;
  return row.generated ? t("自动生成") : row.materialized ? t("已播出并保存") : t("已单独录入");
}

function programCast(program) {
  return castColorSegments(program.people || []);
}

function occurrenceCast(row) {
  return castColorSegments([...(selectedProgram.value?.people || []), ...(row.guests || [])], row.absent_members);
}

function programMatchesCast(program) {
  if (!castFilter.value.length || allCastSelected.value) return true;
  const people = program.people || [];
  return castFilter.value.some(selectedName => {
    const member = NIJIGASAKI_CAST.find(item => item.name === selectedName);
    return Boolean(member && [member.name, ...member.aliases].some(name => people.includes(name)));
  });
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
    error.value = requestError.message || t("单集列表加载失败");
  } finally {
    occurrenceLoading.value = false;
  }
}

async function loadPrograms() {
  const requestId = ++programSearchRequest;
  loading.value = true;
  error.value = "";
  try {
    const params = new URLSearchParams();
    if (keyword.value.trim()) params.set("q", keyword.value.trim());
    const query = params.toString();
    const data = await api(`/api/programs${query ? `?${query}` : ""}`);
    if (requestId !== programSearchRequest) return;
    programs.value = data.programs || [];
    if (detailMode.value && !selectedProgram.value) error.value = t("节目不存在或已被删除");
    await loadOccurrences();
  } catch (requestError) {
    if (requestId !== programSearchRequest) return;
    error.value = requestError.message || t("节目列表加载失败");
  } finally {
    if (requestId === programSearchRequest) loading.value = false;
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
  editAccessNotice.value = editAccessMessage.value;
}

watch(programId, () => {
  if (programs.value.length) {
    error.value = "";
    loadOccurrences();
  }
});

watch(keyword, () => {
  if (detailMode.value) return;
  window.clearTimeout(programSearchTimer);
  programSearchTimer = window.setTimeout(() => loadPrograms(), 220);
});

onMounted(() => {
  loadPrograms();
  checkAdminSession();
  document.addEventListener("click", closeCastFilter);
});

onUnmounted(() => document.removeEventListener("click", closeCastFilter));
onUnmounted(() => window.clearTimeout(programSearchTimer));
</script>

<template>
  <main class="page programs-page program-archive-page">
    <div class="programs-topline">
      <div>
        <p class="eyebrow">PROGRAM ARCHIVE / READ ONLY</p>
         <h1>{{ detailMode ? t("节目详情") : t("已录入节目") }}</h1>
         <p class="programs-intro">{{ t("查看节目资料、排期时期和单集记录；编辑操作需要管理员登录。") }}</p>
       </div>
       <RouterLink class="back" to="/programs">← {{ t("返回播出日历") }}</RouterLink>
    </div>

    <p v-if="error" class="state error">{{ error }}</p>
    <p v-if="editAccessNotice" class="program-edit-access-notice" role="status">
      {{ editAccessNotice }}
    </p>

    <template v-if="!detailMode">
       <section class="program-calendar-card program-readonly-list-card" :class="{ 'program-cast-filter-open': castFilterOpen }">
        <div class="section-heading">
           <div><p class="eyebrow">CURRENT ENTRIES</p><h2>{{ t("已录入节目") }}</h2></div>
          <div class="program-readonly-list-actions">
             <span class="section-count">{{ filteredPrograms.length }}<small v-if="castFilter.length && !allCastSelected"> / {{ programs.length }}</small></span>
             <button type="button" class="secondary program-action-button" @click="openAdminEditor(newAdminProgramPath)">{{ t("新建节目") }}</button>
          </div>
        </div>
        <div class="program-readonly-tools">
          <label class="program-readonly-search">
             <span>{{ t("关键词") }}</span>
             <input v-model="keyword" type="search" :placeholder="t('搜索节目、子节目、成员、单集标题或备注')" :aria-label="t('搜索节目、子节目、成员、单集标题或备注')">
          </label>
          <div class="program-calendar-filter program-cast-filter program-readonly-cast-filter">
             <span class="program-calendar-filter-label">{{ t("按 Cast 筛选") }}</span>
            <details ref="castFilterDetails" class="program-cast-filter-details" @toggle="syncCastFilter">
              <summary class="program-cast-filter-summary"><strong>{{ castFilterLabel }}</strong><b>⌄</b></summary>
              <div class="program-cast-filter-panel">
                <div class="program-cast-filter-actions">
                   <button type="button" class="secondary program-action-button" @click="selectAllCast">{{ t("全选") }}</button>
                   <button type="button" class="secondary program-action-button" @click="clearCastFilter">{{ t("清空") }}</button>
                </div>
                <div class="program-cast-tags">
                  <button v-for="member in NIJIGASAKI_CAST" :key="member.name" type="button" class="program-cast-tag" :class="{ selected: castFilter.includes(member.name) }" :aria-pressed="castFilter.includes(member.name)" @click="toggleCastFilter(member.name)"><i class="program-cast-dot" :style="{ backgroundColor: member.color }"></i>{{ member.name }}</button>
                </div>
              </div>
            </details>
          </div>
        </div>
         <p v-if="loading" class="state">{{ t("正在读取节目……") }}</p>
         <p v-else-if="!programs.length" class="muted">{{ t("还没有录入节目。") }}</p>
         <p v-else-if="!filteredPrograms.length" class="muted">{{ t("当前搜索和 Cast 筛选没有匹配的节目。") }}</p>
        <div v-else class="program-readonly-list">
          <RouterLink v-for="program in filteredPrograms" :key="program.id" class="program-readonly-item" :class="{ 'is-subprogram': Boolean(program.parent_id) }" :to="`/programs/archive/${encodeURIComponent(program.id)}`">
             <span v-if="programCast(program).length" class="program-admin-cast-line" :aria-label="t('固定参与成员')"><i v-for="member in programCast(program)" :key="member.name" :style="{ '--cast-color': member.color }"></i></span>
            <div>
                <div class="program-admin-tags"><span class="program-kind" :class="`program-kind-${program.category}`">{{ programType(program) }}</span><span v-if="program.parent_id" class="program-parent-title-key" :title="program.title">{{ program.title }}</span><span class="program-status" :class="`status-${program.status}`">{{ programStatus(program) }}</span></div>
               <h3>{{ program.parent_id ? program.subprogram_name : program.title }}</h3>
               <p>{{ scheduleLabel(program) }} · {{ t("已播") }} {{ program.episode_count }} {{ t("期") }}</p>
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
             <button type="button" class="secondary program-action-button" @click="returnToList">← {{ t("已录入节目") }}</button>
             <button type="button" class="secondary program-action-button" @click="openAdminEditor(selectedAdminProgramPath)">{{ t("编辑节目") }}</button>
          </div>
        </div>
        <div class="program-readonly-detail-grid">
          <div>
              <div class="program-admin-tags"><span class="program-kind" :class="`program-kind-${selectedProgram.category}`">{{ programType(selectedProgram) }}</span><span class="program-subprogram-key">{{ selectedProgram.parent_id ? selectedProgram.subprogram_name : t("主节目") }}</span><span class="program-status" :class="`status-${selectedProgram.status}`">{{ programStatus(selectedProgram) }}</span></div>
            <p v-if="selectedProgram.description" class="program-description">{{ selectedProgram.description }}</p>
            <dl class="program-meta">
               <dt>{{ t("节目属性") }}</dt><dd>{{ formatLabel(selectedProgram) }}</dd>
               <dt>{{ t("已播期数") }}</dt><dd>{{ selectedProgram.episode_count }} {{ t("期") }}</dd>
               <dt>{{ t("首集编号") }}</dt><dd>{{ t("第 {count} 期", { count: selectedProgram.episode_start }) }}</dd>
               <dt>{{ t("固定成员") }}</dt>
               <dd>
                 <div v-if="selectedProgram.people?.length" class="program-people-tags"><span v-for="person in selectedProgram.people" :key="person">{{ person }}</span></div>
                  <span v-else class="muted">{{ t("未填写") }}</span>
               </dd>
                <template v-if="selectedRelatedLink">
                  <dt>{{ selectedRelatedLink.label }}</dt>
                  <dd><a class="program-meta-link" :href="selectedRelatedLink.href" target="_blank" rel="noopener noreferrer">{{ selectedRelatedLink.display }} ↗</a></dd>
                </template>
                 <dt>{{ t("档案区间") }}</dt><dd>{{ selectedProgram.start_date }}{{ selectedProgram.end_date ? ` → ${selectedProgram.end_date}` : ` → ${t("至今")}` }}</dd>
            </dl>
          </div>
          <div class="program-readonly-periods">
             <p class="eyebrow">{{ t("排期时期") }}</p>
            <ul class="program-period-summary">
              <li v-for="(period, index) in selectedProgram.periods" :key="period.id || `${period.start_date}-${index}`">
                <strong>{{ periodScheduleLabel(period) }}</strong>
                 <small>{{ period.start_date }}{{ period.end_date ? ` → ${period.end_date}` : ` → ${t("至今")}` }} · {{ timezoneLabel(period.timezone) }}</small>
              </li>
            </ul>
          </div>
        </div>
         <section class="program-readonly-occurrences">
           <div class="section-heading">
              <div><p class="eyebrow">EPISODE INDEX</p><h3>{{ t("单集节目列表") }}</h3></div>
             <div class="program-readonly-occurrence-actions">
                <button type="button" class="secondary program-action-button" @click="openAdminEditor(selectedAdminOccurrencePath)">{{ t("编辑单集") }}</button>
               <span class="section-count">{{ visibleOccurrences.length }}</span>
             </div>
           </div>
           <p v-if="occurrenceLoading" class="state">{{ t("正在读取单集排期……") }}</p>
           <p v-else-if="!visibleOccurrences.length" class="muted">{{ t("当前没有可公开显示的单集记录。") }}</p>
           <div v-else class="program-readonly-episodes">
             <article v-for="row in visibleOccurrences" :key="`${row.id || 'generated'}-${row.original_date}-${row.original_time || 'all-day'}`" class="program-readonly-episode" :class="{ cancelled: row.status === 'cancelled' }">
                 <span v-if="occurrenceCast(row).length" class="program-readonly-episode-cast-line" role="img" :aria-label="`${t('出场成员')}：${occurrenceCast(row).map(member => member.name).join('、')}`" :title="occurrenceCast(row).map(member => member.name).join('、')"><i v-for="member in occurrenceCast(row)" :key="member.name" :style="{ '--cast-color': member.color }"></i></span>
                <div class="program-readonly-episode-heading"><strong>{{ episodeLabel(row) }}</strong><span class="program-status" :class="occurrenceStateClass(row)">{{ occurrenceStatus(row) }}</span><strong v-if="row.title" class="program-readonly-episode-title">{{ row.title }}</strong></div>
                 <time>{{ dateLabel(row.date) }}{{ row.time ? ` · ${row.time}` : ` · ${t("全天")}` }}</time>
                 <small>{{ occurrenceSourceLabel(row) }} · {{ row.delivery === "live" ? t("直播") : t("录播") }}{{ row.guests?.length ? ` · ${t("嘉宾")} ${row.guests.length} ${t("人")}` : "" }}{{ row.absent_members?.length ? ` · ${t("缺席")} ${row.absent_members.length} ${t("人")}` : "" }}</small>
               <div v-if="occurrenceLinkItems(row).length" class="program-episode-links">
                 <template v-for="link in occurrenceLinkItems(row)" :key="link.key">
                   <a v-if="link.href" class="program-meta-link" :href="link.href" target="_blank" rel="noopener noreferrer">{{ link.label }}：{{ link.display }} ↗</a>
                   <span v-else>{{ link.label }}：{{ link.display }}</span>
                 </template>
                </div>
                 <p v-if="row.absent_members?.length" class="program-episode-absence">{{ t("缺席：") }}{{ row.absent_members.join("、") }}</p>
                <p v-if="row.note">{{ row.note }}</p>
            </article>
          </div>
        </section>
      </section>
    </template>
  </main>
</template>
