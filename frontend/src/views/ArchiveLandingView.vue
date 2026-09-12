<script setup>
import { t } from "../i18n";

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
    path: "/collabo",
    theme: "illustration",
    index: "03",
    eyebrow: "COLLABORATION ILLUSTRATIONS",
    title: "联动立绘",
    description: "按年份浏览官方联动记录，查看本地整理的立绘与来源页面。",
    action: "浏览联动立绘",
  },
  {
    path: "/news",
    theme: "news",
    index: "04",
    eyebrow: "NEWS ARCHIVE",
    title: "官网新闻",
    description: "整理虹咲官方 Topics、News 与历史公告。",
    action: "进入官网新闻",
  },
];
</script>

<template>
  <main class="page archive-landing-page">
    <section class="archive-landing-intro">
      <p class="eyebrow">NIJIGASAKI DATA ARCHIVE</p>
     <h1>{{ t("选择一个档案入口") }}<span class="title-mark" aria-hidden="true"></span></h1>
     <p>{{ t("音乐、节目、新闻，以及还在慢慢整理中的联动记录。") }}</p>
   </section>

   <section class="archive-choice-grid" :aria-label="t('档案入口')">
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
         <h2>{{ t(choice.title) }}</h2>
         <p>{{ t(choice.description) }}</p>
       </div>
       <span class="archive-choice-footer"><span>{{ t(choice.action) }}</span><b aria-hidden="true">↗</b></span>
      </RouterLink>

    </section>
  </main>
</template>
