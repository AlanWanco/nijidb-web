<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, RouterView } from "vue-router";
import PalettePicker from "./components/PalettePicker.vue";
import { effectiveFlavor, normalizeTheme, paletteFor } from "./theme";

const savedTheme = normalizeTheme(localStorage.getItem("theme"));
const theme = ref(savedTheme);
const colorScheme = window.matchMedia("(prefers-color-scheme: dark)");
const systemDark = ref(colorScheme.matches);
const flavor = computed(() => effectiveFlavor(theme.value, systemDark.value));
const palette = ref(localStorage.getItem(`palette-${flavor.value}`) || "mauve");
const themeOptions = [
  { value: "system", label: "自动", description: "跟随系统设置" },
  { value: "latte", label: "日间", description: "浅色模式" },
  { value: "mocha", label: "夜间", description: "深色模式" },
];
document.documentElement.dataset.theme = theme.value;

function accentInk(hex) {
  const channels = [1, 3, 5].map(index => Number.parseInt(hex.slice(index, index + 2), 16) / 255);
  const linear = channels.map(channel => channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2] > 0.55 ? "#11111b" : "#fff";
}

function applyTheme() {
  const current = paletteFor(flavor.value, palette.value);
  palette.value = current[0];
  document.documentElement.dataset.theme = theme.value;
  document.documentElement.dataset.palette = current[0];
  document.documentElement.style.setProperty("--accent", current[2]);
  document.documentElement.style.setProperty("--accent-ink", accentInk(current[2]));
  localStorage.setItem(`palette-${flavor.value}`, current[0]);
}

function setPalette(key) {
  palette.value = key;
  applyTheme();
}

function setTheme(nextTheme) {
  if (!themeOptions.some(option => option.value === nextTheme)) return;
  theme.value = nextTheme;
  localStorage.setItem("theme", theme.value);
  applyTheme();
}

function handleSchemeChange(event) {
  systemDark.value = event.matches;
}

watch(flavor, () => {
  palette.value = localStorage.getItem(`palette-${flavor.value}`) || "mauve";
  applyTheme();
});

onMounted(() => {
  applyTheme();
  colorScheme.addEventListener("change", handleSchemeChange);
});

onBeforeUnmount(() => {
  colorScheme.removeEventListener("change", handleSchemeChange);
});
</script>

<template>
  <div class="app-shell">
    <header class="site-header">
      <div class="brand-area">
        <RouterLink class="brand" to="/">
          <span class="brand-mark" aria-hidden="true"><b>虹</b><i></i></span>
          <span class="brand-copy"><strong>NIJIGASAKI DB</strong><small>NIJIGASAKI DATA ARCHIVE</small></span>
        </RouterLink>
        <nav class="section-nav" aria-label="内容分类">
          <RouterLink class="music-link" to="/music">音乐</RouterLink>
          <RouterLink class="program-link" to="/programs">节目档案</RouterLink>
          <span class="planned" aria-disabled="true" title="即将推出">联动立绘</span>
        </nav>
      </div>
      <nav class="site-nav">
        <RouterLink class="site-settings-link" to="/admin" aria-label="设置" title="设置">
          <svg class="site-settings-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m12 2 1.3 2.2 2.5.7 2.2-1.2 2.3 2.3-1.2 2.2.7 2.5L22 12l-2.2 1.3-.7 2.5 1.2 2.2-2.3 2.3-2.2-1.2-2.5.7L12 22l-1.3-2.2-2.5-.7-2.2 1.2-2.3-2.3 1.2-2.2-.7-2.5L2 12l2.2-1.3.7-2.5-1.2-2.2L6 3.7l2.2 1.2 2.5-.7L12 2Z"></path><circle cx="12" cy="12" r="3.2"></circle></svg>
          <span>设置</span>
        </RouterLink>
        <span class="nav-divider" aria-hidden="true"></span>
        <div class="theme-switch" role="group" aria-label="显示模式">
          <button v-for="option in themeOptions" :key="option.value" type="button" :class="{ selected: theme === option.value }" :aria-pressed="theme === option.value" :aria-label="option.label" :title="option.description" @click="setTheme(option.value)">
            <svg v-if="option.value === 'system'" class="theme-switch-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2"></rect><path d="M8 21h8m-4-4v4"></path></svg>
            <svg v-else-if="option.value === 'latte'" class="theme-switch-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.5"></circle><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4m11.4-11.4 1.4-1.4"></path></svg>
            <svg v-else class="theme-switch-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 14.8A8.5 8.5 0 1 1 9.2 3.5a6.8 6.8 0 0 0 11.3 11.3Z"></path></svg>
            <span>{{ option.label }}</span>
          </button>
        </div>
        <PalettePicker :flavor="flavor" :selected="palette" @select="setPalette" />
      </nav>
    </header>
    <RouterView />
    <footer>
      <span class="footer-source">数据源：<a href="https://www.lovelive-anime.jp/nijigasaki/cd.php" target="_blank" rel="noopener noreferrer">lovelive-anime.jp</a></span>
      <span class="footer-divider" aria-hidden="true"></span>
        <span class="footer-title">友链</span>
        <span class="footer-links">
          <a href="https://events.nijigaku.fans/" target="_blank" rel="noopener noreferrer">Nijigaku Events</a>
          <a href="https://ll-fans.jp/" target="_blank" rel="noopener noreferrer">LL-Fans</a>
          <a href="https://anilive.nekoss.cn/" target="_blank" rel="noopener noreferrer">AniLive</a>
          <a href="https://anilive-library.nekoss.cn/" target="_blank" rel="noopener noreferrer">AniLive Library</a>
        </span>
        <span class="footer-divider footer-project-divider" aria-hidden="true"></span>
        <span class="footer-title">项目地址：</span>
        <a class="footer-repo-link" href="https://github.com/AlanWanco/nijidb-web" target="_blank" rel="noopener noreferrer">
          <svg class="footer-github-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 .5a12 12 0 0 0-3.79 23.39c.6.11.82-.26.82-.58v-2.04c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.74.08-.74 1.2.09 1.84 1.23 1.84 1.23 1.07 1.83 2.8 1.3 3.48.99.11-.77.42-1.3.76-1.6-2.67-.3-5.47-1.34-5.47-5.93 0-1.31.47-2.38 1.23-3.22-.12-.3-.53-1.52.12-3.17 0 0 1-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.3-1.55 3.3-1.23 3.3-1.23.65 1.65.24 2.87.12 3.17.77.84 1.23 1.91 1.23 3.22 0 4.6-2.8 5.62-5.48 5.92.43.37.82 1.1.82 2.22v3.29c0 .32.22.7.83.58A12 12 0 0 0 12 .5Z"/></svg>
          <span>github.com/AlanWanco/nijidb-web</span>
        </a>
      </footer>
  </div>
</template>
