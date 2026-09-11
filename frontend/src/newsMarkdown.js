import DOMPurify from "dompurify";
import { marked } from "marked";

export function newsHref(value, base) {
  if (!String(value || "").trim()) return "";
  try {
    const url = new URL(String(value || ""), base || window.location.origin);
    return ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password
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
    const url =
      reference?.url ||
      (local.startsWith("pic/") ? "" : newsHref(raw, article.source_url));
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
