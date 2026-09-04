<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import FullCalendar from "@fullcalendar/vue3";
import dayGridPlugin from "@fullcalendar/daygrid";
import interactionPlugin from "@fullcalendar/interaction";
import zhCnLocale from "@fullcalendar/core/locales/zh-cn";
import enGbLocale from "@fullcalendar/core/locales/en-gb";
import jaLocale from "@fullcalendar/core/locales/ja";
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
const castFilterDetails = ref(null);
const adminAuthenticated = ref(false);
const editAccessNotice = ref("");
const router = useRouter();
const route = useRoute();
let calendarTouchStart = null;
const calendarAnimationClass = ref("");
let calendarAnimationFrame = 0;
let calendarAnimationTimer = 0;
const requestedMonth = computed(() => routeMonth(route.params.month));
const initialMonth = requestedMonth.value || monthKey(today);
const visibleMonth = ref(initialMonth);
const jumpYear = ref(Number(initialMonth.slice(0, 4)));
const jumpMonth = ref(Number(initialMonth.slice(5, 7)));
const editAccessMessage = computed(() => t("需要登录管理员后才能编辑；未来将开放编辑审核。"));

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
  if (!filters.cast.length || allCastSelected.value) return t("全部 Cast");
  return t("已选 {count} 位", { count: filters.cast.length });
});
const visibleMonthLabel = computed(() => {
  const [year, month] = visibleMonth.value.split("-").map(Number);
  return new Intl.DateTimeFormat(localeTag(), { year: "numeric", month: "long" }).format(new Date(year, month - 1, 1, 12));
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
  timeZone: "local",
  nextDayThreshold: "24:00:00",
  firstDay: 1,
  locale: locale.value === "en" ? enGbLocale : locale.value === "ja" ? jaLocale : zhCnLocale,
  height: "auto",
  fixedWeekCount: false,
  dayMaxEvents: false,
  eventDisplay: "block",
  initialDate: `${initialMonth}-01`,
  eventContent: renderEventContent,
  headerToolbar: false,
  events: [],
  datesSet: loadCalendar,
  eventClick: selectEvent,
  eventDidMount: ({ event, el }) => {
    const props = event.extendedProps || {};
    const cast = eventCast(event);
    const fallback = props.category === "official" ? "#5979ad" : "#5b9478";
    el.style.setProperty("--program-event-color", cast[0]?.color || fallback);
    el.setAttribute("aria-label", event.title);
    el.title = event.title;
  },
  eventClassNames: ({ event }) => [
    `program-event-${event.extendedProps.category}`,
    event.extendedProps.aired ? "program-event-aired" : "program-event-upcoming",
    event.extendedProps.occurrenceStatus === "cancelled" ? "program-event-cancelled" : "",
  ].filter(Boolean),
});

watch(filteredEvents, events => {
  calendarOptions.events = events;
  if (selectedEvent.value && !events.some(event => event.id === selectedEvent.value.eventId)) closeDrawer();
}, { immediate: true });

watch(locale, nextLocale => {
  calendarOptions.locale = nextLocale === "en" ? enGbLocale : nextLocale === "ja" ? jaLocale : zhCnLocale;
});

watch(requestedMonth, month => {
  if (!month) return;
  visibleMonth.value = month;
  jumpYear.value = Number(month.slice(0, 4));
  jumpMonth.value = Number(month.slice(5, 7));
  calendarRef.value?.getApi?.().gotoDate(`${month}-01`);
}, { immediate: true });

const selectedProgram = computed(() => {
  const programId = selectedEvent.value?.programId;
  return programs.value.find(program => program.id === programId) || null;
});
const selectedAdminEditorPath = computed(() => selectedProgram.value && selectedEvent.value
  ? programAdminPath(selectedProgram.value.id, {
    panel: "occurrences",
    occurrenceId: selectedEvent.value.occurrenceId,
    occurrenceDate: selectedEvent.value.originalScheduleDate || selectedEvent.value.originalDate,
  })
  : "/admin/programs");

function programType(program) {
  return program.category === "official" ? t("官方节目") : t("个人节目");
}

function programStatus(program) {
  return program.status === "completed" ? t("已完结") : t("进行中");
}

function updateStatusLabel(program) {
  return program.update_status === "not_updated" ? t("未更新") : t("近期有更新");
}

function periodScheduleLabel(period) {
  const time = period.schedule_time ? ` ${period.schedule_time}` : "";
  if (period.frequency === "single") return `${t("单次")}${time}`;
  if (period.frequency === "individual") return `${t("月更 · 逐期设置")}${time}`;
  const weekday = weekdayNames.value[period.weekday] || "";
  if (period.frequency === "monthly") {
    const week = period.week_index > 0 ? t("第{count}周", { count: period.week_index }) : t("倒数第{count}周", { count: Math.abs(period.week_index) });
    return `${t("每月")}${week}${weekday}${time}`;
  }
  const interval = period.week_interval > 1 ? t("每{count}周", { count: period.week_interval }) : t("每周");
  return `${interval}${weekday}${time}`;
}

function periodRangeLabel(period) {
  return `${period.start_date}${period.end_date ? ` → ${period.end_date}` : ` → ${t("至今")}`}`;
}

function timezoneLabel(value) {
  return value ? t(timezoneLabels[value] || value) : t("东京时间");
}

function occurrenceAirStatus(event) {
  if (event.occurrenceStatus === "cancelled") return t("已取消");
  const airStatus = event.aired ? t("已播出") : t("未播出");
  return event.occurrenceStatus === "rescheduled" ? `${t("已改期")} · ${airStatus}` : airStatus;
}

function scheduleLabel(program) {
  const periods = program.periods || [];
  if (periods.length === 1) return periodScheduleLabel(periods[0]);
  if (periods.length > 1) return t("{count} 个分段时期", { count: periods.length });
  return t("未设置排期");
}

function formatLabel(program) {
  return `${program.format === "radio" ? t("广播") : t("有画面")} · ${program.platform === "tv" ? t("电视台") : t("网络")} · ${program.delivery === "live" ? t("直播") : t("录播")}`;
}

function fullDateLabel(value) {
  if (!value) return "";
  const parsed = new Date(`${value}T12:00:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(localeTag(), { year: "numeric", month: "long", day: "numeric" }).format(parsed);
}

const deviceTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const localDateTimeFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: deviceTimeZone,
  calendar: "gregory",
  numberingSystem: "latn",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

function localDateTimeParts(value) {
  const raw = String(value || "");
  if (!raw.includes("T")) return raw ? { date: raw.split("T", 1)[0], time: "" } : null;
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return null;
  const parts = Object.fromEntries(localDateTimeFormatter.formatToParts(parsed).map(part => [part.type, part.value]));
  return {
    date: `${parts.year}-${parts.month}-${parts.day}`,
    time: `${parts.hour}:${parts.minute}`,
  };
}

function shiftCalendarDate(value, days) {
  const raw = String(value || "").split("T", 1)[0];
  if (!raw) return "";
  const parsed = new Date(`${raw}T12:00:00`);
  if (Number.isNaN(parsed.getTime())) return raw;
  parsed.setDate(parsed.getDate() + days);
  return `${parsed.getFullYear()}-${String(parsed.getMonth() + 1).padStart(2, "0")}-${String(parsed.getDate()).padStart(2, "0")}`;
}

function monthKey(value) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}`;
}

function routeMonth(value) {
  const raw = String(value || "");
  if (!/^\d{6}$/.test(raw)) return "";
  const month = Number(raw.slice(4, 6));
  return month >= 1 && month <= 12 ? `${raw.slice(0, 4)}-${raw.slice(4, 6)}` : "";
}

function nextMonthKey(value) {
  const [year, month] = value.split("-").map(Number);
  return monthKey(new Date(year, month, 1, 12));
}

function eventDate(event) {
  const start = String(event.startStr || event.start || "");
  return event.allDay || !start.includes("T") ? start.split("T", 1)[0] : localDateTimeParts(start)?.date || start.split("T", 1)[0];
}

function eventTime(event) {
  const props = event.extendedProps || {};
  const start = String(event.startStr || event.start || "");
  return start.includes("T") ? localDateTimeParts(start)?.time || "" : props.originalTime || "";
}

function eventPeople(event) {
  const props = event.extendedProps || event || {};
  return [...(props.people || []), ...(props.guests || [])].map(person => String(person || "").trim()).filter(Boolean);
}

function eventCast(event) {
  const props = event.extendedProps || event || {};
  return castColorSegments(eventPeople(event), props.absentMembers || props.absent_members);
}

function eventMatchesFilters(event) {
  const props = event.extendedProps || {};
  if (filters.delivery && props.delivery !== filters.delivery) return false;
  if (!filters.cast.length || allCastSelected.value) return true;
  const cast = eventCast(event);
  return filters.cast.some(selectedName => {
    return cast.some(member => member.name === selectedName);
  });
}

function compareEvents(left, right) {
  return eventDate(left).localeCompare(eventDate(right))
    || (eventTime(left) || "99:99").localeCompare(eventTime(right) || "99:99")
    || String(left.title || "").localeCompare(String(right.title || ""));
}

function listDateLabel(value) {
  const parsed = new Date(`${value}T12:00:00`);
  return new Intl.DateTimeFormat(localeTag(), { month: "long", day: "numeric", weekday: "short" }).format(parsed);
}

function eventDeliveryLabel(event) {
  return event.extendedProps?.delivery === "live" ? t("直播") : t("录播");
}

function eventEpisodeLabel(event) {
  const props = event.extendedProps || {};
  return props.special === "EX" ? t("EX 特别节目") : t("第 {count} 期", { count: props.episode });
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

function closeCastFilter(event) {
  if (!castFilterDetails.value?.open || castFilterDetails.value.contains(event.target)) return;
  castFilterDetails.value.open = false;
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
  const targetMonth = `${jumpYear.value}-${String(jumpMonth.value).padStart(2, "0")}`;
  if (targetMonth !== visibleMonth.value) animateCalendar(targetMonth > visibleMonth.value ? "next" : "previous");
  calendarApi.gotoDate(new Date(jumpYear.value, jumpMonth.value - 1, 1, 12));
}

function previousMonth() {
  const calendarApi = calendarRef.value?.getApi?.();
  if (!calendarApi) return;
  animateCalendar("previous");
  calendarApi.prev();
}

function nextMonth() {
  const calendarApi = calendarRef.value?.getApi?.();
  if (!calendarApi) return;
  animateCalendar("next");
  calendarApi.next();
}

function animateCalendar(direction) {
  window.cancelAnimationFrame(calendarAnimationFrame);
  window.clearTimeout(calendarAnimationTimer);
  calendarAnimationClass.value = "";
  calendarAnimationFrame = window.requestAnimationFrame(() => {
    calendarAnimationClass.value = `program-calendar-slide-${direction}`;
    calendarAnimationTimer = window.setTimeout(() => {
      calendarAnimationClass.value = "";
      calendarAnimationTimer = 0;
    }, 260);
  });
}

function handleCalendarTouchStart(event) {
  const touch = event.touches?.[0];
  if (!touch) return;
  calendarTouchStart = { x: touch.clientX, y: touch.clientY };
}

function handleCalendarTouchEnd(event) {
  if (!calendarTouchStart) return;
  const start = calendarTouchStart;
  calendarTouchStart = null;
  const touch = event.changedTouches?.[0];
  if (!touch) return;
  const horizontalDistance = touch.clientX - start.x;
  const verticalDistance = touch.clientY - start.y;
  if (Math.abs(horizontalDistance) < 48 || Math.abs(horizontalDistance) <= Math.abs(verticalDistance)) return;
  if (horizontalDistance < 0) nextMonth();
  else previousMonth();
}

function handleCalendarTouchCancel() {
  calendarTouchStart = null;
}

function goToToday() {
  const calendarApi = calendarRef.value?.getApi?.();
  if (!calendarApi) return;
  const targetMonth = monthKey(today);
  if (targetMonth !== visibleMonth.value) animateCalendar(targetMonth > visibleMonth.value ? "next" : "previous");
  calendarApi.today();
}

function renderEventContent(info) {
  const props = info.event.extendedProps;
  const cast = eventCast(info.event);
  const deliveryLabel = props.delivery === "live" ? t("直播") : t("录播");
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

  const categoryLabel = props.category === "official" ? t("官方节目") : t("个人节目");
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
    strip.setAttribute("aria-label", `${t("出场成员")}: ${cast.map(member => member.name).join(", ")}`);
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
    const params = new URLSearchParams({
      start: shiftCalendarDate(info?.startStr, -2),
      end: shiftCalendarDate(info?.endStr, 2),
    });
    const data = await api(`/api/programs/calendar?${params}`);
    programs.value = data.programs;
    allEvents.value = data.events;
    if (selectedEvent.value && !selectedProgram.value) closeDrawer();
  } catch (requestError) {
    error.value = requestError.message || t("节目日历加载失败");
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
  const original = localDateTimeParts(props.originalStart || props.originalDate);
  selectedEvent.value = {
    eventId: event.id,
    title: event.title,
    date: eventDate(event),
    time: eventTime(event),
    ...props,
    originalDate: original?.date || props.originalDate,
    originalTime: original?.time || props.originalTime || "",
    originalScheduleDate: props.originalDate || "",
  };
  drawerOpen.value = true;
}

function closeDrawer() {
  drawerOpen.value = false;
  selectedEvent.value = null;
  editAccessNotice.value = "";
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

onMounted(() => {
  checkAdminSession();
  document.addEventListener("click", closeCastFilter);
});
onUnmounted(() => {
  document.removeEventListener("click", closeCastFilter);
  window.cancelAnimationFrame(calendarAnimationFrame);
  window.clearTimeout(calendarAnimationTimer);
});
</script>

<template>
  <main class="page programs-page" :class="{ 'programs-list-mode': viewMode === 'list' }">
    <div class="programs-topline">
       <div>
         <p class="eyebrow">PROGRAM ARCHIVE</p>
         <h1>{{ t("节目档案") }}</h1>
         <p class="programs-intro">{{ t("整理虹咲官方节目与成员个人节目，按排期查看每周和每月更新。") }}</p>
       </div>
       <RouterLink class="back" to="/music">← {{ t("返回音乐目录") }}</RouterLink>
    </div>

      <p v-if="error" class="state error">{{ error }}</p>
      <div class="program-layout">
        <section class="program-calendar-card">
          <div class="section-heading">
            <div><p class="eyebrow">SCHEDULE / CALENDAR</p><h2>{{ t("播出日历") }}</h2></div>
           <div class="program-calendar-heading-actions">
             <RouterLink class="secondary program-action-button" to="/programs/archive">{{ t("已录入节目") }}</RouterLink>
             <div class="program-view-switch" role="tablist" :aria-label="t('节目视图')">
               <button type="button" :class="{ selected: viewMode === 'calendar' }" role="tab" :aria-selected="viewMode === 'calendar'" @click="viewMode = 'calendar'">{{ t("日历") }}</button>
               <button type="button" :class="{ selected: viewMode === 'list' }" role="tab" :aria-selected="viewMode === 'list'" @click="viewMode = 'list'">{{ t("列表") }}</button>
             </div>
           </div>
          </div>
          <div class="program-calendar-tools">
            <div class="program-filter-group">
              <div class="program-calendar-filter program-cast-filter">
               <span class="program-calendar-filter-label">{{ t("筛选") }}</span>
                <details ref="castFilterDetails" class="program-cast-filter-details">
                  <summary class="program-cast-filter-summary"><strong>{{ castFilterLabel }}</strong><b>⌄</b></summary>
                  <div class="program-cast-filter-panel">
                    <div class="program-cast-filter-actions">
                      <button type="button" class="secondary program-action-button" @click="selectAllCast">{{ t("全选") }}</button>
                      <button type="button" class="secondary program-action-button" @click="clearCastFilter">{{ t("清空") }}</button>
                    </div>
                    <div class="program-cast-tags">
                      <button v-for="member in NIJIGASAKI_CAST" :key="member.name" type="button" class="program-cast-tag" :class="{ selected: filters.cast.includes(member.name) }" :aria-pressed="filters.cast.includes(member.name)" @click="toggleCastFilter(member.name)"><i class="program-cast-dot" :style="{ backgroundColor: member.color }"></i>{{ member.name }}</button>
                    </div>
                  </div>
                </details>
              </div>
             <label class="program-calendar-filter">{{ t("播出方式") }}
               <select v-model="filters.delivery" :aria-label="t('按播出方式筛选')">
                 <option value="">{{ t("直播与录播") }}</option>
                 <option value="live">{{ t("直播") }}</option>
                 <option value="recorded">{{ t("录播") }}</option>
               </select>
             </label>
             <button v-if="activeFilterCount" type="button" class="secondary program-action-button program-clear-filter" @click="clearFilters">{{ t("清除筛选") }}</button>
            </div>
            <div class="program-calendar-jump">
               <label><span class="sr-only">{{ t("年份") }}</span><select v-model.number="jumpYear" :aria-label="t('选择年份')"><option v-for="year in yearOptions" :key="year" :value="year">{{ year }} {{ t("年") }}</option></select></label>
               <label><span class="sr-only">{{ t("月份") }}</span><select v-model.number="jumpMonth" :aria-label="t('选择月份')"><option v-for="month in monthOptions" :key="month.value" :value="month.value">{{ month.value }} {{ t("月") }}</option></select></label>
               <button type="button" class="program-action-button" @click="jumpToMonth">{{ t("跳转") }}</button>
            </div>
          </div>
          <div class="program-calendar-summary">
            <span>{{ filteredProgramCount }} {{ t("个节目") }} · {{ monthEvents.length }} {{ t("期") }}</span>
            <span v-if="activeFilterCount">{{ t("已应用 {count} 项筛选", { count: activeFilterCount }) }}</span>
        </div>
        <div class="program-calendar-note">
          <span><i class="program-legend-dot official"></i>{{ t("官方节目") }}</span>
          <span><i class="program-legend-dot personal"></i>{{ t("个人节目") }}</span>
          <span><i class="program-legend-dot live"></i>{{ t("直播") }}</span>
          <span><i class="program-legend-dot recorded"></i>{{ t("录播") }}</span>
          <span><i class="program-legend-state aired"></i>{{ t("已播出") }}</span>
          <span><i class="program-legend-state upcoming"></i>{{ t("未播出") }}</span>
          <span><i class="program-legend-dot cancelled"></i>{{ t("已取消") }}</span>
          <small>{{ t("点击单集查看节目详情") }}</small>
         </div>
         <div class="program-calendar-sticky-header" :aria-label="t('日历导航和星期')">
           <div class="program-calendar-sticky-heading">
             <div class="program-calendar-sticky-nav">
                <button type="button" :aria-label="t('上个月')" :title="t('上个月')" @click="previousMonth">←</button>
                <button type="button" :aria-label="t('今天')" :title="t('今天')" @click="goToToday">{{ t("今天") }}</button>
                <button type="button" :aria-label="t('下个月')" :title="t('下个月')" @click="nextMonth">→</button>
             </div>
             <strong>{{ visibleMonthLabel }}</strong>
           </div>
            <div v-show="viewMode === 'calendar'" class="program-calendar-weekdays" :aria-label="t('星期')">
             <span v-for="weekday in weekdayNames" :key="weekday">{{ weekday }}</span>
           </div>
          </div>
          <div v-show="viewMode === 'calendar'">
            <div class="program-calendar-touch-zone" :class="calendarAnimationClass" @touchstart.passive="handleCalendarTouchStart" @touchend.passive="handleCalendarTouchEnd" @touchcancel.passive="handleCalendarTouchCancel">
              <FullCalendar ref="calendarRef" class="program-calendar" :options="calendarOptions" />
            </div>
             <p v-if="!filteredEvents.length && !loading" class="muted program-empty">{{ t("当前筛选没有匹配的节目。") }}</p>
          </div>
        <div v-if="viewMode === 'list'" class="program-list-view">
          <div class="program-list-heading">
             <div><p class="eyebrow">MONTHLY RUNNING ORDER</p><h3>{{ visibleMonthLabel }}{{ t("节目列表") }}</h3></div>
            <span class="section-count">{{ monthEvents.length }} EVENTS</span>
          </div>
           <p v-if="!listGroups.length" class="muted program-empty">{{ t("这个月没有符合筛选条件的节目。") }}</p>
          <section v-for="group in listGroups" :key="group.date" class="program-list-date-group">
             <div class="program-list-date"><strong>{{ listDateLabel(group.date) }}</strong><span>{{ group.events.length }} {{ t("期") }}</span></div>
             <button v-for="event in group.events" :key="event.id" type="button" class="program-list-event" :class="eventStateClass(event)" @click="openEvent(event)">
               <span v-if="eventCast(event).length" class="program-list-cast-line" :aria-label="t('出场成员')"><i v-for="member in eventCast(event)" :key="member.name" :style="{ '--cast-color': member.color }"></i></span>
               <span class="program-list-time">{{ eventTime(event) || t("全天") }}</span>
               <span class="program-list-main"><strong>{{ event.title }}</strong><small>{{ eventDeliveryLabel(event) }} · {{ occurrenceAirStatus(event.extendedProps) }}</small></span>
               <span v-if="eventCast(event).length" class="program-list-cast" :aria-label="t('出场成员')"><i v-for="member in eventCast(event)" :key="member.name" :style="{ '--cast-color': member.color }" :title="member.name"></i></span>
              <span class="program-list-arrow" aria-hidden="true">→</span>
            </button>
          </section>
        </div>
      </section>
    </div>

    <Transition name="program-drawer-backdrop">
      <div v-if="drawerOpen" class="program-drawer-backdrop" @click="closeDrawer"></div>
    </Transition>
    <Transition name="program-drawer-panel">
       <aside v-if="drawerOpen && selectedEvent && selectedProgram" class="program-drawer" :aria-label="t('节目详情')">
       <div class="program-drawer-header">
         <span class="eyebrow">{{ t("节目详情") }}</span>
         <button type="button" class="icon-button" :aria-label="t('关闭详情')" :title="t('关闭详情')" @click="closeDrawer">×</button>
      </div>
      <div class="program-drawer-body">
         <div class="program-drawer-tags">
            <span class="program-kind" :class="`program-kind-${selectedProgram.category}`">{{ programType(selectedProgram) }}</span>
            <span class="program-subprogram-key">{{ selectedProgram.subprogram_name || t("主节目") }}</span>
           <span class="program-status" :class="`status-${selectedProgram.status}`">{{ programStatus(selectedProgram) }}</span>
            <span v-if="selectedProgram.update_status === 'updated'" class="program-update-status">{{ updateStatusLabel(selectedProgram) }}</span>
        </div>
          <h2>{{ selectedProgram.title }}</h2>
          <div class="program-occurrence-card" :class="{ cancelled: selectedEvent.occurrenceStatus === 'cancelled' }">
             <span v-if="eventCast(selectedEvent).length" class="program-drawer-cast-line" role="img" :aria-label="`${t('出场成员')}：${eventCast(selectedEvent).map(member => member.name).join('、')}`" :title="eventCast(selectedEvent).map(member => member.name).join('、')"><i v-for="member in eventCast(selectedEvent)" :key="member.name" :style="{ '--cast-color': member.color }"></i></span>
                <span>{{ eventEpisodeLabel({ extendedProps: selectedEvent }) }} · {{ selectedEvent.delivery === "live" ? t("直播") : t("录播") }} · {{ occurrenceAirStatus(selectedEvent) }}</span>
           <strong v-if="selectedEvent.occurrenceTitle" class="program-occurrence-title">{{ selectedEvent.occurrenceTitle }}</strong>
           <strong>{{ fullDateLabel(selectedEvent.date) }}</strong>
          <b v-if="selectedEvent.time">{{ selectedEvent.time }}</b>
            <small>{{ t("显示时区") }}：{{ deviceTimeZone }}；{{ t("排期时区") }}：{{ timezoneLabel(selectedEvent.timezone) }}</small>
            <p v-if="selectedEvent.adjustedDate">{{ t("原定") }} {{ fullDateLabel(selectedEvent.originalDate) }}，{{ t("本期已改期") }}</p>
            <p v-if="selectedEvent.occurrenceStatus === 'cancelled'">{{ t("本期已取消") }}</p>
             <template v-if="selectedEvent.guests?.length">
               <p>{{ t("本期嘉宾") }}</p>
              <div class="program-guest-tags">
                <span v-for="guest in selectedEvent.guests" :key="guest">{{ guest }}</span>
              </div>
            </template>
             <p v-if="selectedEvent.absentMembers?.length" class="program-occurrence-absence">{{ t("本期缺席：") }}{{ selectedEvent.absentMembers.join("、") }}</p>
            <p v-if="selectedEvent.note">{{ selectedEvent.note }}</p>
        </div>
        <p v-if="selectedProgram.description" class="program-description">{{ selectedProgram.description }}</p>
        <dl class="program-meta">
            <dt>{{ t("节目状态") }}</dt><dd>{{ programStatus(selectedProgram) }}</dd>
              <dt>{{ t("节目属性") }}</dt><dd>{{ formatLabel(selectedProgram) }}</dd>
           <dt>{{ t("更新排期") }}</dt><dd>{{ scheduleLabel(selectedProgram) }}</dd>
           <dt>{{ t("已播期数") }}</dt><dd>{{ selectedProgram.episode_count }} {{ t("期") }}</dd>
            <dt>{{ t("参与成员") }}</dt>
           <dd>
             <div v-if="selectedProgram.people.length" class="program-people-tags">
               <span v-for="person in selectedProgram.people" :key="person">{{ person }}</span>
             </div>
              <span v-else class="muted">{{ t("未填写") }}</span>
           </dd>
           <template v-if="relatedLinkItem(selectedProgram)">
              <dt>{{ t("相关链接") }}</dt>
             <dd><a class="program-meta-link" :href="relatedLinkItem(selectedProgram).href" target="_blank" rel="noopener noreferrer">{{ relatedLinkItem(selectedProgram).display }} ↗</a></dd>
           </template>
           <template v-for="link in occurrenceLinkItems(selectedEvent)" :key="link.key">
              <dt>{{ link.label }}</dt>
              <dd><a v-if="link.href" class="program-meta-link" :href="link.href" target="_blank" rel="noopener noreferrer">{{ link.display }} ↗</a><span v-else>{{ link.display }}</span></dd>
            </template>
            <dt>{{ t("排期时期") }}</dt>
          <dd>
            <ul class="program-period-summary">
              <li v-for="(period, index) in selectedProgram.periods" :key="period.id || `${period.start_date}-${index}`">
                <strong>{{ periodScheduleLabel(period) }}</strong>
                <small>{{ periodRangeLabel(period) }} · {{ timezoneLabel(period.timezone) }}</small>
              </li>
            </ul>
           </dd>
            <dt>{{ t("档案区间") }}</dt><dd>{{ selectedProgram.start_date }}{{ selectedProgram.end_date ? ` → ${selectedProgram.end_date}` : ` → ${t("至今")}` }}</dd>
         </dl>
           <div class="program-drawer-links">
              <button type="button" class="secondary program-action-button" @click="openAdminEditor(selectedAdminEditorPath)">{{ t("编辑单集") }}</button>
              <RouterLink class="secondary program-action-button" :to="`/programs/archive/${encodeURIComponent(selectedProgram.id)}`">{{ t("查看节目详情") }}</RouterLink>
           </div>
           <p v-if="editAccessNotice" class="program-edit-access-notice" role="status">
             {{ editAccessNotice }}
           </p>
       </div>
      </aside>
    </Transition>
  </main>
</template>
