export const COLLABO_CHARACTER_TAGS = [
  {
    id: "ayumu",
    labels: { "zh-CN": "上原步梦", en: "Ayumu Uehara", ja: "上原歩夢" },
    color: "#ed7d95",
    aliases: ["上原歩夢", "歩夢", "步梦"],
  },
  {
    id: "kasumi",
    labels: { "zh-CN": "中须霞", en: "Kasumi Nakasu", ja: "中須かすみ" },
    color: "#e7d600",
    aliases: ["中須かすみ", "かすみ", "霞"],
  },
  {
    id: "shizuku",
    labels: { "zh-CN": "樱坂雫", en: "Shizuku Osaka", ja: "桜坂しずく" },
    color: "#01b7ed",
    aliases: ["桜坂しずく", "しずく", "雫"],
  },
  {
    id: "karin",
    labels: { "zh-CN": "朝香果林", en: "Karin Asaka", ja: "朝香果林" },
    color: "#485ec6",
    aliases: ["果林"],
  },
  {
    id: "ai",
    labels: { "zh-CN": "宫下爱", en: "Ai Miyashita", ja: "宮下愛" },
    color: "#ff5800",
    aliases: ["宮下愛", "爱", "愛"],
  },
  {
    id: "kanata",
    labels: { "zh-CN": "近江彼方", en: "Kanata Konoe", ja: "近江彼方" },
    color: "#a664a0",
    aliases: ["彼方"],
  },
  {
    id: "setsuna",
    labels: { "zh-CN": "优木雪菜", en: "Setsuna Yuki", ja: "優木せつ菜" },
    color: "#d81c2f",
    aliases: ["優木せつ菜", "中川菜々", "中川菜菜", "雪菜", "せつ菜"],
  },
  {
    id: "emma",
    labels: { "zh-CN": "艾玛·维尔德", en: "Emma Verde", ja: "エマ・ヴェルデ" },
    color: "#84c36e",
    aliases: ["エマ・ヴェルデ", "艾玛", "エマ"],
  },
  {
    id: "rina",
    labels: { "zh-CN": "天王寺璃奈", en: "Rina Tennoji", ja: "天王寺璃奈" },
    color: "#9ca5b9",
    aliases: ["璃奈"],
  },
  {
    id: "shioriko",
    labels: { "zh-CN": "三船栞子", en: "Shioriko Mifune", ja: "三船栞子" },
    color: "#37b484",
    aliases: ["栞子"],
  },
  {
    id: "mia",
    labels: { "zh-CN": "米娅·泰勒", en: "Mia Taylor", ja: "ミア・テイラー" },
    color: "#a99e98",
    aliases: ["ミア・テイラー", "米娅", "ミア"],
  },
  {
    id: "lanzhu",
    labels: { "zh-CN": "钟岚珠", en: "Lanzhu Zhong", ja: "鐘嵐珠" },
    color: "#f8c8c4",
    aliases: ["鐘嵐珠", "ランジュ", "岚珠", "嵐珠"],
  },
  {
    id: "yu",
    labels: { "zh-CN": "高咲侑", en: "Yuu Takasaki", ja: "高咲侑" },
    color: "#e9a8c2",
    aliases: ["侑", "Yuu"],
  },
];

export const COLLABO_CHARACTER_TAG_IDS = COLLABO_CHARACTER_TAGS.map((tag) => tag.id);

const characterTagMap = new Map(
  COLLABO_CHARACTER_TAGS.flatMap((tag) =>
    [tag.id, tag.labels["zh-CN"], tag.labels.ja, tag.labels.en, ...tag.aliases].flatMap((value) => [
      [value, tag.id],
      [value.toLocaleLowerCase(), tag.id],
    ]),
  ),
);

export function normalizeCharacterTags(value) {
  const values = (Array.isArray(value) ? value : [value]).flatMap((entry) =>
    String(entry || "").split(/[,，、\n]+/),
  );
  const selected = new Set();
  for (const entry of values) {
    const token = String(entry || "").trim();
    const normalizedToken = token.toLocaleLowerCase();
    if (["all", "全部", "全员"].includes(normalizedToken)) {
      COLLABO_CHARACTER_TAG_IDS.forEach((id) => selected.add(id));
      continue;
    }
    const id = characterTagMap.get(token) || characterTagMap.get(normalizedToken);
    if (id) selected.add(id);
  }
  return COLLABO_CHARACTER_TAG_IDS.filter((id) => selected.has(id));
}

export function characterTag(id) {
  return COLLABO_CHARACTER_TAGS.find((tag) => tag.id === id) || null;
}

export function characterLabel(id, language = "zh-CN") {
  const tag = characterTag(id);
  if (!tag) return String(id || "");
  const localeKey = String(language).startsWith("en") ? "en" : String(language).startsWith("ja") ? "ja" : "zh-CN";
  return tag.labels[localeKey] || tag.labels.ja;
}
