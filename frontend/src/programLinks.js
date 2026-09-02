const BILIBILI_BV_PATTERN = /(?<![A-Za-z0-9])(BV[0-9A-Za-z]{10})(?![A-Za-z0-9])/i;

function bilibiliId(value) {
  const match = String(value || "").trim().match(BILIBILI_BV_PATTERN);
  return match ? `BV${match[1].slice(2)}` : "";
}

function externalUrl(value) {
  const raw = String(value || "").trim();
  return /^https?:\/\//i.test(raw) ? raw : "";
}

function linkValue(item, source) {
  const camelCase = item.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
  return source?.[item] ?? source?.[camelCase] ?? "";
}

function linkItems(values) {
  return [
    { key: "source", label: "源地址", value: linkValue("source_url", values), allowBilibili: false },
    { key: "mirror", label: "搬运地址", value: linkValue("mirror_url", values), allowBilibili: true },
    { key: "subtitle", label: "字幕地址", value: linkValue("subtitle_url", values), allowBilibili: true },
  ].filter(item => String(item.value || "").trim()).map(item => {
    const id = item.allowBilibili ? bilibiliId(item.value) : "";
    return {
      ...item,
      display: id || "外部链接",
      href: id ? `https://www.bilibili.com/video/${id}` : externalUrl(item.value),
      type: id ? "bilibili" : "external",
    };
  });
}

export function occurrenceLinkItems(occurrence) {
  return linkItems(occurrence);
}

export function relatedLinkItem(program) {
  const href = externalUrl(program?.official_url);
  return href ? { key: "related", label: "相关链接", display: "外部链接", href, type: "external" } : null;
}

export function programAdminPath(programId, { panel = "edit", occurrenceId = "", occurrenceDate = "" } = {}) {
  const params = new URLSearchParams({ program: String(programId), panel });
  if (occurrenceId !== null && occurrenceId !== undefined && String(occurrenceId)) params.set("occurrence", String(occurrenceId));
  if (occurrenceDate) params.set("date", String(occurrenceDate));
  return `/admin/programs?${params}`;
}
