<script setup>
import { onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { formatLocalDateTime } from "../datetime";

const route = useRoute();
const router = useRouter();
const query = ref(typeof route.query.q === "string" ? route.query.q : "");
const releases = ref([]);
const lastSync = ref(null);
const loading = ref(true);
const error = ref("");

async function loadReleases() {
  loading.value = true;
  error.value = "";
  try {
    const data = await api(`/api/releases${query.value ? `?q=${encodeURIComponent(query.value)}` : ""}`);
    releases.value = data.releases;
    lastSync.value = data.last;
  } catch (requestError) {
    error.value = requestError.message || "目录加载失败";
  } finally {
    loading.value = false;
  }
}

function search() {
  const nextQuery = query.value.trim();
  router.push(nextQuery ? { path: "/", query: { q: nextQuery } } : { path: "/" });
}

watch(() => route.query.q, value => {
  query.value = typeof value === "string" ? value : "";
  loadReleases();
});

onMounted(loadReleases);
</script>

<template>
  <main class="page">
    <section class="hero">
      <div class="hero-topline">
        <p class="eyebrow"><span class="eyebrow-dot"></span>MUSIC ARCHIVE / CD</p>
        <span class="hero-stamp">OFFICIAL DATA<br><strong>LOCAL INDEX</strong></span>
      </div>
      <h1>虹咲音乐档案<span class="title-mark" aria-hidden="true"></span></h1>
      <p class="hero-description">官方发行资料的本地化、可搜索档案。<br><span>从封面、曲目到特典，整理每一份虹咲音乐记录。</span></p>
      <form class="search" @submit.prevent="search">
        <svg class="search-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.5"></circle><path d="m16 16 4.5 4.5"></path></svg>
        <input v-model="query" name="q" placeholder="搜索标题或艺术家" aria-label="搜索标题或艺术家">
        <button><span>搜索档案</span><span aria-hidden="true">↗</span></button>
      </form>
    </section>

    <div class="toolbar">
      <div class="toolbar-main">
        <span class="status-dot" aria-hidden="true"></span>
        <div><strong>{{ releases.length }} 张发行</strong><span class="toolbar-label">COLLECTED INDEX</span></div>
      </div>
      <div class="toolbar-meta">
        <span v-if="query" class="filter-chip">搜索：{{ query }}</span>
        <span class="sync-time"><span class="sync-mark" aria-hidden="true"></span><template v-if="lastSync">上次检查 <time :datetime="lastSync.checked_at" :title="`按当前设备时区显示`">{{ formatLocalDateTime(lastSync.checked_at) }}</time></template><template v-else>等待首次同步</template></span>
      </div>
    </div>

    <p v-if="loading" class="state">正在读取目录……</p>
    <p v-else-if="error" class="state error">{{ error }}</p>
    <section v-else class="cover-grid">
      <RouterLink v-for="(item, index) in releases" :key="item.id" class="cover-tile" :to="`/release/${item.id}`" :title="item.title">
        <div class="cover-media">
          <img v-if="item.cover_url" :src="item.cover_url" loading="lazy" :alt="item.title">
          <div v-else class="missing">NO<br>COVER</div>
          <span class="cover-index">{{ String(releases.length - index).padStart(2, "0") }}</span>
          <span class="cover-hover">查看详情 <b aria-hidden="true">↗</b></span>
        </div>
        <div class="cover-caption">
          <span class="cover-title">{{ item.title }}</span>
          <small>{{ item.artist || "Nijigasaki Music" }}</small>
        </div>
      </RouterLink>
      <div v-if="!releases.length" class="empty">还没有匹配的数据。</div>
    </section>
  </main>
</template>
