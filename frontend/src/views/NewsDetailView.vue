<script setup>
import {
  computed,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from "vue";
import {
  onBeforeRouteLeave,
  onBeforeRouteUpdate,
  RouterLink,
  useRoute,
  useRouter,
} from "vue-router";
import NewsLightbox from "../components/NewsLightbox.vue";
import { useDetailNavigation } from "../composables/useDetailNavigation";
import { newsHref, newsSummaryText, renderNewsMarkdown } from "../newsMarkdown";
import {
  newsSourceGroup,
  newsSourceLabel,
  newsTagLabel,
  newsTagOptions,
} from "../newsLabels";
import { api } from "../api";
import { locale, t } from "../i18n";

const route = useRoute();
const router = useRouter();
const article = ref(null);
const previous = ref(null);
const following = ref(null);
const loading = ref(true);
const error = ref("");
const authenticated = ref(false);
const userRole = ref("");
const administrator = computed(() => userRole.value === "admin");
const editMode = ref(false);
const loginPrompt = ref(false);
const saving = ref(false);
const uploading = ref(false);
const removingImage = ref(0);
const refreshing = ref(false);
const bodyRef = ref(null);
const lightboxImages = ref([]);
const lightboxIndex = ref(-1);
const baseline = ref("");
let requestId = 0;
const selectedImages = ref([]);
const saveMessage = ref("");
const saveError = ref("");
const editForm = reactive({
  title: "",
  published_at: "",
  category: "",
  tags: [],
  summary: "",
  body_markdown: "",
  source_url: "",
});
const editTagOptions = computed(() => {
  const catalog = article.value?.tag_options;
  return Array.isArray(catalog) && catalog.length
    ? catalog
    : newsTagOptions.map((name) => ({ id: name, name }));
});
const selectedTagNames = computed(() =>
  editForm.tags
    .map((tagId) => editTagOptions.value.find((tag) => tag.id === tagId)?.name)
    .filter(Boolean),
);

const filterQuery = computed(() => {
  const result = {};
  if (typeof route.query.q === "string" && route.query.q)
    result.q = route.query.q;
  if (typeof route.query.tags === "string" && route.query.tags)
    result.tags = route.query.tags;
  if (typeof route.query.source === "string" && route.query.source)
    result.source = newsSourceGroup(route.query.source);
  return result;
});
const listQuery = computed(() => {
  const result = { ...filterQuery.value };
  if (
    typeof route.query.page === "string" &&
    route.query.page &&
    route.query.page !== "1"
  )
    result.page = route.query.page;
  return result;
});
const renderedBody = computed(() =>
  renderNewsMarkdown(
    article.value?.body_markdown || article.value?.summary || "",
    article.value || {},
  ),
);
const busy = computed(
  () =>
    saving.value ||
    uploading.value ||
    refreshing.value ||
    Boolean(removingImage.value),
);
const dirty = computed(
  () =>
    editMode.value &&
    (JSON.stringify(editForm) !== baseline.value ||
      selectedImages.value.length > 0),
);
const touch = useDetailNavigation({
  previous: () => previous.value && router.push(detailLink(previous.value)),
  following: () => following.value && router.push(detailLink(following.value)),
  enabled: () =>
    !loading.value &&
    !error.value &&
    !editMode.value &&
    !busy.value &&
    lightboxIndex.value < 0,
});
function openImage(url) {
  const gallery = (article.value?.images || [])
    .filter((image) => image.url && newsHref(image.url))
    .map((image) => ({
      url: newsHref(image.url),
      alt: image.alt_text || article.value.title,
    }));
  const inline = [...(bodyRef.value?.querySelectorAll("img") || [])].map(
    (image) => ({ url: image.src, alt: image.alt }),
  );
  lightboxImages.value = [
    ...new Map(
      [...gallery, ...inline].map((image) => [image.url, image]),
    ).values(),
  ];
  lightboxIndex.value = lightboxImages.value.findIndex(
    (image) => image.url === newsHref(url),
  );
}
function openInlineImage(event) {
  const image = event.target.closest?.("img");
  if (!image) return;
  event.preventDefault();
  openImage(image.src);
}
function canLeave() {
  if (busy.value) return false;
  return !dirty.value || window.confirm(t("还有未保存的修改，确定离开？"));
}
onBeforeRouteLeave(canLeave);
onBeforeRouteUpdate(canLeave);
function beforeUnload(event) {
  if (dirty.value || busy.value) {
    event.preventDefault();
    event.returnValue = "";
  }
}
onMounted(() => window.addEventListener("beforeunload", beforeUnload));
onBeforeUnmount(() => {
  requestId += 1;
  window.removeEventListener("beforeunload", beforeUnload);
});

async function refreshArticle() {
  if (!article.value || busy.value || editMode.value) return;
  refreshing.value = true;
  saveMessage.value = "";
  saveError.value = "";
  try {
    const data = await api(
      `/api/admin/news/${encodeURIComponent(article.value.id)}/refresh`,
      { method: "POST" },
    );
    article.value = data.article;
    fillEditForm();
    saveMessage.value = t(
      data.changed ? "新闻已刷新，手动修改已保留" : "官网内容没有变化",
    );
  } catch (requestError) {
    if (requestError.status === 401) {
      authenticated.value = false;
      userRole.value = "";
    }
    saveError.value = requestError.message || t("请求失败");
  } finally {
    refreshing.value = false;
  }
}

function formatDate(value) {
  return value ? value.replace(/-/g, "/") : "—";
}

function tagDefinition(tagName) {
  return editTagOptions.value.find((tag) => tag.name === tagName) || null;
}

function tagLabel(tagName) {
  return newsTagLabel(tagName, locale.value, tagDefinition(tagName));
}

function updateDocumentTitle() {
  document.title = article.value?.title
    ? `${article.value.title} · Nijigasaki DB`
    : `${t("新闻详情")} · Nijigasaki DB`;
}

function fillEditForm() {
  if (!article.value) return;
  const availableTags = editTagOptions.value;
  const articleTagIds = Array.isArray(article.value.tag_ids) && article.value.tag_ids.length
    ? article.value.tag_ids
    : (article.value.tags || [])
        .map((tagName) => availableTags.find((tag) => tag.name === tagName)?.id)
        .filter(Boolean);
  Object.assign(editForm, {
    title: article.value.title || "",
    published_at: article.value.published_at || "",
    category: article.value.category || "",
    tags: articleTagIds,
    summary: article.value.summary || "",
    body_markdown: article.value.body_markdown || "",
    source_url: article.value.source_url || "",
  });
  baseline.value = JSON.stringify(editForm);
}

async function loadAuth() {
  try {
    const data = await api("/api/auth/session");
    authenticated.value = Boolean(data.authenticated);
    userRole.value = data.role || (data.authenticated ? "admin" : "");
  } catch {
    authenticated.value = false;
    userRole.value = "";
  }
}

async function loadDetail() {
  const id = ++requestId;
  lightboxIndex.value = -1;
  selectedImages.value = [];
  loginPrompt.value = false;
  loading.value = true;
  error.value = "";
  saveMessage.value = "";
  saveError.value = "";
  editMode.value = false;
  try {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filterQuery.value))
      params.set(key, value);
    const suffix = params.toString() ? `?${params}` : "";
    const data = await api(
      `/api/news/${encodeURIComponent(route.params.newsId)}${suffix}`,
    );
    if (id !== requestId) return;
    article.value = data.article;
    previous.value = data.previous;
    following.value = data.following;
    fillEditForm();
  } catch (requestError) {
    if (id !== requestId) return;
    article.value = null;
    error.value = requestError.message || t("新闻详情加载失败");
  } finally {
    if (id === requestId) loading.value = false;
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

function toggleEditTag(tagId) {
  if (!editTagOptions.value.some((tag) => tag.id === tagId)) return;
  editForm.tags = editForm.tags.includes(tagId)
    ? editForm.tags.filter((value) => value !== tagId)
    : [...editForm.tags, tagId];
}

function selectImages(event) {
  selectedImages.value = [...(event.target.files || [])];
}

function clearSelectedImages() {
  selectedImages.value = [];
}

async function deleteImage(image) {
  if (!article.value || removingImage.value) return;
  const prompt =
    image.kind === "manual"
      ? t("删除这张图片？")
      : t("删除来源图片后，自动刷新不会恢复它，确定继续吗？");
  if (!window.confirm(prompt)) return;
  removingImage.value = image.id;
  saveError.value = "";
  try {
    const data = await api(`/api/admin/news/images/${image.id}`, {
      method: "DELETE",
    });
    article.value = data.article;
    saveMessage.value = t("图片已删除");
  } catch (requestError) {
    if (requestError.status === 401) {
      authenticated.value = false;
      userRole.value = "";
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
      const response = await fetch(
        `/api/admin/news/${encodeURIComponent(article.value.id)}/images`,
        {
          method: "POST",
          headers: {
            "Content-Type": file.type || "application/octet-stream",
            "X-Filename": encodeURIComponent(file.name),
            "X-Alt-Text": encodeURIComponent(article.value.title || ""),
          },
          body: file,
          credentials: "same-origin",
        },
      );
      const contentType = response.headers.get("content-type") || "";
      const payload = contentType.includes("application/json")
        ? await response.json()
        : {};
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
    const data = await api(
      `/api/admin/news/${encodeURIComponent(article.value.id)}`,
      {
        method: "PATCH",
        body: {
          title: editForm.title,
          published_at: editForm.published_at,
          category: editForm.category,
          tags: selectedTagNames.value,
          tag_ids: [...editForm.tags],
          summary: editForm.summary,
          body_markdown: editForm.body_markdown,
          source_url: editForm.source_url,
          updated_at: article.value.updated_at,
        },
      },
    );
    article.value = data.article;
    await uploadSelectedImages();
    clearSelectedImages();
    fillEditForm();
    editMode.value = false;
    saveMessage.value = t("新闻已保存");
  } catch (requestError) {
    if (requestError.status === 401) {
      authenticated.value = false;
      userRole.value = "";
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

watch(
  () => route.fullPath,
  () => {
    loadDetail();
    loadAuth();
  },
);
watch(() => [article.value?.title, locale.value], updateDocumentTitle);

onMounted(() => {
  loadDetail();
  loadAuth();
});
</script>

<template>
  <main
    class="page news-detail-page"
    @touchstart.passive="touch.onTouchStart"
    @touchmove.passive="touch.onTouchMove"
    @touchend.passive="touch.onTouchEnd"
    @touchcancel.passive="touch.onTouchCancel"
  >
    <NewsLightbox
      :images="lightboxImages"
      :start="lightboxIndex"
      @close="lightboxIndex = -1"
    />
    <p v-if="loading" class="state">{{ t("正在读取新闻……") }}</p>
    <p v-else-if="error" class="state error">{{ error }}</p>
    <template v-else-if="article">
      <div class="news-detail-topline">
        <RouterLink class="back-link" :to="listLink()"
          >← {{ t("返回新闻列表") }}</RouterLink
        >
        <span class="eyebrow"
          >{{ newsSourceLabel(article.source) }} / {{ article.page_name }}</span
        >
      </div>

      <article class="news-detail-card">
        <header class="news-detail-header">
          <div class="news-detail-meta">
            <time :datetime="article.published_at">{{
              formatDate(article.published_at)
            }}</time
            ><span>{{ article.category || t("未分类") }}</span
            ><span v-if="article.edited" class="edited-badge">{{
              t("已编辑")
            }}</span>
          </div>
          <h1>{{ article.title }}</h1>
          <div class="news-detail-tags">
            <span v-for="tag in article.tags" :key="tag" :title="tag"
              >#{{ tagLabel(tag) }}</span
            >
          </div>
          <p class="news-detail-summary">
            {{ newsSummaryText(article.summary) || t("暂无摘要") }}
          </p>
          <div class="news-detail-actions">
            <button
              v-if="administrator"
              type="button"
              class="secondary"
              :disabled="busy || editMode"
              @click="refreshArticle"
            >
              {{ refreshing ? t("刷新中……") : t("从官网刷新此条") }}
            </button>
            <button
              v-if="administrator || !authenticated"
              type="button"
              class="secondary"
              :disabled="busy"
              @click="beginEdit"
            >
              {{ authenticated ? t("编辑该页") : t("登录后编辑") }}
            </button>
            <a
              v-if="article.source_url && newsHref(article.source_url)"
              class="secondary news-source-button"
              :href="newsHref(article.source_url)"
              target="_blank"
              rel="noopener noreferrer"
              >{{ t("查看官网原页 ↗") }}</a
            >
          </div>
          <p v-if="loginPrompt" class="news-login-prompt">
            {{ t("管理员登录后可以直接在此页面编辑新闻。") }}
            <RouterLink
              :to="{
                path: '/admin/login',
                query: { redirect: route.fullPath },
              }"
              >{{ t("去登录") }} →</RouterLink
            >
          </p>
          <p v-if="saveMessage" class="success">{{ saveMessage }}</p>
          <p v-if="saveError" class="state error">{{ saveError }}</p>
        </header>

        <form v-if="editMode" class="news-edit-form" @submit.prevent="saveEdit">
          <fieldset :disabled="busy" class="news-editor-fieldset">
            <div class="news-edit-heading">
              <span class="eyebrow">INLINE EDITOR / ADMIN</span
              ><span>{{ t("保存后仍会保留本地来源记录。") }}</span>
            </div>
            <label
              >{{ t("标题")
              }}<input v-model="editForm.title" required maxlength="500"
            /></label>
            <div class="news-edit-grid">
              <label
                >{{ t("发布日期")
                }}<input v-model="editForm.published_at" type="date"
              /></label>
              <label
                >{{ t("分类")
                }}<input v-model="editForm.category" maxlength="100"
              /></label>
            </div>
            <div class="news-edit-tag-field">
              <span class="news-edit-label">{{ t("选择标签") }}</span>
              <div
                class="news-edit-tag-picker"
                role="group"
                :aria-label="t('选择标签')"
              >
                <button
                  v-for="tag in editTagOptions"
                  :key="tag.id"
                  type="button"
                  class="news-edit-tag"
                  :class="{ selected: editForm.tags.includes(tag.id) }"
                  :aria-pressed="editForm.tags.includes(tag.id)"
                  @click="toggleEditTag(tag.id)"
                >
                  #{{ newsTagLabel(tag.name, locale, tag) }}
                </button>
              </div>
              <small>{{ t("只能选择预设标签。") }}</small>
            </div>
            <label
              >{{ t("摘要")
              }}<textarea
                v-model="editForm.summary"
                rows="5"
                maxlength="200"
              ></textarea>
            </label>
            <label
              >{{ t("来源网址")
              }}<input v-model="editForm.source_url" type="url"
            /></label>
            <label
              >{{ t("正文 Markdown")
              }}<textarea v-model="editForm.body_markdown" rows="16"></textarea>
            </label>
            <label class="news-image-picker"
              >{{ t("补录图片")
              }}<input
                type="file"
                accept="image/jpeg,image/png,image/gif,image/webp,image/bmp,image/avif"
                multiple
                @change="selectImages"
              /><small>{{
                t("可一次选择多张图片，保存后追加到本条新闻的图库。")
              }}</small></label
            >
            <p v-if="selectedImages.length" class="news-selected-images">
              {{ t("待上传 {count} 张图片", { count: selectedImages.length }) }}
              <button
                type="button"
                class="text-button"
                @click="clearSelectedImages"
              >
                {{ t("清空") }}
              </button>
            </p>
            <div v-if="article.images?.length" class="news-existing-gallery">
              <figure
                v-for="image in article.images"
                :key="image.id"
                class="news-existing-image"
              >
                <img
                  v-if="image.url"
                  :src="image.url"
                  loading="lazy"
                  :alt="image.alt_text || article.title"
                />
                <figcaption>
                  <span>{{
                    image.kind === "manual"
                      ? t("手动补录")
                      : image.kind === "archive"
                        ? t("本地归档")
                        : t("官网图片")
                  }}</span
                  ><button
                    type="button"
                    class="text-button"
                    :disabled="removingImage === image.id"
                    @click="deleteImage(image)"
                  >
                    {{ removingImage === image.id ? t("删除中……") : t("删除") }}
                  </button>
                </figcaption>
              </figure>
            </div>
            <div class="news-edit-actions">
              <button :disabled="saving || uploading">
                {{
                  uploading
                    ? t("上传图片中……")
                    : saving
                      ? t("保存中……")
                      : t("保存新闻")
                }}</button
              ><button type="button" class="secondary" @click="cancelEdit">
                {{ t("取消") }}
              </button>
            </div>
          </fieldset>
        </form>

        <div
          v-else
          class="news-detail-content"
          :class="{ 'has-cover': article.images?.length }"
        >
          <div
            ref="bodyRef"
            class="news-detail-copy"
            @click="openInlineImage"
            @keydown.enter="openInlineImage"
            @keydown.space="openInlineImage"
            v-html="renderedBody"
          ></div>
          <div v-if="article.images?.length" class="news-detail-gallery">
            <button
              v-for="image in article.images"
              :key="image.id"
              type="button"
              class="news-detail-image"
              :disabled="!image.url"
              :aria-label="t('查看大图')"
              @click="openImage(image.url)"
            >
              <img
                v-if="image.url"
                :src="image.url"
                loading="lazy"
                :alt="image.alt_text || article.title"
              />
              <span v-else>{{ t("图片暂不可用") }}</span>
            </button>
          </div>
          <p v-else class="news-no-images">
            {{ t("这条新闻暂时没有本地图片。") }}
          </p>
        </div>
      </article>

      <p class="muted news-nav-hint">{{ t("左右按键 / 滑动切换新闻") }}</p>
      <nav class="news-detail-nav" aria-label="News navigation">
        <RouterLink
          v-if="previous"
          class="news-nav-card previous"
          :to="detailLink(previous)"
          ><small>← {{ t("上一条") }}</small
          ><strong>{{ previous.title }}</strong
          ><span>{{ formatDate(previous.published_at) }}</span></RouterLink
        >
        <span v-else class="news-nav-card disabled"
          ><small>← {{ t("上一条") }}</small
          ><strong>{{ t("已经是第一条") }}</strong></span
        >
        <RouterLink
          v-if="following"
          class="news-nav-card following"
          :to="detailLink(following)"
          ><small>{{ t("下一条") }} →</small
          ><strong>{{ following.title }}</strong
          ><span>{{ formatDate(following.published_at) }}</span></RouterLink
        >
        <span v-else class="news-nav-card disabled"
          ><small>{{ t("下一条") }} →</small
          ><strong>{{ t("已经是最后一条") }}</strong></span
        >
      </nav>
    </template>
  </main>
</template>
