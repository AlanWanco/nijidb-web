<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import VueDatePicker from "@vuepic/vue-datepicker";
import "@vuepic/vue-datepicker/dist/main.css";
import { api } from "../api";
import { NIJIGASAKI_CAST, castColorSegments } from "../programCast";

const weekdayNames = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const castCandidates = NIJIGASAKI_CAST.map(member => member.name);
const weekOptions = [1, 2, 3, 4, 5];
const timezoneOptions = [
  { value: "Asia/Tokyo", label: "东京时间（UTC+9）" },
  { value: "Asia/Shanghai", label: "中国标准时间（UTC+8）" },
  { value: "Asia/Seoul", label: "韩国时间（UTC+9）" },
  { value: "UTC", label: "协调世界时（UTC）" },
  { value: "America/Los_Angeles", label: "美国太平洋时间" },
  { value: "America/New_York", label: "美国东部时间" },
];
const router = useRouter();
const route = useRoute();
const programs = ref([]);
const programSearch = ref("");
const programCastFilter = ref([]);
const programCastFilterDetails = ref(null);
const programCastFilterOpen = ref(false);
const editingId = ref("");
const editorOpen = ref(false);
const activePanel = ref("list");
const loading = ref(true);
const saving = ref(false);
const deletingId = ref("");
const occurrenceLoading = ref(false);
const occurrenceSaving = ref(false);
const occurrenceRows = ref([]);
const occurrenceListRef = ref(null);
const occurrenceEditorRef = ref(null);
const customPerson = ref("");
const occurrenceGuestInput = ref("");
const message = ref("");
const error = ref("");
let toastTimer = 0;

function scheduleToastDismiss() {
  window.clearTimeout(toastTimer);
  if (!message.value && !error.value) return;
  toastTimer = window.setTimeout(() => {
    message.value = "";
    error.value = "";
    toastTimer = 0;
  }, 4000);
}

function blankProgram() {
  return {
    title: "",
    parent_id: "",
    subprogram_name: "主节目",
    category: "personal",
    format: "video",
    platform: "network",
    delivery: "recorded",
    auto_generate: true,
    episode_start: 1,
    people: [],
    official_url: "",
    description: "",
    periods: [blankPeriod()],
  };
}

function blankPeriod() {
  return {
    id: "",
    start_date: "",
    end_date: "",
    frequency: "weekly",
    week_interval: 1,
    week_direction: "first",
    week_number: 1,
    weekday: 0,
    schedule_time: "",
    timezone: "Asia/Tokyo",
  };
}

function blankOccurrence() {
  return {
    id: "",
    original_date: "",
    generated_date: "",
    original_time: "",
    delivery: "",
    effective_date: "",
    schedule_shift_days: 0,
    shift_following_days: 0,
    source_url: "",
    mirror_url: "",
    subtitle_url: "",
    status: "scheduled",
    special: "",
    adjusted_date: "",
    adjusted_time: "",
    note: "",
    guests: [],
    generated: false,
    individual: false,
    materialized: false,
    manual: false,
    timezone: "",
  };
}

const form = reactive(blankProgram());
const occurrenceDraft = reactive(blankOccurrence());
const peopleOptions = computed(() => castCandidates);
const allProgramCastSelected = computed(() => programCastFilter.value.length === castCandidates.length);
const programCastFilterLabel = computed(() => {
  if (!programCastFilter.value.length || allProgramCastSelected.value) return "全部 Cast";
  return `已选 ${programCastFilter.value.length} 位`;
});
const filteredPrograms = computed(() => programs.value.filter(programMatchesFilters));
const customPeople = computed(() => form.people.filter(person => !castCandidates.includes(person)));
const occurrenceOriginalLocked = computed(() => Boolean(!occurrenceDraft.individual && (occurrenceDraft.generated || occurrenceDraft.id)));
const parentProgramTitle = computed(() => programs.value.find(program => program.id === form.parent_id)?.title || form.title || "当前主节目");
const generatedOccurrenceCount = computed(() => occurrenceRows.value.filter(row => row.generated).length);
const materializedOccurrenceCount = computed(() => occurrenceRows.value.filter(row => row.materialized && row.status !== "deleted").length);
const deletedOccurrenceCount = computed(() => occurrenceRows.value.filter(row => row.status === "deleted").length);
const adjustedOccurrenceCount = computed(() => occurrenceRows.value.filter(row => !row.generated && !row.materialized && row.status !== "deleted").length);
const occurrenceTimezone = computed(() => occurrenceDraft.timezone || form.periods.find(period => period.start_date && occurrenceDraft.original_date >= period.start_date && (!period.end_date || occurrenceDraft.original_date <= period.end_date))?.timezone || form.periods[0]?.timezone || "Asia/Tokyo");
const occurrenceTimezoneLabel = computed(() => timezoneOptions.find(option => option.value === occurrenceTimezone.value)?.label || occurrenceTimezone.value);
const occurrencePeriod = computed(() => {
  const anchor = occurrenceDraft.generated_date || occurrenceDraft.original_date;
  return form.periods.find(period => period.start_date && anchor >= period.start_date && (!period.end_date || anchor <= period.end_date)) || null;
});
const canShiftFollowing = computed(() => occurrencePeriod.value?.frequency === "weekly" && Number(occurrencePeriod.value.week_interval) === 2);
const occurrenceRescheduleBaseDate = computed(() => occurrenceDraft.original_date
  ? addDays(occurrenceDraft.original_date, Number(occurrenceDraft.schedule_shift_days) || 0)
  : occurrenceDraft.effective_date || occurrenceDraft.adjusted_date || "");
const occurrenceDeleteLabel = computed(() => occurrenceDraft.status === "deleted" ? "恢复单集" : "删除单集");
const selectedOccurrenceIndex = computed(() => {
  if (!occurrenceDraft.original_date) return -1;
  return occurrenceRows.value.findIndex(row => row.original_date === occurrenceDraft.original_date
    && (occurrenceDraft.id ? String(row.id) === String(occurrenceDraft.id) : row.generated_date === occurrenceDraft.generated_date));
});
const canPreviousOccurrence = computed(() => selectedOccurrenceIndex.value > 0);
const canNextOccurrence = computed(() => selectedOccurrenceIndex.value >= 0 && selectedOccurrenceIndex.value < occurrenceRows.value.length - 1);
const requestedProgramId = computed(() => typeof route.query.program === "string" ? route.query.program : "");
const requestedPanel = computed(() => typeof route.query.panel === "string" ? route.query.panel : "edit");
const requestedOccurrenceId = computed(() => typeof route.query.occurrence === "string" ? route.query.occurrence : "");
const requestedOccurrenceDate = computed(() => typeof route.query.date === "string" ? route.query.date : "");
const requestedNewProgram = computed(() => route.query.new === "1");

watch(() => occurrenceDraft.original_date, value => {
  if (occurrenceDraft.status === "rescheduled" && !occurrenceDraft.adjusted_date) occurrenceDraft.adjusted_date = value;
});
const panelOrder = ["list", "edit", "occurrences"];
const panelIndex = computed(() => panelOrder.indexOf(activePanel.value));
const panelTitle = computed(() => {
  if (activePanel.value === "edit") return editingId.value ? "编辑节目" : "添加节目";
  if (activePanel.value === "occurrences") return "单集列表";
  return "已录入节目";
});
const panelDescription = computed(() => {
  if (activePanel.value === "edit") return "编辑节目基本资料、参与成员和排期规则；内容会保留在当前页面状态中。";
  if (activePanel.value === "occurrences") return "查看自动生成的单集，并单独处理改期、取消、删除、补录和临时嘉宾。";
  return "从节目索引中选择要编辑的条目，或直接新建一个主节目。";
});
const canMovePrevious = computed(() => panelIndex.value > 0);
const canMoveNext = computed(() => activePanel.value === "list" || (activePanel.value === "edit" && Boolean(editingId.value)));

const occurrenceItemRefs = new Map();

function programType(program) {
  return program.category === "official" ? "官方节目" : "个人节目";
}

function programStatus(program) {
  return program.status === "completed" ? "已完结" : "进行中";
}

function updateStatusLabel(program) {
  return program.update_status === "not_updated" ? "未更新" : "近期有更新";
}

function programMatchesSearch(program) {
  const query = programSearch.value.trim().toLocaleLowerCase();
  if (!query) return true;
  const text = [program.title, program.subprogram_name, program.description, ...(program.people || [])]
    .filter(Boolean)
    .join(" ")
    .toLocaleLowerCase();
  return text.includes(query);
}

function programMatchesCast(program) {
  if (!programCastFilter.value.length || allProgramCastSelected.value) return true;
  const people = program.people || [];
  return programCastFilter.value.some(selectedName => {
    const member = NIJIGASAKI_CAST.find(item => item.name === selectedName);
    return Boolean(member && [member.name, ...member.aliases].some(name => people.includes(name)));
  });
}

function programMatchesFilters(program) {
  return programMatchesSearch(program) && programMatchesCast(program);
}

function toggleProgramCastFilter(name) {
  programCastFilter.value = programCastFilter.value.includes(name)
    ? programCastFilter.value.filter(item => item !== name)
    : [...programCastFilter.value, name];
}

function selectAllProgramCast() {
  programCastFilter.value = NIJIGASAKI_CAST.map(member => member.name);
}

function clearProgramCastFilter() {
  programCastFilter.value = [];
}

function closeProgramCastFilter(event) {
  if (!programCastFilterDetails.value?.open || programCastFilterDetails.value.contains(event.target)) return;
  programCastFilterDetails.value.open = false;
  programCastFilterOpen.value = false;
}

function syncProgramCastFilter(event) {
  programCastFilterOpen.value = event.target.open;
}

function occurrenceStatus(row) {
  if (row.status === "deleted") return "已删除";
  if (row.status === "cancelled") return "已取消";
  const airStatus = row.aired ? "已播出" : "未播出";
  return row.status === "rescheduled" ? `已改期 · ${airStatus}` : airStatus;
}

function episodeLabel(episode, special = "") {
  return special === "EX" ? "EX 特别节目" : `第 ${episode} 期`;
}

function occurrenceEpisodeLabel(row) {
  const label = episodeLabel(row.episode, row.special);
  return ["cancelled", "deleted"].includes(row.status) ? `原定 ${label}` : label;
}

function scheduleLabel(program) {
  const periods = program.periods || [];
  if (periods.length > 1) return `${periods.length} 个分段时期`;
  if (!periods.length) return "未设置排期";
  return periodScheduleLabel(periods[0]);
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

function formatLabel(program) {
  return `${program.format === "radio" ? "广播" : "有画面"} · ${program.platform === "tv" ? "电视台" : "网络"} · ${program.delivery === "live" ? "直播" : "录播"}`;
}

function showError(requestError) {
  if (requestError.status === 401) {
    router.replace({ path: "/admin/login", query: { redirect: "/admin/programs" } });
    return;
  }
  error.value = requestError.message || "请求失败";
}

function showPanel(panel) {
  if (panel === "occurrences" && !editingId.value) return;
  activePanel.value = panel;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function previousPanel() {
  if (!canMovePrevious.value) return;
  showPanel(panelOrder[panelIndex.value - 1]);
}

function nextPanel() {
  if (activePanel.value === "list" && !editorOpen.value) {
    startNewProgram();
    return;
  }
  if (!canMoveNext.value) return;
  showPanel(panelOrder[panelIndex.value + 1]);
}

watch([message, error], scheduleToastDismiss);

async function loadPrograms() {
  loading.value = true;
  try {
    const data = await api("/api/programs");
    programs.value = data.programs;
    return true;
  } catch (requestError) {
    showError(requestError);
    return false;
  } finally {
    loading.value = false;
  }
}

async function openRequestedProgram() {
  if (requestedNewProgram.value) {
    startNewProgram();
    return;
  }
  if (!requestedProgramId.value) return;
  const program = programs.value.find(item => item.id === requestedProgramId.value);
  if (!program) {
    error.value = "节目不存在或已被删除";
    return;
  }
  await editProgram(program);
  if (requestedPanel.value !== "occurrences" && !requestedOccurrenceId.value && !requestedOccurrenceDate.value) return;

  activePanel.value = "occurrences";
  if (!requestedOccurrenceId.value && !requestedOccurrenceDate.value) {
    await focusOccurrenceEditor();
    return;
  }
  const row = occurrenceRows.value.find(item => requestedOccurrenceId.value && String(item.id) === requestedOccurrenceId.value)
    || occurrenceRows.value.find(item => requestedOccurrenceDate.value && item.original_date === requestedOccurrenceDate.value);
  if (row) {
    editOccurrence(row);
    await focusOccurrenceEditor(row);
  } else {
    error.value = "单集不存在或已不在当前排期中";
    await focusOccurrenceEditor();
  }
}

function applyProgram(program) {
  Object.assign(form, {
    title: program.title || "",
    parent_id: program.parent_id || "",
    subprogram_name: program.subprogram_name || (program.parent_id ? "" : "主节目"),
    category: program.category || "personal",
    format: program.format || "video",
    platform: program.platform || "network",
    delivery: program.delivery || "recorded",
    auto_generate: program.auto_generate !== false,
    episode_start: Number(program.episode_start) === 0 ? 0 : 1,
    people: [...(program.people || [])],
    official_url: program.official_url || "",
    description: program.description || "",
    periods: (program.periods && program.periods.length ? program.periods : [legacyPeriod(program)]).map(period => {
      const weekIndex = Number(period.week_index) || 1;
      return {
        ...blankPeriod(),
        ...period,
        frequency: period.frequency === "irregular" ? "single" : period.frequency || "weekly",
        week_interval: Number(period.week_interval) || 1,
        week_direction: weekIndex < 0 ? "last" : "first",
        week_number: Math.abs(weekIndex) || 1,
      };
    }),
  });
}

function legacyPeriod(program) {
  const frequency = program.frequency === "irregular" || program.monthly_mode === "irregular" ? "single" : program.frequency || "weekly";
  return {
    start_date: program.start_date || "",
    end_date: frequency === "single" ? program.start_date || "" : program.end_date || "",
    frequency,
    week_interval: program.week_interval || 1,
    week_index: program.week_index || 1,
    weekday: program.weekday || 0,
    schedule_time: program.schedule_time || "",
    timezone: program.timezone || "Asia/Tokyo",
  };
}

function resetOccurrenceDraft() {
  Object.assign(occurrenceDraft, blankOccurrence());
  occurrenceGuestInput.value = "";
}

function resetForm() {
  Object.assign(form, blankProgram());
  editingId.value = "";
  editorOpen.value = false;
  activePanel.value = "list";
  occurrenceRows.value = [];
  resetOccurrenceDraft();
}

function startNewProgram() {
  resetForm();
  editorOpen.value = true;
  activePanel.value = "edit";
  message.value = "正在新建主节目";
  error.value = "";
}

async function loadOccurrences(programId) {
  if (!programId) return;
  occurrenceLoading.value = true;
  try {
    const data = await api(`/api/admin/programs/${programId}/occurrences`);
    occurrenceRows.value = data.occurrences;
  } catch (requestError) {
    showError(requestError);
  } finally {
    occurrenceLoading.value = false;
  }
}

async function editProgram(program) {
  applyProgram(program);
  editorOpen.value = true;
  editingId.value = program.id;
  activePanel.value = "edit";
  resetOccurrenceDraft();
  message.value = "";
  error.value = "";
  await loadOccurrences(program.id);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function setPeriodFrequency(period, value) {
  const previousFrequency = period.frequency;
  period.frequency = value;
  if (value === "single") period.end_date = period.start_date;
  else if (previousFrequency === "single") period.end_date = "";
}

function setPeriodWeekDirection(period, value) {
  period.week_direction = value;
}

function togglePerson(person) {
  if (form.people.includes(person)) form.people = form.people.filter(item => item !== person);
  else form.people.push(person);
}

function castMember(person) {
  return NIJIGASAKI_CAST.find(member => [member.name, ...member.aliases].includes(person));
}

function programCast(program) {
  return castColorSegments(program.people);
}

function addCustomPerson() {
  const person = customPerson.value.trim();
  if (person && !form.people.includes(person)) form.people.push(person);
  customPerson.value = "";
}

function removePerson(person) {
  form.people = form.people.filter(item => item !== person);
}

function addOccurrenceGuest() {
  const guest = occurrenceGuestInput.value.trim();
  if (guest && !occurrenceDraft.guests.includes(guest)) occurrenceDraft.guests.push(guest);
  occurrenceGuestInput.value = "";
}

function removeOccurrenceGuest(guest) {
  occurrenceDraft.guests = occurrenceDraft.guests.filter(item => item !== guest);
}

function setOccurrenceStatus(status) {
  occurrenceDraft.status = status;
  if (status === "rescheduled") {
    occurrenceDraft.adjusted_date ||= occurrenceRescheduleBaseDate.value;
    occurrenceDraft.adjusted_time ||= occurrenceDraft.original_time;
  } else {
    occurrenceDraft.adjusted_date = "";
    occurrenceDraft.adjusted_time = "";
    occurrenceDraft.shift_following_days = 0;
  }
}

function addDays(value, days) {
  if (!value) return "";
  const [year, month, day] = value.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, month - 1, day + days));
  return shifted.toISOString().slice(0, 10);
}

function setOccurrenceShift(days) {
  occurrenceDraft.shift_following_days = days;
  if (days) occurrenceDraft.adjusted_date = addDays(occurrenceRescheduleBaseDate.value, days);
}

function newSubprogram(parent) {
  resetForm();
  editorOpen.value = true;
  activePanel.value = "edit";
  form.parent_id = parent.id;
  form.title = parent.title;
  form.subprogram_name = "";
  message.value = `正在添加「${parent.title}」的子节目`;
  error.value = "";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function newSubprogramFromEditor() {
  if (!editingId.value || form.parent_id) return;
  const parent = programs.value.find(program => program.id === editingId.value);
  newSubprogram(parent || { id: editingId.value, title: form.title });
}

function addPeriod() {
  const previous = form.periods[form.periods.length - 1];
  let startDate = "";
  if (previous?.end_date) {
    const parsed = new Date(`${previous.end_date}T12:00:00`);
    parsed.setDate(parsed.getDate() + 1);
    startDate = parsed.toISOString().slice(0, 10);
  }
  form.periods.push({ ...blankPeriod(), start_date: startDate });
}

function removePeriod(index) {
  if (form.periods.length === 1) return;
  form.periods.splice(index, 1);
}

function programBody() {
  const periods = form.periods.map(period => ({
    start_date: period.start_date,
    end_date: period.frequency === "single" ? period.start_date : period.end_date,
    frequency: period.frequency,
    week_interval: Number(period.week_interval) || 0,
    week_index: period.frequency === "monthly" ? (period.week_direction === "last" ? -Number(period.week_number) : Number(period.week_number)) : 0,
    weekday: Number(period.weekday),
    schedule_time: period.schedule_time,
    timezone: period.timezone || "Asia/Tokyo",
  }));
  const sorted = [...periods].sort((left, right) => left.start_date.localeCompare(right.start_date));
  const first = sorted[0] || {};
  const last = sorted[sorted.length - 1] || {};
  const allSingle = sorted.length > 0 && sorted.every(period => period.frequency === "single");
  return {
    title: form.title,
    category: form.category,
     format: form.format,
     platform: form.platform,
      delivery: form.delivery,
     auto_generate: form.auto_generate,
       episode_start: Number(form.episode_start) === 0 ? 0 : 1,
       people: [...form.people],
      official_url: form.official_url,
      description: form.description,
     parent_id: form.parent_id,
     subprogram_name: form.parent_id ? form.subprogram_name : "主节目",
     start_date: first.start_date || "",
    end_date: allSingle ? "" : last.end_date || "",
    frequency: first.frequency || "weekly",
    week_interval: first.week_interval || 1,
    week_index: first.week_index || 0,
    weekday: first.weekday || 0,
    schedule_time: first.schedule_time || "",
    periods,
  };
}

async function saveProgram(successMessage = "") {
  saving.value = true;
  message.value = "";
  error.value = "";
  try {
    const path = editingId.value ? `/api/admin/programs/${editingId.value}` : "/api/admin/programs";
    const method = editingId.value ? "PATCH" : "POST";
    const data = await api(path, { method, body: programBody() });
    const saved = data.program;
    const index = programs.value.findIndex(program => program.id === saved.id);
    if (index === -1) programs.value.push(saved);
    else programs.value.splice(index, 1, saved);
    editingId.value = saved.id;
    applyProgram(saved);
    await loadOccurrences(saved.id);
    message.value = successMessage || (method === "PATCH" ? "节目已更新" : "节目已添加，现在可以维护单集排期");
    return true;
  } catch (requestError) {
    showError(requestError);
    return false;
  } finally {
    saving.value = false;
  }
}

async function saveAutoGeneration() {
  if (!editingId.value) return;
  const requested = form.auto_generate;
  try {
    const data = await api(`/api/admin/programs/${editingId.value}/auto-generation`, {
      method: "PATCH",
      body: { auto_generate: requested },
    });
    await Promise.all([loadPrograms(), loadOccurrences(editingId.value)]);
    const retained = Number(data.materialized_count || 0);
    message.value = requested
      ? "已开启后续单集自动生成"
      : retained
        ? `已关闭自动生成，并保留 ${retained} 个已播出单集，现在可以逐条添加单集`
        : "已关闭后续单集自动生成，现在可以逐条添加单集";
  } catch (requestError) {
    form.auto_generate = !requested;
    showError(requestError);
  }
}

async function convertMonthlyToIndividual() {
  if (!editingId.value || !form.periods.some(period => period.frequency === "monthly")) return;
  if (!window.confirm("将当前节目中的固定月更改为逐期设置？之后按月生成单集，首期使用时期开始日，后续从每月 1 日作为占位，可逐期修改原定日期和时间。")) return;
  const previousFrequencies = form.periods.map(period => period.frequency);
  form.periods.forEach(period => {
    if (period.frequency === "monthly") period.frequency = "individual";
  });
  const saved = await saveProgram("已将固定月更切换为逐期设置");
  if (!saved) {
    form.periods.forEach((period, index) => {
      period.frequency = previousFrequencies[index];
    });
  }
}

async function applyRescheduledToOriginal() {
  if (!editingId.value) return;
  const count = occurrenceRows.value.filter(row => row.status === "rescheduled").length;
  if (!count || !window.confirm(`将 ${count} 期的已改期日期和时间覆盖为新的原定播出时间，并清除改期标记？`)) return;
  occurrenceSaving.value = true;
  message.value = "";
  error.value = "";
  try {
    const data = await api(`/api/admin/programs/${editingId.value}/occurrences/restore-rescheduled`, { method: "POST" });
    await Promise.all([loadPrograms(), loadOccurrences(editingId.value)]);
    resetOccurrenceDraft();
    message.value = `已将 ${data.count} 期改期时间覆盖为新的原定播出时间`;
  } catch (requestError) {
    showError(requestError);
  } finally {
    occurrenceSaving.value = false;
  }
}

function editOccurrence(row) {
  activePanel.value = "occurrences";
  Object.assign(occurrenceDraft, {
    id: row.id || "",
    original_date: row.original_date || "",
    generated_date: row.generated_date || "",
    original_time: row.original_time || "",
    delivery: row.delivery_override || "",
    effective_date: row.date || row.adjusted_date || row.original_date || "",
    schedule_shift_days: Number(row.schedule_shift_days) || 0,
    shift_following_days: [-7, 7].includes(Number(row.shift_following_days)) ? Number(row.shift_following_days) : 0,
    source_url: row.source_url || "",
    mirror_url: row.mirror_url || "",
    subtitle_url: row.subtitle_url || "",
    status: row.status || "scheduled",
    special: row.special || "",
    adjusted_date: row.adjusted_date || (row.status === "rescheduled" ? row.original_date || "" : ""),
     adjusted_time: row.adjusted_time || row.original_time || "",
     note: row.note || "",
    guests: [...(row.guests || [])],
    generated: Boolean(row.generated),
    individual: Boolean(row.individual),
    materialized: Boolean(row.materialized),
    manual: Boolean(row.manual),
    timezone: row.timezone || "",
  });
  occurrenceGuestInput.value = "";
}

function occurrenceRowKey(row) {
  return `${row.id || "generated"}-${row.original_date}`;
}

function setOccurrenceItemRef(row, element) {
  const key = occurrenceRowKey(row);
  if (element) occurrenceItemRefs.set(key, element);
  else occurrenceItemRefs.delete(key);
}

function scrollOccurrenceListTo(row) {
  const list = occurrenceListRef.value;
  const item = row && occurrenceItemRefs.get(occurrenceRowKey(row));
  if (!list || !item) return;
  const listRect = list.getBoundingClientRect();
  const itemRect = item.getBoundingClientRect();
  const itemTop = list.scrollTop + itemRect.top - listRect.top;
  const centeredTop = itemTop - (list.clientHeight - item.offsetHeight) / 2;
  const maxTop = Math.max(0, list.scrollHeight - list.clientHeight);
  const top = Math.min(maxTop, Math.max(0, centeredTop));
  list.scrollTo({ top, behavior: "smooth" });
}

async function focusOccurrenceEditor(row = null) {
  await nextTick();
  occurrenceEditorRef.value?.scrollIntoView({ behavior: "smooth", block: "start" });
  if (row) {
    await nextTick();
    scrollOccurrenceListTo(row);
  }
}

function selectAdjacentOccurrence(direction) {
  const index = selectedOccurrenceIndex.value;
  const nextIndex = index + direction;
  if (index < 0 || nextIndex < 0 || nextIndex >= occurrenceRows.value.length) return;
  editOccurrence(occurrenceRows.value[nextIndex]);
}

function captureOccurrencePosition() {
  return {
    x: window.scrollX,
    y: window.scrollY,
    listTop: occurrenceListRef.value?.scrollTop || 0,
  };
}

async function restoreOccurrencePosition(position) {
  await nextTick();
  window.scrollTo({ left: position.x, top: position.y, behavior: "auto" });
  if (occurrenceListRef.value) occurrenceListRef.value.scrollTop = position.listTop;
}

function newOccurrence() {
  activePanel.value = "occurrences";
  resetOccurrenceDraft();
}

function leaveOccurrenceFocus() {
  activePanel.value = "edit";
}

async function saveOccurrence() {
  if (!editingId.value) return;
  const position = captureOccurrencePosition();
  occurrenceSaving.value = true;
  message.value = "";
  error.value = "";
  try {
    const body = {
      original_date: occurrenceDraft.original_date,
      generated_date: occurrenceDraft.generated_date,
      original_time: occurrenceDraft.original_time,
      delivery: occurrenceDraft.delivery,
      shift_following_days: occurrenceDraft.shift_following_days,
      source_url: occurrenceDraft.source_url,
      mirror_url: occurrenceDraft.mirror_url,
      subtitle_url: occurrenceDraft.subtitle_url,
      status: occurrenceDraft.status,
      special: occurrenceDraft.special,
      adjusted_date: occurrenceDraft.adjusted_date,
      adjusted_time: occurrenceDraft.adjusted_time,
      note: occurrenceDraft.note,
      guests: [...occurrenceDraft.guests],
    };
    const path = occurrenceDraft.id
      ? `/api/admin/programs/${editingId.value}/occurrences/${occurrenceDraft.id}`
      : `/api/admin/programs/${editingId.value}/occurrences`;
    const data = await api(path, { method: occurrenceDraft.id ? "PATCH" : "POST", body });
    await Promise.all([loadPrograms(), loadOccurrences(editingId.value)]);
    message.value = occurrenceDraft.id ? "单集排期已更新" : "单集排期已添加";
    const saved = occurrenceRows.value.find(row => String(row.id) === String(data.occurrence?.id));
    if (saved) editOccurrence(saved);
    else resetOccurrenceDraft();
    await restoreOccurrencePosition(position);
  } catch (requestError) {
    showError(requestError);
  } finally {
    occurrenceSaving.value = false;
  }
}

async function deleteOccurrence() {
  if (!occurrenceDraft.id || !editingId.value) return;
  if (!window.confirm("删除后，这条单集不会再显示在生成列表和日历中；如需保留请使用“因故取消”。继续吗？")) return;
  const position = captureOccurrencePosition();
  occurrenceSaving.value = true;
  try {
    await api(`/api/admin/programs/${editingId.value}/occurrences/${occurrenceDraft.id}`, { method: "DELETE" });
    await Promise.all([loadPrograms(), loadOccurrences(editingId.value)]);
    resetOccurrenceDraft();
    await restoreOccurrencePosition(position);
    message.value = "单集已删除";
  } catch (requestError) {
    showError(requestError);
  } finally {
    occurrenceSaving.value = false;
  }
}

async function restoreDeletedOccurrence() {
  if (occurrenceDraft.status !== "deleted") return;
  occurrenceDraft.status = "scheduled";
  occurrenceDraft.adjusted_date = "";
  occurrenceDraft.adjusted_time = "";
  await saveOccurrence();
  if (!error.value) message.value = "单集已恢复";
}

async function toggleOccurrenceDeletion() {
  if (occurrenceDraft.status === "deleted") await restoreDeletedOccurrence();
  else await deleteOccurrence();
}

async function deleteProgram(program) {
  if (!window.confirm(`确定删除「${program.title}」吗？`)) return;
  deletingId.value = program.id;
  message.value = "";
  error.value = "";
  try {
    await api(`/api/admin/programs/${program.id}`, { method: "DELETE" });
    programs.value = programs.value.filter(item => item.id !== program.id);
    if (editingId.value === program.id) resetForm();
    message.value = "节目已删除";
  } catch (requestError) {
    showError(requestError);
  } finally {
    deletingId.value = "";
  }
}

onMounted(async () => {
  if (await loadPrograms()) await openRequestedProgram();
  document.addEventListener("click", closeProgramCastFilter);
});
onUnmounted(() => {
  window.clearTimeout(toastTimer);
  document.removeEventListener("click", closeProgramCastFilter);
});
</script>

<template>
  <main class="page program-admin-page" :class="{ 'occurrence-focused': activePanel === 'occurrences' }">
    <div class="programs-topline">
      <div>
        <p class="eyebrow">PROGRAM ARCHIVE / CONTROL ROOM</p>
        <h1>节目档案</h1>
        <p class="programs-intro">手动维护官方节目和个人节目，日历会根据排期规则生成。</p>
      </div>
      <RouterLink class="back" to="/admin">← 返回运行设置</RouterLink>
    </div>

    <nav class="program-panel-nav" aria-label="节目档案工作区">
      <button type="button" class="program-panel-arrow" :disabled="!canMovePrevious" aria-label="切换到上一个工作区" @click="previousPanel">←</button>
      <div class="program-panel-context">
        <div class="program-panel-meta"><span class="eyebrow">WORKSPACE {{ panelIndex + 1 }} / 03</span><div class="program-panel-steps"><span v-for="(panel, index) in panelOrder" :key="panel" :class="{ active: index === panelIndex }"></span></div></div>
        <strong>{{ panelTitle }}</strong>
        <small>{{ panelDescription }}</small>
      </div>
      <button type="button" class="program-panel-arrow" :disabled="!canMoveNext" :aria-label="activePanel === 'list' && !editorOpen ? '新建节目' : '切换到下一个工作区'" @click="nextPanel">→</button>
    </nav>

    <div v-if="error || message" class="program-toast" :class="error ? 'error' : 'success'" :role="error ? 'alert' : 'status'" aria-live="assertive">{{ error || message }}</div>

    <section v-if="activePanel === 'list'" class="program-admin-list program-panel-content" :class="{ 'program-cast-filter-open': programCastFilterOpen }">
      <div class="section-heading">
        <div><p class="eyebrow">CURRENT ENTRIES</p><h2>已录入节目</h2></div>
        <span class="section-count">{{ filteredPrograms.length }}<small v-if="programSearch || programCastFilter.length"> / {{ programs.length }}</small></span>
      </div>
      <div class="program-admin-list-footer">
        <p class="muted">选择一个节目进入编辑；已保存的节目可以继续切换到单集列表。</p>
        <div class="program-admin-list-tools">
          <label class="program-admin-search">
            <span>关键词</span>
            <input v-model="programSearch" type="search" placeholder="搜索节目、子节目或成员" aria-label="搜索节目、子节目或成员">
          </label>
          <div class="program-calendar-filter program-cast-filter program-admin-cast-filter">
            <span class="program-calendar-filter-label">按 Cast 筛选</span>
              <details ref="programCastFilterDetails" class="program-cast-filter-details" @toggle="syncProgramCastFilter">
              <summary class="program-cast-filter-summary"><strong>{{ programCastFilterLabel }}</strong><b>⌄</b></summary>
              <div class="program-cast-filter-panel">
                <div class="program-cast-filter-actions">
                  <button type="button" class="secondary program-action-button" @click="selectAllProgramCast">全选</button>
                  <button type="button" class="secondary program-action-button" @click="clearProgramCastFilter">清空</button>
                </div>
                <div class="program-cast-tags">
                  <button v-for="member in NIJIGASAKI_CAST" :key="member.name" type="button" class="program-cast-tag" :class="{ selected: programCastFilter.includes(member.name) }" :aria-pressed="programCastFilter.includes(member.name)" @click="toggleProgramCastFilter(member.name)"><i class="program-cast-dot" :style="{ backgroundColor: member.color }"></i>{{ member.name }}</button>
                </div>
              </div>
            </details>
          </div>
          <button type="button" class="secondary program-action-button" @click="startNewProgram">新建节目</button>
        </div>
      </div>
      <p v-if="loading" class="state">正在读取节目……</p>
      <p v-else-if="!programs.length" class="muted">还没有录入节目。</p>
      <p v-else-if="!filteredPrograms.length" class="muted">当前关键词和 Cast 筛选没有匹配的节目。</p>
      <article v-for="program in filteredPrograms" :key="program.id" class="program-admin-item" :class="{ 'is-subprogram': Boolean(program.parent_id) }">
        <span v-if="programCast(program).length" class="program-admin-cast-line" aria-label="固定参与成员">
          <i v-for="member in programCast(program)" :key="member.name" :style="{ '--cast-color': member.color }"></i>
        </span>
        <div>
           <div class="program-admin-tags"><span class="program-kind">{{ program.parent_id ? "子节目" : "主节目" }}</span><span class="program-subprogram-key">{{ program.subprogram_name || "主节目" }}</span><span class="program-status" :class="`status-${program.status}`">{{ programStatus(program) }}</span><span v-if="program.update_status === 'updated'" class="program-update-status">{{ updateStatusLabel(program) }}</span><span class="program-type-label" :class="`program-type-${program.category}`">{{ programType(program) }} · {{ formatLabel(program) }}</span></div>
          <h3>{{ program.title }}</h3>
          <p>{{ scheduleLabel(program) }} · 已播 {{ program.episode_count }} 期{{ program.parent_id ? ` · 挂在 ${program.title}` : "" }}</p>
        </div>
        <div class="program-admin-actions">
          <button type="button" class="secondary program-action-button" @click="editProgram(program)">编辑</button>
          <button type="button" class="danger program-action-button" :disabled="deletingId === program.id" @click="deleteProgram(program)">{{ deletingId === program.id ? "删除中……" : "删除" }}</button>
        </div>
      </article>
    </section>

    <form v-if="activePanel === 'edit' && editorOpen" class="settings-card program-editor program-panel-content" @submit.prevent="saveProgram()">
      <div class="form-heading">
        <span class="form-number">{{ editingId ? "02" : "01" }}</span>
        <div><p class="form-kicker">MANUAL ENTRY</p><h2>{{ editingId ? "编辑节目" : "添加节目" }}</h2></div>
        <div v-if="editingId && !form.parent_id" class="form-heading-actions">
          <button type="button" class="secondary program-action-button" @click="newSubprogramFromEditor">添加子节目</button>
        </div>
      </div>

      <div class="program-form-grid">
          <label class="program-field-wide">节目名称<input v-model="form.title" :readonly="Boolean(form.parent_id)" required placeholder="例如：虹咲学园放送室"><small v-if="form.parent_id">子节目名称继承自主节目，排期和内容配置仍可独立修改。</small></label>
           <div v-if="form.parent_id" class="program-form-field">
             <span class="program-field-label">节目层级</span>
             <div class="program-readonly-control">子节目 · {{ parentProgramTitle }}</div>
             <small>父节目固定为当前编辑的主节目；如需归属其他主节目，请从对应主节目编辑页添加。</small>
          </div>
          <label v-if="form.parent_id" class="program-form-field"><span class="program-field-label">子节目名称</span><input v-model="form.subprogram_name" required placeholder="例如：嘉宾回"><small>显示在同一个节目名称下，用于区分不同子节目。</small></label>

          <div class="program-form-field">
          <span class="program-field-label">节目类型</span>
          <div class="choice-tags" role="radiogroup" aria-label="节目类型">
            <button type="button" :class="{ selected: form.category === 'official' }" @click="form.category = 'official'">官方节目</button>
            <button type="button" :class="{ selected: form.category === 'personal' }" @click="form.category = 'personal'">个人节目</button>
          </div>
        </div>
        <div class="program-form-field">
          <span class="program-field-label">节目形式</span>
          <div class="choice-tags" role="radiogroup" aria-label="节目形式">
            <button type="button" :class="{ selected: form.format === 'video' }" @click="form.format = 'video'">有画面</button>
            <button type="button" :class="{ selected: form.format === 'radio' }" @click="form.format = 'radio'">广播（无画面）</button>
          </div>
        </div>
        <div class="program-form-field">
          <span class="program-field-label">播出平台</span>
          <div class="choice-tags" role="radiogroup" aria-label="播出平台">
            <button type="button" :class="{ selected: form.platform === 'tv' }" @click="form.platform = 'tv'">电视台</button>
            <button type="button" :class="{ selected: form.platform === 'network' }" @click="form.platform = 'network'">网络</button>
          </div>
        </div>
        <div class="program-form-field">
          <span class="program-field-label">播放方式</span>
          <div class="choice-tags" role="radiogroup" aria-label="播放方式">
            <button type="button" :class="{ selected: form.delivery === 'live' }" @click="form.delivery = 'live'">直播</button>
            <button type="button" :class="{ selected: form.delivery === 'recorded' }" @click="form.delivery = 'recorded'">录播</button>
          </div>
        </div>
        <div class="program-form-field">
          <span class="program-field-label">首集编号</span>
          <div class="choice-tags" role="radiogroup" aria-label="首集编号">
            <button type="button" :class="{ selected: form.episode_start === 0 }" @click="form.episode_start = 0">第 0 期</button>
            <button type="button" :class="{ selected: form.episode_start === 1 }" @click="form.episode_start = 1">第 1 期</button>
          </div>
          <small>设置节目正篇的起始期数；EX 特别节目不占用正篇编号。</small>
        </div>

        <div class="program-form-field program-field-wide">
           <span class="program-field-label">固定参与成员 / 主持</span>
          <div class="people-picker">
             <button v-for="person in peopleOptions" :key="person" type="button" class="person-tag" :class="{ selected: form.people.includes(person) }" @click="togglePerson(person)"><i v-if="castMember(person)" class="program-cast-dot" :style="{ backgroundColor: castMember(person).color }"></i>{{ person }}</button>
            <div v-for="person in customPeople" :key="person" class="person-tag selected custom-person-tag">
              <span>{{ person }}</span><button type="button" aria-label="移除成员" @click="removePerson(person)">×</button>
            </div>
          </div>
          <div class="custom-person-entry">
             <input v-model="customPerson" placeholder="添加固定主持或嘉宾" @keydown.enter.prevent="addCustomPerson">
             <button type="button" class="secondary program-action-button" @click="addCustomPerson">添加成员</button>
           </div>
           <small>这里填写批量排期下固定出现的虹咲成员、主持人或常驻嘉宾；单期临时嘉宾请在下方单集编辑。</small>
        </div>
          <label class="program-field-wide">相关链接<input v-model="form.official_url" type="url" placeholder="https://"><small>节目层面的补充链接；源地址、搬运地址和字幕地址请在单集编辑中填写。</small></label>
         <label class="program-field-wide">节目简介<textarea v-model="form.description" rows="3" placeholder="可选"></textarea></label>
      </div>

      <div class="program-schedule">
        <div class="form-heading subsection-heading"><span class="form-number">02</span><div><p class="form-kicker">BROADCAST PERIODS</p><h2>排期时期</h2></div></div>
        <p class="muted period-help">同一个节目可以分成多个时期，例如先每周、再隔周、最后改为每月。时期之间请用日期分开。</p>
        <article v-for="(period, index) in form.periods" :key="period.id || index" class="program-period-card">
          <div class="program-period-heading">
            <div><span class="program-period-number">时期 {{ index + 1 }}</span><strong>{{ periodScheduleLabel(period) }}</strong></div>
             <button v-if="form.periods.length > 1" type="button" class="danger program-action-button" @click="removePeriod(index)">移除</button>
          </div>
          <div class="program-form-grid period-form-grid">
            <div class="program-form-field program-field-wide">
              <span class="program-field-label">更新方式</span>
              <div class="choice-tags" role="radiogroup" aria-label="更新方式">
                <button type="button" :class="{ selected: period.frequency === 'weekly' }" @click="setPeriodFrequency(period, 'weekly')">周更</button>
                <button v-if="!['monthly', 'individual'].includes(period.frequency)" type="button" :class="{ selected: period.frequency === 'monthly' }" @click="setPeriodFrequency(period, 'monthly')">月更</button>
                <span v-else class="program-frequency-monthly">
                  <button type="button" :class="{ selected: period.frequency === 'monthly' }" @click="setPeriodFrequency(period, 'monthly')">月更</button>
                  <button type="button" :class="{ selected: period.frequency === 'individual' }" @click="setPeriodFrequency(period, 'individual')">逐期设置</button>
                </span>
                <button type="button" :class="{ selected: period.frequency === 'single' }" @click="setPeriodFrequency(period, 'single')">单次</button>
              </div>
              <small v-if="period.frequency === 'single'">单次表示一个单独的节目，只需要选择播出日期和时间。</small>
               <small v-else-if="period.frequency === 'individual'">按月生成单集：首期使用时期开始日，后续从每月 1 日作为占位；之后可在单集列表中直接修改每期原定日期和时间。</small>
            </div>
            <div v-if="period.frequency === 'weekly'" class="program-form-field">
              <span class="program-field-label">更新间隔</span>
              <div class="inline-number"><input v-model.number="period.week_interval" type="number" min="1" max="52" required><span>周一次</span></div>
              <small>填写 2 即为隔周更新。</small>
            </div>
            <div v-if="period.frequency === 'monthly'" class="program-form-field">
              <span class="program-field-label">周次方向</span>
              <div class="choice-tags" role="radiogroup" aria-label="周次方向">
                <button type="button" :class="{ selected: period.week_direction === 'first' }" @click="setPeriodWeekDirection(period, 'first')">顺数</button>
                <button type="button" :class="{ selected: period.week_direction === 'last' }" @click="setPeriodWeekDirection(period, 'last')">倒数</button>
              </div>
            </div>
            <div v-if="period.frequency === 'weekly' || period.frequency === 'monthly'" class="program-form-field program-field-wide">
              <span class="program-field-label">星期</span>
              <div class="choice-tags weekday-tags" role="radiogroup" aria-label="星期">
                <button v-for="(name, weekday) in weekdayNames" :key="name" type="button" :class="{ selected: period.weekday === weekday }" @click="period.weekday = weekday">{{ name }}</button>
              </div>
            </div>
            <div v-if="period.frequency === 'monthly'" class="program-form-field program-field-wide">
              <span class="program-field-label">第几周</span>
              <div class="choice-tags week-tags" role="radiogroup" aria-label="第几周">
                <button v-for="week in weekOptions" :key="week" type="button" :class="{ selected: period.week_number === week }" @click="period.week_number = week">第 {{ week }} 周</button>
              </div>
            </div>
            <div class="program-form-field">
              <span class="program-field-label">播出时间</span>
              <VueDatePicker v-model="period.schedule_time" class="program-date-picker" time-picker model-type="HH:mm" format="HH:mm" locale="zh-CN" auto-apply :clearable="true" :is-24="true" :teleport="true" text-input placeholder="选择时间" />
              <small>留空表示全天事件。</small>
            </div>
            <div class="program-form-field">
              <span class="program-field-label">更新时间时区</span>
              <select v-model="period.timezone">
                <option v-for="option in timezoneOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
              </select>
              <small>默认使用东京时间。</small>
            </div>
            <label v-if="period.frequency === 'single'">播出日期<VueDatePicker v-model="period.start_date" class="program-date-picker" model-type="yyyy-MM-dd" format="yyyy-MM-dd" locale="zh-CN" :enable-time-picker="false" auto-apply :clearable="false" :teleport="true" placeholder="选择日期" /></label>
            <label v-else>时期开始<VueDatePicker v-model="period.start_date" class="program-date-picker" model-type="yyyy-MM-dd" format="yyyy-MM-dd" locale="zh-CN" :enable-time-picker="false" auto-apply :clearable="false" :teleport="true" placeholder="选择日期" /></label>
            <label v-if="period.frequency !== 'single'">时期结束<VueDatePicker v-model="period.end_date" class="program-date-picker" model-type="yyyy-MM-dd" format="yyyy-MM-dd" locale="zh-CN" auto-apply :clearable="true" :teleport="true" placeholder="留空表示进行中" /><small>{{ index < form.periods.length - 1 ? "用于划分下一个排期时期。" : "填入结束日期后会自动标记为已完结；留空表示进行中。" }}</small></label>
          </div>
        </article>
         <button type="button" class="secondary add-period-button program-action-button" @click="addPeriod">＋ 添加排期时期</button>
      </div>

      <div class="actions">
         <button class="program-action-button" :disabled="saving">{{ saving ? "保存中……" : editingId ? "保存修改" : "添加节目" }}</button>
         <button v-if="editingId" type="button" class="secondary program-action-button" @click="startNewProgram">新建节目</button>
      </div>
    </form>

    <section ref="occurrenceEditorRef" v-if="activePanel === 'occurrences' && editorOpen && editingId" class="settings-card program-occurrence-editor program-panel-content">
         <div class="section-heading">
         <div><p class="eyebrow">GENERATED EPISODES / OVERRIDES</p><h2>自动生成单集</h2></div>
          <div class="settings-heading-actions">
            <button type="button" class="secondary program-action-button" @click="leaveOccurrenceFocus">返回节目配置</button>
            <button type="button" class="secondary program-action-button" @click="newOccurrence">添加单集</button>
          </div>
        </div>
        <div class="occurrence-generation-bar">
           <label class="occurrence-auto-toggle">
             <input v-model="form.auto_generate" type="checkbox" :disabled="saving || occurrenceSaving" @change="saveAutoGeneration">
             <span><strong>自动生成后续单集</strong><small>{{ form.auto_generate ? "按排期规则生成未来约半年的单集。" : "已关闭自动生成，只保留已播出的单集和手动添加的单集。" }}</small></span>
          </label>
          <div class="occurrence-bulk-actions">
            <button v-if="form.periods.some(period => period.frequency === 'monthly')" type="button" class="secondary program-action-button" :disabled="saving || occurrenceSaving" @click="convertMonthlyToIndividual">固定月更 → 逐期设置</button>
              <button v-if="occurrenceRows.some(row => row.status === 'rescheduled')" type="button" class="secondary program-action-button" :disabled="saving || occurrenceSaving" @click="applyRescheduledToOriginal">已改期 → 覆盖为原定</button>
          </div>
        </div>
         <p class="muted occurrence-help">选择某一期后，可单独改期、取消、补录或增加本期嘉宾。当前显示 {{ generatedOccurrenceCount }} 个自动单集{{ materializedOccurrenceCount ? `，${materializedOccurrenceCount} 个已播出并保存` : "" }}{{ adjustedOccurrenceCount ? `，${adjustedOccurrenceCount} 个单独调整` : "" }}{{ deletedOccurrenceCount ? `，${deletedOccurrenceCount} 个已删除` : "" }}。</p>
      <p v-if="occurrenceLoading" class="state">正在读取单集排期……</p>
       <div v-else class="occurrence-editor-layout">
         <div ref="occurrenceListRef" class="occurrence-list">
              <button v-for="row in occurrenceRows" :key="occurrenceRowKey(row)" :ref="element => setOccurrenceItemRef(row, element)" type="button" class="occurrence-list-item" :class="{ selected: occurrenceDraft.original_date === row.original_date }" @click="editOccurrence(row)">
              <span><b>{{ occurrenceEpisodeLabel(row) }}</b><em :class="{ cancelled: row.status === 'cancelled', deleted: row.status === 'deleted', aired: row.aired }">{{ occurrenceStatus(row) }}</em></span>
            <strong>{{ row.date }}</strong>
             <small>{{ row.status === "deleted" ? "已删除，不参与生成" : row.generated ? "自动生成" : row.materialized ? "已播出并保存" : row.adjusted_date ? `原定 ${row.original_date}` : "已单独调整" }}{{ row.guests?.length ? ` · 嘉宾 ${row.guests.length} 人` : "" }}</small>
          </button>
          <p v-if="!occurrenceRows.length" class="muted">当前区间没有自动生成的单集，可以手动添加一条记录。</p>
        </div>
        <form class="occurrence-form" @submit.prevent="saveOccurrence">
           <div class="occurrence-form-heading">
             <button type="button" class="occurrence-nav-button" :disabled="!canPreviousOccurrence" aria-label="上一集" title="上一集" @click="selectAdjacentOccurrence(-1)">←</button>
             <div><p class="form-kicker">ONE EPISODE</p><h3>{{ occurrenceDraft.id ? "编辑单集" : "添加单集调整" }}</h3></div>
             <button type="button" class="occurrence-nav-button" :disabled="!canNextOccurrence" aria-label="下一集" title="下一集" @click="selectAdjacentOccurrence(1)">→</button>
             <span v-if="occurrenceDraft.id" class="program-kind">已保存</span>
           </div>
            <label>原定日期<VueDatePicker v-model="occurrenceDraft.original_date" class="program-date-picker" model-type="yyyy-MM-dd" format="yyyy-MM-dd" locale="zh-CN" :enable-time-picker="false" auto-apply :clearable="false" :readonly="occurrenceOriginalLocked" :teleport="true" placeholder="选择日期" /><small>{{ occurrenceDraft.individual ? "逐期设置模式：可直接修改本期原定日期。" : occurrenceOriginalLocked ? "来自节目排期规则，不能修改原定日期。" : "手动补录单集时填写原定日期。" }}</small></label>
               <label>原定时间<VueDatePicker v-model="occurrenceDraft.original_time" class="program-date-picker" time-picker model-type="HH:mm" format="HH:mm" locale="zh-CN" auto-apply :clearable="true" :is-24="true" :readonly="occurrenceOriginalLocked" :teleport="true" text-input placeholder="选择时间" /><small>{{ occurrenceDraft.individual ? "逐期设置模式：可直接修改本期原定时间。" : occurrenceOriginalLocked ? "来自排期规则，只能修改调整日期；调整时间已默认沿用原定时间。" : "手动补录单集时填写原定时间。" }}节目排期时区：{{ occurrenceTimezoneLabel }}；日历会按访问设备时区显示。</small></label>
           <div class="program-form-field">
             <span class="program-field-label">本期播出方式</span>
             <div class="choice-tags" role="radiogroup" aria-label="本期播出方式">
               <button type="button" :class="{ selected: !occurrenceDraft.delivery }" @click="occurrenceDraft.delivery = ''">跟随节目默认</button>
               <button type="button" :class="{ selected: occurrenceDraft.delivery === 'live' }" @click="occurrenceDraft.delivery = 'live'">直播</button>
               <button type="button" :class="{ selected: occurrenceDraft.delivery === 'recorded' }" @click="occurrenceDraft.delivery = 'recorded'">录播</button>
             </div>
             <small>仅作用于这一期，不会修改整个节目的默认播出方式。</small>
           </div>
           <div class="program-form-field">
             <span class="program-field-label">本期类型</span>
             <div class="choice-tags" role="radiogroup" aria-label="本期类型">
               <button type="button" :class="{ selected: occurrenceDraft.special !== 'EX' }" @click="occurrenceDraft.special = ''">普通节目</button>
               <button type="button" :class="{ selected: occurrenceDraft.special === 'EX' }" @click="occurrenceDraft.special = 'EX'">EX 特别节目</button>
             </div>
              <small>EX 特别节目不占用正篇期数，后续正篇编号顺延；请在自动生成单集结束后手动添加。</small>
           </div>
           <div class="program-form-field">
             <span class="program-field-label">本期状态</span>
             <div class="choice-tags">
               <button type="button" :class="{ selected: occurrenceDraft.status === 'scheduled' }" @click="setOccurrenceStatus('scheduled')">正常播出</button>
               <button type="button" :class="{ selected: occurrenceDraft.status === 'rescheduled' }" @click="setOccurrenceStatus('rescheduled')">已改期</button>
               <button type="button" :class="{ selected: occurrenceDraft.status === 'cancelled' }" @click="setOccurrenceStatus('cancelled')">因故取消</button>
             </div>
              <small>正常播出不会修改排期日期；需要更换日期时选择“已改期”。“因故取消”会保留单集并标记已取消；删除会移除单集，不再显示。</small>
           </div>
             <div v-if="occurrenceDraft.status === 'rescheduled'" class="occurrence-date-row">
               <label>调整日期<VueDatePicker v-model="occurrenceDraft.adjusted_date" class="program-date-picker" model-type="yyyy-MM-dd" format="yyyy-MM-dd" locale="zh-CN" :enable-time-picker="false" auto-apply :clearable="false" :teleport="true" placeholder="选择新日期" required /></label>
                <label>调整时间<VueDatePicker v-model="occurrenceDraft.adjusted_time" class="program-date-picker" time-picker model-type="HH:mm" format="HH:mm" locale="zh-CN" auto-apply :clearable="true" :is-24="true" :teleport="true" text-input placeholder="沿用原定时间" /></label>
             </div>
             <div v-if="occurrenceDraft.status === 'rescheduled' && canShiftFollowing" class="program-form-field program-field-wide">
               <span class="program-field-label">后续排期</span>
               <div class="choice-tags">
                 <button type="button" :class="{ selected: occurrenceDraft.shift_following_days === -7 }" @click="setOccurrenceShift(occurrenceDraft.shift_following_days === -7 ? 0 : -7)">{{ occurrenceDraft.shift_following_days === -7 ? "已提前一周" : "提前一周" }}</button>
                 <button type="button" :class="{ selected: occurrenceDraft.shift_following_days === 7 }" @click="setOccurrenceShift(occurrenceDraft.shift_following_days === 7 ? 0 : 7)">{{ occurrenceDraft.shift_following_days === 7 ? "已顺延一周" : "顺延一周" }}</button>
               </div>
               <small>本期原定周视为不播；选择提前或顺延后，之后的隔两周排期也从这里起同步平移一周。</small>
             </div>
             <div class="occurrence-link-fields">
              <label class="occurrence-source-field"><span class="program-field-label">源地址</span><input v-model="occurrenceDraft.source_url" type="url" placeholder="https://"></label>
              <label><span class="program-field-label">搬运地址</span><input v-model="occurrenceDraft.mirror_url" type="text" placeholder="BV号或B站地址"></label>
              <label><span class="program-field-label">字幕地址</span><input v-model="occurrenceDraft.subtitle_url" type="text" placeholder="BV号或B站地址"></label>
              <small>源地址填写 HTTP/HTTPS 地址；搬运地址和字幕地址支持 BV 号、B 站地址或其他 HTTP/HTTPS 地址。</small>
            </div>
            <label>备注<textarea v-model="occurrenceDraft.note" rows="3" placeholder="例如：延期至下周、嘉宾变更……"></textarea></label>
           <div class="program-form-field occurrence-guests-field">
             <span class="program-field-label">本期嘉宾</span>
             <div v-if="occurrenceDraft.guests.length" class="people-picker">
               <span v-for="guest in occurrenceDraft.guests" :key="guest" class="person-tag selected custom-person-tag"><span>{{ guest }}</span><button type="button" aria-label="移除本期嘉宾" @click="removeOccurrenceGuest(guest)">×</button></span>
             </div>
             <div class="custom-person-entry">
               <input v-model="occurrenceGuestInput" placeholder="添加本期嘉宾" @keydown.enter.prevent="addOccurrenceGuest">
                <button type="button" class="secondary program-action-button" @click="addOccurrenceGuest">添加嘉宾</button>
             </div>
             <small>仅作用于这一期，不会修改节目默认参与成员。</small>
           </div>
          <div class="actions">
             <button class="program-action-button" :disabled="occurrenceSaving">{{ occurrenceSaving ? "保存中……" : "保存本期调整" }}</button>
              <button v-if="occurrenceDraft.id" type="button" class="program-action-button" :class="occurrenceDraft.status === 'deleted' ? 'secondary' : 'danger'" :disabled="occurrenceSaving" @click="toggleOccurrenceDeletion">{{ occurrenceDeleteLabel }}</button>
             <button type="button" class="secondary program-action-button" @click="resetOccurrenceDraft">清空</button>
          </div>
        </form>
      </div>
    </section>

 </main>
</template>
