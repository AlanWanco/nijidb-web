<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import CollaboImage from "../components/CollaboImage.vue";
import { listCollaborations } from "../collabo/api";
import { coverUrl, dateLabel, PAGE_SIZE, pageNumber, safeUrl, yearValues } from "../collabo/model";
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
const page = computed(() => pageNumber(route.query.page));
const pages = computed(() => Math.max(1, Math.ceil((data.value?.total || 0) / PAGE_SIZE)));
const source = computed(() => data.value?.source);
function queryFor(nextPage = 1) {
  return {
    ...(q.value ? { q: q.value } : {}),
    ...(yearQuery.value ? { year: yearQuery.value } : {}),
    ...(nextPage > 1 ? { page: nextPage } : {}),
  };
}
function searchItems() {
  router.push({
    path: "/collabo",
    query: {
      ...(search.value.trim() ? { q: search.value.trim() } : {}),
      ...(yearQuery.value ? { year: yearQuery.value } : {}),
    },
  });
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
    },
  });
}
async function load() {
  const id = ++requestId;
  loading.value = true;
  error.value = "";
  search.value = q.value;
  try {
    const result = await listCollaborations({ q: q.value, year: yearQuery.value, page: page.value });
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
watch(() => [q.value, yearQuery.value, page.value], load, { immediate: true });
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

    <form class="cb-toolbar" @submit.prevent="searchItems">
      <div class="cb-search">
        <svg class="cb-search-icon" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="10.8" cy="10.8" r="6.5"></circle>
          <path d="m16 16 4.5 4.5"></path>
        </svg>
        <input
          v-model="search"
          type="search"
          :aria-label="c('标题、合作方或关键词')"
          :placeholder="c('标题、合作方或关键词')"
        /><button type="submit">{{ c("搜索") }} ↗</button>
      </div>
      <RouterLink class="cb-link cb-manage-link" to="/admin/collabo">{{ c("管理联动") }} ↗</RouterLink>
    </form>
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
    <div class="cb-index-heading">
      <span
        >{{ c("联动一览") }} <b>{{ String(data?.total || 0).padStart(3, "0") }}</b></span
      ><small>CHRONOLOGICAL INDEX</small>
    </div>
    <p v-if="data?.mode === 'preview'" class="cb-notice">{{ c("本地采集预览 · 图片尚待人工审核") }}</p>
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
