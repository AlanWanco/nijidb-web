<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import CollaboGallery from "../components/CollaboGallery.vue";
import { getCollaboration } from "../collabo/api";
import { characterLabel } from "../collabo/characters.js";
import { dateLabel, safeUrl } from "../collabo/model";
import { c } from "../collabo/text";
import { localeTag } from "../i18n";
import { useDetailNavigation } from "../composables/useDetailNavigation";
import "../collabo/style.css";

const route = useRoute();
const router = useRouter();
const data = ref(null);
const loading = ref(true);
const error = ref("");
const lightbox = ref(false);
let requestId = 0;
const item = computed(() => data.value?.item);
const images = computed(() => item.value?.images || []);
const dateTitle = computed(() => ({ announced: "公布日期", starts: "开启日期" })[item.value?.date_kind] || "首次公开");
const context = computed(() =>
  Object.fromEntries(
    ["q", "year", "tags", "group", "page"].filter((key) => typeof route.query[key] === "string").map((key) => [key, route.query[key]]),
  ),
);
const relatedLinks = computed(() => {
  const links = new Map();
  for (const link of item.value?.links || []) if (link.url) links.set(link.url, link);
  for (const image of images.value) {
    if (image.source_page && !links.has(image.source_page))
      links.set(image.source_page, { url: image.source_page, title: image.source_title });
  }
  return [...links.values()];
});
function periodDateLabel(period) {
  return `${dateLabel(period.start_date, localeTag())} → ${dateLabel(period.end_date, localeTag())}`;
}

function target(entry) {
  return { path: `/collabo/${entry.slug}`, query: context.value };
}
function navigate(entry) {
  if (entry?.slug && !loading.value && !lightbox.value) router.push(target(entry));
}
const touch = useDetailNavigation({
  previous: () => navigate(data.value?.previous),
  following: () => navigate(data.value?.following),
  enabled: () => !loading.value && !lightbox.value,
});
async function load() {
  const id = ++requestId;
  loading.value = true;
  error.value = "";
  data.value = null;
  try {
    const result = await getCollaboration(route.params.slug, context.value);
    if (id !== requestId) return;
    data.value = result;
    document.title = `${result.item.title} · Nijigasaki DB`;
    if (result.item.slug !== route.params.slug) router.replace(target(result.item));
  } catch (err) {
    if (id === requestId) error.value = err.status === 404 ? c("未找到联动") : err.message;
  } finally {
    if (id === requestId) loading.value = false;
  }
}
watch(() => route.params.slug, load, { immediate: true });
onBeforeUnmount(() => {
  requestId += 1;
});
</script>

<template>
  <main
    class="page cb-page cb-detail-page"
    @touchstart.passive="touch.onTouchStart"
    @touchmove.passive="touch.onTouchMove"
    @touchend.passive="touch.onTouchEnd"
    @touchcancel.passive="touch.onTouchCancel"
  >
    <div class="cb-detail-top">
      <RouterLink class="back" :to="{ path: '/collabo', query: context }">← {{ c("返回联动一览") }}</RouterLink
      ><span class="cb-muted cb-key-hint">{{ c("左右按键 / 滑动切换联动") }}</span>
    </div>
    <p v-if="loading" class="cb-state" aria-busy="true">{{ c("读取中……") }}</p>
    <div v-else-if="error" class="cb-state" role="alert">
      <p>{{ error }}</p>
      <button type="button" @click="load">{{ c("重试") }}</button>
    </div>
    <template v-else-if="item">
      <nav class="cb-detail-nav" :aria-label="c('联动一览')">
        <RouterLink v-if="data.previous" :to="target(data.previous)"
          ><span>← {{ c("上一联动") }}</span
          ><strong>{{ data.previous.title }}</strong></RouterLink
        ><span v-else class="cb-muted">{{ c("没有更早或更晚的记录") }}</span>
        <RouterLink v-if="data.following" :to="target(data.following)"
          ><span>{{ c("下一联动") }} →</span><strong>{{ data.following.title }}</strong></RouterLink
        ><span v-else class="cb-muted">{{ c("没有更早或更晚的记录") }}</span>
      </nav>
      <p v-if="data.mode === 'preview'" class="cb-notice">{{ c("本地采集预览 · 图片已直接展示") }}</p>
      <article :key="item.id" class="cb-detail-layout">
        <CollaboGallery
          :images="images"
          :title="item.title"
          :cover-id="item.cover_image_id"
          @lightbox="lightbox = $event"
        />
        <section class="cb-detail-info">
          <p class="eyebrow">COLLABORATION / {{ item.date.slice(0, 4) }}</p>
          <h1>{{ item.title }}</h1>
          <dl class="cb-facts">
            <dt>{{ c(dateTitle) }}</dt>
            <dd>
              <time :datetime="item.date">{{ dateLabel(item.date, localeTag()) }}</time>
            </dd>
            <dt v-if="item.periods.length">{{ c("联动时间段") }}</dt>
            <dd v-if="item.periods.length">
              <ol class="cb-period-list">
                <li v-for="(period, index) in item.periods" :key="`${period.start_date}-${period.end_date}-${index}`">
                  <div class="cb-period-heading">
                    <strong v-if="period.title">{{ period.title }}</strong>
                    <time :datetime="`${period.start_date}/${period.end_date}`">{{ periodDateLabel(period) }}</time>
                  </div>
                  <p v-if="period.description">{{ period.description }}</p>
                </li>
              </ol>
            </dd>
            <dt>{{ c("角色标签") }}</dt>
            <dd>
              <div v-if="item.tags.length" class="cb-character-tags" :aria-label="c('角色标签')">
                <span v-for="tag in item.tags" :key="tag">{{ characterLabel(tag, localeTag()) }}</span>
              </div>
              <span v-else>—</span>
            </dd>
            <dt>{{ c("合作方") }}</dt>
            <dd>{{ item.partners.join(" · ") || "—" }}</dd>
            <dt>{{ c("版权标注") }}</dt>
            <dd>{{ item.credit || "—" }}</dd>
            <dt>{{ c("图片画廊") }}</dt>
            <dd>{{ c("{count} 张图片", { count: images.length }) }}</dd>
          </dl>
          <div class="cb-notes">
            <h2>{{ c("资料与备注") }}</h2>
            <p>{{ item.note || c("暂无补充说明") }}</p>
          </div>
          <div v-if="relatedLinks.length" class="cb-related">
            <h2>{{ c("相关页面") }}</h2>
            <a v-for="link in relatedLinks" :key="link.url" :href="link.url" target="_blank" rel="noopener noreferrer"
              ><span>{{ link.title || c("来源页面（标题待补充）") }}</span
              ><b aria-hidden="true">↗</b></a
            >
          </div>
          <RouterLink class="cb-edit-link" :to="`/admin/collabo/${item.id}`">{{ c("编辑联动") }} ↗</RouterLink>
        </section>
      </article>
      <div v-if="safeUrl(data.source?.url)" class="cb-source-credit">
        <strong>{{ c("原 Wiki 记录") }}</strong
        ><a :href="safeUrl(data.source.url)" target="_blank" rel="noopener noreferrer">{{ data.source.title }} ↗</a>
      </div>
    </template>
  </main>
</template>
