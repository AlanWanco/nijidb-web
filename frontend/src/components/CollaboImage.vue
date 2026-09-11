<script setup>
import { ref, watch } from "vue";
import { c } from "../collabo/text";
const props = defineProps({ src: String, alt: { type: String, default: "" }, eager: Boolean });
const failed = ref(false);
watch(
  () => props.src,
  () => {
    failed.value = false;
  },
);
</script>

<template>
  <img
    v-if="src && !failed"
    :src="src"
    :alt="alt"
    :loading="eager ? 'eager' : 'lazy'"
    decoding="async"
    @error="failed = true"
  />
  <span v-else class="cb-image-placeholder"
    ><span aria-hidden="true">◯</span><small>{{ c(src ? "图片加载失败" : "立绘整理中") }}</small></span
  >
</template>
