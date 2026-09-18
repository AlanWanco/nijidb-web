import { api } from "../api";
import {
  COLLABO_CHARACTER_TAG_IDS,
  combinationGroup,
  normalizeCharacterTags,
} from "./characters.js";
import { filterItems, normalizeItem, PAGE_SIZE, pageNumber } from "./model";

// The database API is the primary source. A missing endpoint alone falls back to
// the legacy read-only catalog so older deployments remain viewable.
let previewPromise;
async function previewCatalog() {
  if (!previewPromise) {
    previewPromise = Promise.all([
      import("../content/collaborationIllustrations.json"),
      api("/api/collaboration-illustrations"),
    ])
      .then(([{ default: catalog }, manifest]) => ({
        source: catalog.source,
        items: catalog.years
          .flatMap((year) => year.items)
          .map((item) =>
            normalizeItem({
              ...item,
              // Preview-only stable alias. The database generates and owns the permanent random slug.
              slug: `${item.first_seen.replaceAll("-", "")}-${item.id.slice(0, 6)}`,
              images: manifest.items?.[item.id]?.images || [],
            }),
          ),
      }))
      .catch((error) => {
        previewPromise = null;
        throw error;
      });
  }
  return previewPromise;
}

function characterTagQuery(tags) {
  const normalized = normalizeCharacterTags(tags);
  return normalized.length === COLLABO_CHARACTER_TAG_IDS.length ? "" : normalized.join(",");
}

function combinationGroupQuery(group) {
  return combinationGroup(group)?.id || "";
}

async function optionalApi(path, options) {
  try {
    const result = await api(path, options);
    // Older deployments serve their SPA for unknown paths.
    if (typeof result === "string" && /^\s*(?:<!doctype html|<html)/i.test(result)) return null;
    if (!result || typeof result !== "object") throw new Error("联动接口返回格式无效");
    return result;
  } catch (error) {
    if (error.status === 404) return null;
    throw error;
  }
}

export async function listCollaborations({ q = "", year = "", tags = [], group = "", page = 1, admin = false } = {}) {
  const params = new URLSearchParams({
    q,
    year,
    tags: characterTagQuery(tags),
    group: combinationGroupQuery(group),
    page: String(pageNumber(page)),
    page_size: String(PAGE_SIZE),
  });
  const payload = await optionalApi(`${admin ? "/api/admin/collabo" : "/api/collabo"}?${params}`);
  if (payload) {
    if (!Array.isArray(payload.items)) throw new Error("联动接口缺少 items 字段");
    const items = payload.items.map(normalizeItem);
    if (!admin && items.some((item) => !item.slug)) throw new Error("联动接口缺少有效的永久链接 slug");
    return { ...payload, items, mode: "database" };
  }
  const catalog = await previewCatalog();
  const items = filterItems(catalog.items, { q, year, tags, group });
  const offset = (pageNumber(page) - 1) * PAGE_SIZE;
  return {
    items: items.slice(offset, offset + PAGE_SIZE),
    total: items.length,
    years: [...new Set(catalog.items.map((item) => item.date.slice(0, 4)))].sort().reverse(),
    source: catalog.source,
    mode: "preview",
  };
}

export async function getCollaboration(slugOrId, { admin = false, q = "", year = "", tags = [], group = "" } = {}) {
  const prefix = admin ? "/api/admin/collabo" : "/api/collabo";
  const params = new URLSearchParams({ q, year, tags: characterTagQuery(tags), group: combinationGroupQuery(group) });
  const payload = await optionalApi(`${prefix}/${encodeURIComponent(slugOrId)}?${params}`);
  if (payload) {
    if (!payload.item) throw new Error("联动接口缺少 item 字段");
    const item = normalizeItem(payload.item);
    if (!admin && !item.slug) throw new Error("联动接口缺少有效的永久链接 slug");
    return { ...payload, item, mode: "database" };
  }
  // Do not resurrect a deleted database record from a legacy import.
  const index = await optionalApi(`${prefix}?page=1&page_size=1`);
  if (index) throw Object.assign(new Error("联动记录不存在"), { status: 404 });
  const catalog = await previewCatalog();
  const item = catalog.items.find((entry) => (admin ? entry.id : entry.slug) === slugOrId);
  if (!item) throw Object.assign(new Error("联动记录不存在"), { status: 404 });
  const ordered = filterItems(catalog.items, { q, year, tags, group });
  const position = ordered.findIndex((entry) => entry.id === item.id);
  const summary = (entry) => (entry ? { id: entry.id, slug: entry.slug, title: entry.title } : null);
  return {
    item,
    source: catalog.source,
    mode: "preview",
    previous: position >= 0 ? summary(ordered[position - 1]) : null,
    following: position >= 0 ? summary(ordered[position + 1]) : null,
  };
}

export async function saveCollaboration(item) {
  const payload = await api(`/api/admin/collabo${item.id ? `/${encodeURIComponent(item.id)}` : ""}`, {
    method: item.id ? "PATCH" : "POST",
    body: item,
  });
  if (!payload?.item?.id) throw new Error("保存接口未返回有效记录，未确认保存成功");
  return normalizeItem(payload.item);
}

export async function deleteCollaboration(identifier) {
  return api(`/api/admin/collabo/${encodeURIComponent(identifier)}`, { method: "DELETE" });
}

async function parseCollaborationUploadResponse(response) {
  const payload = await response.json();
  if (!response.ok || !Array.isArray(payload.images)) {
    const error = new Error(payload.detail || "图片上传失败");
    error.status = response.status;
    throw error;
  }
  return payload.images;
}

export async function uploadCollaborationImages(files) {
  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  const response = await fetch("/api/admin/collabo/assets", {
    method: "POST",
    body: form,
    credentials: "same-origin",
  });
  return parseCollaborationUploadResponse(response);
}

export async function uploadCollaborationImageUrl(url) {
  const response = await fetch("/api/admin/collabo/assets", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
    credentials: "same-origin",
  });
  return parseCollaborationUploadResponse(response);
}
