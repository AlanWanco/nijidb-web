import {
  COLLABO_CHARACTER_TAG_IDS,
  automaticCombinationGroupIds,
  characterTag,
  combinationGroupMatches,
  normalizeCharacterTags,
} from "./characters.js";

export const PAGE_SIZE = 24;
export const SLUG_PATTERN = /^\d{8}-[a-f0-9]{6}$/;

export function safeUrl(value, local = false) {
  if (typeof value !== "string" || !value.trim()) return "";
  const raw = value.trim();
  if (raw.includes("\\")) return "";
  if (local && /^\/(?:media|api\/(?:collabo|collaboration-illustrations)\/assets)\//.test(raw) && !raw.includes("\\")) return raw;
  try {
    const url = new URL(raw);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
  } catch {
    return "";
  }
}

export function dateLabel(value, language = "zh-CN") {
  if (!value) return "—";
  const date = new Date(`${value}T12:00:00`);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat(language, {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(date);
}

export function normalizePeriods(value) {
  const entries = Array.isArray(value) ? value : value && typeof value === "object" ? [value] : [];
  return entries
    .map((period) => ({
      start_date: String(period.start_date || period.start || ""),
      end_date: String(period.end_date || period.end || ""),
      title: String(period.title || period.label || ""),
      description: String(period.description || period.note || ""),
    }))
    .filter((period) => period.start_date || period.end_date || period.title || period.description);
}

export function normalizeImage(image, index = 0) {
  const normalized = { ...image };
  // Older API responses may still contain this legacy field. Do not expose or submit image review state anymore.
  delete normalized.review_status;
  return {
    ...normalized,
    id: String(image.id || image.sha256 || `image-${index}`),
    url: safeUrl(image.url || image.path, true),
    thumbnail_url: safeUrl(image.thumbnail_url, true),
    source_url: safeUrl(image.source_url),
    source_page: safeUrl(image.source_page),
    source_title: String(image.source_title || ""),
    caption: String(image.caption || ""),
    alt: String(image.alt || ""),
    width: Number(image.width) || null,
    height: Number(image.height) || null,
  };
}

export function normalizeItem(item) {
  const links = (item.links || item.official_links || []).map((link) => ({
    title: String(link.title || ""),
    url: safeUrl(link.url),
  }));
  const images = (item.images || []).map(normalizeImage).filter((image) => image.url);
  return {
    ...item,
    id: String(item.id),
    slug: SLUG_PATTERN.test(item.slug) ? item.slug : "",
    title: String(item.title || ""),
    date: item.date || item.announced_on || item.starts_on || item.first_seen || "",
    date_kind: item.date_kind || "first_seen",
    partners: Array.isArray(item.partners || item.collaboration) ? (item.partners || item.collaboration) : [],
    tags: normalizeCharacterTags(item.tags ?? item.character_tags ?? []),
    periods: normalizePeriods(item.periods ?? item.time_periods ?? []),
    credit: item.credit || "",
    note: item.note || "",
    links,
    images,
    image_count: item.image_count ?? images.length,
    cover_image_id: String(item.cover_image_id || images[0]?.id || ""),
    cover_url: safeUrl(item.cover_url, true),
    thumbnail_url: safeUrl(item.thumbnail_url, true),
    review_status: item.review_status || "pending",
  };
}

export function coverImage(item) {
  return item.images?.find((image) => image.id === item.cover_image_id) || item.images?.[0] || null;
}

export function coverUrl(item) {
  const image = coverImage(item);
  return item.thumbnail_url || image?.thumbnail_url || item.cover_url || image?.url || "";
}

export function yearValues(value) {
  const values = Array.isArray(value) ? value : [value];
  return [
    ...new Set(
      values
        .flatMap((entry) => String(entry || "").split(","))
        .map((entry) => entry.trim())
        .filter((entry) => /^\d{4}$/.test(entry)),
    ),
  ];
}

export function filterItems(items, { q = "", year = "", tags = [], group = "" } = {}) {
  const keyword = String(q || "").trim().toLocaleLowerCase();
  const years = yearValues(year);
  const normalizedTags = normalizeCharacterTags(tags);
  const selectedTags = normalizedTags.length === COLLABO_CHARACTER_TAG_IDS.length ? [] : normalizedTags;
  return items
    .filter((item) => {
      if (group && !combinationGroupMatches(item.tags ?? item.character_tags ?? [], group)) return false;
      const itemTags = normalizeCharacterTags(item.tags ?? item.character_tags ?? []);
      const periods = normalizePeriods(item.periods ?? item.time_periods ?? []);
      const tagSearch = itemTags.flatMap((tag) => {
        const definition = characterTag(tag);
        return definition ? [tag, ...Object.values(definition.labels), ...definition.aliases] : [tag];
      });
      return (
        (!years.length || years.includes(String(item.date || "").slice(0, 4))) &&
        (!selectedTags.length || selectedTags.some((tag) => itemTags.includes(tag))) &&
        (!keyword || [
          item.title,
          ...item.partners,
          ...tagSearch,
          item.note,
          ...periods.flatMap((period) => [period.title, period.description, period.start_date, period.end_date]),
        ].join(" ").toLocaleLowerCase().includes(keyword))
      );
    })
    .sort((a, b) => b.date.localeCompare(a.date) || a.id.localeCompare(b.id));
}

export function cardCombinationTagIds(value) {
  return automaticCombinationGroupIds(value);
}

export function pageNumber(value) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : 1;
}
