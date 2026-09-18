import { t } from "./i18n";

const BILIBILI_BV_PATTERN = /(?<![A-Za-z0-9])(BV[0-9A-Za-z]{10})(?![A-Za-z0-9])/i;

function bilibiliId(value) {
  const match = String(value || "").trim().match(BILIBILI_BV_PATTERN);
  return match ? `BV${match[1].slice(2)}` : "";
}

function hasEmptyPort(raw) {
  const match = raw.match(/^(?:[a-z][a-z\d+.-]*:)?\/\/([^/?#]*)/i);
  return Boolean(match?.[1]?.endsWith(":"));
}

function safePath(raw) {
  try {
    const decoded = decodeURIComponent(raw);
    if (/[\u0000-\u001f\u007f\\]/.test(decoded)) return false;
    const parts = decoded.split("/");
    return !parts.some((part, index) => part === "." || part === ".." || (index > 0 && index < parts.length - 1 && !part));
  } catch {
    return false;
  }
}

function externalUrl(value) {
  const raw = String(value || "").trim();
  if (!raw || /[\u0000-\u001f\u007f\\]/.test(raw) || raw.includes("#") || hasEmptyPort(raw)) return "";
  try {
    const url = new URL(raw);
    const port = url.port ? Number(url.port) : null;
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password && url.hostname
      && safePath(url.pathname)
      && !(raw.includes("?") && !url.search)
      && (port === null || Number.isInteger(port) && port > 0 && port <= 65535)
      ? url.href
      : "";
  } catch {
    return "";
  }
}

function linkValue(item, source) {
  const camelCase = item.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
  return source?.[item] ?? source?.[camelCase] ?? "";
}

function linkItems(values) {
  return [
    { key: "source", label: t("源地址"), value: linkValue("source_url", values), allowBilibili: false },
    { key: "mirror", label: t("搬运地址"), value: linkValue("mirror_url", values), allowBilibili: true },
    { key: "subtitle", label: t("字幕地址"), value: linkValue("subtitle_url", values), allowBilibili: true },
  ].filter(item => String(item.value || "").trim()).map(item => {
    const id = item.allowBilibili ? bilibiliId(item.value) : "";
    return {
      ...item,
      display: id || t("外部链接"),
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
  return href ? { key: "related", label: t("相关链接"), display: t("外部链接"), href, type: "external" } : null;
}

export function programAdminPath(programId, { panel = "edit", occurrenceId = "", occurrenceDate = "" } = {}) {
  const params = new URLSearchParams({ program: String(programId), panel });
  if (occurrenceId !== null && occurrenceId !== undefined && String(occurrenceId)) params.set("occurrence", String(occurrenceId));
  if (occurrenceDate) params.set("date", String(occurrenceDate));
  return `/admin/programs?${params}`;
}
