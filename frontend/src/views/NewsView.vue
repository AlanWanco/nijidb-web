<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { formatLocalDateTime } from "../datetime";
import { locale, t } from "../i18n";
import { newsSourceGroup, newsSourceLabel, newsTagLabel } from "../newsLabels";
import { newsSummaryText } from "../newsMarkdown";

const route = useRoute();
const router = useRouter();
const query = ref(typeof route.query.q === "string" ? route.query.q : "");
const items = ref([]);
const tagOptions = ref([]);
const sourceOptions = ref([]);
const lastSync = ref(null);
const total = ref(0);
const page = ref(Number(route.query.page) || 1);
const pages = ref(1);
const loading = ref(true);
const error = ref("");
let requestId = 0;
const heroRef = ref(null);
const heroStyle = ref({
  "--news-pointer-x": "0px",
  "--news-pointer-y": "0px",
  "--news-focus-x": "0px",
  "--news-focus-y": "0px",
});

const activeTags = computed(() => {
  const raw = typeof route.query.tags === "string" ? route.query.tags : "";
  return raw
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
});
const activeSource = computed(() =>
  typeof route.query.source === "string"
    ? newsSourceGroup(route.query.source)
    : "",
);
const filterQuery = computed(() => {
  const result = {};
  if (route.query.q) result.q = route.query.q;
  if (route.query.tags) result.tags = route.query.tags;
  if (route.query.source) result.source = activeSource.value;
  return result;
});
const visibleTagOptions = computed(() => tagOptions.value);
const pageLabel = computed(() => `${page.value} / ${pages.value}`);

function handlePointerMove(event) {
  if (event.pointerType && event.pointerType !== "mouse") return;
  const hero = heroRef.value;
  if (!hero) return;
  const bounds = hero.getBoundingClientRect();
  const x = (event.clientX - bounds.left) / bounds.width - 0.5;
  const y = (event.clientY - bounds.top) / bounds.height - 0.5;
  heroStyle.value = {
    "--news-pointer-x": `${x * 24}px`,
    "--news-pointer-y": `${y * 18}px`,
    "--news-focus-x": `${x * 50}px`,
    "--news-focus-y": `${y * 34}px`,
  };
}

function resetPointer() {
  heroStyle.value = {
    "--news-pointer-x": "0px",
    "--news-pointer-y": "0px",
    "--news-focus-x": "0px",
    "--news-focus-y": "0px",
  };
}

function formatDate(value) {
  if (!value) return "—";
  return value.replace(/-/g, "/");
}

function sourceLabel(source) {
  return newsSourceLabel(source) || t("官网新闻");
}

function makeQuery(next = {}) {
  const result = {};
  const nextQuery = Object.prototype.hasOwnProperty.call(next, "q")
    ? next.q
    : query.value.trim();
  const nextTags = Object.prototype.hasOwnProperty.call(next, "tags")
    ? next.tags
    : activeTags.value.join(",");
  const nextSource = Object.prototype.hasOwnProperty.call(next, "source")
    ? next.source
    : activeSource.value;
  if (nextQuery) result.q = nextQuery;
  if (nextTags) result.tags = nextTags;
  if (nextSource) result.source = nextSource;
  if (next.page && next.page > 1) result.page = String(next.page);
  return result;
}

function submitSearch() {
  router.push({
    path: "/news",
    query: makeQuery({ q: query.value.trim(), page: 1 }),
  });
}

function toggleTag(tag) {
  const next = activeTags.value.includes(tag)
    ? activeTags.value.filter((value) => value !== tag)
    : [...activeTags.value, tag];
  router.push({
    path: "/news",
    query: makeQuery({ tags: next.join(","), page: 1 }),
  });
}

function setSource(source) {
  router.push({
    path: "/news",
    query: makeQuery({
      source: activeSource.value === source ? "" : source,
      page: 1,
    }),
  });
}

function clearFilters() {
  query.value = "";
  router.push({ path: "/news" });
}

function goToPage(nextPage) {
  if (nextPage < 1 || nextPage > pages.value || nextPage === page.value) return;
  router.push({ path: "/news", query: makeQuery({ page: nextPage }) });
}

function detailTo(item) {
  const query = { ...filterQuery.value };
  if (page.value > 1) query.page = String(page.value);
  return { path: `/news/${item.id}`, query };
}

async function loadNews() {
  const id = ++requestId;
  loading.value = true;
  error.value = "";
  try {
    const params = new URLSearchParams();
    if (typeof route.query.q === "string" && route.query.q)
      params.set("q", route.query.q);
    if (typeof route.query.tags === "string" && route.query.tags)
      params.set("tags", route.query.tags);
    if (typeof route.query.source === "string" && route.query.source)
      params.set("source", activeSource.value);
    params.set("page", String(Math.max(1, Number(route.query.page) || 1)));
    params.set("page_size", "24");
    const data = await api(`/api/news?${params}`);
    if (id !== requestId) return;
    items.value = data.items || [];
    tagOptions.value = data.tag_options || [];
    sourceOptions.value = data.source_options || [];
    total.value = data.total || 0;
    page.value = data.page || 1;
    pages.value = data.pages || 1;
    lastSync.value = data.last_sync || null;
  } catch (requestError) {
    if (id === requestId)
      error.value = requestError.message || t("新闻加载失败");
  } finally {
    if (id === requestId) loading.value = false;
  }
}

watch(
  () => route.fullPath,
  () => {
    query.value = typeof route.query.q === "string" ? route.query.q : "";
    loadNews();
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  requestId += 1;
});
</script>

<template>
  <main class="page news-page">
    <section
      ref="heroRef"
      class="hero news-hero"
      :style="heroStyle"
      @pointermove="handlePointerMove"
      @pointerleave="resetPointer"
    >
      <div class="news-hero-grid" aria-hidden="true"></div>
      <div class="hero-topline news-hero-fade">
        <p class="eyebrow">
          <span class="eyebrow-dot"></span>OFFICIAL SITE NEWS / ARCHIVE
        </p>
        <span class="hero-stamp"
          >OFFICIAL SITE NEWS<br /><strong>LOCAL INDEX</strong></span
        >
      </div>
      <div class="news-hero-content news-hero-fade">
        <h1>
          {{ t("官网新闻") }}<span class="title-mark" aria-hidden="true"></span>
        </h1>
        <p class="hero-description">
          {{ t("整理虹咲官方 Topics、News 与历史公告。") }}<br /><span>{{
            t("以本地 Markdown 为基础，持续追踪官网 Topics。")
          }}</span>
        </p>
        <form class="search news-search" @submit.prevent="submitSearch">
          <svg class="search-icon" viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="10.8" cy="10.8" r="6.5"></circle>
            <path d="m16 16 4.5 4.5"></path>
          </svg>
          <input
            v-model="query"
            name="q"
            :placeholder="t('搜索新闻标题、摘要或标签')"
            :aria-label="t('搜索新闻标题、摘要或标签')"
          />
          <button>
            <span>{{ t("搜索新闻") }}</span
            ><span aria-hidden="true">↗</span>
          </button>
        </form>
      </div>
      <div class="news-hero-art" aria-hidden="true">
        <span class="news-art-orbit news-art-orbit-outer"></span>
        <span class="news-art-orbit news-art-orbit-inner"></span>
        <span class="news-art-cross news-art-cross-h"></span>
        <span class="news-art-cross news-art-cross-v"></span>
        <span class="news-art-card news-art-card-main"></span>
        <span class="news-art-card news-art-card-small"></span>
        <span class="news-art-dot news-art-dot-main"></span>
        <span class="news-art-dot news-art-dot-small"></span>
        <span class="news-art-focus"></span>
      </div>
      <div class="news-hero-scroll news-hero-fade">
        <span>SCROLL / NEWS</span><i aria-hidden="true"></i>
      </div>
    </section>

    <div class="news-toolbar">
      <div class="toolbar-main">
        <span class="status-dot" aria-hidden="true"></span>
        <div>
          <strong>{{ total }}{{ t("条新闻") }}</strong
          ><span class="toolbar-label">OFFICIAL NEWS INDEX</span>
        </div>
      </div>
      <div class="toolbar-meta">
        <span v-if="activeTags.length || activeSource" class="filter-chip">{{
          t("已应用 {count} 项筛选", {
            count: activeTags.length + (activeSource ? 1 : 0),
          })
        }}</span>
        <span class="sync-time"
          ><span class="sync-mark" aria-hidden="true"></span
          ><template v-if="lastSync"
            >{{ t("Topics 上次检查") }}
            <time
              :datetime="lastSync.checked_at"
              :title="t('按当前设备时区显示')"
              >{{ formatLocalDateTime(lastSync.checked_at) }}</time
            ></template
          ><template v-else>{{
            total ? t("本地新闻归档已载入") : t("等待新闻首次同步")
          }}</template></span
        >
      </div>
    </div>

    <section class="news-filters" aria-label="News filters">
      <div class="news-filter-heading">
        <span class="eyebrow">FILTERS / TAGS</span
        ><button
          v-if="activeTags.length || activeSource || route.query.q"
          class="text-button"
          type="button"
          @click="clearFilters"
        >
          {{ t("清除筛选") }}
        </button>
      </div>
      <div
        v-if="sourceOptions.length"
        class="news-filter-row news-source-filter"
      >
        <button
          v-for="source in sourceOptions"
          :key="source.name"
          type="button"
          class="news-filter-chip"
          :class="{ selected: activeSource === source.name }"
          @click="setSource(source.name)"
        >
          {{ source.label }} <small>{{ source.count }}</small>
        </button>
      </div>
      <div v-if="visibleTagOptions.length" class="news-filter-row">
        <button
          v-for="tag in visibleTagOptions"
          :key="tag.name"
          type="button"
          class="news-filter-chip"
          :class="{ selected: activeTags.includes(tag.name) }"
          @click="toggleTag(tag.name)"
          :title="tag.name"
        >
          #{{ newsTagLabel(tag.name, locale) }} <small>{{ tag.count }}</small>
        </button>
      </div>
    </section>

    <p v-if="loading" class="state">{{ t("正在读取新闻……") }}</p>
    <p v-else-if="error" class="state error">{{ error }}</p>
    <section v-else class="news-list-section">
      <transition-group
        v-if="items.length"
        name="news-card"
        tag="div"
        class="news-grid"
      >
        <RouterLink
          v-for="(item, index) in items"
          :key="item.id"
          class="news-card"
          :to="detailTo(item)"
          :style="{ '--news-card-delay': `${Math.min(index, 11) * 35}ms` }"
        >
          <div class="news-card-media">
            <img
              v-if="item.cover_url"
              :src="item.cover_url"
              loading="lazy"
              :alt="item.title"
            />
            <div v-else class="news-card-placeholder">
              <span>NEWS</span><b>{{ String(index + 1).padStart(2, "0") }}</b>
            </div>
            <span class="news-card-index">{{
              String((page - 1) * 24 + index + 1).padStart(3, "0")
            }}</span>
            <span class="news-card-arrow" aria-hidden="true">↗</span>
          </div>
          <div class="news-card-body">
            <div class="news-card-meta">
              <time :datetime="item.published_at">{{
                formatDate(item.published_at)
              }}</time
              ><span>{{ item.category || t("未分类") }}</span>
            </div>
            <h2>{{ item.title }}</h2>
            <p>{{ newsSummaryText(item.summary) || t("暂无摘要") }}</p>
            <div class="news-card-foot">
              <span>{{ sourceLabel(item.source) }}</span
              ><span v-if="item.image_count"
                >{{ item.image_count }}{{ t("张图片") }}</span
              >
            </div>
            <div class="news-card-tags">
              <span v-for="tag in item.tags.slice(0, 3)" :key="tag" :title="tag"
                >#{{ newsTagLabel(tag, locale) }}</span
              >
            </div>
          </div>
        </RouterLink>
      </transition-group>
      <div v-else class="news-empty">
        <span class="news-empty-mark" aria-hidden="true">∅</span>
        <h2>{{ t("没有匹配的新闻。") }}</h2>
        <p>{{ t("尝试换一个关键词或清除 tags 筛选。") }}</p>
      </div>

      <nav v-if="pages > 1" class="news-pagination" aria-label="News pages">
        <button
          type="button"
          class="secondary"
          :disabled="page <= 1"
          @click="goToPage(page - 1)"
        >
          ← {{ t("上一页") }}
        </button>
        <span>{{ pageLabel }}</span>
        <button
          type="button"
          class="secondary"
          :disabled="page >= pages"
          @click="goToPage(page + 1)"
        >
          {{ t("下一页") }} →
        </button>
      </nav>
    </section>
  </main>
</template>
