<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import VueDatePicker from "@vuepic/vue-datepicker";
import "@vuepic/vue-datepicker/dist/main.css";
import { api } from "../api";
import { locale, localeTag, t } from "../i18n";
import { NIJIGASAKI_CAST, castColorSegments, castMemberMatches } from "../programCast";
import { occurrenceLinkItems } from "../programLinks";
import NewsLightbox from "../components/NewsLightbox.vue";

const weekdayNames = computed(() => locale.value === "en"
  ? ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
  : locale.value === "ja" ? ["月", "火", "水", "木", "金", "土", "日"] : ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]);
const castCandidates = NIJIGASAKI_CAST.map(member => member.name);
const weekOptions = [1, 2, 3, 4, 5];
const timezoneOptions = computed(() => [
  { value: "Asia/Tokyo", label: t("东京时间（UTC+9）") },
  { value: "Asia/Shanghai", label: t("中国标准时间（UTC+8）") },
  { value: "Asia/Seoul", label: t("韩国时间（UTC+9）") },
  { value: "UTC", label: t("协调世界时（UTC）") },
  { value: "America/Los_Angeles", label: t("美国太平洋时间") },
  { value: "America/New_York", label: t("美国东部时间") },
]);
const router = useRouter();
const route = useRoute();
const programs = ref([]);
const programSearch = ref("");

function queryValues(value) {
  return (Array.isArray(value) ? value : [value])
    .flatMap(item => String(item || "").split(","))
    .map(item => item.trim())
    .filter(Boolean);
}

function castValuesFromQuery(value) {
  const names = [];
  queryValues(value).forEach(name => {
    const member = NIJIGASAKI_CAST.find(item => [item.name, ...item.aliases].includes(name));
    if (member && !names.includes(member.name)) names.push(member.name);
  });
  return names;
}

function sameValues(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

const programCastFilter = ref(castValuesFromQuery(route.query.cast));
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
const occurrenceAutoSaveState = ref("");
const occurrenceRows = ref([]);
const occurrenceListRef = ref(null);
const occurrenceEditorRef = ref(null);
const occurrenceImageFileInput = ref(null);
const occurrenceImageUrl = ref("");
const occurrenceImageUploading = ref(false);
const occurrenceImageDeletingId = ref("");
const occurrenceLightboxImages = ref([]);
const occurrenceLightboxIndex = ref(-1);
const customPerson = ref("");
const occurrenceGuestInput = ref("");
const importFileInput = ref(null);
const importPayload = ref(null);
const importPreview = ref(null);
const importFileName = ref("");
const importSubmitting = ref(false);
const importTargetMode = ref("new");
const importTargetProgramId = ref("");
const importTargetParentProgramId = ref("");
const message = ref("");
const error = ref("");
const occurrenceEditingRowKey = ref("");
let occurrenceAutoSaveTimer = 0;
let occurrenceAutoSaveQueued = false;
let occurrenceDraftSequence = 0;
let occurrenceDraftHydrating = false;
let occurrenceDraftBaseline = "";
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
    monthly_mode: "week",
    auto_generate: true,
    week_interval: 1,
    week_direction: "first",
    week_number: 1,
    day_index: 0,
    day_direction: "first",
    day_number: 1,
    weekday: 0,
    schedule_time: "",
    timezone: "Asia/Tokyo",
  };
}

function blankOccurrence() {
  return {
    id: "",
    original_date: "",
    title: "",
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
    images: [],
    guests: [],
    absent_members: [],
    generated: false,
    individual: false,
    materialized: false,
    manual: false,
    timezone: "",
    episode: 0,
    _draft: false,
    _draftKey: "",
  };
}

const form = reactive(blankProgram());
const occurrenceDraft = reactive(blankOccurrence());
const episodeStartMode = ref("one");
const episodeStartCustom = ref("");
const peopleOptions = computed(() => castCandidates);
const occurrenceGuestOptions = NIJIGASAKI_CAST;
const occurrenceAbsentCast = computed(() => NIJIGASAKI_CAST.filter(member => castMemberMatches(occurrenceDraft.absent_members, member)));
const allProgramCastSelected = computed(() => programCastFilter.value.length === castCandidates.length);
const programCastFilterLabel = computed(() => {
  if (!programCastFilter.value.length || allProgramCastSelected.value) return t("全部 Cast");
  return t("已选 {count} 位", { count: programCastFilter.value.length });
});
const filteredPrograms = computed(() => programs.value.filter(programMatchesFilters));
const customPeople = computed(() => form.people.filter(person => !castCandidates.includes(person)));
const occurrenceOriginalLocked = computed(() => Boolean(!occurrenceDraft.individual && (occurrenceDraft.generated || occurrenceDraft.id)));
const parentProgramTitle = computed(() => programs.value.find(program => program.id === form.parent_id)?.title || form.title || t("当前主节目"));
const relatedSubprograms = computed(() => {
  if (!editingId.value || form.parent_id) return [];
  return programs.value
    .filter(program => program.parent_id === editingId.value)
    .sort((left, right) => String(left.subprogram_name || "").localeCompare(String(right.subprogram_name || "")));
});
const generatedOccurrenceCount = computed(() => occurrenceRows.value.filter(row => row.generated).length);
const materializedOccurrenceCount = computed(() => occurrenceRows.value.filter(row => row.materialized && row.status !== "deleted").length);
const deletedOccurrenceCount = computed(() => occurrenceRows.value.filter(row => row.status === "deleted").length);
const adjustedOccurrenceCount = computed(() => occurrenceRows.value.filter(row => !row._draft && !row.generated && !row.materialized && row.status !== "deleted").length);
const occurrenceHelp = computed(() => {
  const details = [
    materializedOccurrenceCount.value ? t("{count} 个已播出并保存", { count: materializedOccurrenceCount.value }) : "",
    adjustedOccurrenceCount.value ? t("{count} 个单独调整", { count: adjustedOccurrenceCount.value }) : "",
    deletedOccurrenceCount.value ? t("{count} 个已删除", { count: deletedOccurrenceCount.value }) : "",
  ].filter(Boolean).join("，");
  return t("选择某一期后，可单独改期、取消、补录或增加本期嘉宾。当前显示 {generated} 个自动单集{details}。", {
    generated: generatedOccurrenceCount.value,
    details: details ? `，${details}` : "",
  });
});
const occurrenceTimezone = computed(() => occurrenceDraft.timezone || form.periods.find(period => period.start_date && occurrenceDraft.original_date >= period.start_date && (!period.end_date || occurrenceDraft.original_date <= period.end_date))?.timezone || form.periods[0]?.timezone || "Asia/Tokyo");
const occurrenceTimezoneLabel = computed(() => timezoneOptions.value.find(option => option.value === occurrenceTimezone.value)?.label || occurrenceTimezone.value);
const occurrencePeriod = computed(() => {
  const anchor = occurrenceDraft.generated_date || occurrenceDraft.original_date;
  return form.periods.find(period => period.start_date && anchor >= period.start_date && (!period.end_date || anchor <= period.end_date)) || null;
});
const recurringPeriods = computed(() => form.periods.filter(period => !["individual", "single"].includes(period.frequency)));
const occurrenceGenerationDescription = computed(() => {
  if (!form.auto_generate) return t("已关闭后续自动生成；仅保留已保存单集，不会为已有节目补建首期。");
  const currentPeriod = occurrencePeriod.value;
  if (currentPeriod && !["individual", "single"].includes(currentPeriod.frequency)) {
    return currentPeriod.auto_generate === false
      ? t("节目级开关已开启，但当前排期时期已关闭自动生成。")
      : t("按排期规则生成未来约半年的单集。");
  }
  if (recurringPeriods.value.length && recurringPeriods.value.every(period => period.auto_generate === false)) {
    return t("节目级开关已开启，但所有排期时期都已关闭自动生成。");
  }
  if (recurringPeriods.value.some(period => period.auto_generate === false)) {
    return t("节目级开关已开启，部分排期时期已关闭自动生成。");
  }
  return t("按排期规则生成未来约半年的单集。");
});
const canShiftFollowing = computed(() => occurrencePeriod.value?.frequency === "weekly" && Number(occurrencePeriod.value.week_interval) === 2);
const occurrenceRescheduleBaseDate = computed(() => occurrenceDraft.original_date
  ? addDays(occurrenceDraft.original_date, Number(occurrenceDraft.schedule_shift_days) || 0)
  : occurrenceDraft.effective_date || occurrenceDraft.adjusted_date || "");
const occurrenceDeleteLabel = computed(() => occurrenceDraft.status === "deleted" ? t("恢复单集") : t("删除单集"));
const occurrenceAutoSaveEnabled = computed(() => Boolean(editingId.value && (occurrenceDraft.id || occurrenceDraft.generated || occurrenceDraft.materialized)));
const selectedOccurrenceIndex = computed(() => {
  if (!occurrenceDraft.original_date) return -1;
  return occurrenceRows.value.findIndex(row => row.original_date === occurrenceDraft.original_date
    && row.original_time === occurrenceDraft.original_time
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
  if (activePanel.value === "edit") return editingId.value ? t("编辑节目") : t("添加节目");
  if (activePanel.value === "occurrences") return t("单集列表");
  return t("已录入节目");
});
const panelDescription = computed(() => {
  if (activePanel.value === "edit") return t("编辑节目基本资料、参与成员和排期规则；内容会保留在当前页面状态中。");
  if (activePanel.value === "occurrences") return t("查看自动生成的单集，并单独处理改期、取消、删除、补录和临时嘉宾。");
  return t("从节目索引中选择要编辑的条目，或直接新建一个主节目。");
});
const canMovePrevious = computed(() => panelIndex.value > 0);
const canMoveNext = computed(() => activePanel.value === "list" || (activePanel.value === "edit" && Boolean(editingId.value)));

const occurrenceItemRefs = new Map();

function programType(program) {
  return program.category === "official" ? t("官方节目") : t("个人节目");
}

function programStatus(program) {
  return program.status === "completed" ? t("已完结") : t("进行中");
}

function updateStatusLabel(program) {
  return program.update_status === "not_updated" ? t("未更新") : t("近期有更新");
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
  if (row.status === "deleted") return t("已删除");
  if (row.status === "cancelled") return t("已取消");
  const airStatus = row.aired ? t("已播出") : t("未播出");
  return row.status === "rescheduled" ? `${t("已改期")} · ${airStatus}` : airStatus;
}

function episodeLabel(episode, special = "") {
  return special === "EX" ? t("EX 特别节目") : t("第 {count} 期", { count: episode });
}

function occurrenceEpisodeLabel(row) {
  const label = episodeLabel(row.episode, row.special);
  return ["cancelled", "deleted"].includes(row.status) ? `${t("原定")} ${label}` : label;
}

function scheduleLabel(program) {
  const periods = program.periods || [];
  if (periods.length > 1) return t("{count} 个分段时期", { count: periods.length });
  if (!periods.length) return t("未设置排期");
  return periodScheduleLabel(periods[0]);
}

function programDateRangeLabel(program) {
  if (!program) return "";
  const start = String(program.start_date || "").trim();
  if (!start) return t("未填写");
  if (program.status !== "completed") return start;
  const periodEnds = (program.periods || [])
    .map(period => String(period.end_date || "").trim())
    .filter(Boolean)
    .sort();
  const end = String(program.end_date || "").trim() || periodEnds[periodEnds.length - 1] || start;
  return `${start} → ${end}`;
}

function periodScheduleLabel(period) {
  const time = period.schedule_time ? ` ${period.schedule_time}` : "";
  if (period.frequency === "single") return `${t("单次")}${time}`;
  if (period.frequency === "individual") return `${t("逐期设置 · 手动录入单集")}${time}`;
  const weekday = weekdayNames.value[period.weekday] || "";
  if (period.frequency === "monthly") {
    if (period.monthly_mode === "irregular") return `${t("每月 · 日期不定")}${time}`;
    if (period.monthly_mode === "day") {
      const direction = period.day_direction || (period.day_index < 0 ? "last" : "first");
      const number = period.day_number || Math.abs(period.day_index) || 1;
      const day = direction === "last" ? t("倒数第{count}天", { count: number }) : t("第{count}天", { count: number });
      return `${t("每月")}${day}${time}`;
    }
    const direction = period.week_direction || (period.week_index < 0 ? "last" : "first");
    const number = period.week_number || Math.abs(period.week_index) || 1;
    const week = direction === "last" ? t("倒数第{count}周", { count: number }) : t("第{count}周", { count: number });
    return `${t("每月")}${week}${weekday}${time}`;
  }
  const interval = period.week_interval > 1 ? t("每{count}周", { count: period.week_interval }) : t("每周");
  return `${interval}${weekday}${time}`;
}

function normalizeEpisodeStart(value) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 && parsed <= 9999 ? parsed : 1;
}

function syncEpisodeStartControls(value) {
  const normalized = normalizeEpisodeStart(value);
  form.episode_start = normalized;
  episodeStartMode.value = normalized === 0 ? "zero" : normalized === 1 ? "one" : "custom";
  episodeStartCustom.value = episodeStartMode.value === "custom" ? String(normalized) : "";
}

function setEpisodeStartMode(mode) {
  episodeStartMode.value = mode;
  if (mode === "zero") {
    form.episode_start = 0;
    episodeStartCustom.value = "";
  } else if (mode === "one") {
    form.episode_start = 1;
    episodeStartCustom.value = "";
  } else {
    if (!episodeStartCustom.value && Number(form.episode_start) > 1) episodeStartCustom.value = String(form.episode_start);
    form.episode_start = episodeStartCustom.value;
  }
}

function episodeStartPayload() {
  if (episodeStartMode.value === "zero") return 0;
  if (episodeStartMode.value === "one") return 1;
  const raw = String(episodeStartCustom.value ?? "").trim();
  if (!/^\d+$/.test(raw)) throw new Error(t("自定义首集编号必须是 0 到 9999 的整数"));
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed > 9999) throw new Error(t("自定义首集编号必须是 0 到 9999 的整数"));
  return parsed;
}

function formatLabel(program) {
  return `${program.format === "radio" ? t("广播") : t("有画面")} · ${program.platform === "tv" ? t("电视台") : t("网络")} · ${program.delivery === "live" ? t("直播") : t("录播")}`;
}

function showError(requestError) {
  if (requestError.status === 401) {
    router.replace({ path: "/admin/login", query: { redirect: "/admin/programs" } });
    return;
  }
  error.value = requestError.message || t("请求失败");
}

function downloadJson(fileName, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function safeJsonFileName(value, fallback) {
  const name = String(value || fallback).trim().replace(/[\\/:*?"<>|]+/g, "-").slice(0, 80);
  return `${name || fallback}.json`;
}

async function downloadTemplateJson() {
  message.value = "";
  error.value = "";
  try {
    const payload = await api("/api/admin/program-json-template");
    downloadJson(safeJsonFileName("nijidb-program-template", "nijidb-program-template"), payload);
    message.value = t("JSON 说明模板已下载");
  } catch (requestError) {
    showError(requestError);
  }
}

function exportProgramJson() {
  if (!editingId.value) {
    downloadTemplateJson();
    return;
  }
  downloadProgramJson();
}

async function downloadProgramJson() {
  message.value = "";
  error.value = "";
  try {
    const payload = await api(`/api/admin/programs/${editingId.value}/export`);
    downloadJson(safeJsonFileName(payload.program?.title, "nijidb-program"), payload);
    message.value = t("节目 JSON 已导出");
  } catch (requestError) {
    showError(requestError);
  }
}

function openImportPicker() {
  importFileInput.value?.click();
}

function stripJsonComments(source) {
  let result = "";
  let inString = false;
  let escaped = false;
  for (let index = 0; index < source.length; index += 1) {
    const current = source[index];
    const next = source[index + 1];
    if (inString) {
      result += current;
      if (escaped) escaped = false;
      else if (current === "\\") escaped = true;
      else if (current === '"') inString = false;
      continue;
    }
    if (current === '"') {
      inString = true;
      result += current;
    } else if (current === "/" && next === "/") {
      while (index < source.length && source[index] !== "\n") index += 1;
      result += "\n";
    } else if (current === "/" && next === "*") {
      index += 2;
      while (index < source.length && !(source[index] === "*" && source[index + 1] === "/")) index += 1;
      index += 1;
      result += " ";
    } else {
      result += current;
    }
  }
  return result.replace(/^\uFEFF/, "");
}

function importTitleMatched(preview) {
  const candidates = preview?._program_scope === "subprogram" ? preview.parent_choices : preview?.matches;
  return candidates?.some(match => match.match === "title") || false;
}

async function handleImportFile(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  message.value = "";
  error.value = "";
  try {
    const payload = JSON.parse(stripJsonComments(await file.text()));
    const preview = await api("/api/admin/programs/import/preview", { method: "POST", body: payload });
    importPayload.value = payload;
    importPreview.value = preview;
    importFileName.value = file.name;
    importTargetMode.value = preview._program_scope === "main" && preview.matches?.length ? "overwrite" : "new";
    importTargetProgramId.value = preview.matches?.[0]?.id || "";
    importTargetParentProgramId.value = preview.import_options?.target_parent_program_id || preview.parent_choices?.[0]?.id || "";
  } catch (requestError) {
    showError(requestError instanceof SyntaxError ? new Error(t("JSON 格式无效，请检查括号、逗号或字符串")) : requestError);
  }
}

function closeImportPreview() {
  importPayload.value = null;
  importPreview.value = null;
  importFileName.value = "";
  importTargetMode.value = "new";
  importTargetProgramId.value = "";
  importTargetParentProgramId.value = "";
}

function importDeliveryLabel(value) {
  if (value === "live") return t("直播");
  if (value === "recorded") return t("录播");
  return t("跟随节目默认");
}

function importScheduleModeLabel(value) {
  if (value === "current") return t("当前节目设置");
  return value === "generated" ? t("自动生成模式") : t("逐期准确模式");
}

function importStatusLabel(value) {
  if (value === "cancelled") return t("因故取消");
  if (value === "rescheduled") return t("已改期");
  if (value === "deleted") return t("已删除");
  return t("正常播出");
}

function importSpecialLabel(value) {
  return value === "EX" ? t("EX 特别节目") : t("普通单集");
}

function importOccurrenceEpisodeLabel(occurrence) {
  if (occurrence.special === "EX") return t("EX 特别节目");
  const label = t("第 {count} 期", { count: occurrence.episode });
  return ["cancelled", "deleted"].includes(occurrence.status) ? `${t("原定")} ${label}` : label;
}

async function submitImport() {
  if (!importPayload.value || importSubmitting.value) return;
  importSubmitting.value = true;
  message.value = "";
  error.value = "";
  try {
    const body = JSON.parse(JSON.stringify(importPayload.value));
    body.import_options = {
      ...(body.import_options || {}),
      target_mode: importTargetMode.value,
      target_program_id: importTargetMode.value === "overwrite" ? importTargetProgramId.value : "",
      target_parent_program_id: importPreview.value._program_scope === "subprogram" ? importTargetParentProgramId.value : "",
    };
    const data = await api("/api/admin/programs/import", { method: "POST", body });
    const saved = data.program;
    closeImportPreview();
    await loadPrograms();
    editingId.value = saved.id;
    editorOpen.value = true;
    setActivePanel("edit");
    applyProgram(saved);
    await loadOccurrences(saved.id);
    message.value = `${data.overwritten ? t("已覆盖") : t("已导入")}「${saved.title}」${data.imported_subprograms ? `，${data.imported_subprograms} ${t("个子节目")}` : ""}${data.imported_occurrences ? `，${data.imported_occurrences} ${t("期")} ${t("单集")}` : ""}`;
    if (data.automatic_backup?.filename) message.value += `；${t("导入前已自动备份当前数据库")}`;
    if (data.warnings?.length) message.value += `；${data.warnings.length} ${t("条导入提示")}`;
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (requestError) {
    showError(requestError);
  } finally {
    importSubmitting.value = false;
  }
}

function syncPanelQuery(panel) {
  const nextQuery = { ...route.query };
  if (panel === "list") {
    delete nextQuery.program;
    delete nextQuery.panel;
    delete nextQuery.occurrence;
    delete nextQuery.date;
  } else {
    if (editingId.value) nextQuery.program = editingId.value;
    else delete nextQuery.program;
    nextQuery.panel = panel === "occurrences" ? "occurrences" : "edit";
    if (panel !== "occurrences") {
      delete nextQuery.occurrence;
      delete nextQuery.date;
    }
  }
  if (JSON.stringify(nextQuery) !== JSON.stringify(route.query)) router.replace({ path: route.path, query: nextQuery });
}

function setActivePanel(panel, syncUrl = true) {
  activePanel.value = panel;
  if (syncUrl) syncPanelQuery(panel);
}

function showPanel(panel) {
  if (panel === "occurrences" && !editingId.value) return;
  setActivePanel(panel);
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

watch(() => route.query.cast, value => {
  const nextFilter = castValuesFromQuery(value);
  if (!sameValues(programCastFilter.value, nextFilter)) programCastFilter.value = nextFilter;
});

watch(programCastFilter, value => {
  const nextQuery = { ...route.query };
  if (value.length && !allProgramCastSelected.value) nextQuery.cast = value.join(",");
  else delete nextQuery.cast;
  if (JSON.stringify(nextQuery) !== JSON.stringify(route.query)) router.replace({ path: route.path, query: nextQuery });
}, { deep: true });

watch(() => occurrenceBody(), () => {
  if (occurrenceDraftHydrating || !occurrenceAutoSaveEnabled.value) return;
  if (occurrenceDraftSignature() === occurrenceDraftBaseline) return;
  queueOccurrenceAutoSave();
}, { deep: true });

async function refreshPrograms() {
  const data = await api("/api/programs");
  programs.value = data.programs;
}

async function loadPrograms() {
  loading.value = true;
  try {
    await refreshPrograms();
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
  const requestedPanelValue = requestedPanel.value;
  const requestedOccurrenceIdValue = requestedOccurrenceId.value;
  const requestedOccurrenceDateValue = requestedOccurrenceDate.value;
  const program = programs.value.find(item => item.id === requestedProgramId.value);
  if (!program) {
    error.value = t("节目不存在或已被删除");
    return;
  }
  await editProgram(program, { syncUrl: false });
  if (requestedPanelValue !== "occurrences" && !requestedOccurrenceIdValue && !requestedOccurrenceDateValue) return;

  setActivePanel("occurrences");
  if (!requestedOccurrenceIdValue && !requestedOccurrenceDateValue) {
    await focusOccurrenceEditor();
    return;
  }
  const row = occurrenceRows.value.find(item => requestedOccurrenceIdValue && String(item.id) === requestedOccurrenceIdValue)
    || occurrenceRows.value.find(item => requestedOccurrenceDateValue && item.original_date === requestedOccurrenceDateValue);
  if (row) {
    editOccurrence(row);
    await focusOccurrenceEditor(row);
  } else {
    error.value = t("单集不存在或已不在当前排期中");
    await focusOccurrenceEditor();
  }
}

function applyProgram(program) {
  const episodeStart = normalizeEpisodeStart(program.episode_start);
  const sourcePeriods = program.periods && program.periods.length ? program.periods : [legacyPeriod(program)];
  const periods = sourcePeriods.map(period => {
    const legacyIrregular = period.frequency === "irregular";
    const frequency = legacyIrregular ? "monthly" : period.frequency || "weekly";
    const monthlyMode = frequency === "monthly"
      ? period.monthly_mode || (legacyIrregular ? "irregular" : program.monthly_mode || "week")
      : "week";
    const hasAutoGenerate = period.auto_generate !== undefined
      && period.auto_generate !== null
      && period.auto_generate !== "";
    const autoGenerate = frequency === "individual"
      ? false
      : frequency === "single"
        ? true
        : hasAutoGenerate
          ? ![false, 0, "0", "false"].includes(period.auto_generate)
          : program.auto_generate !== false;
    const weekIndex = Number(period.week_index) || 1;
    return {
      ...blankPeriod(),
      ...period,
      frequency,
      monthly_mode: ["week", "day", "irregular"].includes(monthlyMode) ? monthlyMode : "week",
      auto_generate: autoGenerate,
      week_interval: Number(period.week_interval) || 1,
      week_direction: weekIndex < 0 ? "last" : "first",
      week_number: Math.abs(weekIndex) || 1,
      day_direction: Number(period.day_index) < 0 ? "last" : "first",
      day_number: Math.abs(Number(period.day_index)) || 1,
    };
  });
  const singleRecurringPeriod = periods.length === 1 && !["individual", "single"].includes(periods[0].frequency);
  if (singleRecurringPeriod) {
    // Loading legacy data must never turn a disabled program back on.
    periods[0].auto_generate = periods[0].auto_generate && program.auto_generate !== false;
  }
  Object.assign(form, {
    title: program.title || "",
    parent_id: program.parent_id || "",
    subprogram_name: program.subprogram_name || (program.parent_id ? "" : "主节目"),
    category: program.category || "personal",
    format: program.format || "video",
    platform: program.platform || "network",
    delivery: program.delivery || "recorded",
    auto_generate: singleRecurringPeriod ? periods[0].auto_generate !== false : program.auto_generate !== false,
    episode_start: episodeStart,
    people: [...(program.people || [])],
    official_url: program.official_url || "",
    description: program.description || "",
    periods,
  });
  syncEpisodeStartControls(episodeStart);
}

function legacyPeriod(program) {
  const legacyIrregular = program.frequency === "irregular" || program.monthly_mode === "irregular";
  const frequency = legacyIrregular ? "monthly" : program.frequency || "weekly";
  return {
    start_date: program.start_date || "",
    end_date: frequency === "single" ? program.start_date || "" : program.end_date || "",
    frequency,
    monthly_mode: legacyIrregular ? "irregular" : program.monthly_mode || "week",
    auto_generate: frequency === "individual" ? false : frequency === "single" ? true : program.auto_generate !== false,
    week_interval: program.week_interval || 1,
    week_index: legacyIrregular ? 0 : program.week_index || 1,
    day_index: legacyIrregular ? 0 : program.day_index || 0,
    day_direction: Number(program.day_index) < 0 ? "last" : "first",
    day_number: Math.abs(Number(program.day_index)) || 1,
    weekday: legacyIrregular || frequency === "individual" ? 0 : program.weekday || 0,
    schedule_time: legacyIrregular ? "" : program.schedule_time || "",
    timezone: program.timezone || "Asia/Tokyo",
  };
}

function resetOccurrenceDraft() {
  window.clearTimeout(occurrenceAutoSaveTimer);
  occurrenceAutoSaveTimer = 0;
  occurrenceAutoSaveQueued = false;
  occurrenceDraftHydrating = true;
  Object.assign(occurrenceDraft, blankOccurrence());
  occurrenceGuestInput.value = "";
  occurrenceImageUrl.value = "";
  closeOccurrenceLightbox();
  occurrenceEditingRowKey.value = "";
  occurrenceAutoSaveState.value = "";
  occurrenceDraftBaseline = occurrenceDraftSignature();
  nextTick(() => {
    occurrenceDraftHydrating = false;
    occurrenceDraftBaseline = occurrenceDraftSignature();
  });
}

function resetForm() {
  Object.assign(form, blankProgram());
  episodeStartMode.value = "one";
  episodeStartCustom.value = "";
  editingId.value = "";
  editorOpen.value = false;
  setActivePanel("list");
  occurrenceRows.value = [];
  resetOccurrenceDraft();
}

function startNewProgram() {
  resetForm();
  editorOpen.value = true;
  setActivePanel("edit");
  message.value = t("正在新建主节目");
  error.value = "";
}

async function refreshOccurrences(programId) {
  if (!programId) return;
  const data = await api(`/api/admin/programs/${programId}/occurrences`);
  occurrenceRows.value = data.occurrences;
}

async function loadOccurrences(programId) {
  if (!programId) return;
  occurrenceLoading.value = true;
  try {
    await refreshOccurrences(programId);
  } catch (requestError) {
    showError(requestError);
  } finally {
    occurrenceLoading.value = false;
  }
}

async function editProgram(program, { syncUrl = true } = {}) {
  applyProgram(program);
  editorOpen.value = true;
  editingId.value = program.id;
  setActivePanel("edit", syncUrl);
  resetOccurrenceDraft();
  message.value = "";
  error.value = "";
  await loadOccurrences(program.id);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function returnToParentProgram() {
  const parent = programs.value.find(program => program.id === form.parent_id);
  if (!parent) {
    error.value = t("所属主节目不存在或已被删除");
    return;
  }
  await editProgram(parent);
}

function setPeriodFrequency(period, value) {
  const previousFrequency = period.frequency;
  period.frequency = value;
  if (value === "single") period.end_date = period.start_date;
  else if (previousFrequency === "single") period.end_date = "";
  if (value === "individual") {
    period.auto_generate = false;
    period.monthly_mode = "week";
    period.day_index = 0;
    period.schedule_time = "";
  } else if (value === "single") {
    period.auto_generate = true;
    period.monthly_mode = "week";
    period.day_index = 0;
  } else if (value !== previousFrequency && previousFrequency === "individual") {
    period.auto_generate = true;
  }
  if (value !== "monthly") period.monthly_mode = "week";
  if (value === "monthly" && !period.monthly_mode) period.monthly_mode = "week";
  if (value === "monthly" && period.monthly_mode === "irregular") {
    period.week_index = 0;
    period.day_index = 0;
    period.weekday = 0;
  }
}

function setMonthlyMode(period, value) {
  period.monthly_mode = value;
  if (value === "irregular") {
    period.week_index = 0;
    period.day_index = 0;
    period.weekday = 0;
  } else if (value === "day") {
    period.week_index = 0;
    period.day_index = 0;
    period.weekday = 0;
    period.day_direction ||= "first";
    normalizePeriodDayNumber(period);
  } else {
    period.day_index = 0;
    period.week_number ||= 1;
    period.week_direction ||= "first";
  }
}

function setPeriodWeekDirection(period, value) {
  period.week_direction = value;
}

function setPeriodDayDirection(period, value) {
  period.day_direction = value;
  normalizePeriodDayNumber(period);
}

function normalizePeriodDayNumber(period) {
  const max = period.day_direction === "last" ? 14 : 28;
  const number = Math.trunc(Number(period.day_number) || 1);
  period.day_number = Math.min(Math.max(number, 1), max);
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

function occurrenceCast(row) {
  const selected = occurrenceRowKey(row) === occurrenceRowKey(occurrenceDraft);
  const guests = selected ? occurrenceDraft.guests : row.guests;
  const absentMembers = selected ? occurrenceDraft.absent_members : row.absent_members;
  return castColorSegments([...(form.people || []), ...(guests || [])], absentMembers);
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

function occurrenceGuestSelected(member) {
  return !castMemberMatches(occurrenceDraft.absent_members, member)
    && (castMemberMatches(form.people, member) || castMemberMatches(occurrenceDraft.guests, member));
}

function occurrenceGuestIsFixed(member) {
  return castMemberMatches(form.people, member);
}

function toggleOccurrenceGuest(member) {
  const selected = occurrenceGuestSelected(member);
  const matchingGuest = occurrenceDraft.guests.find(guest => [member.name, ...member.aliases].includes(String(guest || "").trim()));
  const matchingAbsence = occurrenceDraft.absent_members.find(absent => [member.name, ...member.aliases].includes(String(absent || "").trim()));
  if (selected) {
    if (occurrenceGuestIsFixed(member) && !matchingAbsence) occurrenceDraft.absent_members.push(member.name);
    if (matchingGuest) removeOccurrenceGuest(matchingGuest);
    return;
  }
  if (matchingAbsence) occurrenceDraft.absent_members = occurrenceDraft.absent_members.filter(item => item !== matchingAbsence);
  if (!occurrenceGuestIsFixed(member) && !matchingGuest) occurrenceDraft.guests.push(member.name);
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
  setActivePanel("edit");
  form.parent_id = parent.id;
  form.title = parent.title;
  form.subprogram_name = "";
  message.value = t("正在添加「{title}」的子节目", { title: parent.title });
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

function setPeriodAutoGeneration(period, enabled) {
  period.auto_generate = enabled;
  if (form.periods.length === 1) form.auto_generate = enabled;
}

function programBody() {
  const periods = form.periods.map(period => ({
    start_date: period.start_date,
    end_date: period.frequency === "single" ? period.start_date : period.end_date,
    frequency: period.frequency,
    monthly_mode: period.frequency === "monthly"
      ? (period.monthly_mode === "irregular" ? "irregular" : period.monthly_mode === "day" ? "day" : "week")
      : "week",
    auto_generate: period.frequency === "individual" ? false : period.frequency === "single" ? true : period.auto_generate !== false,
    week_interval: Number(period.week_interval) || 1,
    week_index: period.frequency === "monthly" && period.monthly_mode === "week"
      ? (period.week_direction === "last" ? -Number(period.week_number) : Number(period.week_number))
      : 0,
    day_index: period.frequency === "monthly" && period.monthly_mode === "day"
      ? (period.day_direction === "last" ? -Number(period.day_number) : Number(period.day_number))
      : 0,
    weekday: period.frequency === "weekly" || (period.frequency === "monthly" && period.monthly_mode === "week") ? Number(period.weekday) : 0,
    schedule_time: period.schedule_time,
    timezone: period.timezone || "Asia/Tokyo",
  }));
  const sorted = [...periods].sort((left, right) => left.start_date.localeCompare(right.start_date));
  const first = sorted[0] || {};
  const last = sorted[sorted.length - 1] || {};
  const allSingle = sorted.length > 0 && sorted.every(period => period.frequency === "single");
  const singleRecurringPeriod = sorted.length === 1 && !["individual", "single"].includes(sorted[0].frequency);
  return {
    title: form.title,
    category: form.category,
     format: form.format,
     platform: form.platform,
      delivery: form.delivery,
     auto_generate: singleRecurringPeriod ? sorted[0].auto_generate : form.auto_generate,
        episode_start: episodeStartPayload(),
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
    day_index: first.day_index || 0,
    weekday: first.weekday || 0,
    monthly_mode: first.monthly_mode || "week",
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
    message.value = successMessage || (method === "PATCH" ? t("节目已更新") : t("节目已添加，现在可以维护单集排期"));
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
    if (form.periods.length === 1 && !["individual", "single"].includes(form.periods[0].frequency)) {
      form.periods[0].auto_generate = requested;
    }
    const retained = Number(data.materialized_count || 0);
    message.value = requested
      ? t("已开启后续单集自动生成")
      : retained
        ? t("已关闭自动生成，并保留 {count} 个已播出单集，现在可以逐条添加单集", { count: retained })
        : t("已关闭后续单集自动生成，现在可以逐条添加单集");
  } catch (requestError) {
    form.auto_generate = !requested;
    showError(requestError);
  }
}

async function convertMonthlyToIndividual() {
  const fixedMonthlyPeriods = form.periods.filter(period => period.frequency === "monthly" && period.monthly_mode !== "irregular");
  if (!editingId.value || !fixedMonthlyPeriods.length) return;
  if (!window.confirm(t("将当前节目中的固定月更改为逐期设置？之后不再自动生成单集，已有单集会保留，可按实际日期逐期维护。"))) return;
  const previousFrequencies = form.periods.map(period => period.frequency);
  const previousMonthlyModes = form.periods.map(period => period.monthly_mode);
  form.periods.forEach(period => {
    if (period.frequency === "monthly" && period.monthly_mode !== "irregular") {
      period.frequency = "individual";
      period.monthly_mode = "week";
      period.auto_generate = false;
    }
  });
  const saved = await saveProgram(t("已将固定月更切换为逐期设置"));
  if (!saved) {
    form.periods.forEach((period, index) => {
      period.frequency = previousFrequencies[index];
      period.monthly_mode = previousMonthlyModes[index];
    });
  }
}

async function applyRescheduledToOriginal() {
  if (!editingId.value) return;
  const count = occurrenceRows.value.filter(row => row.status === "rescheduled").length;
  if (!count || !window.confirm(t("将 {count} 期的已改期日期和时间覆盖为新的原定播出时间，并清除改期标记？", { count }))) return;
  occurrenceSaving.value = true;
  message.value = "";
  error.value = "";
  try {
    const data = await api(`/api/admin/programs/${editingId.value}/occurrences/restore-rescheduled`, { method: "POST" });
    await Promise.all([loadPrograms(), loadOccurrences(editingId.value)]);
    resetOccurrenceDraft();
    message.value = t("已将 {count} 期改期时间覆盖为新的原定播出时间", { count: data.count });
  } catch (requestError) {
    showError(requestError);
  } finally {
    occurrenceSaving.value = false;
  }
}

function editOccurrence(row) {
  window.clearTimeout(occurrenceAutoSaveTimer);
  occurrenceAutoSaveTimer = 0;
  occurrenceAutoSaveQueued = false;
  occurrenceDraftHydrating = true;
  occurrenceImageUrl.value = "";
  closeOccurrenceLightbox();
  setActivePanel("occurrences");
  occurrenceEditingRowKey.value = occurrenceRowKey(row);
  Object.assign(occurrenceDraft, {
    id: row.id || "",
    episode: Number(row.episode) || 0,
    original_date: row.original_date || "",
    title: row.title || "",
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
     adjusted_time: row.adjusted_time || (row.status === "rescheduled" ? row.original_time || "" : ""),
     note: row.note || "",
     images: [...(row.images || [])],
     guests: [...(row.guests || [])],
     absent_members: [...(row.absent_members || [])],
     generated: Boolean(row.generated),
    individual: Boolean(row.individual),
    materialized: Boolean(row.materialized),
    manual: Boolean(row.manual),
    timezone: row.timezone || "",
    _draft: Boolean(row._draft),
    _draftKey: row._draftKey || "",
  });
  occurrenceGuestInput.value = "";
  occurrenceAutoSaveState.value = "";
  occurrenceDraftBaseline = occurrenceDraftSignature();
  nextTick(() => {
    occurrenceDraftHydrating = false;
    occurrenceDraftBaseline = occurrenceDraftSignature();
  });
}

function occurrenceImages(value = occurrenceDraft) {
  const source = value?.images || [];
  return (Array.isArray(source) ? source : [])
    .filter(image => image?.url)
    .map(image => ({ ...image, alt: image.alt || image.alt_text || "" }));
}

function openOccurrenceLightbox(index = 0) {
  const images = occurrenceImages();
  if (!images.length) return;
  occurrenceLightboxImages.value = images;
  occurrenceLightboxIndex.value = Math.max(0, Math.min(images.length - 1, index));
}

function closeOccurrenceLightbox() {
  occurrenceLightboxIndex.value = -1;
  occurrenceLightboxImages.value = [];
}

function openOccurrenceImagePicker() {
  occurrenceImageFileInput.value?.click();
}

function applyOccurrenceImageResponse(data) {
  const saved = data?.occurrence;
  if (!saved) return;
  const rowKey = occurrenceEditingRowKey.value || occurrenceRowKey(occurrenceDraft);
  occurrenceDraft.images = [...(saved.images || [])];
  updateSavedOccurrence(rowKey, saved);
}

async function ensureOccurrenceSavedForImage() {
  if (occurrenceDraft.id) return true;
  if (!occurrenceDraft.original_date) {
    error.value = t("请先填写单集日期，再添加返图");
    return false;
  }
  const saved = await saveOccurrence();
  if (!saved || !occurrenceDraft.id) {
    if (!error.value) error.value = t("请先保存单集，再添加返图");
    return false;
  }
  return true;
}

async function uploadOccurrenceImageRequest(path, options) {
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const requestError = new Error(typeof payload === "object" ? payload.detail || t("请求失败") : t("请求失败"));
    requestError.status = response.status;
    throw requestError;
  }
  return payload;
}

async function uploadOccurrenceImageFiles(files) {
  if (!files.length || occurrenceImageUploading.value || occurrenceSaving.value) return;
  if (!(await ensureOccurrenceSavedForImage())) return;
  occurrenceImageUploading.value = true;
  message.value = "";
  error.value = "";
  let added = 0;
  try {
    for (const file of files) {
      const data = await uploadOccurrenceImageRequest(
        `/api/admin/programs/${encodeURIComponent(editingId.value)}/occurrences/${encodeURIComponent(occurrenceDraft.id)}/images`,
        {
          method: "POST",
          headers: {
            "Content-Type": file.type || "application/octet-stream",
            "X-Filename": encodeURIComponent(file.name || "clipboard-image"),
            "X-Alt-Text": encodeURIComponent(file.name || "clipboard-image"),
          },
          body: file,
        },
      );
      applyOccurrenceImageResponse(data);
      added += 1;
    }
    message.value = t("已添加 {count} 张返图", { count: added });
  } catch (requestError) {
    showError(requestError);
    if (added) message.value = t("已添加 {count} 张返图，另有图片失败", { count: added });
  } finally {
    occurrenceImageUploading.value = false;
  }
}

async function handleOccurrenceImageFiles(event) {
  const files = [...(event.target.files || [])];
  event.target.value = "";
  await uploadOccurrenceImageFiles(files);
}

async function handleOccurrenceImagePaste(event) {
  if (occurrenceImageUploading.value || occurrenceSaving.value) return;
  const item = Array.from(event.clipboardData?.items || []).find(
    (candidate) => candidate.kind === "file" && candidate.type.startsWith("image/"),
  );
  const file = item?.getAsFile();
  if (!file) return;
  event.preventDefault();
  await uploadOccurrenceImageFiles([file]);
}

async function addOccurrenceImageUrl() {
  const url = occurrenceImageUrl.value.trim();
  if (!url || occurrenceImageUploading.value) return;
  if (!(await ensureOccurrenceSavedForImage())) return;
  occurrenceImageUploading.value = true;
  message.value = "";
  error.value = "";
  try {
    const data = await api(
      `/api/admin/programs/${encodeURIComponent(editingId.value)}/occurrences/${encodeURIComponent(occurrenceDraft.id)}/images`,
      { method: "POST", body: { url } },
    );
    applyOccurrenceImageResponse(data);
    occurrenceImageUrl.value = "";
    message.value = t("已添加返图直链");
  } catch (requestError) {
    showError(requestError);
  } finally {
    occurrenceImageUploading.value = false;
  }
}

async function deleteOccurrenceImage(image) {
  if (!occurrenceDraft.id || !image?.id || occurrenceImageDeletingId.value) return;
  if (!window.confirm(t("确定删除这张返图吗？"))) return;
  occurrenceImageDeletingId.value = String(image.id);
  message.value = "";
  error.value = "";
  try {
    const data = await api(
      `/api/admin/programs/${encodeURIComponent(editingId.value)}/occurrences/${encodeURIComponent(occurrenceDraft.id)}/images/${encodeURIComponent(image.id)}`,
      { method: "DELETE" },
    );
    applyOccurrenceImageResponse(data);
    message.value = t("返图已删除");
  } catch (requestError) {
    showError(requestError);
  } finally {
    occurrenceImageDeletingId.value = "";
  }
}

function occurrenceRowKey(row) {
  return row._draftKey || `${row.id || "generated"}-${row.original_date}-${row.original_time || "all-day"}`;
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
  const targetTop = itemTop - list.clientHeight * 0.15 + item.offsetHeight / 2;
  const maxTop = Math.max(0, list.scrollHeight - list.clientHeight);
  const top = Math.min(maxTop, Math.max(0, targetTop));
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

function nextOccurrenceEpisode() {
  const episodeStart = normalizeEpisodeStart(form.episode_start);
  const regularOccurrences = occurrenceRows.value.filter(row => !row._draft && row.special !== "EX" && !["cancelled", "deleted"].includes(row.status));
  return episodeStart + regularOccurrences.length;
}

function newOccurrence() {
  setActivePanel("occurrences");
  const pending = occurrenceRows.value.find(row => row._draft);
  if (pending) {
    editOccurrence(pending);
    nextTick(() => scrollOccurrenceListTo(pending));
    return;
  }
  const lastOccurrence = [...occurrenceRows.value].reverse().find(row => !row._draft && row.status !== "deleted");
  const originalDate = lastOccurrence?.original_date || lastOccurrence?.date || "";
  const defaultPeriod = form.periods.find(period => {
    if (!period.start_date) return false;
    return !originalDate || (originalDate >= period.start_date && (!period.end_date || originalDate <= period.end_date));
  }) || form.periods[0];
  const originalTime = lastOccurrence
    ? String(lastOccurrence.original_time ?? lastOccurrence.time ?? "")
    : String(defaultPeriod?.schedule_time || "");
  const draft = {
    ...blankOccurrence(),
    original_date: originalDate,
    original_time: originalTime,
    effective_date: originalDate,
    timezone: lastOccurrence?.timezone || form.periods[0]?.timezone || "",
    _draft: true,
    _draftKey: `draft-${Date.now()}-${occurrenceDraftSequence += 1}`,
    episode: nextOccurrenceEpisode(),
  };
  occurrenceRows.value = [...occurrenceRows.value, draft];
  editOccurrence(draft);
  nextTick(() => scrollOccurrenceListTo(draft));
}

function leaveOccurrenceFocus() {
  setActivePanel("edit");
}

function occurrenceBody(overrides = {}) {
  return {
    original_date: occurrenceDraft.original_date,
    title: occurrenceDraft.title,
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
    absent_members: [...occurrenceDraft.absent_members],
    ...overrides,
  };
}

function savedOccurrenceRow(previous, saved) {
  const status = saved.status || previous?.status || "scheduled";
  const originalDate = saved.original_date ?? previous?.original_date ?? "";
  const originalTime = saved.original_time ?? previous?.original_time ?? "";
  const adjustedDate = saved.adjusted_date || "";
  const adjustedTime = saved.adjusted_time || "";
  const effectiveDate = saved.date ?? (status === "rescheduled" && adjustedDate ? adjustedDate : originalDate);
  const effectiveTime = saved.time ?? (status === "rescheduled" && adjustedTime ? adjustedTime : originalTime);
  return {
    ...previous,
    ...saved,
    id: saved.id || previous?.id || "",
    date: effectiveDate,
    time: effectiveTime,
    delivery: saved.delivery || previous?.delivery || form.delivery,
    delivery_override: saved.delivery_override ?? saved.delivery ?? previous?.delivery_override ?? "",
    status,
    generated: typeof saved.generated === "boolean" ? saved.generated : false,
    individual: typeof saved.individual === "boolean" ? saved.individual : previous?.individual ?? occurrenceDraft.individual,
    materialized: saved.materialized == null ? Boolean(previous?.materialized) : Boolean(saved.materialized),
    manual: typeof saved.manual === "boolean" ? saved.manual : !saved.generated_date && !Boolean(saved.materialized),
    timezone: saved.timezone || previous?.timezone || occurrenceDraft.timezone || form.periods[0]?.timezone || "Asia/Tokyo",
    _draft: false,
    _draftKey: "",
    aired: typeof saved.aired === "boolean" ? saved.aired : Boolean(previous?.aired),
  };
}

function occurrenceSortValue(row, key) {
  return String(row[key] || "");
}

function synchronizeOccurrenceRows() {
  const orderedByOriginal = occurrenceRows.value
    .map((row, index) => ({ row, index }))
    .sort((left, right) => occurrenceSortValue(left.row, "original_date").localeCompare(occurrenceSortValue(right.row, "original_date"))
      || occurrenceSortValue(left.row, "original_time").localeCompare(occurrenceSortValue(right.row, "original_time"))
      || Number(left.row.id || 0) - Number(right.row.id || 0)
      || left.index - right.index);
  let episode = normalizeEpisodeStart(form.episode_start);
  orderedByOriginal.forEach(({ row }) => {
    row.episode = episode;
    if (!["cancelled", "deleted"].includes(row.status) && row.special !== "EX") episode += 1;
  });
  occurrenceRows.value = [...occurrenceRows.value].sort((left, right) => occurrenceSortValue(left, "date").localeCompare(occurrenceSortValue(right, "date"))
    || occurrenceSortValue(left, "time").localeCompare(occurrenceSortValue(right, "time"))
    || occurrenceSortValue(left, "original_date").localeCompare(occurrenceSortValue(right, "original_date"))
    || Number(left.id || 0) - Number(right.id || 0));
}

function updateSavedOccurrence(rowKey, saved) {
  const index = occurrenceRows.value.findIndex(row => (saved.id && String(row.id) === String(saved.id)) || occurrenceRowKey(row) === rowKey);
  if (index < 0) return null;
  occurrenceRows.value.splice(index, 1, savedOccurrenceRow(occurrenceRows.value[index], saved));
  synchronizeOccurrenceRows();
  return occurrenceRows.value.find(row => saved.id && String(row.id) === String(saved.id)) || null;
}

function occurrenceDraftSignature() {
  return JSON.stringify(occurrenceBody());
}

function queueOccurrenceAutoSave(delay = 400) {
  window.clearTimeout(occurrenceAutoSaveTimer);
  occurrenceAutoSaveState.value = "pending";
  occurrenceAutoSaveTimer = window.setTimeout(() => {
    occurrenceAutoSaveTimer = 0;
    saveOccurrence({ auto: true });
  }, delay);
}

async function saveOccurrence({ auto = false } = {}) {
  if (!editingId.value) return;
  if (occurrenceSaving.value) {
    if (auto) occurrenceAutoSaveQueued = true;
    return false;
  }
  window.clearTimeout(occurrenceAutoSaveTimer);
  occurrenceAutoSaveTimer = 0;
  const position = captureOccurrencePosition();
  const rowKey = occurrenceEditingRowKey.value || occurrenceRowKey(occurrenceDraft);
  const draftId = occurrenceDraft.id;
  const body = occurrenceBody();
  const draftSignature = JSON.stringify(body);
  occurrenceSaving.value = true;
  if (!auto) {
    message.value = "";
    error.value = "";
  } else {
    occurrenceAutoSaveState.value = "saving";
  }
  try {
    const path = draftId
      ? `/api/admin/programs/${editingId.value}/occurrences/${draftId}`
      : `/api/admin/programs/${editingId.value}/occurrences`;
    const data = await api(path, { method: draftId ? "PATCH" : "POST", body });
    const saved = data.occurrence;
    const wasCurrent = occurrenceEditingRowKey.value === rowKey;
    const draftUnchanged = wasCurrent && occurrenceDraftSignature() === draftSignature;
    if (saved) updateSavedOccurrence(rowKey, saved);
    if (wasCurrent && saved) {
      occurrenceDraft.id = saved.id || occurrenceDraft.id;
      occurrenceDraft.generated = false;
      occurrenceDraft.materialized = Boolean(saved.materialized);
      occurrenceDraft._draft = false;
      occurrenceDraft._draftKey = "";
    }

    // The mutation response only describes this row. Reload the generated list as well,
    // because a status/date change can affect episode numbers and following weekly slots.
    await Promise.all([refreshPrograms(), refreshOccurrences(editingId.value)]);
    const refreshed = saved
      ? occurrenceRows.value.find(row => (saved.id && String(row.id) === String(saved.id)) || occurrenceRowKey(row) === rowKey)
      : null;
    const currentSelection = wasCurrent && occurrenceEditingRowKey.value === rowKey;
    if (currentSelection && saved) {
      occurrenceEditingRowKey.value = refreshed ? occurrenceRowKey(refreshed) : occurrenceEditingRowKey.value;
      if (draftUnchanged && refreshed) editOccurrence(refreshed);
    }
    if (auto) {
      if (currentSelection && draftUnchanged) {
        occurrenceDraftBaseline = occurrenceDraftSignature();
        occurrenceAutoSaveState.value = "saved";
      }
    } else {
      message.value = draftId ? t("单集排期已更新") : t("单集排期已添加");
      if (currentSelection && draftUnchanged) occurrenceDraftBaseline = occurrenceDraftSignature();
      await restoreOccurrencePosition(position);
    }
    return true;
  } catch (requestError) {
    if (auto) occurrenceAutoSaveState.value = "error";
    showError(requestError);
    return false;
  } finally {
    occurrenceSaving.value = false;
    if (occurrenceAutoSaveQueued) {
      occurrenceAutoSaveQueued = false;
      queueOccurrenceAutoSave(0);
    }
  }
}

async function deleteOccurrence() {
  if ((!occurrenceDraft.id && !occurrenceDraft.generated) || !editingId.value) return;
  if (!window.confirm(t("删除后，这条单集不会再显示在生成列表和日历中；如需保留请使用“因故取消”。继续吗？"))) return;
  window.clearTimeout(occurrenceAutoSaveTimer);
  occurrenceAutoSaveTimer = 0;
  occurrenceAutoSaveQueued = false;
  const position = captureOccurrencePosition();
  const draftId = occurrenceDraft.id;
  occurrenceSaving.value = true;
  try {
    if (draftId) {
      await api(`/api/admin/programs/${editingId.value}/occurrences/${draftId}`, { method: "DELETE" });
    } else {
      await api(`/api/admin/programs/${editingId.value}/occurrences`, {
        method: "POST",
        body: occurrenceBody({ status: "deleted", adjusted_date: "", adjusted_time: "", shift_following_days: 0 }),
      });
    }
    await Promise.all([refreshPrograms(), refreshOccurrences(editingId.value)]);
    resetOccurrenceDraft();
    await restoreOccurrencePosition(position);
    message.value = t("单集已删除");
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
  if (!error.value) message.value = t("单集已恢复");
}

async function toggleOccurrenceDeletion() {
  if (occurrenceDraft.status === "deleted") await restoreDeletedOccurrence();
  else await deleteOccurrence();
}

async function clearOccurrenceList() {
  if (!editingId.value) return;
  const title = form.title || t("当前主节目");
  if (!window.confirm(t("第一次确认：确定清空「{title}」的全部单集覆盖吗？排期规则生成的单集不会删除。", { title }))) return;
  if (!window.confirm(t("第二次确认：清空「{title}」的全部单集覆盖不可恢复，继续吗？", { title }))) return;
  window.clearTimeout(occurrenceAutoSaveTimer);
  occurrenceAutoSaveTimer = 0;
  occurrenceAutoSaveQueued = false;
  occurrenceSaving.value = true;
  message.value = "";
  error.value = "";
  try {
    const data = await api(`/api/admin/programs/${editingId.value}/occurrences`, { method: "DELETE" });
    await Promise.all([refreshPrograms(), refreshOccurrences(editingId.value)]);
    resetOccurrenceDraft();
    message.value = t("已清空 {count} 条单集覆盖", { count: data.deleted_count || 0 });
  } catch (requestError) {
    showError(requestError);
  } finally {
    occurrenceSaving.value = false;
  }
}

async function deleteProgram(program) {
  if (!window.confirm(t("第一次确认：确定删除「{title}」吗？删除后节目及其排期、单集资料都会移除。", { title: program.title }))) return;
  if (!window.confirm(t("第二次确认：删除「{title}」不可恢复，继续删除吗？", { title: program.title }))) return;
  deletingId.value = program.id;
  message.value = "";
  error.value = "";
  try {
    await api(`/api/admin/programs/${program.id}`, { method: "DELETE" });
    programs.value = programs.value.filter(item => item.id !== program.id);
    if (editingId.value === program.id) resetForm();
    message.value = t("节目已删除");
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
  window.clearTimeout(occurrenceAutoSaveTimer);
  document.removeEventListener("click", closeProgramCastFilter);
});
</script>

<template>
  <main class="page program-admin-page" :class="{ 'occurrence-focused': activePanel === 'occurrences' }">
    <div class="programs-topline">
      <div>
        <p class="eyebrow">PROGRAM ARCHIVE / CONTROL ROOM</p>
        <h1>{{ t("节目档案") }}</h1>
        <p class="programs-intro">{{ t("手动维护官方节目和个人节目，日历会根据排期规则生成。") }}</p>
      </div>
      <RouterLink class="back" to="/admin">← {{ t("返回运行设置") }}</RouterLink>
    </div>

    <nav class="program-panel-nav" :aria-label="t('节目档案工作区')">
      <button type="button" class="program-panel-arrow" :disabled="!canMovePrevious" :aria-label="t('切换到上一个工作区')" @click="previousPanel">←</button>
      <div class="program-panel-context">
        <div class="program-panel-meta"><span class="eyebrow">WORKSPACE {{ panelIndex + 1 }} / 03</span><div class="program-panel-steps"><span v-for="(panel, index) in panelOrder" :key="panel" :class="{ active: index === panelIndex }"></span></div></div>
        <strong>{{ panelTitle }}</strong>
        <small>{{ panelDescription }}</small>
      </div>
      <button type="button" class="program-panel-arrow" :disabled="!canMoveNext" :aria-label="activePanel === 'list' && !editorOpen ? t('新建节目') : t('切换到下一个工作区')" @click="nextPanel">→</button>
    </nav>

    <div v-if="error || message" class="program-toast" :class="error ? 'error' : 'success'" :role="error ? 'alert' : 'status'" aria-live="assertive">{{ error || message }}</div>

    <section v-if="activePanel === 'list'" class="program-admin-list program-panel-content" :class="{ 'program-cast-filter-open': programCastFilterOpen }">
      <div class="section-heading">
        <div><p class="eyebrow">CURRENT ENTRIES</p><h2>{{ t("已录入节目") }}</h2></div>
        <span class="section-count">{{ filteredPrograms.length }}<small v-if="programSearch || programCastFilter.length"> / {{ programs.length }}</small></span>
      </div>
      <div class="program-admin-list-footer">
        <p class="muted">{{ t("选择一个节目进入编辑；已保存的节目可以继续切换到单集列表。") }}</p>
        <div class="program-admin-list-tools">
          <label class="program-admin-search">
            <span>{{ t("关键词") }}</span>
            <input v-model="programSearch" type="search" :placeholder="t('搜索节目、子节目或成员')" :aria-label="t('搜索节目、子节目或成员')">
          </label>
          <div class="program-calendar-filter program-cast-filter program-admin-cast-filter">
            <span class="program-calendar-filter-label">{{ t("按 Cast 筛选") }}</span>
              <details ref="programCastFilterDetails" class="program-cast-filter-details" @toggle="syncProgramCastFilter">
              <summary class="program-cast-filter-summary"><strong>{{ programCastFilterLabel }}</strong><b>⌄</b></summary>
              <div class="program-cast-filter-panel">
                <div class="program-cast-filter-actions">
                  <button type="button" class="secondary program-action-button" @click="selectAllProgramCast">{{ t("全选") }}</button>
                  <button type="button" class="secondary program-action-button" @click="clearProgramCastFilter">{{ t("清空") }}</button>
                </div>
                <div class="program-cast-tags">
                  <button v-for="member in NIJIGASAKI_CAST" :key="member.name" type="button" class="program-cast-tag" :class="{ selected: programCastFilter.includes(member.name) }" :aria-pressed="programCastFilter.includes(member.name)" @click="toggleProgramCastFilter(member.name)"><i class="program-cast-dot" :style="{ backgroundColor: member.color }"></i>{{ member.name }}</button>
                </div>
              </div>
            </details>
          </div>
          <button type="button" class="secondary program-action-button" @click="startNewProgram">{{ t("新建节目") }}</button>
        </div>
      </div>
      <p v-if="loading" class="state">{{ t("正在读取节目……") }}</p>
      <p v-else-if="!programs.length" class="muted">{{ t("还没有录入节目。") }}</p>
      <p v-else-if="!filteredPrograms.length" class="muted">{{ t("当前关键词和 Cast 筛选没有匹配的节目。") }}</p>
       <article v-for="program in filteredPrograms" :key="program.id" class="program-admin-item" :class="{ 'is-subprogram': Boolean(program.parent_id) }">
         <span v-if="programCast(program).length" class="program-admin-cast-line" :aria-label="t('固定参与成员')">
           <i v-for="member in programCast(program)" :key="member.name" :style="{ '--cast-color': member.color }"></i>
         </span>
         <div>
             <div class="program-admin-tags"><span class="program-kind">{{ program.parent_id ? t("子节目") : t("主节目") }}</span><span v-if="program.parent_id" class="program-parent-title-key" :title="program.title">{{ program.title }}</span><span class="program-status" :class="`status-${program.status}`">{{ programStatus(program) }}</span><span v-if="program.update_status === 'updated'" class="program-update-status">{{ updateStatusLabel(program) }}</span><span class="program-type-label" :class="`program-type-${program.category}`">{{ programType(program) }} · {{ formatLabel(program) }}</span></div>
           <h3>{{ program.parent_id ? program.subprogram_name : program.title }}</h3>
            <p>{{ t("节目日期") }}：{{ programDateRangeLabel(program) }} · {{ scheduleLabel(program) }} · {{ t("已播") }} {{ program.episode_count }} {{ t("期") }}</p>
         </div>
        <div class="program-admin-actions">
          <button type="button" class="secondary program-action-button" @click="editProgram(program)">{{ t("编辑") }}</button>
          <button type="button" class="danger program-action-button" :disabled="deletingId === program.id" @click="deleteProgram(program)">{{ deletingId === program.id ? t("删除中……") : t("删除") }}</button>
        </div>
      </article>
    </section>

    <form v-if="activePanel === 'edit' && editorOpen" class="settings-card program-editor program-panel-content" @submit.prevent="saveProgram()">
      <div class="form-heading">
         <span class="form-number">{{ editingId && !form.parent_id ? "02" : "01" }}</span>
         <div><p class="form-kicker">MANUAL ENTRY</p><h2>{{ editingId ? t("编辑节目") : t("添加节目") }}</h2></div>
         <div class="form-heading-actions program-editor-header-actions" :class="{ 'has-parent': editingId && form.parent_id }">
            <button v-if="editingId && form.parent_id" type="button" class="back program-subprogram-back" @click="returnToParentProgram">← {{ t("返回主节目") }}</button>
            <div class="program-editor-json-actions">
              <button type="button" class="secondary program-action-button" :title="t('选择一个节目 JSON，先预览节目、排期和全部单集，再确认导入')" @click="openImportPicker">{{ t("导入 JSON") }}</button>
              <button type="button" class="secondary program-action-button" :title="t('导出当前节目设置；未保存节目时下载说明模板')" @click="exportProgramJson">{{ t("导出 JSON") }}</button>
            </div>
         </div>
      </div>

      <div class="program-form-grid">
            <label class="program-field-wide">{{ t("节目名称") }}<input v-model="form.title" :readonly="Boolean(form.parent_id)" required :placeholder="t('例如：虹咲学园放送室')"><small v-if="form.parent_id">{{ t("子节目标签归属于主节目，排期和内容配置仍可独立修改。") }}</small></label>
           <div v-if="form.parent_id" class="program-form-field">
              <span class="program-field-label">{{ t("节目层级") }}</span>
              <div class="program-readonly-control">{{ t("子节目") }} · {{ parentProgramTitle }}</div>
              <small>{{ t("父节目固定为当前编辑的主节目；如需归属其他主节目，请从对应主节目编辑页添加。") }}</small>
          </div>
             <label v-if="form.parent_id" class="program-form-field"><span class="program-field-label">{{ t("子节目标签") }}</span><input v-model="form.subprogram_name" required :placeholder="t('例如：嘉宾回')"><small>{{ t("显示为主节目下的标签，用于区分不同子节目。") }}</small></label>

          <div class="program-form-field">
           <span class="program-field-label">{{ t("节目类型") }}</span>
           <div class="choice-tags" role="radiogroup" :aria-label="t('节目类型')">
             <button type="button" :class="{ selected: form.category === 'official' }" @click="form.category = 'official'">{{ t("官方节目") }}</button>
             <button type="button" :class="{ selected: form.category === 'personal' }" @click="form.category = 'personal'">{{ t("个人节目") }}</button>
          </div>
        </div>
        <div class="program-form-field">
           <span class="program-field-label">{{ t("节目形式") }}</span>
           <div class="choice-tags" role="radiogroup" :aria-label="t('节目形式')">
             <button type="button" :class="{ selected: form.format === 'video' }" @click="form.format = 'video'">{{ t("有画面") }}</button>
             <button type="button" :class="{ selected: form.format === 'radio' }" @click="form.format = 'radio'">{{ t("广播（无画面）") }}</button>
          </div>
        </div>
        <div class="program-form-field">
           <span class="program-field-label">{{ t("播出平台") }}</span>
           <div class="choice-tags" role="radiogroup" :aria-label="t('播出平台')">
             <button type="button" :class="{ selected: form.platform === 'tv' }" @click="form.platform = 'tv'">{{ t("电视台") }}</button>
             <button type="button" :class="{ selected: form.platform === 'network' }" @click="form.platform = 'network'">{{ t("网络") }}</button>
          </div>
        </div>
        <div class="program-form-field">
           <span class="program-field-label">{{ t("播放方式") }}</span>
           <div class="choice-tags" role="radiogroup" :aria-label="t('播放方式')">
             <button type="button" :class="{ selected: form.delivery === 'live' }" @click="form.delivery = 'live'">{{ t("直播") }}</button>
             <button type="button" :class="{ selected: form.delivery === 'recorded' }" @click="form.delivery = 'recorded'">{{ t("录播") }}</button>
          </div>
        </div>
         <div class="program-form-field">
            <span class="program-field-label">{{ t("首集编号") }}</span>
            <div class="choice-tags" role="radiogroup" :aria-label="t('首集编号')">
              <button type="button" :class="{ selected: episodeStartMode === 'zero' }" @click="setEpisodeStartMode('zero')">{{ t("第 0 期") }}</button>
              <button type="button" :class="{ selected: episodeStartMode === 'one' }" @click="setEpisodeStartMode('one')">{{ t("第 1 期") }}</button>
              <button type="button" :class="{ selected: episodeStartMode === 'custom' }" @click="setEpisodeStartMode('custom')">{{ t("自定义") }}</button>
            </div>
            <label v-if="episodeStartMode === 'custom'" class="program-custom-episode-start">
              <span>{{ t("自定义首集编号") }}</span>
              <input v-model="episodeStartCustom" type="number" min="0" max="9999" step="1" :aria-label="t('自定义首集编号')" :placeholder="t('请输入期数')">
              <em>{{ t("期") }}</em>
            </label>
            <small>{{ t("设置节目正篇的起始期数；EX 特别节目不占用正篇编号。") }}</small>
         </div>

        <div class="program-form-field program-field-wide">
           <span class="program-field-label">{{ t("固定参与成员 / 主持") }}</span>
          <div class="people-picker">
             <button v-for="person in peopleOptions" :key="person" type="button" class="person-tag" :class="{ selected: form.people.includes(person) }" @click="togglePerson(person)"><i v-if="castMember(person)" class="program-cast-dot" :style="{ backgroundColor: castMember(person).color }"></i>{{ person }}</button>
            <div v-for="person in customPeople" :key="person" class="person-tag selected custom-person-tag">
               <span>{{ person }}</span><button type="button" :aria-label="t('移除成员')" @click="removePerson(person)">×</button>
            </div>
          </div>
          <div class="custom-person-entry">
             <input v-model="customPerson" :placeholder="t('添加固定主持或嘉宾')" @keydown.enter.prevent="addCustomPerson">
             <button type="button" class="secondary program-action-button" @click="addCustomPerson">{{ t("添加成员") }}</button>
           </div>
           <small>{{ t("这里填写批量排期下固定出现的虹咲成员、主持人或常驻嘉宾；单期临时嘉宾请在下方单集编辑。") }}</small>
        </div>
          <label class="program-field-wide">{{ t("相关链接") }}<input v-model="form.official_url" type="url" placeholder="https://"><small>{{ t("节目层面的补充链接；源地址、搬运地址和字幕地址请在单集编辑中填写。") }}</small></label>
           <label class="program-field-wide">{{ t("节目简介") }}<textarea v-model="form.description" rows="3" :placeholder="t('可选')"></textarea></label>
           <div v-if="editingId && !form.parent_id" class="program-subprogram-panel program-field-wide">
             <div class="program-subprogram-panel-heading">
               <div><span class="program-field-label">{{ t("子节目") }}</span><small>{{ relatedSubprograms.length ? t("选择子节目进入独立编辑页") : t("当前主节目还没有子节目") }}</small></div>
               <span class="program-subprogram-count">{{ relatedSubprograms.length }}</span>
             </div>
             <div v-if="relatedSubprograms.length" class="program-subprogram-list">
               <button v-for="subprogram in relatedSubprograms" :key="subprogram.id" type="button" class="program-subprogram-link" @click="editProgram(subprogram)">
                 <strong>{{ subprogram.subprogram_name }}</strong>
                 <small>{{ scheduleLabel(subprogram) }} · {{ t("已播") }} {{ subprogram.episode_count }} {{ t("期") }}</small>
                 <span aria-hidden="true">↗</span>
               </button>
             </div>
             <button type="button" class="secondary program-action-button program-subprogram-add" @click="newSubprogramFromEditor">＋ {{ t("添加子节目") }}</button>
           </div>
       </div>

      <div class="program-schedule">
          <div class="form-heading subsection-heading"><span class="form-number">02</span><div><p class="form-kicker">BROADCAST PERIODS</p><h2>{{ t("排期时期") }}</h2></div></div>
         <p class="muted period-help">{{ t("同一个节目可以分成多个时期，例如先每周、再隔周、最后改为每月。时期之间请用日期分开。") }}</p>
        <article v-for="(period, index) in form.periods" :key="period.id || index" class="program-period-card">
          <div class="program-period-heading">
             <div><span class="program-period-number">{{ t("时期 {count}", { count: index + 1 }) }}</span><strong>{{ periodScheduleLabel(period) }}</strong></div>
              <button v-if="form.periods.length > 1" type="button" class="danger program-action-button" @click="removePeriod(index)">{{ t("移除") }}</button>
          </div>
          <div class="program-form-grid period-form-grid">
            <div class="program-form-field program-field-wide">
               <span class="program-field-label">{{ t("更新方式") }}</span>
               <div class="choice-tags" role="radiogroup" :aria-label="t('更新方式')">
                 <button type="button" :class="{ selected: period.frequency === 'weekly' }" @click="setPeriodFrequency(period, 'weekly')">{{ t("周更") }}</button>
                 <button type="button" :class="{ selected: period.frequency === 'monthly' }" @click="setPeriodFrequency(period, 'monthly')">{{ t("月更") }}</button>
                 <button type="button" :class="{ selected: period.frequency === 'individual' }" @click="setPeriodFrequency(period, 'individual')">{{ t("逐期设置") }}</button>
                 <button type="button" :class="{ selected: period.frequency === 'single' }" @click="setPeriodFrequency(period, 'single')">{{ t("单次") }}</button>
              </div>
               <small v-if="period.frequency === 'monthly' && period.monthly_mode === 'irregular'">{{ t("仅新建节目时按时期开始日创建首期；开启后续自动生成时按每月 1 日生成占位单集。已有节目不补建首期。") }}</small>
               <small v-else-if="period.frequency === 'individual'">{{ t("逐期设置不自动生成后续单集；仅新建节目时按时期开始日期和默认时间创建首期，已有节目不补建。") }}</small>
               <small v-else-if="period.frequency === 'single'">{{ t("新建单次节目时按播出日期和时间创建一条单集，已有节目不补建首期。") }}</small>
            </div>
            <div v-if="period.frequency === 'weekly'" class="program-form-field">
               <span class="program-field-label">{{ t("更新间隔") }}</span>
               <div class="inline-number"><input v-model.number="period.week_interval" type="number" min="1" max="52" required><span>{{ t("周一次") }}</span></div>
               <small>{{ t("填写 2 即为隔周更新。") }}</small>
            </div>
            <div v-if="period.frequency === 'weekly'" class="program-form-field program-field-wide">
               <span class="program-field-label">{{ t("星期") }}</span>
               <div class="choice-tags weekday-tags" role="radiogroup" :aria-label="t('星期')">
                <button v-for="(name, weekday) in weekdayNames" :key="name" type="button" :class="{ selected: period.weekday === weekday }" @click="period.weekday = weekday">{{ name }}</button>
              </div>
            </div>
            <div v-if="period.frequency === 'monthly'" class="program-form-field program-field-wide">
               <span class="program-field-label">{{ t("月更规律") }}</span>
               <div class="choice-tags" role="radiogroup" :aria-label="t('月更规律')">
                 <button type="button" :class="{ selected: period.monthly_mode !== 'irregular' }" @click="setMonthlyMode(period, 'week')">{{ t("有规律") }}</button>
                 <button type="button" :class="{ selected: period.monthly_mode === 'irregular' }" @click="setMonthlyMode(period, 'irregular')">{{ t("无规律") }}</button>
              </div>
            </div>
            <div v-if="period.frequency === 'monthly' && period.monthly_mode !== 'irregular'" class="program-form-field program-field-wide">
               <span class="program-field-label">{{ t("计算方式") }}</span>
               <div class="choice-tags" role="radiogroup" :aria-label="t('计算方式')">
                 <button type="button" :class="{ selected: period.monthly_mode === 'week' }" @click="setMonthlyMode(period, 'week')">{{ t("周次计算") }}</button>
                 <button type="button" :class="{ selected: period.monthly_mode === 'day' }" @click="setMonthlyMode(period, 'day')">{{ t("按日计算") }}</button>
              </div>
            </div>
            <div v-if="period.frequency === 'monthly' && period.monthly_mode === 'week'" class="program-form-field">
               <span class="program-field-label">{{ t("周次方向") }}</span>
               <div class="choice-tags" role="radiogroup" :aria-label="t('周次方向')">
                 <button type="button" :class="{ selected: period.week_direction === 'first' }" @click="setPeriodWeekDirection(period, 'first')">{{ t("顺数") }}</button>
                 <button type="button" :class="{ selected: period.week_direction === 'last' }" @click="setPeriodWeekDirection(period, 'last')">{{ t("倒数") }}</button>
              </div>
            </div>
            <div v-if="period.frequency === 'monthly' && period.monthly_mode === 'week'" class="program-form-field">
               <span class="program-field-label">{{ t("第几周") }}</span>
               <div class="choice-tags week-tags" role="radiogroup" :aria-label="t('第几周')">
                 <button v-for="week in weekOptions" :key="week" type="button" :class="{ selected: period.week_number === week }" @click="period.week_number = week">{{ t("第 {count} 周", { count: week }) }}</button>
              </div>
            </div>
            <div v-if="period.frequency === 'monthly' && period.monthly_mode === 'week'" class="program-form-field program-field-wide">
               <span class="program-field-label">{{ t("星期") }}</span>
               <div class="choice-tags weekday-tags" role="radiogroup" :aria-label="t('星期')">
                <button v-for="(name, weekday) in weekdayNames" :key="name" type="button" :class="{ selected: period.weekday === weekday }" @click="period.weekday = weekday">{{ name }}</button>
              </div>
            </div>
            <div v-if="period.frequency === 'monthly' && period.monthly_mode === 'day'" class="program-form-field">
               <span class="program-field-label">{{ t("日次方向") }}</span>
               <div class="choice-tags" role="radiogroup" :aria-label="t('日次方向')">
                 <button type="button" :class="{ selected: period.day_direction === 'first' }" @click="setPeriodDayDirection(period, 'first')">{{ t("顺数") }}</button>
                 <button type="button" :class="{ selected: period.day_direction === 'last' }" @click="setPeriodDayDirection(period, 'last')">{{ t("倒数") }}</button>
              </div>
            </div>
            <div v-if="period.frequency === 'monthly' && period.monthly_mode === 'day'" class="program-form-field">
               <span class="program-field-label">{{ t("第几天") }}</span>
               <div class="inline-number">
                 <input v-model.number="period.day_number" type="number" min="1" :max="period.day_direction === 'last' ? 14 : 28" step="1" required @change="normalizePeriodDayNumber(period)">
                 <span>{{ period.day_direction === 'last' ? t("倒数第几天") : t("第几天") }}</span>
               </div>
               <small>{{ period.day_direction === 'last' ? t("倒数最多支持 14 天") : t("顺数最多支持 28 天") }}</small>
            </div>
            <div v-if="period.frequency === 'weekly' || period.frequency === 'monthly' || period.frequency === 'individual' || period.frequency === 'single'" class="program-form-field period-time-field">
               <span class="program-field-label">{{ t("播出时间") }}</span>
               <VueDatePicker v-model="period.schedule_time" class="program-date-picker" time-picker model-type="HH:mm" format="HH:mm" :locale="localeTag()" auto-apply :clearable="true" :is-24="true" :teleport="true" text-input :placeholder="t('选择时间')" @keydown.enter.prevent />
               <small v-if="period.frequency === 'individual'">{{ t("作为新增单集的默认时间；每期可以单独修改。") }}</small>
               <small v-else>{{ t("留空表示全天事件。") }}</small>
            </div>
            <p v-else class="muted period-schedule-note program-field-wide">{{ t("播出时间未知；单集保存时可填写每期实际时间。") }}</p>
            <div class="program-form-field period-timezone-field">
               <span class="program-field-label">{{ t("更新时间时区") }}</span>
              <select v-model="period.timezone">
                <option v-for="option in timezoneOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
              </select>
               <small>{{ t("默认使用东京时间。") }}</small>
            </div>
             <label v-if="period.frequency === 'single'" class="period-start-field">{{ t("播出日期") }}<VueDatePicker v-model="period.start_date" class="program-date-picker" model-type="yyyy-MM-dd" format="yyyy-MM-dd" :locale="localeTag()" :enable-time-picker="false" auto-apply :clearable="false" :teleport="true" :placeholder="t('选择日期')" /></label>
             <label v-else class="period-start-field">{{ t("时期开始") }}<VueDatePicker v-model="period.start_date" class="program-date-picker" model-type="yyyy-MM-dd" format="yyyy-MM-dd" :locale="localeTag()" :enable-time-picker="false" auto-apply :clearable="false" :teleport="true" :placeholder="t('选择日期')" /></label>
             <label v-if="period.frequency !== 'single'" class="period-end-field">{{ t("时期结束") }}<VueDatePicker v-model="period.end_date" class="program-date-picker" model-type="yyyy-MM-dd" format="yyyy-MM-dd" :locale="localeTag()" auto-apply :clearable="true" :teleport="true" :placeholder="t('留空表示进行中')" /><small>{{ index < form.periods.length - 1 ? t("用于划分下一个排期时期。") : t("填入结束日期后会自动标记为已完结；留空表示进行中。") }}</small></label>
             <label v-if="period.frequency !== 'individual' && period.frequency !== 'single'" class="period-auto-toggle program-field-wide">
                <input :checked="period.auto_generate" type="checkbox" :disabled="saving || occurrenceSaving" @change="setPeriodAutoGeneration(period, $event.target.checked)">
                <span>
                  <strong>{{ t("本时期自动生成后续单集") }}</strong>
                  <small v-if="period.frequency === 'monthly' && period.monthly_mode === 'irregular'">{{ t("仅新建节目时按时期开始日创建首期；开启后续自动生成时按每月 1 日生成占位单集。已有节目不补建首期。") }}</small>
                  <small v-else-if="period.auto_generate">{{ t("按排期规则生成未来约半年的单集。") }}</small>
                  <small v-else>{{ t("已关闭后续自动生成；仅保留已保存单集，不会为已有节目补建首期。") }}</small>
                </span>
              </label>
          </div>
        </article>
          <button type="button" class="secondary add-period-button program-action-button" @click="addPeriod">＋ {{ t("添加排期时期") }}</button>
      </div>

        <div class="actions program-editor-actions">
            <button class="program-action-button" :disabled="saving">{{ saving ? t("保存中……") : editingId ? t("保存修改") : t("添加节目") }}</button>
           <span v-if="editingId" class="program-editor-danger-actions">
              <button type="button" class="danger program-action-button" :disabled="deletingId === editingId" @click="deleteProgram({ id: editingId, title: form.title })">{{ deletingId === editingId ? t("删除中……") : t("删除节目") }}</button>
           </span>
           <input ref="importFileInput" class="program-json-file-input" type="file" accept=".json,application/json" @change="handleImportFile">
        </div>
     </form>

    <section ref="occurrenceEditorRef" v-if="activePanel === 'occurrences' && editorOpen && editingId" class="settings-card program-occurrence-editor program-panel-content">
         <div class="section-heading">
          <div><p class="eyebrow">GENERATED EPISODES / OVERRIDES</p><h2>{{ t("自动生成单集") }}</h2></div>
          <div class="settings-heading-actions">
             <button type="button" class="secondary program-action-button" @click="leaveOccurrenceFocus">{{ t("返回节目配置") }}</button>
          </div>
        </div>
        <div class="occurrence-generation-bar">
           <label class="occurrence-auto-toggle">
             <input v-model="form.auto_generate" type="checkbox" :disabled="saving || occurrenceSaving" @change="saveAutoGeneration">
              <span><strong>{{ t("节目级自动生成后续单集") }}</strong><small>{{ occurrenceGenerationDescription }}</small></span>
          </label>
          <div class="occurrence-bulk-actions">
             <button v-if="form.periods.some(period => period.frequency === 'monthly' && period.monthly_mode !== 'irregular')" type="button" class="secondary program-action-button" :disabled="saving || occurrenceSaving" @click="convertMonthlyToIndividual">{{ t("固定月更 → 逐期设置") }}</button>
               <button v-if="occurrenceRows.some(row => row.status === 'rescheduled')" type="button" class="secondary program-action-button" :disabled="saving || occurrenceSaving" @click="applyRescheduledToOriginal">{{ t("已改期 → 覆盖为原定") }}</button>
          </div>
        </div>
          <p class="muted occurrence-help">{{ occurrenceHelp }}</p>
       <p v-if="occurrenceLoading" class="state">{{ t("正在读取单集排期……") }}</p>
        <div v-else class="occurrence-editor-layout">
           <div class="occurrence-list-column">
             <div ref="occurrenceListRef" class="occurrence-list">
                <button v-for="row in occurrenceRows" :key="occurrenceRowKey(row)" :ref="element => setOccurrenceItemRef(row, element)" type="button" class="occurrence-list-item" :class="{ selected: occurrenceRowKey(occurrenceDraft) === occurrenceRowKey(row), 'is-draft': row._draft }" @click="editOccurrence(row)">
                <span v-if="occurrenceCast(row).length" class="occurrence-list-cast-line" role="img" :aria-label="`${t('出场成员')}：${occurrenceCast(row).map(member => member.name).join('、')}`" :title="occurrenceCast(row).map(member => member.name).join('、')"><i v-for="member in occurrenceCast(row)" :key="member.name" :style="{ '--cast-color': member.color, backgroundColor: member.color }"></i></span>
              <span><b>{{ occurrenceEpisodeLabel(row) }}</b><em :class="{ cancelled: row.status === 'cancelled', deleted: row.status === 'deleted', aired: row.aired }">{{ row._draft ? t("待填写") : occurrenceStatus(row) }}</em></span>
             <strong>{{ row._draft ? t("时间未定") : row.date }}</strong>
              <small v-if="row.title" class="occurrence-list-title">{{ row.title }}</small>
                <small v-if="row._draft">{{ t("请填写日期后保存") }}</small>
                <small v-else>{{ row.status === "deleted" ? t("已删除，不参与生成") : row.generated ? t("自动生成") : row.materialized ? t("已播出并保存") : row.adjusted_date ? `${t("原定")} ${row.original_date}` : t("已单独调整") }}{{ row.guests?.length ? ` · ${t("嘉宾")} ${row.guests.length} ${t("人")}` : "" }}{{ row.absent_members?.length ? ` · ${t("缺席")} ${row.absent_members.length} ${t("人")}` : "" }}</small>
                <small v-if="row.absent_members?.length" class="occurrence-list-absence">{{ t("缺席：") }}{{ row.absent_members.join("、") }}</small>
                </button>
             <p v-if="!occurrenceRows.length" class="muted">{{ t("当前区间没有自动生成的单集，可以手动添加一条记录。") }}</p>
             </div>
             <button type="button" class="secondary program-action-button occurrence-add-button" @click="newOccurrence">＋ {{ t("添加单集") }}</button>
           </div>
         <form class="occurrence-form" @submit.prevent="saveOccurrence">
           <div class="occurrence-form-heading">
              <button type="button" class="occurrence-nav-button" :disabled="!canPreviousOccurrence" :aria-label="t('上一集')" :title="t('上一集')" @click="selectAdjacentOccurrence(-1)">←</button>
               <div><p class="form-kicker">ONE EPISODE</p><h3>{{ occurrenceDraft._draft ? t("添加单集") : occurrenceDraft.id || occurrenceDraft.generated ? t("编辑单集") : t("添加单集调整") }}</h3></div>
               <button type="button" class="occurrence-nav-button" :disabled="!canNextOccurrence" :aria-label="t('下一集')" :title="t('下一集')" @click="selectAdjacentOccurrence(1)">→</button>
               <span v-if="occurrenceDraft.id || occurrenceDraft.generated" class="program-kind">{{ occurrenceDraft.id ? t("已保存") : t("自动生成") }}</span>
            </div>
             <label>{{ t("单集标题") }}<input v-model="occurrenceDraft.title" type="text" :placeholder="t('例如：特别企划、嘉宾回顾……')"><small>{{ t("可选；填写后会显示在日历和单集详情中。") }}</small></label>
             <label>{{ t("原定日期") }}<VueDatePicker v-model="occurrenceDraft.original_date" class="program-date-picker" model-type="yyyy-MM-dd" format="yyyy-MM-dd" :locale="localeTag()" :enable-time-picker="false" auto-apply :clearable="false" :readonly="occurrenceOriginalLocked" :teleport="true" :placeholder="t('选择日期')" /><small>{{ occurrenceDraft.individual ? t("逐期设置模式：可直接修改本期原定日期。") : occurrenceOriginalLocked ? t("来自节目排期规则，不能修改原定日期。") : t("手动补录单集时填写原定日期。") }}</small></label>
                <label>{{ t("原定时间") }}<VueDatePicker v-model="occurrenceDraft.original_time" class="program-date-picker" time-picker model-type="HH:mm" format="HH:mm" :locale="localeTag()" auto-apply :clearable="true" :is-24="true" :readonly="occurrenceOriginalLocked" :teleport="true" text-input :placeholder="t('选择时间')" /><small>{{ occurrenceDraft.individual ? t("逐期设置模式：可直接修改本期原定时间。") : occurrenceOriginalLocked ? t("来自排期规则，只能修改调整日期；调整时间已默认沿用原定时间。") : t("手动补录单集时填写原定时间。") }}{{ t("节目排期时区") }}：{{ occurrenceTimezoneLabel }}；{{ t("日历会按访问设备时区显示") }}。</small></label>
           <div class="program-form-field">
              <span class="program-field-label">{{ t("本期播出方式") }}</span>
              <div class="choice-tags" role="radiogroup" :aria-label="t('本期播出方式')">
                <button type="button" :class="{ selected: !occurrenceDraft.delivery }" @click="occurrenceDraft.delivery = ''">{{ t("跟随节目默认") }}</button>
                <button type="button" :class="{ selected: occurrenceDraft.delivery === 'live' }" @click="occurrenceDraft.delivery = 'live'">{{ t("直播") }}</button>
                <button type="button" :class="{ selected: occurrenceDraft.delivery === 'recorded' }" @click="occurrenceDraft.delivery = 'recorded'">{{ t("录播") }}</button>
              </div>
              <small>{{ t("仅作用于这一期，不会修改整个节目的默认播出方式。") }}</small>
           </div>
           <div class="program-form-field">
              <span class="program-field-label">{{ t("本期类型") }}</span>
              <div class="choice-tags" role="radiogroup" :aria-label="t('本期类型')">
                <button type="button" :class="{ selected: occurrenceDraft.special !== 'EX' }" @click="occurrenceDraft.special = ''">{{ t("普通节目") }}</button>
                <button type="button" :class="{ selected: occurrenceDraft.special === 'EX' }" @click="occurrenceDraft.special = 'EX'">{{ t("EX 特别节目") }}</button>
              </div>
               <small>{{ t("EX 特别节目不占用正篇期数，后续正篇编号顺延；请在自动生成单集结束后手动添加。") }}</small>
           </div>
           <div class="program-form-field">
              <span class="program-field-label">{{ t("本期状态") }}</span>
              <div class="choice-tags">
                <button type="button" :class="{ selected: occurrenceDraft.status === 'scheduled' }" @click="setOccurrenceStatus('scheduled')">{{ t("正常播出") }}</button>
                <button type="button" :class="{ selected: occurrenceDraft.status === 'rescheduled' }" @click="setOccurrenceStatus('rescheduled')">{{ t("已改期") }}</button>
                <button type="button" :class="{ selected: occurrenceDraft.status === 'cancelled' }" @click="setOccurrenceStatus('cancelled')">{{ t("因故取消") }}</button>
              </div>
               <small>{{ t("正常播出不会修改排期日期；需要更换日期时选择“已改期”。“因故取消”会保留单集并标记已取消；删除会移除单集，不再显示。") }}</small>
           </div>
             <div v-if="occurrenceDraft.status === 'rescheduled'" class="occurrence-date-row">
                <label>{{ t("调整日期") }}<VueDatePicker v-model="occurrenceDraft.adjusted_date" class="program-date-picker" model-type="yyyy-MM-dd" format="yyyy-MM-dd" :locale="localeTag()" :enable-time-picker="false" auto-apply :clearable="false" :teleport="true" :placeholder="t('选择新日期')" required /></label>
                 <label>{{ t("调整时间") }}<VueDatePicker v-model="occurrenceDraft.adjusted_time" class="program-date-picker" time-picker model-type="HH:mm" format="HH:mm" :locale="localeTag()" auto-apply :clearable="true" :is-24="true" :teleport="true" text-input :placeholder="t('沿用原定时间')" /></label>
             </div>
             <div v-if="occurrenceDraft.status === 'rescheduled' && canShiftFollowing" class="program-form-field program-field-wide">
                <span class="program-field-label">{{ t("后续排期") }}</span>
                <div class="choice-tags">
                  <button type="button" :class="{ selected: occurrenceDraft.shift_following_days === -7 }" @click="setOccurrenceShift(occurrenceDraft.shift_following_days === -7 ? 0 : -7)">{{ occurrenceDraft.shift_following_days === -7 ? t("已提前一周") : t("提前一周") }}</button>
                  <button type="button" :class="{ selected: occurrenceDraft.shift_following_days === 7 }" @click="setOccurrenceShift(occurrenceDraft.shift_following_days === 7 ? 0 : 7)">{{ occurrenceDraft.shift_following_days === 7 ? t("已顺延一周") : t("顺延一周") }}</button>
                </div>
                <small>{{ t("本期原定周视为不播；选择提前或顺延后，之后的隔两周排期也从这里起同步平移一周。") }}</small>
             </div>
             <div class="program-form-field occurrence-guests-field">
              <span class="program-field-label">{{ t("本期嘉宾") }}</span>
              <div v-if="occurrenceDraft.guests.length" class="people-picker">
                 <span v-for="guest in occurrenceDraft.guests" :key="guest" class="person-tag selected custom-person-tag"><i v-if="castMember(guest)" class="program-cast-dot" :style="{ backgroundColor: castMember(guest).color }"></i><span>{{ guest }}</span><button type="button" :aria-label="t('移除本期嘉宾')" @click="removeOccurrenceGuest(guest)">×</button></span>
              </div>
              <div class="occurrence-guest-cast-picker">
                 <span class="program-field-label">{{ t("虹咲成员（可多选）") }}</span>
                <div v-if="occurrenceGuestOptions.length" class="people-picker">
                   <button v-for="member in occurrenceGuestOptions" :key="member.name" type="button" class="person-tag" :class="{ selected: occurrenceGuestSelected(member), 'program-guest-fixed': occurrenceGuestIsFixed(member) }" :aria-pressed="occurrenceGuestSelected(member)" :title="occurrenceGuestIsFixed(member) ? t('节目固定成员，默认出席；取消选择将记录为缺席') : t('选中后记录为本期嘉宾')" @click="toggleOccurrenceGuest(member)"><i class="program-cast-dot" :style="{ backgroundColor: member.color }"></i>{{ member.name }}</button>
                </div>
                 <small>{{ t("节目固定成员默认已选；取消选择会记录为本期缺席。其他成员默认未选，选中后记录为本期嘉宾。") }}</small>
              </div>
               <p v-if="occurrenceAbsentCast.length" class="occurrence-absence-note">{{ t("本期缺席：") }}{{ occurrenceAbsentCast.map(member => member.name).join("、") }}</p>
              <div class="custom-person-entry">
                 <input v-model="occurrenceGuestInput" :placeholder="t('添加本期嘉宾')" @keydown.enter.prevent="addOccurrenceGuest">
                  <button type="button" class="secondary program-action-button" @click="addOccurrenceGuest">{{ t("添加嘉宾") }}</button>
              </div>
                <small>{{ t("虹咲成员选择和缺席记录仅作用于这一期；自定义嘉宾也不会修改节目默认参与成员。") }}</small>
             </div>
              <div class="occurrence-link-fields">
                <label class="occurrence-source-field"><span class="program-field-label">{{ t("源地址") }}</span><input v-model="occurrenceDraft.source_url" type="url" placeholder="https://"></label>
                <label><span class="program-field-label">{{ t("搬运地址") }}</span><input v-model="occurrenceDraft.mirror_url" type="text" :placeholder="t('BV号或B站地址')"></label>
                <label><span class="program-field-label">{{ t("字幕地址") }}</span><input v-model="occurrenceDraft.subtitle_url" type="text" :placeholder="t('BV号或B站地址')"></label>
                <small>{{ t("源地址填写 HTTP/HTTPS 地址；搬运地址和字幕地址支持 BV 号、B 站地址或其他 HTTP/HTTPS 地址。") }}</small>
              </div>
              <label>{{ t("备注") }}<textarea v-model="occurrenceDraft.note" rows="3" :placeholder="t('例如：延期至下周、嘉宾变更……')"></textarea></label>
              <div class="program-form-field occurrence-images-field" @paste="handleOccurrenceImagePaste">
                <span class="program-field-label">{{ t("当期节目返图") }}</span>
                <div v-if="occurrenceImages().length" class="occurrence-admin-image-grid">
                  <div v-for="(image, index) in occurrenceImages()" :key="image.id || `${image.url}-${index}`" class="occurrence-admin-image-item">
                    <button type="button" class="occurrence-admin-image-preview" :aria-label="t('查看返图 {count}', { count: index + 1 })" @click="openOccurrenceLightbox(index)"><img :src="image.url" :alt="image.alt" loading="lazy"></button>
                    <button type="button" class="occurrence-admin-image-delete" :disabled="occurrenceImageDeletingId === String(image.id)" @click="deleteOccurrenceImage(image)">{{ occurrenceImageDeletingId === String(image.id) ? t("删除中……") : t("删除") }}</button>
                  </div>
                </div>
                <div class="occurrence-image-actions">
                  <input ref="occurrenceImageFileInput" class="program-json-file-input" type="file" accept="image/jpeg,image/png,image/gif,image/webp,image/bmp,image/avif" multiple @change="handleOccurrenceImageFiles">
                  <button type="button" class="secondary program-action-button" :disabled="occurrenceImageUploading || occurrenceSaving" @click="openOccurrenceImagePicker">{{ occurrenceImageUploading ? t("上传图片中……") : t("上传返图") }}</button>
                  <input v-model="occurrenceImageUrl" type="url" :placeholder="t('粘贴图片直链（HTTP/HTTPS）')" :disabled="occurrenceImageUploading || occurrenceSaving" @keydown.enter.prevent="addOccurrenceImageUrl">
                  <button type="button" class="secondary program-action-button" :disabled="occurrenceImageUploading || occurrenceSaving || !occurrenceImageUrl.trim()" @click="addOccurrenceImageUrl">{{ t("添加直链") }}</button>
                </div>
                <small>{{ t("可上传图片到本地（配置 R2 时会同步）、粘贴图片直链，或直接粘贴剪贴板图片；点击缩略图可预览大图。") }}</small>
              </div>
           <div class="actions">
               <span v-if="occurrenceAutoSaveEnabled && occurrenceAutoSaveState" class="occurrence-auto-save-status" :class="`is-${occurrenceAutoSaveState}`" role="status" aria-live="polite">{{ occurrenceAutoSaveState === 'saved' ? t("已自动保存") : occurrenceAutoSaveState === 'error' ? t("自动保存失败，请检查输入") : t("自动保存中……") }}</span>
               <button v-if="!occurrenceAutoSaveEnabled" class="program-action-button" :disabled="occurrenceSaving">{{ occurrenceSaving ? t("保存中……") : t("保存本期调整") }}</button>
               <button v-if="occurrenceDraft.id || occurrenceDraft.generated" type="button" class="program-action-button" :class="occurrenceDraft.status === 'deleted' ? 'secondary' : 'danger'" :disabled="occurrenceSaving" @click="toggleOccurrenceDeletion">{{ occurrenceDeleteLabel }}</button>
               <button type="button" class="danger program-action-button" :disabled="occurrenceSaving" @click="clearOccurrenceList">{{ t("清空列表") }}</button>
          </div>
        </form>
      </div>
    </section>

      <div v-if="importPreview" class="program-json-modal" role="dialog" aria-modal="true" aria-labelledby="program-json-preview-title" @click.self="closeImportPreview">
        <section class="program-json-dialog">
          <div class="program-json-dialog-scroll">
         <div class="program-json-dialog-heading">
            <div><p class="eyebrow">JSON IMPORT / PREVIEW</p><h2 id="program-json-preview-title">{{ t("导入预览") }}</h2><small>{{ importFileName }} · {{ importScheduleModeLabel(importPreview.import_options?.schedule_mode) }} · {{ importTargetMode === "overwrite" ? t("将覆盖所选节目") : t("将新建节目") }}</small></div>
            <button type="button" class="secondary program-action-button" :disabled="importSubmitting" @click="closeImportPreview">{{ t("关闭") }}</button>
         </div>
         <section class="program-json-target-section">
            <div class="program-json-preview-heading"><div><p class="form-kicker">IMPORT TARGET</p><h3>{{ t("导入目标") }}</h3></div></div>
            <div v-if="importPreview._program_scope === 'subprogram'" class="program-json-target-select">
              <label>{{ t("所属主节目") }}<select v-model="importTargetParentProgramId" required><option value="" disabled>{{ t("请选择主节目") }}</option><option v-for="parent in importPreview.parent_choices" :key="parent.id" :value="parent.id">{{ parent.display_name }}{{ parent.match === "id" ? ` · ${t("原主节目 ID 匹配")}` : parent.match === "title" ? ` · ${t("原主节目名称匹配")}` : "" }}</option></select></label>
              <small v-if="importPreview.parent_program?.title">{{ t("导出时所属主节目") }}：{{ importPreview.parent_program.title }}；{{ t("可以改选其他主节目") }}</small>
            </div>
            <div class="program-json-target-options">
               <label class="program-json-target-option" :class="{ selected: importTargetMode === 'new' }"><input v-model="importTargetMode" type="radio" value="new"><span><strong>{{ importPreview._program_scope === 'subprogram' ? t("在主节目下新建子节目") : t("新建节目") }}</strong><small>{{ importPreview._program_scope === 'subprogram' ? t("保留原节目不变，在所选主节目下创建一个新的子节目。") : t("保留原节目不变，创建一个新的节目记录。") }}</small></span></label>
               <label class="program-json-target-option" :class="{ selected: importTargetMode === 'overwrite' }"><input v-model="importTargetMode" type="radio" value="overwrite" :disabled="importPreview._program_scope === 'subprogram' ? !importPreview.parent_choices?.length : !importPreview.matches?.length"><span><strong>{{ importPreview._program_scope === 'subprogram' ? t("覆盖主节目下的子节目") : t("覆盖已有节目") }}</strong><small v-if="importPreview._program_scope === 'subprogram'">{{ importPreview.parent_choices?.length ? t("覆盖所选主节目下同名或同 ID 的子节目。") : t("没有找到可选的主节目。") }}</small><small v-else>{{ importPreview.matches?.length ? t("用当前 JSON 完整替换所选节目的资料、排期和单集。") : t("没有找到同 ID 或同名称的已有节目。") }}</small></span></label>
            </div>
             <label v-if="importTargetMode === 'overwrite' && importPreview._program_scope !== 'subprogram'" class="program-json-target-select">{{ t("选择覆盖目标") }}<select v-model="importTargetProgramId" required><option v-for="match in importPreview.matches" :key="match.id" :value="match.id">{{ match.display_name }} · {{ match.match === "id" ? t("ID 匹配") : t("名称匹配") }}</option></select></label>
             <p v-if="importTargetMode === 'overwrite'" class="program-json-danger-note">{{ importPreview._program_scope === 'subprogram' ? t("覆盖会删除所选子节目原有的排期和单集，再写入本次 JSON；此操作不可自动撤销，请确认目标无误。") : t("覆盖会删除目标节目原有的排期和单集，再写入本次 JSON；此操作不可自动撤销，请确认目标无误。") }}</p>
         </section>
         <div class="program-json-summary">
           <div><span class="program-field-label">{{ t("节目名称") }}</span><strong :class="{ 'program-json-match-title': importTitleMatched(importPreview) }">{{ importPreview.program.title }}</strong></div>
            <div v-if="importPreview._program_scope === 'subprogram'"><span class="program-field-label">{{ t("子节目标签") }}</span><strong>{{ importPreview.program.subprogram_name }}</strong></div>
           <div><span class="program-field-label">{{ t("类型") }}</span><strong>{{ importPreview.program.category === "official" ? t("官方节目") : t("个人节目") }}</strong></div>
           <div><span class="program-field-label">{{ t("形式") }}</span><strong>{{ formatLabel(importPreview.program) }}</strong></div>
           <div><span class="program-field-label">{{ t("成员") }}</span><strong>{{ importPreview.program.people?.join("、") || t("未填写") }}</strong></div>
           <div><span class="program-field-label">{{ t("是否完结") }}</span><strong>{{ programStatus(importPreview.program) }}</strong></div>
           <div><span class="program-field-label">{{ t("是否开启自动生成后续单集") }}</span><strong>{{ importPreview.program.auto_generate ? t("是") : t("否") }}</strong></div>
        </div>
         <section class="program-json-preview-section">
            <div class="program-json-preview-heading"><div><p class="form-kicker">BROADCAST PERIODS</p><h3>{{ t("排期时期") }}</h3></div><span class="section-count">{{ importPreview.counts.periods }}</span></div>
          <div class="program-json-period-list">
            <article v-for="(period, index) in importPreview.program.periods" :key="`${period.start_date}-${index}`" class="program-json-period-item">
               <strong>{{ t("时期 {count}", { count: index + 1 }) }}</strong><span>{{ periodScheduleLabel(period) }}</span><small>{{ period.start_date }} → {{ period.end_date || t("进行中") }}</small>
            </article>
           </div>
         </section>
         <section v-if="importPreview.subprograms?.length" class="program-json-preview-section">
            <div class="program-json-preview-heading"><div><p class="form-kicker">SUBPROGRAMS</p><h3>{{ t("子节目") }}</h3></div><span class="section-count">{{ importPreview.subprograms.length }}</span></div>
            <div class="program-json-period-list">
              <article v-for="subprogram in importPreview.subprograms" :key="subprogram.program.id || subprogram.program.subprogram_name" class="program-json-period-item">
                <strong>{{ subprogram.program.subprogram_name }}</strong><span>{{ formatLabel(subprogram.program) }}</span><small>{{ subprogram.counts.periods }} {{ t("排期时期") }} · {{ subprogram.counts.occurrences }} {{ t("单集") }} · {{ t("是否完结") }}：{{ programStatus(subprogram.program) }} · {{ t("是否开启自动生成后续单集") }}：{{ subprogram.program.auto_generate ? t("是") : t("否") }}</small>
              </article>
            </div>
         </section>
          <section class="program-json-preview-section">
           <div class="program-json-preview-heading"><div><p class="form-kicker">EPISODE CONTENT</p><h3>{{ t("单集内容") }}</h3></div><span class="section-count">{{ importPreview.counts.occurrences }}</span></div>
           <div v-if="importPreview.occurrences.length" class="program-json-occurrence-list">
             <article v-for="(occurrence, index) in importPreview.occurrences" :key="`${occurrence.original_date}-${index}`" class="program-json-occurrence-item">
               <div class="program-json-occurrence-topline"><strong>{{ importOccurrenceEpisodeLabel(occurrence) }} · {{ occurrence.original_date }}</strong><span>{{ occurrence.original_time || t("全天") }}</span><span v-if="occurrence.status === 'rescheduled'">→ {{ occurrence.adjusted_date }}{{ occurrence.adjusted_time ? ` ${occurrence.adjusted_time}` : "" }}</span><span>{{ importDeliveryLabel(occurrence.delivery) }}</span><span>{{ importSpecialLabel(occurrence.special) }}</span><span :class="{ cancelled: occurrence.status === 'cancelled', rescheduled: occurrence.status === 'rescheduled', deleted: occurrence.status === 'deleted' }">{{ importStatusLabel(occurrence.status) }}</span></div>
               <strong v-if="occurrence.title" class="program-json-occurrence-title">{{ occurrence.title }}</strong>
                <p v-if="occurrence.guests?.length || occurrence.absent_members?.length || occurrence.note">{{ occurrence.guests?.length ? `${t("嘉宾")}：${occurrence.guests.join("、")}` : "" }}{{ occurrence.guests?.length && (occurrence.absent_members?.length || occurrence.note) ? " · " : "" }}{{ occurrence.absent_members?.length ? `${t("缺席")}：${occurrence.absent_members.join("、")}` : "" }}{{ occurrence.absent_members?.length && occurrence.note ? " · " : "" }}{{ occurrence.note }}</p>
               <div v-if="occurrenceLinkItems(occurrence).length" class="program-episode-links">
                 <template v-for="link in occurrenceLinkItems(occurrence)" :key="link.key">
                   <a v-if="link.href" class="program-meta-link" :href="link.href" target="_blank" rel="noopener noreferrer">{{ link.label }}：{{ link.display }} ↗</a>
                   <span v-else>{{ link.label }}：{{ link.display }}</span>
                 </template>
               </div>
            </article>
          </div>
           <p v-else class="muted">{{ t("没有单集覆盖资料；导入后将只按排期规则生成。") }}</p>
        </section>
        <div v-if="importPreview.warnings?.length" class="program-json-warnings">
           <strong>{{ t("导入提示") }}</strong>
          <p v-for="warning in importPreview.warnings" :key="warning">{{ warning }}</p>
        </div>
        <div class="actions program-json-dialog-actions">
           <button type="button" class="secondary program-action-button" :disabled="importSubmitting" @click="closeImportPreview">{{ t("取消") }}</button>
             <button type="button" class="program-action-button" :disabled="importSubmitting || (importPreview._program_scope === 'subprogram' ? !importTargetParentProgramId : importTargetMode === 'overwrite' && !importTargetProgramId)" @click="submitImport">{{ importSubmitting ? t("导入中……") : importTargetMode === "overwrite" ? t("确认覆盖导入") : t("确认新建导入") }}</button>
         </div>
          </div>
        </section>
      </div>
      <NewsLightbox :images="occurrenceLightboxImages" :start="occurrenceLightboxIndex" @close="closeOccurrenceLightbox" />

   </main>
</template>
