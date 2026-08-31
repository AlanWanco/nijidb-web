<script setup>
import { onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";

const route = useRoute();
const router = useRouter();
const query = ref(typeof route.query.q === "string" ? route.query.q : "");
const releases = ref([]);
const lastSync = ref(null);
const loading = ref(true);
const error = ref("");

function formatCheckedAt(value) {
  return value ? value.slice(0, 16).replace("T", " ") : "";
}

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
      <p class="eyebrow">MUSIC ARCHIVE / CD</p>
      <h1>虹咲音乐档案</h1>
      <p>官方发行资料的本地化、可搜索档案。</p>
      <form class="search" @submit.prevent="search">
        <input v-model="query" name="q" placeholder="搜索标题或艺术家" aria-label="搜索标题或艺术家">
        <button>搜索</button>
      </form>
    </section>

    <div class="toolbar">
      <strong>{{ releases.length }} 张发行</strong>
      <span v-if="lastSync">上次检查 {{ formatCheckedAt(lastSync.checked_at) }}</span>
      <span v-else>等待首次同步</span>
    </div>

    <p v-if="loading" class="state">正在读取目录……</p>
    <p v-else-if="error" class="state error">{{ error }}</p>
    <section v-else class="cover-grid">
      <RouterLink v-for="item in releases" :key="item.id" class="cover-tile" :to="`/release/${item.id}`" :title="item.title">
        <img v-if="item.cover_url" :src="item.cover_url" loading="lazy" :alt="item.title">
        <div v-else class="missing">NO<br>COVER</div>
        <span>{{ item.title }}</span>
      </RouterLink>
      <div v-if="!releases.length" class="empty">还没有匹配的数据。</div>
    </section>
  </main>
</template>
