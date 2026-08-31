<script setup>
import DOMPurify from "dompurify";
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import { api } from "../api";

const route = useRoute();
const router = useRouter();
const data = ref(null);
const loading = ref(true);
const error = ref("");
const hiddenSpecs = new Set(["アーティスト", "発売日", "価格", "収録内容", "仕様", "収録曲"]);

const release = computed(() => data.value?.release || null);
const tracks = computed(() => release.value?.tracks || []);
const specs = computed(() => Object.entries(release.value?.specs || {}).filter(([key]) => !hiddenSpecs.has(key)));
const extras = computed(() => release.value?.extras || []);
const safeDetailHtml = computed(() => DOMPurify.sanitize(release.value?.detail_html || ""));

async function loadRelease() {
  loading.value = true;
  error.value = "";
  data.value = null;
  try {
    data.value = await api(`/api/releases/${encodeURIComponent(route.params.releaseId)}`);
    document.title = `${data.value.release.title} · Nijigasaki DB`;
  } catch (requestError) {
    error.value = requestError.message || "专辑加载失败";
  } finally {
    loading.value = false;
  }
}

function navigate(id) {
  if (id) router.push(`/release/${id}`);
}

function handleKeydown(event) {
  if (event.target.closest("input,textarea,select,button,[contenteditable=\"true\"]")) return;
  if (event.key === "ArrowLeft") navigate(data.value?.previous?.id);
  if (event.key === "ArrowRight") navigate(data.value?.following?.id);
}

watch(() => route.params.releaseId, loadRelease, { immediate: true });
window.addEventListener("keydown", handleKeydown);
onBeforeUnmount(() => window.removeEventListener("keydown", handleKeydown));
</script>

<template>
  <main class="page">
    <div class="detail-topline">
      <RouterLink class="back" to="/"><span aria-hidden="true">←</span> 返回专辑目录</RouterLink>
      <span class="keyboard-hint">← / → 浏览发行</span>
    </div>
    <p v-if="loading" class="state">正在读取专辑……</p>
    <p v-else-if="error" class="state error">{{ error }}</p>
    <template v-else-if="release">
      <nav class="release-switch" aria-label="切换专辑">
        <RouterLink v-if="data.previous" :to="`/release/${data.previous.id}`">
          <span class="switch-arrow">←</span><small>上一张</small><strong>{{ data.previous.title }}</strong>
        </RouterLink>
        <span v-else class="switch-button disabled"><span class="switch-arrow">←</span><small>已经是第一张</small></span>
        <RouterLink v-if="data.following" :to="`/release/${data.following.id}`">
          <span class="switch-arrow">→</span><small>下一张</small><strong>{{ data.following.title }}</strong>
        </RouterLink>
        <span v-else class="switch-button disabled"><span class="switch-arrow">→</span><small>已经是最后一张</small></span>
      </nav>

      <article class="release">
        <div class="release-cover">
          <div class="release-cover-art">
            <img v-if="release.cover_url" :src="release.cover_url" :alt="release.title">
            <div v-else class="missing">NO<br>COVER</div>
          </div>
          <div class="release-cover-meta"><span>NIJIGASAKI / RELEASE</span><strong>{{ release.id }}</strong></div>
        </div>
        <div class="release-info">
          <p class="eyebrow">ALBUM / {{ release.id }}</p>
          <h1>{{ release.title }}</h1>
          <p v-if="release.subtitle" class="subtitle">{{ release.subtitle }}</p>
          <dl>
            <dt>【アーティスト】</dt><dd>{{ release.artist || "官方暂未公开" }}</dd>
            <dt>【発売日】</dt><dd>{{ release.release_date || "官方暂未公开" }}</dd>
            <dt>【価格】</dt><dd>{{ release.price || "官方暂未公开" }}</dd>
            <template v-for="[key, value] in specs" :key="key">
              <dt>【{{ key }}】</dt><dd>{{ value }}</dd>
            </template>
          </dl>
          <a class="source" :href="release.source_url" target="_blank" rel="noopener noreferrer">查看官方原页 ↗</a>
        </div>
      </article>

      <section class="tracks">
        <div class="section-heading"><div><p class="eyebrow">TRACKLIST</p><h2>收录内容</h2></div><span class="section-count">{{ tracks.length }} TRACKS</span></div>
        <div v-for="track in tracks" :key="`${track.disc}-${track.number}-${track.title}`" class="track-row">
          <span class="track-no">{{ track.disc }}<br>{{ String(track.number).padStart(2, "0") }}</span>
          <div>
            <strong>{{ track.title }}</strong>
            <p v-for="(value, key) in track.credits" :key="key"><span>{{ key }}</span>{{ value }}</p>
          </div>
        </div>
        <p v-if="!tracks.length" class="muted">官方目前只公开了封面和标题，曲目详情尚未发布。</p>
      </section>

      <section v-if="extras.length" class="extras">
        <div class="section-heading"><div><p class="eyebrow">BONUS / LINKS</p><h2>特典与相关链接</h2></div><span class="section-count">{{ extras.length }} ITEMS</span></div>
        <div class="extra-grid">
          <article v-for="(extra, index) in extras" :key="`${extra.title}-${index}`" class="extra-panel">
            <h3>{{ extra.title }}</h3>
            <a v-if="extra.type === 'link'" class="extra-link" :href="extra.url" target="_blank" rel="noopener noreferrer">打开链接 ↗</a>
            <ul v-else><li v-for="entry in extra.entries" :key="entry">{{ entry }}</li></ul>
          </article>
        </div>
      </section>

      <details v-if="release.detail_html" class="raw">
        <summary>查看原始规格数据（默认折叠，用于核对）</summary>
        <div class="raw-content" v-html="safeDetailHtml"></div>
      </details>
    </template>
  </main>
</template>
