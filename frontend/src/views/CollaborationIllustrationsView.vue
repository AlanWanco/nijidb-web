<script setup>
import { computed, onMounted, ref } from "vue";
import { api } from "../api";
import { localeTag, t } from "../i18n";
import catalog from "../content/collaborationIllustrations.json";

const items = catalog.years.flatMap(year => year.items.map(item => ({ ...item, year: year.year })));
const yearOptions = [...new Set(items.map(item => item.year))].sort((a, b) => b - a);
const illustrationIndex = ref({ available: false, items: {}, total_images: 0 });
const keyword = ref("");
const yearFilter = ref("all");
const statusFilter = ref("all");
const loading = ref(true);
const loadError = ref("");

const hasFilters = computed(() => Boolean(keyword.value.trim()) || yearFilter.value !== "all" || statusFilter.value !== "all");
const availableAssets = computed(() => illustrationIndex.value.available);
const indexedItems = computed(() => illustrationIndex.value.items || {});
const collectedImageCount = computed(() => illustrationIndex.value.total_images || 0);
const completeCount = computed(() => Object.values(indexedItems.value).filter(item => item.status === "complete").length);
const partialCount = computed(() => Object.values(indexedItems.value).filter(item => item.status === "partial").length);
const unavailableCount = computed(() => Object.values(indexedItems.value).filter(item => item.status === "unavailable").length);

function normalized(value) {
  return String(value || "").toLocaleLowerCase();
}

function assetEntry(item) {
  return indexedItems.value[item.id] || null;
}

function imagesFor(item) {
  return assetEntry(item)?.images || [];
}

function statusFor(item) {
  return assetEntry(item)?.status || (imagesFor(item).length ? "complete" : "unavailable");
}

function statusLabel(item) {
  return statusFor(item) === "complete" ? t("完整收录") : statusFor(item) === "partial" ? t("部分收录") : t("未收录");
}

function statusCountLabel(item) {
  return t("已收录 {count} 张", { count: imagesFor(item).length });
}

function sourcePagesFor(item) {
  return [...new Set(imagesFor(item).map(image => image.source_page).filter(Boolean))];
}

function dateLabel(value) {
  if (!value) return "";
  const parsed = new Date(`${value}T12:00:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(localeTag(), { year: "numeric", month: "short", day: "numeric" }).format(parsed);
}

function searchableText(item) {
  return normalized([
    item.title,
    ...(item.collaboration || []),
    item.note,
    item.credit,
  ].join(" "));
}

function matches(item) {
  const query = normalized(keyword.value.trim());
  const status = statusFor(item);
  if (query && !searchableText(item).includes(query)) return false;
  if (yearFilter.value !== "all" && item.year !== Number(yearFilter.value)) return false;
  if (statusFilter.value !== "all" && status !== statusFilter.value) return false;
  return true;
}

const filteredItems = computed(() => items.filter(matches));
const groupedItems = computed(() => {
  const groups = new Map();
  for (const item of filteredItems.value) {
    if (!groups.has(item.year)) groups.set(item.year, []);
    groups.get(item.year).push(item);
  }
  return [...groups.entries()].map(([year, yearItems]) => ({ year, items: yearItems }));
});

function clearFilters() {
  keyword.value = "";
  yearFilter.value = "all";
  statusFilter.value = "all";
}

async function loadIllustrationIndex() {
  try {
    illustrationIndex.value = await api("/api/collaboration-illustrations");
  } catch (error) {
    loadError.value = error.message || t("请求失败");
  } finally {
    loading.value = false;
  }
}

onMounted(loadIllustrationIndex);
</script>

<template>
  <main class="page collaboration-page">
    <section class="collaboration-hero">
      <div class="collaboration-hero-copy">
        <p class="eyebrow">COLLABORATION ILLUSTRATIONS</p>
        <h1>{{ t("联动立绘档案") }}<span class="title-mark" aria-hidden="true"></span></h1>
        <p>{{ t("按年份浏览官方联动记录，查看本地整理的立绘与来源页面。") }}</p>
      </div>
      <div class="collaboration-hero-index" aria-hidden="true">
        <span>2018—2026</span>
        <strong>{{ String(catalog.total_count).padStart(3, "0") }}</strong>
        <small>RECORDS</small>
      </div>
    </section>

    <section class="collaboration-summary" :aria-label="t('联动立绘索引')">
      <div>
        <strong>{{ catalog.total_count }}</strong>
        <span>{{ t("条记录") }}</span>
      </div>
      <div>
        <strong>{{ yearOptions.length }}</strong>
        <span>{{ t("个年份") }}</span>
      </div>
      <div v-if="availableAssets">
        <strong>{{ collectedImageCount }}</strong>
        <span>{{ t("张图片") }}</span>
      </div>
      <div v-if="availableAssets">
        <strong>{{ completeCount }}</strong>
        <span>{{ t("完整收录") }}</span>
      </div>
      <div v-if="availableAssets">
        <strong>{{ partialCount }}</strong>
        <span>{{ t("部分收录") }}</span>
      </div>
      <div v-if="availableAssets">
        <strong>{{ unavailableCount }}</strong>
        <span>{{ t("未收录") }}</span>
      </div>
    </section>

    <p v-if="!loading && !availableAssets" class="collaboration-notice">
      {{ loadError ? t("图片索引加载失败，当前显示元数据和官方来源。") : t("本地图片资源尚未接入，当前仅显示元数据和官方来源。") }}
    </p>

    <section class="collaboration-toolbar" :aria-label="t('筛选')">
      <label class="collaboration-search">
        <span class="sr-only">{{ t("搜索联动、成员或合作方") }}</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.8"></circle><path d="m16 16 5 5"></path></svg>
        <input v-model="keyword" type="search" :placeholder="t('搜索联动、成员或合作方')" />
      </label>
      <label class="collaboration-select">
        <span class="sr-only">{{ t("年份") }}</span>
        <select v-model="yearFilter">
          <option value="all">{{ t("全部年份") }}</option>
          <option v-for="year in yearOptions" :key="year" :value="year">{{ year }}{{ t("年") }}</option>
        </select>
      </label>
      <label class="collaboration-select">
        <span class="sr-only">{{ t("收录状态") }}</span>
        <select v-model="statusFilter">
          <option value="all">{{ t("全部状态") }}</option>
          <option value="complete">{{ t("完整收录") }}</option>
          <option value="partial">{{ t("部分收录") }}</option>
          <option value="unavailable">{{ t("未收录") }}</option>
        </select>
      </label>
      <button v-if="hasFilters" class="collaboration-clear" type="button" @click="clearFilters">{{ t("清除筛选") }}</button>
      <span class="collaboration-result-count">{{ t("显示 {shown} / {total} 项", { shown: filteredItems.length, total: items.length }) }}</span>
    </section>

    <p v-if="loading" class="state">{{ t("加载联动索引……") }}</p>
    <p v-else-if="!filteredItems.length" class="empty collaboration-empty">{{ t("没有匹配的联动记录。") }}</p>
    <div v-else class="collaboration-groups">
      <section v-for="group in groupedItems" :key="group.year" class="collaboration-year-group">
        <header class="collaboration-year-heading">
          <h2>{{ group.year }}</h2>
          <span>{{ group.items.length }} {{ t("项") }}</span>
        </header>
        <div class="collaboration-grid">
          <article
            v-for="item in group.items"
            :key="item.id"
            class="illustration-card"
            :class="`is-${statusFor(item)}`"
          >
            <div v-if="imagesFor(item).length" class="illustration-gallery">
              <a
                v-for="(image, imageIndex) in imagesFor(item)"
                :key="`${image.sha256 || image.path}-${imageIndex}`"
                class="illustration-frame"
                :href="image.path"
                target="_blank"
                rel="noopener noreferrer"
                :style="{ aspectRatio: image.width && image.height ? `${image.width} / ${image.height}` : '1 / 1' }"
              >
                <img :src="image.path" :alt="`${item.title} ${imageIndex + 1}`" loading="lazy" decoding="async" />
              </a>
            </div>
            <div v-else class="illustration-empty">
              <span aria-hidden="true">◎</span>
              <strong>{{ t("未收录") }}</strong>
              <small>{{ availableAssets ? t("暂无图片") : t("本地图片资源尚未接入，当前仅显示元数据和官方来源。") }}</small>
            </div>

            <div class="illustration-card-body">
              <div class="illustration-card-meta">
                <span class="illustration-status">{{ statusLabel(item) }}</span>
                <time :datetime="item.first_seen">{{ dateLabel(item.first_seen) }}</time>
              </div>
              <h3>{{ item.title }}</h3>
              <p v-if="item.collaboration?.length" class="illustration-partners">{{ item.collaboration.join(" · ") }}</p>
              <p class="illustration-image-count">{{ statusCountLabel(item) }}</p>

              <details class="illustration-details">
                <summary>{{ t("查看详情") }}</summary>
                <dl>
                  <template v-if="item.collaboration?.length">
                    <dt>{{ t("合作方") }}</dt>
                    <dd>{{ item.collaboration.join(" · ") }}</dd>
                  </template>
                  <template v-if="item.credit">
                    <dt>{{ t("版权") }}</dt>
                    <dd>{{ item.credit }}</dd>
                  </template>
                  <template v-if="item.note">
                    <dt>{{ t("备注") }}</dt>
                    <dd>{{ item.note }}</dd>
                  </template>
                </dl>
                <div v-if="sourcePagesFor(item).length" class="illustration-sources">
                  <strong>{{ t("图片来源") }}</strong>
                  <a v-for="sourcePage in sourcePagesFor(item)" :key="sourcePage" :href="sourcePage" target="_blank" rel="noopener noreferrer">
                    {{ sourcePage }} ↗
                  </a>
                </div>
                <div v-if="item.official_links?.length" class="illustration-sources">
                  <strong>{{ t("官方来源") }}</strong>
                  <a v-for="link in item.official_links" :key="link.url" :href="link.url" target="_blank" rel="noopener noreferrer">
                    {{ link.title }} ↗
                  </a>
                </div>
              </details>
            </div>
          </article>
        </div>
      </section>
    </div>
  </main>
</template>
