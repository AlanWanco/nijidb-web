// Storage/filter tokens are a controlled set shared by the news editor and filters.
const labels = {
  game: ["游戏", "ゲーム", "Games"],
  "game:loveca": [
    "Love Live! 卡牌",
    "ラブライブ！カードゲーム",
    "Love Live! Card Game",
  ],
  "game:other": ["其他游戏", "その他のゲーム", "Other games"],
  "game:school-fes": ["学园偶像祭", "スクフェス", "School Idol Festival"],
  "game:sukusta": ["学园偶像祭 ALL STARS", "スクスタ", "ALL STARS"],
  "game:visual-novel": ["视觉小说", "ビジュアルノベル", "Visual novels"],
  anime: ["动画", "アニメ", "Anime"],
  "anime:movie": ["剧场版", "劇場版", "Movie"],
  "anime:movie-chapter-1": ["剧场版第1章", "劇場版第1章", "Movie chapter 1"],
  "anime:movie-chapter-2": ["剧场版第2章", "劇場版第2章", "Movie chapter 2"],
  "anime:movie-chapter-3": ["剧场版第3章", "劇場版第3章", "Movie chapter 3"],
  "anime:ova": ["OVA", "OVA", "OVA"],
  "anime:spin-off": ["衍生动画", "スピンオフアニメ", "Spin-off anime"],
  "anime:tv-season-1": ["TV动画第1季", "TVアニメ第1期", "TV season 1"],
  "anime:tv-season-2": ["TV动画第2季", "TVアニメ第2期", "TV season 2"],
  "anime:tv-season-3": ["TV动画第3季", "TVアニメ第3期", "TV season 3"],
  "voice-activity": ["声优活动", "キャスト活動", "Cast activities"],
  "voice:online": ["声优线上活动", "キャスト配信", "Online cast events"],
  "voice:offline": [
    "声优线下活动",
    "キャスト現地イベント",
    "In-person cast events",
  ],
  "voice:radio": ["声优广播", "キャストラジオ", "Cast radio"],
  collaboration: ["联动", "コラボ", "Collaboration"],
  apology: ["致歉与更正", "お詫び・訂正", "Apologies & corrections"],
  goods: ["周边", "グッズ", "Merchandise"],
  music: ["音乐", "音楽", "Music"],
  video: ["影像商品", "映像商品", "Video releases"],
  book: ["书籍杂志", "書籍・雑誌", "Books & magazines"],
  media: ["媒体", "メディア", "Media"],
  event: ["活动", "イベント", "Events"],
  theater: ["影院", "劇場", "Theater"],
  campaign: ["宣传活动", "キャンペーン", "Campaigns"],
  local: ["地方资讯", "ご当地情報", "Local news"],
  streaming: ["节目配信", "番組配信", "Streaming"],
  announcement: ["公告", "お知らせ", "Announcements"],
  other: ["其他", "その他", "Other"],
};
export const newsTagOptions = Object.freeze(Object.keys(labels));

export function newsTagLabel(tag, language = "zh-CN", definition = null) {
  const languageKey = language === "ja" ? "ja" : language === "en" ? "en" : "zh-CN";
  const dynamicLabel = definition?.labels?.[languageKey] || (languageKey === "zh-CN" ? definition?.label : "");
  return dynamicLabel || labels[tag]?.[languageKey === "ja" ? 1 : languageKey === "en" ? 2 : 0] || tag;
}
export function newsSourceGroup(source) {
  return ["niji_topics", "niji_news"].includes(source)
    ? "official_site"
    : source;
}
export function newsSourceLabel(source) {
  return newsSourceGroup(source) === "official_site"
    ? "Official Site News"
    : source === "as_news"
      ? "AS News"
      : source;
}
