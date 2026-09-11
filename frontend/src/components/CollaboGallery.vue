<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import CollaboImage from "./CollaboImage.vue";
import { useDetailNavigation } from "../composables/useDetailNavigation";
import { c } from "../collabo/text";

const props = defineProps({ images: { type: Array, default: () => [] }, title: String, coverId: String });
const emit = defineEmits(["lightbox"]);
const index = ref(0);
const dialog = ref(null);
const enlarged = ref(false);
let savedOverflow = "";
const image = computed(() => props.images[index.value]);
const alt = computed(() => image.value?.alt || `${props.title} · ${c("图片 {number}", { number: index.value + 1 })}`);
function change(delta) {
  index.value = Math.max(0, Math.min(props.images.length - 1, index.value + delta));
}
function restoreScroll() {
  if (!enlarged.value) return;
  document.body.style.overflow = savedOverflow;
  enlarged.value = false;
  emit("lightbox", false);
}
async function open() {
  if (!image.value || enlarged.value) return;
  savedOverflow = document.body.style.overflow;
  enlarged.value = true;
  emit("lightbox", true);
  await nextTick();
  dialog.value?.showModal();
  document.body.style.overflow = "hidden";
}
function close() {
  dialog.value?.close();
  restoreScroll();
}
function keyboard(event) {
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    event.preventDefault();
    event.stopPropagation();
    change(event.key === "ArrowLeft" ? -1 : 1);
  }
}
const touch = useDetailNavigation({
  previous: () => change(-1),
  following: () => change(1),
  enabled: () => enlarged.value,
});
watch(
  () => props.images,
  () => {
    close();
    index.value = Math.max(
      0,
      props.images.findIndex((entry) => entry.id === props.coverId),
    );
  },
  { immediate: true },
);
onBeforeUnmount(restoreScroll);
</script>

<template>
  <section class="cb-gallery" :aria-label="c('图片画廊')">
    <div class="cb-gallery-stage">
      <Transition name="cb-image" mode="out-in">
        <CollaboImage :key="image?.id || 'empty'" :src="image?.url" :alt="alt" eager />
      </Transition>
      <button
        v-if="image"
        class="cb-expand"
        type="button"
        data-no-detail-swipe
        :aria-label="c('查看大图')"
        @click="open"
      >
        ⤢ <span>{{ c("查看大图") }}</span>
      </button>
      <span v-if="image" class="cb-frame-count" aria-hidden="true"
        >{{ String(index + 1).padStart(2, "0") }} / {{ String(images.length).padStart(2, "0") }}</span
      >
    </div>
    <div class="cb-gallery-caption">
      <p>{{ image?.caption || title }}</p>
      <span class="cb-small-nav">
        <button type="button" :disabled="index <= 0" :aria-label="c('上一张图片')" @click="change(-1)">←</button>
        <button type="button" :disabled="index >= images.length - 1" :aria-label="c('下一张图片')" @click="change(1)">
          →
        </button>
      </span>
    </div>
    <div v-if="images.length > 1" class="cb-gallery-strip" data-no-detail-swipe :aria-label="c('图片画廊')">
      <button
        v-for="(entry, number) in images"
        :key="entry.id"
        type="button"
        :class="{ selected: index === number }"
        :aria-pressed="index === number"
        :aria-label="entry.alt || c('图片 {number}', { number: number + 1 })"
        @click="index = number"
      >
        <CollaboImage :src="entry.thumbnail_url || entry.url" alt="" />
        <span>{{ String(number + 1).padStart(2, "0") }}</span>
      </button>
    </div>
    <Teleport to="body">
      <dialog
        ref="dialog"
        class="cb-lightbox"
        :aria-label="c('图片画廊')"
        @close="restoreScroll"
        @cancel="close"
        @click="$event.target === dialog && close()"
        @keydown="keyboard"
      >
        <template v-if="enlarged">
          <div class="cb-lightbox-bar">
            <span>{{ index + 1 }} / {{ images.length }}</span>
            <a :href="image?.url" target="_blank" rel="noopener noreferrer">{{ c("查看原图") }} ↗</a>
            <button type="button" autofocus :aria-label="c('关闭大图')" @click="close">×</button>
          </div>
          <div
            class="cb-lightbox-image"
            @touchstart.passive="touch.onTouchStart"
            @touchmove.passive="touch.onTouchMove"
            @touchend.passive="touch.onTouchEnd"
            @touchcancel.passive="touch.onTouchCancel"
          >
            <CollaboImage :src="image?.url" :alt="alt" eager />
          </div>
          <div class="cb-lightbox-bottom">
            <button type="button" :disabled="index <= 0" :aria-label="c('上一张图片')" @click="change(-1)">←</button>
            <p>{{ image?.caption || alt }}</p>
            <button
              type="button"
              :disabled="index >= images.length - 1"
              :aria-label="c('下一张图片')"
              @click="change(1)"
            >
              →
            </button>
          </div>
        </template>
      </dialog>
    </Teleport>
  </section>
</template>
