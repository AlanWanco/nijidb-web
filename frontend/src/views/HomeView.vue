<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { formatLocalDateTime } from "../datetime";
import { t } from "../i18n";

const route = useRoute();
const router = useRouter();
const query = ref(typeof route.query.q === "string" ? route.query.q : "");
const releases = ref([]);
const lastSync = ref(null);
const loading = ref(true);
const error = ref("");
const heroRef = ref(null);
const heroScrollStyle = ref({
  "--music-hero-copy-opacity": "1",
  "--music-hero-art-opacity": "1",
  "--music-hero-grid-opacity": ".56",
  "--music-hero-copy-shift": "0px",
  "--music-hero-art-shift": "0px",
});
const heroPointerStyle = ref({
  "--music-hero-pointer-x": "0px",
  "--music-hero-pointer-y": "0px",
  "--music-hero-focus-x": "0px",
  "--music-hero-focus-y": "0px",
  "--music-hero-focus-scale": "1",
});
let heroScrollFrame = 0;

function handleHeroPointerMove(event) {
  if (event.pointerType && event.pointerType !== "mouse") return;
  const hero = heroRef.value;
  if (!hero) return;
  const bounds = hero.getBoundingClientRect();
  const x = (event.clientX - bounds.left) / bounds.width - 0.5;
  const y = (event.clientY - bounds.top) / bounds.height - 0.5;
  const intensity = Math.min(1, Math.hypot(x, y) * 1.4);
  heroPointerStyle.value = {
    "--music-hero-pointer-x": `${x * 24}px`,
    "--music-hero-pointer-y": `${y * 18}px`,
    "--music-hero-focus-x": `${x * 46}px`,
    "--music-hero-focus-y": `${y * 34}px`,
    "--music-hero-focus-scale": String(1 + intensity * 0.1),
  };
}

function resetHeroPointer() {
  heroPointerStyle.value = {
    "--music-hero-pointer-x": "0px",
    "--music-hero-pointer-y": "0px",
    "--music-hero-focus-x": "0px",
    "--music-hero-focus-y": "0px",
    "--music-hero-focus-scale": "1",
  };
}

function updateHeroScrollStyle() {
  heroScrollFrame = 0;
  const hero = heroRef.value;
  if (!hero) return;
  const fadeDistance = Math.max(hero.offsetHeight * 0.72, window.innerHeight * 0.65);
  const progress = Math.min(1, Math.max(0, window.scrollY / fadeDistance));
  heroScrollStyle.value = {
    "--music-hero-copy-opacity": String(1 - progress),
    "--music-hero-art-opacity": String(1 - progress * 0.82),
    "--music-hero-grid-opacity": String(0.56 - progress * 0.45),
    "--music-hero-copy-shift": `${progress * -28}px`,
    "--music-hero-art-shift": `${progress * 18}px`,
  };
}

function scheduleHeroScrollStyle() {
  if (heroScrollFrame) return;
  heroScrollFrame = requestAnimationFrame(updateHeroScrollStyle);
}

async function loadReleases() {
  loading.value = true;
  error.value = "";
  try {
    const data = await api(`/api/releases${query.value ? `?q=${encodeURIComponent(query.value)}` : ""}`);
    releases.value = data.releases;
    lastSync.value = data.last;
  } catch (requestError) {
     error.value = requestError.message || t("目录加载失败");
  } finally {
    loading.value = false;
  }
}

function search() {
  const nextQuery = query.value.trim();
  router.push(nextQuery ? { path: "/music", query: { q: nextQuery } } : { path: "/music" });
}

watch(() => route.query.q, value => {
  query.value = typeof value === "string" ? value : "";
  loadReleases();
});

onMounted(() => {
  loadReleases();
  updateHeroScrollStyle();
  window.addEventListener("scroll", scheduleHeroScrollStyle, { passive: true });
  window.addEventListener("resize", scheduleHeroScrollStyle);
});

onBeforeUnmount(() => {
  window.removeEventListener("scroll", scheduleHeroScrollStyle);
  window.removeEventListener("resize", scheduleHeroScrollStyle);
  if (heroScrollFrame) cancelAnimationFrame(heroScrollFrame);
});
</script>

<template>
  <main class="page music-page">
    <section ref="heroRef" class="hero music-hero" :style="[heroScrollStyle, heroPointerStyle]" @pointermove="handleHeroPointerMove" @pointerleave="resetHeroPointer">
      <div class="music-hero-grid" aria-hidden="true"></div>
      <div class="hero-topline music-hero-fade">
        <p class="eyebrow"><span class="eyebrow-dot"></span>MUSIC ARCHIVE / CD</p>
        <span class="hero-stamp">OFFICIAL DATA<br><strong>LOCAL INDEX</strong></span>
      </div>
      <div class="music-hero-content music-hero-fade">
        <h1>{{ t("虹咲音乐档案") }}<span class="title-mark" aria-hidden="true"></span></h1>
        <p class="hero-description">{{ t("官方发行资料的本地化、可搜索档案。") }}<br><span>{{ t("从封面、曲目到特典，整理每一份虹咲音乐记录。") }}</span></p>
        <form class="search" @submit.prevent="search">
          <svg class="search-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.5"></circle><path d="m16 16 4.5 4.5"></path></svg>
          <input v-model="query" name="q" :placeholder="t('搜索标题或艺术家')" :aria-label="t('搜索标题或艺术家')">
          <button><span>{{ t("搜索档案") }}</span><span aria-hidden="true">↗</span></button>
        </form>
      </div>
      <div class="music-hero-art" aria-hidden="true">
        <span class="music-hero-art-code">NO. 01<br><b>{{ String(releases.length).padStart(2, "0") }} RELEASES</b></span>
        <span class="music-hero-ring music-hero-ring-outer"></span>
        <span class="music-hero-ring music-hero-ring-middle"></span>
        <span class="music-hero-ring music-hero-ring-inner"></span>
        <span class="music-hero-axis music-hero-axis-horizontal"></span>
        <span class="music-hero-axis music-hero-axis-vertical"></span>
        <span class="music-hero-block music-hero-block-main"></span>
        <span class="music-hero-block music-hero-block-small"></span>
        <span class="music-hero-dot music-hero-dot-main"></span>
        <span class="music-hero-dot music-hero-dot-small"></span>
        <span class="music-hero-focus"></span>
        <span class="music-hero-scan"></span>
      </div>
      <div class="music-hero-scroll music-hero-fade" aria-hidden="true"><span>SCROLL / INDEX</span><i></i></div>
    </section>

    <div class="toolbar">
      <div class="toolbar-main">
        <span class="status-dot" aria-hidden="true"></span>
         <div><strong>{{ releases.length }}{{ t("张发行") }}</strong><span class="toolbar-label">COLLECTED INDEX</span></div>
      </div>
      <div class="toolbar-meta">
         <span v-if="query" class="filter-chip">{{ t("搜索：{value}", { value: query }) }}</span>
         <span class="sync-time"><span class="sync-mark" aria-hidden="true"></span><template v-if="lastSync">{{ t("上次检查") }} <time :datetime="lastSync.checked_at" :title="t('按当前设备时区显示')">{{ formatLocalDateTime(lastSync.checked_at) }}</time></template><template v-else>{{ t("等待首次同步") }}</template></span>
      </div>
    </div>

     <p v-if="loading" class="state">{{ t("正在读取目录……") }}</p>
    <p v-else-if="error" class="state error">{{ error }}</p>
    <section v-else class="cover-grid">
      <RouterLink v-for="(item, index) in releases" :key="item.id" class="cover-tile" :to="`/release/${item.id}`" :title="item.title">
        <div class="cover-media">
          <img v-if="item.cover_url" :src="item.cover_url" loading="lazy" :alt="item.title">
          <div v-else class="missing">NO<br>COVER</div>
          <span class="cover-index">{{ String(releases.length - index).padStart(2, "0") }}</span>
           <span class="cover-hover">{{ t("查看详情") }} <b aria-hidden="true">↗</b></span>
        </div>
        <div class="cover-caption">
          <span class="cover-title">{{ item.title }}</span>
          <small>{{ item.artist || "Nijigasaki Music" }}</small>
        </div>
      </RouterLink>
       <div v-if="!releases.length" class="empty">{{ t("还没有匹配的数据。") }}</div>
    </section>
  </main>
</template>
