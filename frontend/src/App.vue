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
const themeOrder = ["system", "mocha", "latte"];
document.documentElement.dataset.theme = theme.value;
const themeIcon = computed(() => flavor.value === "mocha"
  ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
  : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.7 15.1A8.7 8.7 0 0 1 8.9 3.3 8.7 8.7 0 1 0 20.7 15.1Z"/></svg>');
const themeTitle = computed(() => flavor.value === "mocha" ? "切换到浅色/系统" : "切换到深色");

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

function cycleTheme() {
  theme.value = themeOrder[(themeOrder.indexOf(theme.value) + 1) % themeOrder.length];
  localStorage.setItem("theme", theme.value);
  document.documentElement.dataset.theme = theme.value;
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
      <RouterLink class="brand" to="/">
        <span class="brand-mark" aria-hidden="true"><b>虹</b><i></i></span>
        <span class="brand-copy"><strong>NIJIGASAKI DB</strong><small>OFFICIAL MUSIC INDEX</small></span>
      </RouterLink>
      <nav class="site-nav">
        <RouterLink to="/">目录</RouterLink>
        <RouterLink to="/admin">设置</RouterLink>
        <span class="nav-divider" aria-hidden="true"></span>
        <button type="button" class="icon-button" :title="themeTitle" aria-label="切换主题" @click="cycleTheme" v-html="themeIcon"></button>
        <PalettePicker :flavor="flavor" :selected="palette" @select="setPalette" />
      </nav>
    </header>
    <RouterView />
    <footer>数据源：<a href="https://www.lovelive-anime.jp/nijigasaki/cd.php" target="_blank" rel="noopener noreferrer">lovelive-anime.jp</a></footer>
  </div>
</template>
