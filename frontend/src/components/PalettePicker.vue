<script setup>
import { computed, ref } from "vue";
import { PALETTES } from "../theme";
import { t } from "../i18n";

const props = defineProps({
  flavor: { type: String, required: true },
  selected: { type: String, required: true },
});

const emit = defineEmits(["select"]);
const open = ref(false);
const palette = computed(() => PALETTES[props.flavor]);
const flavorName = computed(() => props.flavor === "latte" ? "Latte" : "Mocha");
const selectedColor = computed(() => palette.value.find(([key]) => key === props.selected) || palette.value[3]);

function choose(key) {
  emit("select", key);
  open.value = false;
}
</script>

<template>
  <details class="palette-picker" :open="open" @toggle="open = $event.currentTarget.open">
    <summary class="icon-button" :aria-label="t('选择 Catppuccin 主题色')" :title="t('选择 Catppuccin 主题色')">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="7" cy="7" r="2.3"/><circle cx="17" cy="7" r="2.3"/><circle cx="7" cy="17" r="2.3"/><circle cx="17" cy="17" r="2.3"/></svg>
    </summary>
    <div class="palette-menu">
      <strong>{{ flavorName }} · {{ selectedColor[1] }}</strong>
      <div class="palette-swatches" role="group" :aria-label="`${flavorName} ${t('主题色')}`">
        <button
          v-for="[key, name, hex] in palette"
          :key="key"
          type="button"
          class="palette-swatch"
          :class="{ selected: key === selected }"
          :style="{ '--swatch': hex }"
          :title="`${name} ${hex}`"
          :aria-label="`${name} ${hex}`"
          :aria-pressed="key === selected"
          @click="choose(key)"
        ></button>
      </div>
    </div>
  </details>
</template>
