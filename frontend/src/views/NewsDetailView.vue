<script setup>
import DOMPurify from "dompurify";
import { computed, onMounted, reactive, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { t } from "../i18n";

const route = useRoute();
const router = useRouter();
const article = ref(null);
const previous = ref(null);
const following = ref(null);
const loading = ref(true);
const error = ref("");
const authenticated = ref(false);
const editMode = ref(false);
const loginPrompt = ref(false);
const saving = ref(false);
const uploading = ref(false);
const removingImage = ref(0);
const selectedImages = ref([]);
const saveMessage = ref("");
const saveError = ref("");
const editForm = reactive({
  title: "",
  published_at: "",
  category: "",
  tags: "",
  summary: "",
  body_markdown: "",
  source_url: "",
});

const filterQuery = computed(() => {
  const result = {};
  if (typeof route.query.q === "string" && route.query.q) result.q = route.query.q;
  if (typeof route.query.tags === "string" && route.query.tags) result.tags = route.query.tags;
  if (typeof route.query.source === "string" && route.query.source) result.source = route.query.source;
  return result;
});
const listQuery = computed(() => {
  const result = { ...filterQuery.value };
  if (typeof route.query.page === "string" && route.query.page && route.query.page !== "1") result.page = route.query.page;
  return result;
});
const renderedBody = computed(() => renderMarkdown(article.value?.body_markdown || article.value?.summary || ""));

function escapeHtml(value) {
  return String(value || "").replace(/[&<>'"]/g, character => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  }[character]));
}

function safeHref(value) {
  const raw = String(value || "").trim();
  if (raw.startsWith("/")) return raw;
  try {
    const parsed = new URL(raw, window.location.origin);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
  } catch {
    return "";
  }
}

function inlineMarkdown(value) {
  let html = escapeHtml(value);
  html = html.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_, alt, href) => {
    const safe = safeHref(href);
    return safe ? `<img src="${escapeHtml(safe)}" alt="${alt}">` : escapeHtml(alt);
  });
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, href) => {
    const safe = safeHref(href);
    return safe ? `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
  });
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  return html;
}

function renderMarkdown(value) {
  const lines = String(value || "").replace(/\r/g, "").split("\n");
  const output = [];
  let listOpen = false;
  const closeList = () => {
    if (listOpen) {
      output.push("</ul>");
      listOpen = false;
    }
  };
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      closeList();
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      closeList();
      const level = Math.min(4, heading[1].length);
      output.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    const bullet = line.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      if (!listOpen) {
        output.push("<ul>");
        listOpen = true;
      }
      output.push(`<li>${inlineMarkdown(bullet[1])}</li>`);
      continue;
    }
    closeList();
    output.push(`<p>${inlineMarkdown(line)}</p>`);
  }
  closeList();
  return DOMPurify.sanitize(output.join(""), {
    ADD_ATTR: ["target", "rel"],
    ALLOW_DATA_ATTR: false,
  });
}

function formatDate(value) {
  return value ? value.replace(/-/g, "/") : "—";
}

function fillEditForm() {
  if (!article.value) return;
  Object.assign(editForm, {
    title: article.value.title || "",
    published_at: article.value.published_at || "",
    category: article.value.category || "",
    tags: (article.value.tags || []).join(", "),
    summary: article.value.summary || "",
    body_markdown: article.value.body_markdown || "",
    source_url: article.value.source_url || "",
  });
}

async function loadAuth() {
  try {
    const data = await api("/api/auth/session");
    authenticated.value = Boolean(data.authenticated);
  } catch {
    authenticated.value = false;
  }
}

async function loadDetail() {
  loading.value = true;
  error.value = "";
  saveMessage.value = "";
  saveError.value = "";
  editMode.value = false;
  try {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filterQuery.value)) params.set(key, value);
    const suffix = params.toString() ? `?${params}` : "";
    const data = await api(`/api/news/${encodeURIComponent(route.params.newsId)}${suffix}`);
    article.value = data.article;
    previous.value = data.previous;
    following.value = data.following;
    fillEditForm();
  } catch (requestError) {
    article.value = null;
    error.value = requestError.message || t("新闻详情加载失败");
  } finally {
    loading.value = false;
  }
}

function beginEdit() {
  if (!authenticated.value) {
    loginPrompt.value = true;
    return;
  }
  loginPrompt.value = false;
  saveMessage.value = "";
  saveError.value = "";
  fillEditForm();
  editMode.value = true;
}

function selectImages(event) {
  selectedImages.value = [...(event.target.files || [])];
}

function clearSelectedImages() {
  selectedImages.value = [];
}

async function deleteImage(image) {
  if (!article.value || image.kind !== "manual" || removingImage.value) return;
  if (!window.confirm(`${t("删除")}？`)) return;
  removingImage.value = image.id;
  saveError.value = "";
  try {
    const data = await api(`/api/admin/news/images/${image.id}`, { method: "DELETE" });
    article.value = data.article;
    saveMessage.value = t("图片已删除");
  } catch (requestError) {
    if (requestError.status === 401) {
      authenticated.value = false;
      loginPrompt.value = true;
    } else {
      saveError.value = requestError.message || t("图片删除失败");
    }
  } finally {
    removingImage.value = 0;
  }
}

async function uploadSelectedImages() {
  if (!article.value || !selectedImages.value.length) return;
  uploading.value = true;
  try {
    for (const file of selectedImages.value) {
      const response = await fetch(`/api/admin/news/${encodeURIComponent(article.value.id)}/images`, {
        method: "POST",
        headers: {
          "Content-Type": file.type || "application/octet-stream",
          "X-Filename": encodeURIComponent(file.name),
          "X-Alt-Text": article.value.title || "",
        },
        body: file,
        credentials: "same-origin",
      });
      const contentType = response.headers.get("content-type") || "";
      const payload = contentType.includes("application/json") ? await response.json() : {};
      if (!response.ok) {
        const uploadError = new Error(payload.detail || t("图片上传失败"));
        uploadError.status = response.status;
        throw uploadError;
      }
      article.value = payload.article;
    }
  } finally {
    uploading.value = false;
  }
}

function cancelEdit() {
  editMode.value = false;
  loginPrompt.value = false;
  clearSelectedImages();
  fillEditForm();
}

async function saveEdit() {
  if (!article.value || saving.value) return;
  saving.value = true;
  saveMessage.value = "";
  saveError.value = "";
  try {
    const data = await api(`/api/admin/news/${encodeURIComponent(article.value.id)}`, {
      method: "PATCH",
      body: {
        title: editForm.title,
        published_at: editForm.published_at,
        category: editForm.category,
        tags: editForm.tags.split(/[,，、\n]+/).map(value => value.trim()).filter(Boolean),
        summary: editForm.summary,
        body_markdown: editForm.body_markdown,
      },
    });
    article.value = data.article;
    await uploadSelectedImages();
    clearSelectedImages();
    fillEditForm();
    editMode.value = false;
    saveMessage.value = t("新闻已保存");
  } catch (requestError) {
    if (requestError.status === 401) {
      authenticated.value = false;
      loginPrompt.value = true;
    } else {
      saveError.value = requestError.message || t("新闻保存失败");
    }
  } finally {
    saving.value = false;
  }
}

function listLink() {
  return { path: "/news", query: listQuery.value };
}

function detailLink(item) {
  return item ? { path: `/news/${item.id}`, query: listQuery.value } : null;
}

watch(() => route.fullPath, () => {
  loadDetail();
  loadAuth();
});

onMounted(() => {
  loadDetail();
  loadAuth();
});
</script>

<template>
  <main class="page news-detail-page">
    <p v-if="loading" class="state">{{ t("正在读取新闻……") }}</p>
    <p v-else-if="error" class="state error">{{ error }}</p>
    <template v-else-if="article">
      <div class="news-detail-topline">
        <RouterLink class="back-link" :to="listLink()">← {{ t("返回新闻列表") }}</RouterLink>
        <span class="eyebrow">{{ article.source_label }} / {{ article.page_name }}</span>
      </div>

      <article class="news-detail-card">
        <header class="news-detail-header">
          <div class="news-detail-meta"><time :datetime="article.published_at">{{ formatDate(article.published_at) }}</time><span>{{ article.category || t("未分类") }}</span><span v-if="article.edited" class="edited-badge">{{ t("已编辑") }}</span></div>
          <h1>{{ article.title }}</h1>
          <div class="news-detail-tags"><span v-for="tag in article.tags" :key="tag">#{{ tag }}</span></div>
          <p class="news-detail-summary">{{ article.summary || t("暂无摘要") }}</p>
          <div class="news-detail-actions">
            <button type="button" class="secondary" @click="beginEdit">{{ authenticated ? t("页面内编辑") : t("登录后编辑") }}</button>
            <a v-if="article.source_url" class="secondary news-source-button" :href="article.source_url" target="_blank" rel="noopener noreferrer">{{ t("查看官网原页 ↗") }}</a>
          </div>
          <p v-if="loginPrompt" class="news-login-prompt">{{ t("管理员登录后可以直接在此页面编辑新闻。") }} <RouterLink :to="{ path: '/admin/login', query: { redirect: route.fullPath } }">{{ t("去登录") }} →</RouterLink></p>
          <p v-if="saveMessage" class="success">{{ saveMessage }}</p>
          <p v-if="saveError" class="state error">{{ saveError }}</p>
        </header>

        <form v-if="editMode" class="news-edit-form" @submit.prevent="saveEdit">
          <div class="news-edit-heading"><span class="eyebrow">INLINE EDITOR / ADMIN</span><span>{{ t("保存后仍会保留本地来源记录。") }}</span></div>
          <label>{{ t("标题") }}<input v-model="editForm.title" required maxlength="500"></label>
          <div class="news-edit-grid">
            <label>{{ t("发布日期") }}<input v-model="editForm.published_at" type="date"></label>
            <label>{{ t("分类") }}<input v-model="editForm.category" maxlength="100"></label>
          </div>
          <label>{{ t("tags（逗号分隔）") }}<input v-model="editForm.tags" placeholder="goods, collaboration"></label>
          <label>{{ t("摘要") }}<textarea v-model="editForm.summary" rows="5"></textarea></label>
          <label>{{ t("正文 Markdown") }}<textarea v-model="editForm.body_markdown" rows="16"></textarea></label>
          <label class="news-image-picker">{{ t("补录图片") }}<input type="file" accept="image/jpeg,image/png,image/gif,image/webp,image/bmp,image/avif" multiple @change="selectImages"><small>{{ t("可一次选择多张图片，保存后追加到本条新闻的图库。") }}</small></label>
          <p v-if="selectedImages.length" class="news-selected-images">{{ t("待上传 {count} 张图片", { count: selectedImages.length }) }} <button type="button" class="text-button" @click="clearSelectedImages">{{ t("清空") }}</button></p>
          <div v-if="article.images?.length" class="news-existing-gallery">
            <figure v-for="image in article.images" :key="image.id" class="news-existing-image">
              <img v-if="image.url" :src="image.url" loading="lazy" :alt="image.alt_text || article.title">
              <figcaption><span>{{ image.kind === "manual" ? t("手动补录") : image.kind === "archive" ? t("本地归档") : t("官网图片") }}</span><button v-if="image.kind === 'manual'" type="button" class="text-button" :disabled="removingImage === image.id" @click="deleteImage(image)">{{ removingImage === image.id ? t("删除中……") : t("删除") }}</button></figcaption>
            </figure>
          </div>
          <div class="news-edit-actions"><button :disabled="saving || uploading">{{ uploading ? t("上传图片中……") : saving ? t("保存中……") : t("保存新闻") }}</button><button type="button" class="secondary" @click="cancelEdit">{{ t("取消") }}</button></div>
        </form>

        <div v-else class="news-detail-content" :class="{ 'has-cover': article.images?.length }">
          <div class="news-detail-copy" v-html="renderedBody"></div>
          <div v-if="article.images?.length" class="news-detail-gallery">
            <a v-for="image in article.images" :key="image.id" class="news-detail-image" :href="image.url || undefined" target="_blank" rel="noopener noreferrer">
              <img v-if="image.url" :src="image.url" loading="lazy" :alt="image.alt_text || article.title">
              <span v-else>{{ t("图片暂不可用") }}</span>
            </a>
          </div>
          <p v-else class="news-no-images">{{ t("这条新闻暂时没有本地图片。") }}</p>
        </div>
      </article>

      <nav class="news-detail-nav" aria-label="News navigation">
        <RouterLink v-if="previous" class="news-nav-card previous" :to="detailLink(previous)"><small>← {{ t("上一条") }}</small><strong>{{ previous.title }}</strong><span>{{ formatDate(previous.published_at) }}</span></RouterLink>
        <span v-else class="news-nav-card disabled"><small>← {{ t("上一条") }}</small><strong>{{ t("已经是第一条") }}</strong></span>
        <RouterLink v-if="following" class="news-nav-card following" :to="detailLink(following)"><small>{{ t("下一条") }} →</small><strong>{{ following.title }}</strong><span>{{ formatDate(following.published_at) }}</span></RouterLink>
        <span v-else class="news-nav-card disabled"><small>{{ t("下一条") }} →</small><strong>{{ t("已经是最后一条") }}</strong></span>
      </nav>
    </template>
  </main>
</template>
