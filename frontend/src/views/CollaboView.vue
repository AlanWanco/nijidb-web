<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import CollaboImage from "../components/CollaboImage.vue";
import { listCollaborations } from "../collabo/api";
import {
  COLLABO_CHARACTER_TAG_IDS,
  COLLABO_CHARACTER_TAGS,
  COLLABO_COMBINATION_GROUPS,
  characterLabel,
  combinationGroup,
  normalizeCharacterTags,
} from "../collabo/characters.js";
import { cardCombinationTagIds, coverUrl, dateLabel, PAGE_SIZE, pageNumber, safeUrl, yearValues } from "../collabo/model";
import { c } from "../collabo/text";
import { localeTag } from "../i18n";
import "../collabo/style.css";

const route = useRoute();
const router = useRouter();
const data = ref(null);
const loading = ref(true);
const error = ref("");
const search = ref("");
const heroRef = ref(null);
const heroPointerStyle = ref({
  "--cb-hero-pointer-x": "0px",
  "--cb-hero-pointer-y": "0px",
  "--cb-hero-focus-x": "0px",
  "--cb-hero-focus-y": "0px",
  "--cb-hero-focus-scale": "1",
});
let requestId = 0;
function handleHeroPointerMove(event) {
  if (event.pointerType && event.pointerType !== "mouse") return;
  const hero = heroRef.value;
  if (!hero) return;
  const bounds = hero.getBoundingClientRect();
  const x = (event.clientX - bounds.left) / bounds.width - 0.5;
  const y = (event.clientY - bounds.top) / bounds.height - 0.5;
  const intensity = Math.min(1, Math.hypot(x, y) * 1.4);
  heroPointerStyle.value = {
    "--cb-hero-pointer-x": `${x * 24}px`,
    "--cb-hero-pointer-y": `${y * 18}px`,
    "--cb-hero-focus-x": `${x * 46}px`,
    "--cb-hero-focus-y": `${y * 34}px`,
    "--cb-hero-focus-scale": String(1 + intensity * 0.1),
  };
}
function resetHeroPointer() {
  heroPointerStyle.value = {
    "--cb-hero-pointer-x": "0px",
    "--cb-hero-pointer-y": "0px",
    "--cb-hero-focus-x": "0px",
    "--cb-hero-focus-y": "0px",
    "--cb-hero-focus-scale": "1",
  };
}
const q = computed(() => (typeof route.query.q === "string" ? route.query.q : ""));
const selectedYears = computed(() => yearValues(route.query.year));
const yearQuery = computed(() => selectedYears.value.join(","));
const selectedCombinationGroup = computed(() => combinationGroup(route.query.group));
const groupQuery = computed(() => selectedCombinationGroup.value?.id || "");
const selectedCharacterTags = computed(() => (groupQuery.value ? [] : normalizeCharacterTags(route.query.tags)));
const allCharactersSelected = computed(
  () => !selectedCharacterTags.value.length || selectedCharacterTags.value.length === COLLABO_CHARACTER_TAG_IDS.length,
);
const characterTagQuery = computed(() => (allCharactersSelected.value ? "" : selectedCharacterTags.value.join(",")));
const page = computed(() => pageNumber(route.query.page));
const pages = computed(() => Math.max(1, Math.ceil((data.value?.total || 0) / PAGE_SIZE)));
const source = computed(() => data.value?.source);
function queryFor(nextPage = 1) {
  return {
    ...(q.value ? { q: q.value } : {}),
    ...(yearQuery.value ? { year: yearQuery.value } : {}),
    ...(characterTagQuery.value ? { tags: characterTagQuery.value } : {}),
    ...(groupQuery.value ? { group: groupQuery.value } : {}),
    ...(nextPage > 1 ? { page: nextPage } : {}),
  };
}
function searchItems() {
  router.push({
    path: "/collabo",
    query: {
      ...(search.value.trim() ? { q: search.value.trim() } : {}),
      ...(yearQuery.value ? { year: yearQuery.value } : {}),
      ...(characterTagQuery.value ? { tags: characterTagQuery.value } : {}),
      ...(groupQuery.value ? { group: groupQuery.value } : {}),
    },
  });
}
function pushCharacterQuery(tags) {
  router.push({
    path: "/collabo",
    query: {
      ...(q.value ? { q: q.value } : {}),
      ...(yearQuery.value ? { year: yearQuery.value } : {}),
      ...(tags.length ? { tags: tags.join(",") } : {}),
    },
  });
}
function toggleCharacterTag(id) {
  const next = new Set(selectedCharacterTags.value);
  if (!selectedCharacterTags.value.length) {
    next.add(id);
  } else if (next.has(id)) {
    next.delete(id);
  } else {
    next.add(id);
  }
  pushCharacterQuery(next.size === COLLABO_CHARACTER_TAG_IDS.length ? [] : [...next]);
}
function selectAllCharacterTags() {
  pushCharacterQuery([]);
}
function toggleYear(value) {
  const next = new Set(selectedYears.value);
  if (!value) {
    next.clear();
  } else if (next.has(value)) {
    next.delete(value);
  } else {
    next.add(value);
  }
  const years = (data.value?.years || []).filter((entry) => next.has(entry));
  router.push({
    path: "/collabo",
    query: {
      ...(q.value ? { q: q.value } : {}),
      ...(years.length ? { year: years.join(",") } : {}),
      ...(characterTagQuery.value ? { tags: characterTagQuery.value } : {}),
      ...(groupQuery.value ? { group: groupQuery.value } : {}),
    },
  });
}
function toggleCombinationGroup(id) {
  const next = id && groupQuery.value !== id ? id : "";
  router.push({
    path: "/collabo",
    query: {
      ...(q.value ? { q: q.value } : {}),
      ...(yearQuery.value ? { year: yearQuery.value } : {}),
      ...(next ? { group: next } : {}),
    },
  });
}
function cardTags(item) {
  const combinationIds = cardCombinationTagIds(item.tags || []);
  const compact =
    combinationIds.includes("all") ||
    combinationIds.includes("idol12") ||
    combinationIds.some((id) => ["initial9", "anime10", "shioriko10"].includes(id));
  const characterTags = compact
    ? []
    : (item.tags || []).map((id) => ({ key: `character-${id}`, label: characterLabel(id, localeTag()), kind: "character" }));
  const combinationTags = combinationIds.map((id) => ({
    key: `combination-${id}`,
    label: id === "all" ? c("全员") : c(combinationGroup(id)?.label || id),
    kind: "combination",
  }));
  return [...combinationTags, ...characterTags];
}
async function load() {
  const id = ++requestId;
  loading.value = true;
  error.value = "";
  search.value = q.value;
  try {
    const result = await listCollaborations({
      q: q.value,
      year: yearQuery.value,
      tags: characterTagQuery.value,
      group: groupQuery.value,
      page: page.value,
    });
    if (id === requestId) {
      data.value = result;
      const last = Math.max(1, Math.ceil(result.total / PAGE_SIZE));
      if (page.value > last) router.replace({ path: "/collabo", query: queryFor(last) });
    }
  } catch (err) {
    if (id === requestId) error.value = err.message;
  } finally {
    if (id === requestId) loading.value = false;
  }
}
watch(() => [q.value, yearQuery.value, characterTagQuery.value, groupQuery.value, page.value], load, { immediate: true });
onBeforeUnmount(() => {
  requestId += 1;
});
</script>

<template>
  <main class="page cb-page cb-index-page">
    <section
      ref="heroRef"
      class="cb-hero"
      :style="heroPointerStyle"
      @pointermove="handleHeroPointerMove"
      @pointerleave="resetHeroPointer"
    >
      <div class="cb-hero-grid" aria-hidden="true"></div>
      <div class="cb-hero-topline">
        <p class="eyebrow"><span class="cb-eyebrow-dot"></span>COLLABORATION / ILLUSTRATION</p>
        <span class="cb-hero-stamp">OFFICIAL DATA<br /><strong>LOCAL INDEX</strong></span>
      </div>
      <div class="cb-hero-copy">
        <h1>{{ c("联动立绘档案") }}<span class="cb-title-mark" aria-hidden="true"></span></h1>
        <p class="cb-hero-lead">{{ c("每一次相遇，都有新的模样。") }}</p>
        <p class="cb-hero-description">{{ c("从联动企划到限定立绘，收藏虹咲的每一种色彩。") }}</p>
        <form class="search cb-search" @submit.prevent="searchItems">
          <svg class="cb-search-icon" viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="10.8" cy="10.8" r="6.5"></circle>
            <path d="m16 16 4.5 4.5"></path>
          </svg>
          <input
            v-model="search"
            type="search"
            :aria-label="c('标题、合作方、备注或关键词')"
            :placeholder="c('标题、合作方、备注或关键词')"
          /><button type="submit">{{ c("搜索") }} ↗</button>
        </form>
      </div>
      <div class="cb-hero-art" aria-hidden="true">
        <span class="cb-hero-ring cb-hero-ring-outer"></span>
        <span class="cb-hero-ring cb-hero-ring-middle"></span>
        <span class="cb-hero-ring cb-hero-ring-inner"></span>
        <span class="cb-hero-axis cb-hero-axis-horizontal"></span>
        <span class="cb-hero-axis cb-hero-axis-vertical"></span>
        <span class="cb-hero-block cb-hero-block-main"></span>
        <span class="cb-hero-block cb-hero-block-small"></span>
        <span class="cb-hero-dot cb-hero-dot-main"></span>
        <span class="cb-hero-dot cb-hero-dot-small"></span>
        <span class="cb-hero-focus"></span>
        <span class="cb-hero-scan"></span>
        <span class="cb-hero-art-code"
          >NO. 03<br /><b>{{ String(data?.total || 0).padStart(3, "0") }} ITEMS</b></span
        >
      </div>
      <div class="cb-hero-scroll">
        <span aria-hidden="true">SCROLL / INDEX</span><i aria-hidden="true"></i><span>ART &amp; ENCOUNTERS</span>
      </div>
    </section>

    <div class="cb-toolbar">
      <RouterLink class="cb-link cb-manage-link" to="/admin/collabo">{{ c("管理联动") }} ↗</RouterLink>
    </div>
    <section class="cb-year-filter" :aria-label="c('年份')">
      <div class="cb-year-filter-heading">
        <span>YEAR / FILTER</span>
        <strong>{{ selectedYears.length ? `${selectedYears.length} SELECTED` : "ALL YEARS" }}</strong>
      </div>
      <div class="cb-year-options" role="group" :aria-label="c('年份')">
        <button
          type="button"
          class="cb-year-chip"
          :class="{ selected: !selectedYears.length }"
          :aria-pressed="!selectedYears.length"
          @click="toggleYear('')"
        >
          <b>ALL</b>{{ c("全部年份") }}
        </button>
        <button
          v-for="value in data?.years || []"
          :key="value"
          type="button"
          class="cb-year-chip"
          :class="{ selected: selectedYears.includes(value) }"
          :aria-pressed="selectedYears.includes(value)"
          @click="toggleYear(value)"
        >
          {{ value }}
        </button>
      </div>
    </section>
    <section class="cb-character-filter" :aria-label="c('角色')">
      <div class="cb-year-filter-heading">
        <span>CHARACTER / TAG</span>
        <strong>{{ allCharactersSelected ? "ALL CHARACTERS" : `${selectedCharacterTags.length} SELECTED` }}</strong>
      </div>
      <div class="cb-character-options" role="group" :aria-label="c('角色')">
        <button
          type="button"
          class="cb-character-chip"
          :class="{ selected: allCharactersSelected }"
          :aria-pressed="allCharactersSelected"
          @click="selectAllCharacterTags"
        >
          <b>ALL</b>{{ c("全员") }}
        </button>
        <button
          v-for="tag in COLLABO_CHARACTER_TAGS"
          :key="tag.id"
          type="button"
          class="cb-character-chip"
          :class="{ selected: selectedCharacterTags.includes(tag.id) }"
          :aria-pressed="selectedCharacterTags.includes(tag.id)"
          @click="toggleCharacterTag(tag.id)"
        >
          <i class="cb-character-dot" :style="{ backgroundColor: tag.color }"></i>{{ characterLabel(tag.id, localeTag()) }}
        </button>
      </div>
    </section>
    <section class="cb-combination-filter" :aria-label="c('组合筛选')">
      <div class="cb-year-filter-heading">
        <span>COMBINATION / FILTER</span>
        <strong>{{ selectedCombinationGroup ? c(selectedCombinationGroup.label) : c("全部组合") }}</strong>
      </div>
      <div class="cb-combination-options" role="group" :aria-label="c('组合筛选')">
        <button
          type="button"
          class="cb-character-chip cb-combination-chip"
          :class="{ selected: !selectedCombinationGroup }"
          :aria-pressed="!selectedCombinationGroup"
          @click="toggleCombinationGroup('')"
        >
          <b>ALL</b>{{ c("全部组合") }}
        </button>
        <button
          v-for="group in COLLABO_COMBINATION_GROUPS"
          :key="group.id"
          type="button"
          class="cb-character-chip cb-combination-chip"
          :class="{ selected: selectedCombinationGroup?.id === group.id }"
          :aria-pressed="selectedCombinationGroup?.id === group.id"
          @click="toggleCombinationGroup(group.id)"
        >
          {{ c(group.label) }}
        </button>
      </div>
    </section>
    <div class="cb-index-heading">
      <span
        >{{ c("联动一览") }} <b>{{ String(data?.total || 0).padStart(3, "0") }}</b></span
      ><small>CHRONOLOGICAL INDEX</small>
    </div>
    <p v-if="data?.mode === 'preview'" class="cb-notice">{{ c("本地采集预览 · 图片已直接展示") }}</p>
    <div v-if="loading" class="cb-grid" aria-busy="true" :aria-label="c('读取中……')">
      <div v-for="n in PAGE_SIZE" :key="n" class="cb-skeleton"></div>
    </div>
    <div v-else-if="error" class="cb-state" role="alert">
      <p>{{ error }}</p>
      <button type="button" @click="load">{{ c("重试") }}</button>
    </div>
    <p v-else-if="!data?.items.length" class="cb-state">{{ c("还没有匹配的数据。") }}</p>
    <div v-else class="cb-grid">
      <RouterLink
        v-for="(item, index) in data.items"
        :key="item.id"
        class="cb-card"
        :to="{ path: `/collabo/${item.slug}`, query: queryFor(page) }"
      >
        <div class="cb-card-art">
          <CollaboImage :src="coverUrl(item)" :alt="item.title" /><span class="cb-card-no">{{
            String((page - 1) * PAGE_SIZE + index + 1).padStart(3, "0")
          }}</span
          ><span class="cb-card-more" aria-hidden="true">↗</span>
        </div>
        <div class="cb-card-body">
          <div class="cb-card-meta">
            <time :datetime="item.date">{{ dateLabel(item.date, localeTag()) }}</time
            ><span>{{ item.image_count }} IMG</span>
          </div>
          <h2>{{ item.title }}</h2>
          <p>{{ item.partners.join(" · ") }}</p>
          <div v-if="cardTags(item).length" class="cb-card-tags" :aria-label="c('角色标签')">
            <span
              v-for="tag in cardTags(item)"
              :key="tag.key"
              :class="{ 'cb-card-combination-tag': tag.kind === 'combination' }"
              >{{ tag.label }}</span
            >
          </div>
        </div>
      </RouterLink>
    </div>
    <nav v-if="pages > 1 && !error" class="cb-pagination" :aria-label="c('联动一览')">
      <RouterLink v-if="page > 1" :to="{ path: '/collabo', query: queryFor(page - 1) }">← {{ c("上一页") }}</RouterLink
      ><span v-else></span>
      <span>{{ c("第 {page} / {total} 页", { page, total: pages }) }}</span>
      <RouterLink v-if="page < pages" :to="{ path: '/collabo', query: queryFor(page + 1) }"
        >{{ c("下一页") }} →</RouterLink
      ><span v-else></span>
    </nav>
    <div v-if="source && safeUrl(source.url)" class="cb-source-credit">
      <strong>{{ c("数据源：") }}</strong
      ><a :href="safeUrl(source.url)" target="_blank" rel="noopener noreferrer">{{ source.title || source.site }} ↗</a>
    </div>
  </main>
</template>
