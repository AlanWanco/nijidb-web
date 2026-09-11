<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate, RouterLink, useRoute, useRouter } from "vue-router";
import CollaboImage from "../components/CollaboImage.vue";
import { getCollaboration, listCollaborations, saveCollaboration, uploadCollaborationImages } from "../collabo/api";
import { coverUrl, dateLabel, normalizeImage, PAGE_SIZE, safeUrl } from "../collabo/model";
import { c } from "../collabo/text";
import { localeTag } from "../i18n";
import "../collabo/style.css";

const route = useRoute();
const router = useRouter();
const data = ref(null);
const form = ref(null);
const baseline = ref("");
const loading = ref(true);
const saving = ref(false);
const uploading = ref(false);
const error = ref("");
const message = ref("");
const keyword = ref("");
const page = ref(1);
const fileInput = ref(null);
let requestId = 0;
const editing = computed(() => Boolean(route.params.id));
const writable = computed(() => data.value?.mode === "database");
const dirty = computed(() => form.value && JSON.stringify(form.value) !== baseline.value);
const pages = computed(() => Math.max(1, Math.ceil((data.value?.total || 0) / PAGE_SIZE)));
const partnersText = computed({
  get: () => form.value?.partners.join("\n") || "",
  set: (value) => {
    form.value.partners = value.split("\n");
  },
});
function emptyForm() {
  return {
    id: "",
    slug: "",
    title: "",
    date: "",
    date_kind: "announced",
    partners: [],
    credit: "",
    note: "",
    links: [],
    images: [],
    cover_image_id: "",
    review_status: "pending",
  };
}
function setForm(item) {
  // Keep the editable payload bounded; collectors' raw metadata is never overwritten by this form.
  form.value = JSON.parse(
    JSON.stringify({
      ...emptyForm(),
      ...Object.fromEntries(
        [...Object.keys(emptyForm()), "updated_at"].filter((key) => key in item).map((key) => [key, item[key]]),
      ),
    }),
  );
  baseline.value = JSON.stringify(form.value);
}
async function load() {
  const id = ++requestId;
  loading.value = true;
  error.value = "";
  message.value = "";
  form.value = null;
  try {
    let result;
    if (!editing.value || route.params.id === "new")
      result = await listCollaborations({ admin: true, q: keyword.value, page: page.value });
    else result = await getCollaboration(route.params.id, { admin: true });
    if (id !== requestId) return;
    data.value = result;
    if (editing.value) setForm(route.params.id === "new" ? emptyForm() : result.item);
  } catch (err) {
    if (id !== requestId) return;
    error.value = err.message;
    if (err.status === 401) router.replace({ path: "/admin/login", query: { redirect: route.fullPath } });
  } finally {
    if (id === requestId) loading.value = false;
  }
}
function confirmLeave() {
  return !dirty.value || window.confirm(c("还有未保存的修改，确定离开？"));
}
onBeforeRouteLeave(() => !uploading.value && !saving.value && confirmLeave());
onBeforeRouteUpdate((to) => to.params.id === route.params.id || (!uploading.value && !saving.value && confirmLeave()));
function beforeUnload(event) {
  if (dirty.value) {
    event.preventDefault();
    event.returnValue = "";
  }
}
window.addEventListener("beforeunload", beforeUnload);
onBeforeUnmount(() => {
  requestId += 1;
  window.removeEventListener("beforeunload", beforeUnload);
});
watch(() => route.params.id, load, { immediate: true });
function search() {
  page.value = 1;
  load();
}
function turnPage(delta) {
  page.value += delta;
  load();
}
function moveImage(index, direction) {
  const target = index + direction;
  if (target < 0 || target >= form.value.images.length) return;
  const images = form.value.images;
  [images[index], images[target]] = [images[target], images[index]];
}
function addImage() {
  const id = [...crypto.getRandomValues(new Uint8Array(12))]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
  form.value.images.push(normalizeImage({ id: `draft-${id}`, url: "" }));
}
function removeImage(index) {
  const [image] = form.value.images.splice(index, 1);
  if (form.value.cover_image_id === image.id) form.value.cover_image_id = "";
}
function validForm() {
  const value = form.value;
  const parsed = new Date(`${value.date}T12:00:00Z`);
  if (
    !value.title.trim() ||
    !/^\d{4}-\d{2}-\d{2}$/.test(value.date) ||
    Number.isNaN(parsed.getTime()) ||
    parsed.toISOString().slice(0, 10) !== value.date ||
    value.links.some((link) => !link.title.trim() || !safeUrl(link.url)) ||
    value.images.some((image) => !safeUrl(image.url, true) || (image.source_page && !safeUrl(image.source_page)))
  ) {
    error.value = c("请填写标题和有效日期，并补齐链接标题及网址。");
    return false;
  }
  if (
    value.review_status === "approved" &&
    (value.images.some((image) => image.review_status === "pending") ||
      !value.images.some((image) => image.id === value.cover_image_id && image.review_status === "approved"))
  ) {
    error.value = c("图片仍有待审核项，或封面尚未通过审核。");
    return false;
  }
  return true;
}
async function save(next = false) {
  if (!writable.value || saving.value || uploading.value || !validForm()) return;
  const followingId = data.value?.following?.id;
  saving.value = true;
  error.value = "";
  message.value = "";
  try {
    const saved = await saveCollaboration({
      ...form.value,
      partners: form.value.partners.map((value) => value.trim()).filter(Boolean),
    });
    setForm(saved);
    message.value = c("保存成功");
    saving.value = false;
    if (next && followingId) await router.push(`/admin/collabo/${followingId}`);
    else if (route.params.id === "new") await router.replace(`/admin/collabo/${saved.id}`);
  } catch (err) {
    error.value = err.message;
  } finally {
    saving.value = false;
  }
}
function exportDraft() {
  const blob = new Blob(
    [JSON.stringify({ schema_version: 1, kind: "collabo-review-draft", item: form.value }, null, 2)],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `collabo-${form.value.id || "new"}-draft.json`;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function upload(event) {
  const files = [...event.target.files];
  event.target.value = "";
  if (!writable.value || !files.length) return;
  if (
    files.length > 16 ||
    files.some((file) => !["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 20 * 1024 * 1024)
  ) {
    error.value = "JPEG / PNG / WebP · ≤ 20 MB / image · ≤ 16 images";
    return;
  }
  uploading.value = true;
  error.value = "";
  try {
    form.value.images.push(...(await uploadCollaborationImages(files)).map(normalizeImage));
    if (!form.value.cover_image_id) form.value.cover_image_id = form.value.images[0]?.id || "";
  } catch (err) {
    error.value = err.message;
  } finally {
    uploading.value = false;
  }
}
</script>

<template>
  <main class="page cb-page cb-admin-page">
    <div class="cb-detail-top">
      <RouterLink class="back" :to="editing ? '/admin/collabo' : '/collabo'"
        >← {{ c(editing ? "管理联动" : "返回联动一览") }}</RouterLink
      ><RouterLink v-if="!editing" class="cb-button" to="/admin/collabo/new">＋ {{ c("新增联动") }}</RouterLink
      ><RouterLink v-else-if="form?.slug" class="cb-link" :to="`/collabo/${form.slug}`"
        >{{ c("公开预览") }} ↗</RouterLink
      >
    </div>
    <header class="cb-admin-heading">
      <p class="eyebrow">COLLABORATION / CURATOR'S DESK</p>
      <h1>{{ c(editing ? "资料编辑" : "管理联动") }}</h1>
    </header>
    <p v-if="data?.mode === 'preview'" class="cb-notice">
      {{ c("当前为前端预览，数据库接口尚未接入。可编辑并导出草稿，不会写入数据库。") }}
    </p>
    <div v-if="error" class="cb-notice cb-error" role="alert">
      {{ error }} <button v-if="!form" type="button" @click="load">{{ c("重试") }}</button>
    </div>
    <p v-if="loading" class="cb-state" aria-busy="true">{{ c("读取中……") }}</p>
    <template v-else-if="!editing">
      <form class="cb-toolbar" @submit.prevent="search">
        <input
          v-model="keyword"
          type="search"
          :aria-label="c('标题、合作方或关键词')"
          :placeholder="c('标题、合作方或关键词')"
        /><button>{{ c("搜索") }}</button><span>{{ c("{count} 次联动", { count: data?.total || 0 }) }}</span>
      </form>
      <div class="cb-review-list">
        <RouterLink v-for="item in data?.items || []" :key="item.id" :to="`/admin/collabo/${item.id}`"
          ><CollaboImage :src="coverUrl(item)" alt="" /><span
            ><strong>{{ item.title }}</strong
            ><small>{{ dateLabel(item.date, localeTag()) }} · {{ item.image_count }} IMG</small></span
          ><em>{{ c(item.review_status === "approved" ? "已审核" : "待审核") }}</em
          ><b>↗</b></RouterLink
        >
      </div>
      <nav class="cb-pagination">
        <button type="button" :disabled="page <= 1" @click="turnPage(-1)">←</button
        ><span>{{ c("第 {page} / {total} 页", { page, total: pages }) }}</span
        ><button type="button" :disabled="page >= pages" @click="turnPage(1)">→</button>
      </nav>
    </template>
    <form v-else-if="form" class="cb-editor" @submit.prevent="save(false)">
      <fieldset class="cb-editor-fields" :disabled="saving || uploading">
        <div class="cb-edit-section">
          <h2>01 / {{ c("资料与备注") }}</h2>
          <label>{{ c("标题") }}<input v-model="form.title" required maxlength="500" /></label>
          <div class="cb-field-pair">
            <label>{{ c("日期") }}<input v-model="form.date" type="date" required /></label
            ><label
              >{{ c("日期含义")
              }}<select v-model="form.date_kind">
                <option value="announced">{{ c("公布日期") }}</option>
                <option value="starts">{{ c("开启日期") }}</option>
                <option value="first_seen">{{ c("首次公开") }}</option>
              </select></label
            >
          </div>
          <label>{{ c("合作方（每行一项）") }}<textarea v-model="partnersText" rows="3"></textarea></label>
          <label>{{ c("版权标注") }}<input v-model="form.credit" /></label>
          <label>{{ c("备注") }}<textarea v-model="form.note" rows="5"></textarea></label>
          <label
            >{{ c("审核状态")
            }}<select v-model="form.review_status">
              <option value="pending">{{ c("待审核") }}</option>
              <option value="approved">{{ c("已审核") }}</option>
            </select></label
          >
        </div>
        <div class="cb-edit-section">
          <h2>02 / {{ c("相关页面") }}</h2>
          <div v-for="(link, index) in form.links" :key="index" class="cb-link-editor">
            <label>{{ c("链接标题") }}<input v-model="link.title" required /></label
            ><label>URL<input v-model="link.url" type="url" required /></label
            ><button type="button" class="cb-quiet" @click="form.links.splice(index, 1)">{{ c("移除") }}</button>
          </div>
          <button type="button" class="cb-quiet" @click="form.links.push({ title: '', url: '' })">
            ＋ {{ c("添加链接") }}
          </button>
        </div>
      </fieldset>
      <fieldset class="cb-editor-assets" :disabled="saving || uploading">
        <div class="cb-edit-section">
          <h2>
            03 / {{ c("图片画廊") }} <small>{{ form.images.length }}</small>
          </h2>
          <p class="cb-muted">{{ c("全部审核完成后，再批量生成 R2 缩略图。此页面不会提前生成或覆盖原图。") }}</p>
          <div class="cb-upload-actions">
            <button type="button" class="cb-quiet" @click="addImage">＋ {{ c("添加图片 URL") }}</button
            ><button type="button" :disabled="!writable" @click="fileInput.click()">↑ {{ c("上传图片") }}</button
            ><input
              ref="fileInput"
              hidden
              type="file"
              accept="image/jpeg,image/png,image/webp"
              multiple
              @change="upload"
            />
          </div>
          <article
            v-for="(image, index) in form.images"
            :key="image.id"
            class="cb-image-editor"
            :class="{ 'is-cover': form.cover_image_id === image.id, 'is-rejected': image.review_status === 'rejected' }"
          >
            <div class="cb-review-image">
              <CollaboImage :src="safeUrl(image.url, true)" :alt="image.alt || form.title" /><span>{{
                String(index + 1).padStart(2, "0")
              }}</span>
            </div>
            <div class="cb-image-fields">
              <label>URL<input v-model="image.url" required /></label
              ><label>{{ c("图片说明") }}<input v-model="image.caption" /></label
              ><label>{{ c("图片来源") }}<input v-model="image.source_page" type="url" /></label
              ><label>{{ c("链接标题") }}<input v-model="image.source_title" /></label>
              <div class="cb-image-controls">
                <select v-model="image.review_status" :aria-label="c('审核状态')">
                  <option value="pending">{{ c("待审核") }}</option>
                  <option value="approved">{{ c("已审核") }}</option>
                  <option value="rejected">{{ c("排除") }}</option></select
                ><button
                  type="button"
                  class="cb-quiet"
                  :disabled="image.review_status === 'rejected'"
                  @click="form.cover_image_id = image.id"
                >
                  {{ c(form.cover_image_id === image.id ? "当前封面" : "设为封面") }}</button
                ><button
                  type="button"
                  class="cb-quiet"
                  :disabled="index === 0"
                  :aria-label="c('向前移动')"
                  @click="moveImage(index, -1)"
                >
                  ↑</button
                ><button
                  type="button"
                  class="cb-quiet"
                  :disabled="index === form.images.length - 1"
                  :aria-label="c('向后移动')"
                  @click="moveImage(index, 1)"
                >
                  ↓</button
                ><button type="button" class="cb-quiet" @click="removeImage(index)">{{ c("移除") }}</button>
              </div>
            </div>
          </article>
        </div>
      </fieldset>
      <div class="cb-editor-actions">
        <span role="status">{{
          uploading ? c("正在上传……") : saving ? c("保存中……") : message || (dirty ? "●" : "")
        }}</span
        ><button type="button" class="cb-quiet" @click="exportDraft">{{ c("导出草稿 JSON") }}</button
        ><button type="submit" :disabled="!writable || saving || uploading">{{ c("保存到数据库") }}</button
        ><button v-if="data.following" type="button" :disabled="!writable || saving || uploading" @click="save(true)">
          {{ c("保存并审核下一条") }} →
        </button>
      </div>
    </form>
  </main>
</template>
