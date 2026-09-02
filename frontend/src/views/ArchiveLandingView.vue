<script setup>
const archiveChoices = [
  {
    path: "/music",
    theme: "music",
    index: "01",
    eyebrow: "MUSIC ARCHIVE",
    title: "音乐档案",
    description: "从封面、曲目到特典，整理每一份虹咲音乐发行记录。",
    action: "进入音乐档案",
  },
  {
    path: "/programs",
    theme: "program",
    index: "02",
    eyebrow: "PROGRAM ARCHIVE",
    title: "节目档案",
    description: "按排期浏览官方节目与成员个人节目，查看每一期播出记录。",
    action: "进入节目档案",
  },
  {
    theme: "illustration",
    index: "03",
    eyebrow: "COLLABORATION ILLUSTRATIONS",
    title: "联动立绘档案",
    description: "这个入口还在挖坑，等整理好再和你见面。",
    action: "建设中",
  },
];
</script>

<template>
  <main class="page archive-landing-page">
    <section class="archive-landing-intro">
      <p class="eyebrow">NIJIGASAKI DATA ARCHIVE</p>
      <h1>选择一个档案入口<span class="title-mark" aria-hidden="true"></span></h1>
      <p>音乐、节目，以及还在慢慢整理中的联动记录。</p>
    </section>

    <section class="archive-choice-grid" aria-label="档案入口">
      <RouterLink
        v-for="choice in archiveChoices.filter(item => item.path)"
        :key="choice.index"
        class="archive-choice"
        :class="`archive-choice-${choice.theme}`"
        :to="choice.path"
      >
        <span class="archive-choice-index">{{ choice.index }}</span>
        <div class="archive-choice-content">
          <p class="eyebrow">{{ choice.eyebrow }}</p>
          <h2>{{ choice.title }}</h2>
          <p>{{ choice.description }}</p>
        </div>
        <span class="archive-choice-footer"><span>{{ choice.action }}</span><b aria-hidden="true">↗</b></span>
      </RouterLink>

      <article class="archive-choice archive-choice-illustration is-planned" aria-disabled="true">
        <span class="archive-choice-index">{{ archiveChoices[2].index }}</span>
        <div class="archive-choice-content">
          <p class="eyebrow">{{ archiveChoices[2].eyebrow }}</p>
          <h2>{{ archiveChoices[2].title }}</h2>
          <p>{{ archiveChoices[2].description }}</p>
        </div>
        <span class="archive-choice-footer"><span>{{ archiveChoices[2].action }}</span><b aria-hidden="true">···</b></span>
      </article>
    </section>
  </main>
</template>
