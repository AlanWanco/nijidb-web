<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
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
const tagCatalog = ref([]);
const sourceOptions = ref([]);
const lastSync = ref(null);
const authenticated = ref(false);
const tagManagerOpen = ref(false);
const tagDrafts = ref([]);
const tagSaving = ref(false);
const tagError = ref("");
const tagMessage = ref("");
const newTag = reactive({
  key: "",
  zh: "",
  ja: "",
  en: "",
});
const total = ref(0);
const page = ref(Number(route.query.page) || 1);
const pages = ref(1);
const loading = ref(true);
const error = ref("");
let requestId = 0;
const heroRef = ref(null);
const heroScrollStyle = ref({
  "--news-hero-copy-opacity": "1",
  "--news-hero-art-opacity": "1",
  "--news-hero-grid-opacity": ".56",
  "--news-hero-copy-shift": "0px",
  "--news-hero-art-shift": "0px",
});
const heroPointerStyle = ref({
  "--news-hero-pointer-x": "0px",
  "--news-hero-pointer-y": "0px",
  "--news-hero-focus-x": "0px",
  "--news-hero-focus-y": "0px",
  "--news-hero-focus-scale": "1",
});
let heroScrollFrame = 0;

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
  const intensity = Math.min(1, Math.hypot(x, y) * 1.4);
  heroPointerStyle.value = {
    "--news-hero-pointer-x": `${x * 24}px`,
    "--news-hero-pointer-y": `${y * 18}px`,
    "--news-hero-focus-x": `${x * 46}px`,
    "--news-hero-focus-y": `${y * 34}px`,
    "--news-hero-focus-scale": String(1 + intensity * 0.1),
  };
}

function resetPointer() {
  heroPointerStyle.value = {
    "--news-hero-pointer-x": "0px",
    "--news-hero-pointer-y": "0px",
    "--news-hero-focus-x": "0px",
    "--news-hero-focus-y": "0px",
    "--news-hero-focus-scale": "1",
  };
}

function updateHeroScrollStyle() {
  heroScrollFrame = 0;
  const hero = heroRef.value;
  if (!hero) return;
  const fadeDistance = Math.max(hero.offsetHeight * 0.72, window.innerHeight * 0.65);
  const progress = Math.min(1, Math.max(0, window.scrollY / fadeDistance));
  heroScrollStyle.value = {
    "--news-hero-copy-opacity": String(1 - progress),
    "--news-hero-art-opacity": String(1 - progress * 0.82),
    "--news-hero-grid-opacity": String(0.56 - progress * 0.45),
    "--news-hero-copy-shift": `${progress * -28}px`,
    "--news-hero-art-shift": `${progress * 18}px`,
  };
}

function scheduleHeroScrollStyle() {
  if (heroScrollFrame) return;
  heroScrollFrame = requestAnimationFrame(updateHeroScrollStyle);
}

function formatDate(value) {
  if (!value) return "—";
  return value.replace(/-/g, "/");
}

function sourceLabel(source) {
  return newsSourceLabel(source) || t("官网新闻");
}

function tagLabel(tag) {
  return newsTagLabel(tag.name, locale.value, tag);
}

function tagToken(tag) {
  return tag.id || tag.name;
}

function tagSelected(tag) {
  return activeTags.value.includes(tagToken(tag)) || activeTags.value.includes(tag.name);
}

function cloneTag(tag) {
  return {
    id: tag.id,
    name: tag.name,
    labels: { ...(tag.labels || {}) },
  };
}

function openTagManager() {
  tagDrafts.value = tagCatalog.value.map(cloneTag);
  tagError.value = "";
  tagMessage.value = "";
  tagManagerOpen.value = true;
}

function closeTagManager() {
  if (tagSaving.value) return;
  tagManagerOpen.value = false;
  tagError.value = "";
  tagMessage.value = "";
}

async function loadAuth() {
  try {
    const data = await api("/api/auth/session");
    authenticated.value = Boolean(data.authenticated);
  } catch {
    authenticated.value = false;
  }
}

async function saveTagCatalog() {
  if (tagSaving.value) return;
  const previousCatalog = tagCatalog.value;
  tagSaving.value = true;
  tagError.value = "";
  tagMessage.value = "";
  try {
    const data = await api("/api/admin/news/tags", {
      method: "PATCH",
      body: {
        tags: tagDrafts.value.map((tag) => ({
          id: tag.id,
          key: tag.name,
          labels: tag.labels,
        })),
      },
    });
    tagCatalog.value = data.tags || [];
    tagDrafts.value = tagCatalog.value.map(cloneTag);
    tagMessage.value = t("标签目录已保存");
    const renamedTokens = new Map();
    for (const oldTag of previousCatalog) {
      const nextTag = tagCatalog.value.find((tag) => tag.id === oldTag.id);
      if (nextTag) {
        renamedTokens.set(oldTag.id, nextTag.id);
        renamedTokens.set(oldTag.name, nextTag.id);
      }
    }
    const nextFilterTags = activeTags.value.map((token) => renamedTokens.get(token) || token);
    if (nextFilterTags.join(",") !== activeTags.value.join(",")) {
      await router.replace({
        path: "/news",
        query: makeQuery({ tags: nextFilterTags.join(","), page: 1 }),
      });
    } else {
      await loadNews();
    }
  } catch (requestError) {
    if (requestError.status === 401) {
      authenticated.value = false;
      tagManagerOpen.value = false;
    }
    tagError.value = requestError.message || t("标签目录保存失败");
  } finally {
    tagSaving.value = false;
  }
}

function resetNewTag() {
  newTag.key = "";
  newTag.zh = "";
  newTag.ja = "";
  newTag.en = "";
}

async function createTag() {
  if (tagSaving.value || !newTag.key.trim()) return;
  tagSaving.value = true;
  tagError.value = "";
  tagMessage.value = "";
  try {
    const labels = {};
    if (newTag.zh.trim()) labels["zh-CN"] = newTag.zh.trim();
    if (newTag.ja.trim()) labels.ja = newTag.ja.trim();
    if (newTag.en.trim()) labels.en = newTag.en.trim();
    const data = await api("/api/admin/news/tags", {
      method: "POST",
      body: { key: newTag.key.trim(), labels },
    });
    tagCatalog.value = data.tags || [];
    tagDrafts.value = tagCatalog.value.map(cloneTag);
    resetNewTag();
    tagMessage.value = t("标签已新增");
    await loadNews();
  } catch (requestError) {
    if (requestError.status === 401) {
      authenticated.value = false;
      tagManagerOpen.value = false;
    }
    tagError.value = requestError.message || t("标签新增失败");
  } finally {
    tagSaving.value = false;
  }
}

async function deleteTag(tag) {
  if (tagSaving.value || !window.confirm(t("删除这个标签？"))) return;
  tagSaving.value = true;
  tagError.value = "";
  tagMessage.value = "";
  try {
    const data = await api(`/api/admin/news/tags/${encodeURIComponent(tag.id)}`, {
      method: "DELETE",
    });
    tagCatalog.value = data.tags || [];
    tagDrafts.value = tagCatalog.value.map(cloneTag);
    tagMessage.value = t("标签已删除，文章标签已同步");
    const nextFilterTags = activeTags.value.filter(
      (token) => token !== tag.id && token !== tag.name,
    );
    if (nextFilterTags.length !== activeTags.value.length) {
      await router.replace({
        path: "/news",
        query: makeQuery({ tags: nextFilterTags.join(","), page: 1 }),
      });
    } else {
      await loadNews();
    }
  } catch (requestError) {
    if (requestError.status === 401) {
      authenticated.value = false;
      tagManagerOpen.value = false;
    }
    tagError.value = requestError.message || t("标签删除失败");
  } finally {
    tagSaving.value = false;
  }
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
  const token = tagToken(tag);
  const next = activeTags.value.filter(
    (value) => value !== token && value !== tag.name,
  );
  if (next.length === activeTags.value.length) next.push(token);
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
    tagCatalog.value = data.tag_catalog || tagOptions.value;
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

onMounted(() => {
  loadAuth();
  updateHeroScrollStyle();
  window.addEventListener("scroll", scheduleHeroScrollStyle, { passive: true });
  window.addEventListener("resize", scheduleHeroScrollStyle);
});

onBeforeUnmount(() => {
  requestId += 1;
  window.removeEventListener("scroll", scheduleHeroScrollStyle);
  window.removeEventListener("resize", scheduleHeroScrollStyle);
  if (heroScrollFrame) cancelAnimationFrame(heroScrollFrame);
});
</script>

<template>
  <main class="page news-page">
    <section
      ref="heroRef"
      class="hero news-hero"
      :style="[heroScrollStyle, heroPointerStyle]"
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
        <span class="news-art-scan"></span>
        <span class="news-hero-art-code"
          >NO. 04<br /><b>{{ String(total).padStart(3, "0") }} ARTICLES</b></span
        >
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
        ><div class="news-filter-actions">
          <button class="text-button" type="button" @click="clearFilters">
            {{ t("清除筛选") }}
          </button>
          <button
            v-if="authenticated"
            class="text-button"
            type="button"
            @click="tagManagerOpen ? closeTagManager() : openTagManager()"
          >
            {{ tagManagerOpen ? t("关闭") : t("编辑") }}
          </button>
        </div>
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
          :key="tag.id || tag.name"
          type="button"
          class="news-filter-chip"
          :class="{ selected: tagSelected(tag) }"
          @click="toggleTag(tag)"
          :title="tag.name"
        >
          #{{ tagLabel(tag) }} <small>{{ tag.count }}</small>
        </button>
      </div>
      <section v-if="authenticated && tagManagerOpen" class="news-tag-manager">
        <div class="news-tag-manager-heading">
          <div>
            <span class="eyebrow">TAG DIRECTORY / ADMIN</span>
            <h2>{{ t("管理新闻标签") }}</h2>
          </div>
          <button class="text-button" type="button" :disabled="tagSaving" @click="closeTagManager">
            {{ t("关闭") }}
          </button>
        </div>
        <form class="news-tag-create" @submit.prevent="createTag">
          <label>
            {{ t("标签 ID") }}
            <input v-model="newTag.key" maxlength="80" :placeholder="t('例如：event:2026')" />
          </label>
          <label>
            {{ t("中文名称") }}
            <input v-model="newTag.zh" maxlength="100" />
          </label>
          <label>
            {{ t("日本語名称") }}
            <input v-model="newTag.ja" maxlength="100" />
          </label>
          <label>
            {{ t("English name") }}
            <input v-model="newTag.en" maxlength="100" />
          </label>
          <button class="secondary" type="submit" :disabled="tagSaving || !newTag.key.trim()">
            {{ t("新增标签") }}
          </button>
        </form>
        <div class="news-tag-manager-list">
          <div v-for="tag in tagDrafts" :key="tag.id" class="news-tag-manager-row">
            <label class="news-tag-key">
              <span>{{ t("标签 ID") }}</span>
              <input v-model="tag.name" maxlength="80" />
            </label>
            <label>
              <span>{{ t("中文名称") }}</span>
              <input v-model="tag.labels['zh-CN']" maxlength="100" />
            </label>
            <label>
              <span>{{ t("日本語名称") }}</span>
              <input v-model="tag.labels.ja" maxlength="100" />
            </label>
            <label>
              <span>{{ t("English name") }}</span>
              <input v-model="tag.labels.en" maxlength="100" />
            </label>
            <button
              class="text-button danger news-tag-delete"
              type="button"
              :disabled="tagSaving"
              :aria-label="t('删除')"
              :title="t('删除')"
              @click="deleteTag(tag)"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M4 7h16m-10 4v6m4-6v6M9 7V4h6v3m-9 0 1 13h10l1-13" />
              </svg>
              <span class="sr-only">{{ t("删除") }}</span>
            </button>
          </div>
        </div>
        <div class="news-tag-manager-footer">
          <p v-if="tagMessage" class="success">{{ tagMessage }}</p>
          <p v-if="tagError" class="state error">{{ tagError }}</p>
          <button class="secondary" type="button" :disabled="tagSaving" @click="saveTagCatalog">
            {{ tagSaving ? t("保存中……") : t("保存标签目录") }}
          </button>
        </div>
      </section>
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
