import DOMPurify from "dompurify";
import { marked } from "marked";

const OFFICIAL_IMAGE_HOSTS = new Set([
  "www.lovelive-anime.jp",
  "lovelive-anime.jp",
  "lovelive-as.bushimo.jp",
  "img.sunrise-inc.co.jp",
  "img.sunrise-inc.jp",
]);

function hasEmptyPort(raw) {
  const match = raw.match(/^(?:[a-z][a-z\d+.-]*:)?\/\/([^/?#]*)/i);
  return Boolean(match?.[1]?.endsWith(":"));
}

function safePath(path) {
  try {
    const decoded = decodeURIComponent(path);
    if (/[\u0000-\u001f\u007f\\]/.test(decoded)) return false;
    const parts = decoded.split("/");
    return !parts.some((part, index) => part === "." || part === ".." || (index > 0 && index < parts.length - 1 && !part));
  } catch {
    return false;
  }
}

function safeImageUrl(value, base) {
  const raw = String(value || "").trim();
  if (raw.includes("#")) return "";
  const href = newsHref(raw, base);
  if (!href) return "";
  try {
    const url = new URL(href);
    return url.protocol === "https:" && !url.hash && (!url.port || url.port === "443") && OFFICIAL_IMAGE_HOSTS.has(url.hostname) ? href : "";
  } catch {
    return "";
  }
}

export function newsHref(value, base) {
  const raw = String(value || "").trim();
  if (!raw || /[\u0000-\u001f\u007f\\]/.test(raw) || raw.includes("#") || hasEmptyPort(raw)) return "";
  try {
    const url = new URL(raw, base || window.location.origin);
    const port = url.port ? Number(url.port) : null;
    return ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password &&
      safePath(url.pathname) &&
      !(raw.includes("?") && !url.search) &&
      (port === null || Number.isInteger(port) && port > 0 && port <= 65535)
      ? url.href
      : "";
  } catch {
    return "";
  }
}

export function newsSummaryText(value) {
  const fragment = DOMPurify.sanitize(
    marked.parse(String(value || ""), { gfm: true, async: false }),
    {
      RETURN_DOM_FRAGMENT: true,
      FORBID_TAGS: ["style", "script", "img"],
    },
  );
  return fragment.textContent.replace(/\s+/g, " ").trim();
}

export function renderNewsMarkdown(value, article = {}) {
  const html = DOMPurify.sanitize(
    marked.parse(String(value || ""), {
      gfm: true,
      breaks: true,
      async: false,
    }),
    {
      ADD_ATTR: ["target", "rel"],
      FORBID_TAGS: ["style", "form", "input", "button", "iframe"],
      FORBID_ATTR: ["style", "srcset"],
      ALLOW_DATA_ATTR: false,
    },
  );
  const template = document.createElement("template");
  template.innerHTML = html;
  for (const anchor of template.content.querySelectorAll("a")) {
    const href = newsHref(anchor.getAttribute("href"), article.source_url);
    if (href) anchor.setAttribute("href", href);
    else anchor.removeAttribute("href");
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
  }
  for (const image of template.content.querySelectorAll("img")) {
    const raw = image.getAttribute("src") || "";
    const local = raw.replace(/^\.\//, "").replace(/^archive:/, "");
    const reference = article.images?.find(
      (entry) =>
        entry.local_path?.replace(/^archive:/, "") === local ||
        entry.source_url === raw,
    );
    const url = reference?.url || (local.startsWith("pic/") ? "" : safeImageUrl(raw, article.source_url));
    if (!url || !newsHref(url)) {
      image.replaceWith(document.createTextNode(image.alt || ""));
      continue;
    }
    image.src = url;
    image.loading = "lazy";
    image.tabIndex = 0;
    image.setAttribute("role", "button");
  }
  return template.innerHTML;
}
