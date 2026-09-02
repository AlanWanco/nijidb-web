<script setup>
import { computed, reactive, ref, watch } from "vue";
import FullCalendar from "@fullcalendar/vue3";
import dayGridPlugin from "@fullcalendar/daygrid";
import interactionPlugin from "@fullcalendar/interaction";
import zhCnLocale from "@fullcalendar/core/locales/zh-cn";
import { api } from "../api";
import { NIJIGASAKI_CAST, castColorSegments } from "../programCast";

const weekdayNames = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const timezoneLabels = {
  "Asia/Tokyo": "东京时间",
  "Asia/Shanghai": "中国标准时间",
  "Asia/Seoul": "韩国时间",
  UTC: "协调世界时",
  "America/Los_Angeles": "美国太平洋时间",
  "America/New_York": "美国东部时间",
};
const programs = ref([]);
const allEvents = ref([]);
const selectedEvent = ref(null);
const drawerOpen = ref(false);
const loading = ref(true);
const error = ref("");
const calendarRef = ref(null);
const viewMode = ref("calendar");
const filters = reactive({ cast: [], delivery: "" });
const today = new Date();
const visibleMonth = ref(monthKey(today));
const jumpYear = ref(today.getFullYear());
const jumpMonth = ref(today.getMonth() + 1);

const monthOptions = Array.from({ length: 12 }, (_, index) => ({
  value: index + 1,
  label: `${index + 1} 月`,
}));

const filteredEvents = computed(() => allEvents.value.filter(eventMatchesFilters));
const monthEvents = computed(() => filteredEvents.value
  .filter(event => eventDate(event) >= `${visibleMonth.value}-01` && eventDate(event) < nextMonthKey(visibleMonth.value))
  .sort(compareEvents));
const listGroups = computed(() => {
  const groups = new Map();
  monthEvents.value.forEach(event => {
    const date = eventDate(event);
    if (!groups.has(date)) groups.set(date, []);
    groups.get(date).push(event);
  });
  return [...groups.entries()].map(([date, events]) => ({ date, events }));
});
const filteredProgramCount = computed(() => new Set(monthEvents.value.map(event => event.extendedProps?.programId)).size);
const allCastSelected = computed(() => filters.cast.length === NIJIGASAKI_CAST.length);
const activeFilterCount = computed(() => Number(filters.cast.length > 0 && !allCastSelected.value) + Number(Boolean(filters.delivery)));
const castFilterLabel = computed(() => {
  if (!filters.cast.length || allCastSelected.value) return "全部Cast";
  return `已选 ${filters.cast.length} 位`;
});
const visibleMonthLabel = computed(() => {
  const [year, month] = visibleMonth.value.split("-").map(Number);
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long" }).format(new Date(year, month - 1, 1, 12));
});
const yearOptions = computed(() => {
  const years = new Set();
  for (let year = today.getFullYear() - 5; year <= today.getFullYear() + 5; year += 1) years.add(year);
  programs.value.forEach(program => {
    [program.start_date, program.end_date, ...(program.periods || []).flatMap(period => [period.start_date, period.end_date])]
      .filter(Boolean)
      .forEach(value => years.add(Number(String(value).slice(0, 4))));
  });
  years.add(Number(visibleMonth.value.slice(0, 4)));
  return [...years].filter(Boolean).sort((left, right) => left - right);
});

const calendarOptions = reactive({
  plugins: [dayGridPlugin, interactionPlugin],
  initialView: "dayGridMonth",
  firstDay: 1,
  locale: zhCnLocale,
  height: "auto",
  fixedWeekCount: false,
  dayMaxEvents: false,
  eventDisplay: "block",
  eventContent: renderEventContent,
  headerToolbar: { left: "prev,next today", center: "title", right: "" },
  events: [],
  datesSet: loadCalendar,
  eventClick: selectEvent,
  eventDidMount: ({ event, el }) => {
    const props = event.extendedProps || {};
    const cast = castColorSegments(eventPeople(event));
    const fallback = props.category === "official" ? "#5979ad" : "#5b9478";
    el.style.setProperty("--program-event-color", cast[0]?.color || fallback);
  },
  eventClassNames: ({ event }) => [
    `program-event-${event.extendedProps.category}`,
    event.extendedProps.aired ? "program-event-aired" : "program-event-upcoming",
    event.extendedProps.occurrenceStatus === "cancelled" ? "program-event-cancelled" : "",
    event.extendedProps.updateStatus === "not_updated" ? "program-event-not-updated" : "",
  ].filter(Boolean),
});

watch(filteredEvents, events => {
  calendarOptions.events = events;
  if (selectedEvent.value && !events.some(event => event.id === selectedEvent.value.eventId)) closeDrawer();
}, { immediate: true });

const selectedProgram = computed(() => {
  const programId = selectedEvent.value?.programId;
  return programs.value.find(program => program.id === programId) || null;
});

function programType(program) {
  return program.category === "official" ? "官方节目" : "个人节目";
}

function programStatus(program) {
  return program.status === "completed" ? "已完结" : "进行中";
}

function updateStatusLabel(program) {
  return program.update_status === "not_updated" ? "未更新" : "近期有更新";
}

function periodScheduleLabel(period) {
  const time = period.schedule_time ? ` ${period.schedule_time}` : "";
  if (period.frequency === "single") return `单次${time}`;
  if (period.frequency === "individual") return `逐期设置${time}`;
  const weekday = weekdayNames[period.weekday] || "";
  if (period.frequency === "monthly") {
    const week = period.week_index > 0 ? `第${period.week_index}周` : `倒数第${Math.abs(period.week_index)}周`;
    return `每月${week}${weekday}${time}`;
  }
  const interval = period.week_interval > 1 ? `每${period.week_interval}周` : "每周";
  return `${interval}${weekday}${time}`;
}

function periodRangeLabel(period) {
  return `${period.start_date}${period.end_date ? ` → ${period.end_date}` : " → 至今"}`;
}

function timezoneLabel(value) {
  return timezoneLabels[value] || value || "东京时间";
}

function occurrenceAirStatus(event) {
  if (event.occurrenceStatus === "cancelled") return "已取消";
  const airStatus = event.aired ? "已播出" : "未播出";
  return event.occurrenceStatus === "rescheduled" ? `已改期 · ${airStatus}` : airStatus;
}

function scheduleLabel(program) {
  const periods = program.periods || [];
  if (periods.length === 1) return periodScheduleLabel(periods[0]);
  if (periods.length > 1) return `${periods.length} 个分段时期`;
  return "未设置排期";
}

function formatLabel(program) {
  return `${program.format === "radio" ? "广播" : "有画面"} · ${program.platform === "tv" ? "电视台" : "网络"} · ${program.delivery === "live" ? "直播" : "录播"}`;
}

function fullDateLabel(value) {
  if (!value) return "";
  const parsed = new Date(`${value}T12:00:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric" }).format(parsed);
}

function monthKey(value) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}`;
}

function nextMonthKey(value) {
  const [year, month] = value.split("-").map(Number);
  return monthKey(new Date(year, month, 1, 12));
}

function eventDate(event) {
  return String(event.startStr || event.start || "").split("T", 1)[0];
}

function eventTime(event) {
  const props = event.extendedProps || {};
  if (props.adjustedTime) return props.adjustedTime;
  const start = String(event.startStr || event.start || "");
  return start.includes("T") ? start.slice(11, 16) : props.originalTime || "";
}

function eventPeople(event) {
  const props = event.extendedProps || {};
  return [...(props.people || []), ...(props.guests || [])].map(person => String(person || "").trim()).filter(Boolean);
}

function eventCast(event) {
  return castColorSegments(eventPeople(event));
}

function eventMatchesFilters(event) {
  const props = event.extendedProps || {};
  if (filters.delivery && props.delivery !== filters.delivery) return false;
  if (!filters.cast.length || allCastSelected.value) return true;
  const people = eventPeople(event);
  return filters.cast.some(selectedName => {
    const member = NIJIGASAKI_CAST.find(item => item.name === selectedName);
    return Boolean(member && [member.name, ...member.aliases].some(name => people.includes(name)));
  });
}

function compareEvents(left, right) {
  return eventDate(left).localeCompare(eventDate(right))
    || (eventTime(left) || "99:99").localeCompare(eventTime(right) || "99:99")
    || String(left.title || "").localeCompare(String(right.title || ""));
}

function listDateLabel(value) {
  const parsed = new Date(`${value}T12:00:00`);
  return new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "short" }).format(parsed);
}

function eventDeliveryLabel(event) {
  return event.extendedProps?.delivery === "live" ? "直播" : "录播";
}

function eventStateClass(event) {
  const props = event.extendedProps || {};
  return {
    aired: props.aired,
    upcoming: !props.aired,
    cancelled: props.occurrenceStatus === "cancelled",
    rescheduled: props.occurrenceStatus === "rescheduled",
  };
}

function clearFilters() {
  filters.cast = [];
  filters.delivery = "";
}

function toggleCastFilter(name) {
  filters.cast = filters.cast.includes(name)
    ? filters.cast.filter(item => item !== name)
    : [...filters.cast, name];
}

function selectAllCast() {
  filters.cast = NIJIGASAKI_CAST.map(member => member.name);
}

function clearCastFilter() {
  filters.cast = [];
}

function updateVisibleMonth(value) {
  if (!(value instanceof Date) || Number.isNaN(value.getTime())) return;
  visibleMonth.value = monthKey(value);
  jumpYear.value = value.getFullYear();
  jumpMonth.value = value.getMonth() + 1;
}

function jumpToMonth() {
  const calendarApi = calendarRef.value?.getApi?.();
  if (!calendarApi) return;
  calendarApi.gotoDate(new Date(jumpYear.value, jumpMonth.value - 1, 1, 12));
}

function renderEventContent(info) {
  const props = info.event.extendedProps;
  const cast = castColorSegments([...(props.people || []), ...(props.guests || [])]);
  const deliveryLabel = props.delivery === "live" ? "直播" : "录播";
  const content = document.createElement("div");
  content.className = "program-event-content";

  const markerStack = document.createElement("span");
  markerStack.className = "program-event-markers";

  const deliveryDot = document.createElement("span");
  deliveryDot.className = `program-event-delivery ${props.delivery === "live" ? "live" : "recorded"}`;
  deliveryDot.setAttribute("role", "img");
  deliveryDot.setAttribute("aria-label", deliveryLabel);
  deliveryDot.title = deliveryLabel;
  markerStack.append(deliveryDot);

  const categoryLabel = props.category === "official" ? "官方节目" : "个人节目";
  const categoryMarker = document.createElement("span");
  categoryMarker.className = `program-event-category-marker ${props.category === "official" ? "official" : "personal"}`;
  categoryMarker.setAttribute("role", "img");
  categoryMarker.setAttribute("aria-label", categoryLabel);
  categoryMarker.title = categoryLabel;
  markerStack.append(categoryMarker);

  const details = document.createElement("div");
  details.className = "program-event-details";

  const strip = document.createElement("span");
  strip.className = `program-event-color-strip${cast.length ? "" : " empty"}`;
  if (cast.length) {
    strip.setAttribute("role", "img");
    strip.setAttribute("aria-label", `出场成员：${cast.map(member => member.name).join("、")}`);
    strip.title = cast.map(member => member.name).join("、");
    cast.forEach(member => {
      const segment = document.createElement("span");
      segment.className = "program-event-color-segment";
      segment.style.backgroundColor = member.color;
      strip.append(segment);
    });
  }
  details.append(strip);

  const label = document.createElement("span");
  label.className = "program-event-label";
  if (info.timeText) {
    const time = document.createElement("span");
    time.className = "program-event-time";
    time.textContent = info.timeText;
    label.append(time, document.createTextNode(" "));
  }
  label.append(document.createTextNode(info.event.title));
  details.append(label);
  content.append(markerStack, details);
  return { domNodes: [content] };
}

async function loadCalendar(info) {
  updateVisibleMonth(info?.view?.currentStart);
  loading.value = true;
  error.value = "";
  try {
    const params = new URLSearchParams({ start: info?.startStr || "", end: info?.endStr || "" });
    const data = await api(`/api/programs/calendar?${params}`);
    programs.value = data.programs;
    allEvents.value = data.events;
    if (selectedEvent.value && !selectedProgram.value) closeDrawer();
  } catch (requestError) {
    error.value = requestError.message || "节目日历加载失败";
  } finally {
    loading.value = false;
  }
}

function selectEvent(info) {
  info.jsEvent.preventDefault();
  openEvent(info.event);
}

function openEvent(event) {
  const props = event.extendedProps || {};
  selectedEvent.value = {
    eventId: event.id,
    title: event.title,
    date: eventDate(event),
    time: eventTime(event),
    ...props,
  };
  drawerOpen.value = true;
}

function closeDrawer() {
  drawerOpen.value = false;
  selectedEvent.value = null;
}
</script>

<template>
  <main class="page programs-page">
    <div class="programs-topline">
      <div>
        <p class="eyebrow">PROGRAM ARCHIVE</p>
        <h1>节目档案</h1>
        <p class="programs-intro">整理虹咲官方节目与成员个人节目，按排期查看每周和每月更新。</p>
      </div>
      <RouterLink class="back" to="/">← 返回音乐目录</RouterLink>
    </div>

    <p v-if="error" class="state error">{{ error }}</p>
    <div class="program-layout">
      <section class="program-calendar-card">
        <div class="section-heading">
          <div><p class="eyebrow">SCHEDULE / CALENDAR</p><h2>播出日历</h2></div>
          <div class="program-view-switch" role="tablist" aria-label="节目视图">
            <button type="button" :class="{ selected: viewMode === 'calendar' }" role="tab" :aria-selected="viewMode === 'calendar'" @click="viewMode = 'calendar'">日历</button>
            <button type="button" :class="{ selected: viewMode === 'list' }" role="tab" :aria-selected="viewMode === 'list'" @click="viewMode = 'list'">列表</button>
         </div>
         </div>
        <div class="program-calendar-tools">
          <div class="program-filter-group">
            <details class="program-cast-filter">
               <summary class="program-cast-filter-summary"><span>筛选</span><strong>{{ castFilterLabel }}</strong><b>⌄</b></summary>
              <div class="program-cast-filter-panel">
                <div class="program-cast-filter-actions">
                  <button type="button" class="secondary program-action-button" @click="selectAllCast">全选</button>
                  <button type="button" class="secondary program-action-button" @click="clearCastFilter">清空</button>
                </div>
                <div class="program-cast-tags">
                  <button v-for="member in NIJIGASAKI_CAST" :key="member.name" type="button" class="program-cast-tag" :class="{ selected: filters.cast.includes(member.name) }" :aria-pressed="filters.cast.includes(member.name)" @click="toggleCastFilter(member.name)"><i class="program-cast-dot" :style="{ backgroundColor: member.color }"></i>{{ member.name }}</button>
                </div>
              </div>
            </details>
            <label class="program-calendar-filter">播出方式
              <select v-model="filters.delivery" aria-label="按播出方式筛选">
                <option value="">直播与录播</option>
                <option value="live">直播</option>
                <option value="recorded">录播</option>
              </select>
             </label>
             <button v-if="activeFilterCount" type="button" class="secondary program-action-button program-clear-filter" @click="clearFilters">清除筛选</button>
           </div>
           <div class="program-calendar-jump">
             <label><span class="sr-only">年份</span><select v-model.number="jumpYear" aria-label="选择年份"><option v-for="year in yearOptions" :key="year" :value="year">{{ year }} 年</option></select></label>
             <label><span class="sr-only">月份</span><select v-model.number="jumpMonth" aria-label="选择月份"><option v-for="month in monthOptions" :key="month.value" :value="month.value">{{ month.label }}</option></select></label>
             <button type="button" class="program-action-button" @click="jumpToMonth">跳转</button>
           </div>
         </div>
        <div class="program-calendar-summary">
          <strong>{{ visibleMonthLabel }}</strong>
          <span>{{ filteredProgramCount }} 个节目 · {{ monthEvents.length }} 期</span>
          <span v-if="activeFilterCount">已应用 {{ activeFilterCount }} 项筛选</span>
        </div>
        <div class="program-calendar-note">
          <span><i class="program-legend-dot official"></i>官方节目</span>
          <span><i class="program-legend-dot personal"></i>个人节目</span>
          <span><i class="program-legend-dot live"></i>直播</span>
          <span><i class="program-legend-dot recorded"></i>录播</span>
          <span><i class="program-legend-state aired"></i>已播出</span>
          <span><i class="program-legend-state upcoming"></i>未播出</span>
          <span><i class="program-legend-dot cancelled"></i>已取消</span>
          <small>点击单集查看节目详情</small>
        </div>
        <div v-show="viewMode === 'calendar'">
           <FullCalendar ref="calendarRef" class="program-calendar" :options="calendarOptions" />
          <p v-if="!filteredEvents.length && !loading" class="muted program-empty">当前筛选没有匹配的节目。</p>
        </div>
        <div v-if="viewMode === 'list'" class="program-list-view">
          <div class="program-list-heading">
            <div><p class="eyebrow">MONTHLY RUNNING ORDER</p><h3>{{ visibleMonthLabel }}节目列表</h3></div>
            <span class="section-count">{{ monthEvents.length }} EVENTS</span>
          </div>
          <p v-if="!listGroups.length" class="muted program-empty">这个月没有符合筛选条件的节目。</p>
          <section v-for="group in listGroups" :key="group.date" class="program-list-date-group">
            <div class="program-list-date"><strong>{{ listDateLabel(group.date) }}</strong><span>{{ group.events.length }} 期</span></div>
            <button v-for="event in group.events" :key="event.id" type="button" class="program-list-event" :class="eventStateClass(event)" @click="openEvent(event)">
              <span class="program-list-time">{{ eventTime(event) || "全天" }}</span>
              <span class="program-list-main"><strong>{{ event.title }}</strong><small>{{ eventDeliveryLabel(event) }} · {{ occurrenceAirStatus(event.extendedProps) }}</small></span>
              <span v-if="eventCast(event).length" class="program-list-cast" aria-label="出场成员"><i v-for="member in eventCast(event)" :key="member.name" :style="{ '--cast-color': member.color }" :title="member.name"></i></span>
              <span class="program-list-arrow" aria-hidden="true">→</span>
            </button>
          </section>
        </div>
      </section>
    </div>

    <div v-if="drawerOpen" class="program-drawer-backdrop" @click="closeDrawer"></div>
    <aside v-if="drawerOpen && selectedEvent && selectedProgram" class="program-drawer" aria-label="节目详情">
      <div class="program-drawer-header">
        <span class="eyebrow">PROGRAM DETAILS</span>
        <button type="button" class="icon-button" aria-label="关闭详情" title="关闭详情" @click="closeDrawer">×</button>
      </div>
      <div class="program-drawer-body">
         <div class="program-drawer-tags">
            <span class="program-kind" :class="`program-kind-${selectedProgram.category}`">{{ programType(selectedProgram) }}</span>
           <span class="program-subprogram-key">{{ selectedProgram.subprogram_name || "主节目" }}</span>
           <span class="program-status" :class="`status-${selectedProgram.status}`">{{ programStatus(selectedProgram) }}</span>
          <span v-if="selectedProgram.update_status === 'not_updated'" class="program-update-status">{{ updateStatusLabel(selectedProgram) }}</span>
        </div>
        <h2>{{ selectedProgram.title }}</h2>
        <div class="program-occurrence-card" :class="{ cancelled: selectedEvent.occurrenceStatus === 'cancelled' }">
           <span>第 {{ selectedEvent.episode }} 期 · {{ occurrenceAirStatus(selectedEvent) }}</span>
          <strong>{{ fullDateLabel(selectedEvent.date) }}</strong>
          <b v-if="selectedEvent.time">{{ selectedEvent.time }}</b>
          <small>{{ timezoneLabel(selectedEvent.timezone) }}</small>
           <p v-if="selectedEvent.adjustedDate">原定 {{ fullDateLabel(selectedEvent.originalDate) }}，本期已改期</p>
           <p v-if="selectedEvent.occurrenceStatus === 'cancelled'">本期已取消</p>
           <template v-if="selectedEvent.guests?.length">
             <p>本期嘉宾</p>
             <div class="program-guest-tags">
               <span v-for="guest in selectedEvent.guests" :key="guest">{{ guest }}</span>
             </div>
           </template>
           <p v-if="selectedEvent.note">{{ selectedEvent.note }}</p>
        </div>
        <p v-if="selectedProgram.description" class="program-description">{{ selectedProgram.description }}</p>
        <dl class="program-meta">
           <dt>节目状态</dt><dd>{{ programStatus(selectedProgram) }}</dd>
            <template v-if="selectedProgram.parent_id">
              <dt>子节目名称</dt><dd>{{ selectedProgram.subprogram_name }}</dd>
            </template>
            <dt>节目属性</dt><dd>{{ formatLabel(selectedProgram) }}</dd>
          <dt>更新排期</dt><dd>{{ scheduleLabel(selectedProgram) }}</dd>
          <dt>已播期数</dt><dd>{{ selectedProgram.episode_count }} 期</dd>
          <dt>参与成员</dt>
          <dd>
            <div v-if="selectedProgram.people.length" class="program-people-tags">
              <span v-for="person in selectedProgram.people" :key="person">{{ person }}</span>
            </div>
            <span v-else class="muted">未填写</span>
          </dd>
          <dt>排期时期</dt>
          <dd>
            <ul class="program-period-summary">
              <li v-for="(period, index) in selectedProgram.periods" :key="period.id || `${period.start_date}-${index}`">
                <strong>{{ periodScheduleLabel(period) }}</strong>
                <small>{{ periodRangeLabel(period) }} · {{ timezoneLabel(period.timezone) }}</small>
              </li>
            </ul>
          </dd>
          <dt>档案区间</dt><dd>{{ selectedProgram.start_date }}{{ selectedProgram.end_date ? ` → ${selectedProgram.end_date}` : " → 至今" }}</dd>
        </dl>
        <a v-if="selectedProgram.official_url" class="source" :href="selectedProgram.official_url" target="_blank" rel="noopener noreferrer">查看节目来源 ↗</a>
      </div>
    </aside>
  </main>
</template>
