<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useDetailNavigation } from "../composables/useDetailNavigation";
import { t } from "../i18n";

const props = defineProps({
  images: { type: Array, default: () => [] },
  start: { type: Number, default: -1 },
});
const emit = defineEmits(["close"]);
const dialog = ref(null);
const index = ref(0);
const active = computed(() => props.images[index.value]);
let overflow = null;
let opener = null;
function restore() {
  if (overflow !== null) document.body.style.overflow = overflow;
  overflow = null;
  opener?.focus?.();
  opener = null;
}
function close() {
  dialog.value?.close();
  restore();
  emit("close");
}
function change(delta) {
  index.value = Math.max(
    0,
    Math.min(props.images.length - 1, index.value + delta),
  );
}
function keyboard(event) {
  if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  event.preventDefault();
  event.stopPropagation();
  change(event.key === "ArrowLeft" ? -1 : 1);
}
const touch = useDetailNavigation({
  previous: () => change(-1),
  following: () => change(1),
  enabled: () => props.start >= 0,
});
watch(
  () => props.start,
  async (value) => {
    if (value < 0) {
      dialog.value?.close();
      restore();
      return;
    }
    index.value = value;
    await nextTick();
    if (props.start < 0 || dialog.value?.open) return;
    opener = document.activeElement;
    overflow = document.body.style.overflow;
    dialog.value?.showModal();
    document.body.style.overflow = "hidden";
  },
);
onBeforeUnmount(restore);
</script>

<template>
  <Teleport to="body">
    <dialog
      ref="dialog"
      class="news-lightbox"
      :aria-label="t('查看大图')"
      @close="restore"
      @cancel.prevent="close"
      @keydown="keyboard"
      @click="$event.target === dialog && close()"
    >
      <template v-if="start >= 0">
        <div class="news-lightbox-bar">
          <span>{{ index + 1 }} / {{ images.length }}</span>
          <a :href="active?.url" target="_blank" rel="noopener noreferrer"
            >{{ t("查看原图") }} ↗</a
          >
          <button
            type="button"
            autofocus
            :aria-label="t('关闭大图')"
            @click="close"
          >
            ×
          </button>
        </div>
        <div
          class="news-lightbox-stage"
          @touchstart.passive="touch.onTouchStart"
          @touchmove.passive="touch.onTouchMove"
          @touchend.passive="touch.onTouchEnd"
          @touchcancel.passive="touch.onTouchCancel"
        >
          <img :src="active?.url" :alt="active?.alt || ''" />
        </div>
        <div class="news-lightbox-bar">
          <button
            type="button"
            :disabled="index <= 0"
            :aria-label="t('上一张图片')"
            @click="change(-1)"
          >
            ←
          </button>
          <p>{{ active?.alt }}</p>
          <button
            type="button"
            :disabled="index >= images.length - 1"
            :aria-label="t('下一张图片')"
            @click="change(1)"
          >
            →
          </button>
        </div>
      </template>
    </dialog>
  </Teleport>
</template>
