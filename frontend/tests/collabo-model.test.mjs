import assert from "node:assert/strict";
import test from "node:test";
import {
  normalizeItem,
  normalizeImage,
  coverUrl,
  filterItems,
  pageNumber,
  safeUrl,
  yearValues,
} from "../src/collabo/model.js";
import { swipeDirection } from "../src/composables/useDetailNavigation.js";
import {
  COLLABO_CHARACTER_TAG_IDS,
  COLLABO_COMBINATION_GROUPS,
  automaticCombinationGroupIds,
  combinationGroupMatches,
  normalizeCharacterTags,
} from "../src/collabo/characters.js";

test("source and image URLs reject active schemes and credentials", () => {
  for (const url of [
    "javascript:alert(1)",
    "data:text/html,test",
    "https://user:pass@example.com/",
    "//example.com",
    "https:\\evil",
  ]) {
    assert.equal(safeUrl(url), "");
  }
  assert.equal(safeUrl("https://example.com/news?item=1#title"), "https://example.com/news?item=1#title");
  assert.equal(safeUrl("/media/image.jpg", true), "/media/image.jpg");
  assert.equal(safeUrl("/admin/secret", true), "");
});

test("legacy fields and all original links survive normalization without image review state", () => {
  const raw = {
    id: "legacy",
    first_seen: "2026-09-11",
    collaboration: ["Partner"],
    note: "line1\nline2",
    credit: "Copyright",
    official_links: [{ title: "Official title", url: "https://example.com/" }],
    images: Array.from({ length: 3 }, (_, i) => ({ id: `${i}`, path: `/media/${i}.jpg` })),
  };
  const item = normalizeItem(raw);
  assert.equal(item.date, raw.first_seen);
  assert.equal(item.note, raw.note);
  assert.equal(item.credit, raw.credit);
  assert.deepEqual(item.links, raw.official_links);
  assert.deepEqual(item.partners, raw.collaboration);
  assert.equal(item.review_status, "pending");
  assert.ok(item.images.every((image) => !Object.hasOwn(image, "review_status")));
});

test("character tags normalize aliases and all selection", () => {
  assert.deepEqual(normalizeCharacterTags(["上原歩夢", "lanzhu"]), ["ayumu", "lanzhu"]);
  assert.deepEqual(normalizeCharacterTags("全员"), COLLABO_CHARACTER_TAG_IDS);
});

test("periods and character tags survive item normalization and filtering", () => {
  const item = normalizeItem({
    id: "tagged",
    date: "2026-01-01",
    title: "Tagged",
    tags: ["ayumu"],
    periods: [{ start_date: "2026-01-02", end_date: "2026-01-03", title: "First", description: "Details" }],
  });
  assert.deepEqual(item.tags, ["ayumu"]);
  assert.equal(item.periods[0].title, "First");
  assert.deepEqual(filterItems([item], { tags: ["ayumu"] }).map((entry) => entry.id), ["tagged"]);
  assert.deepEqual(filterItems([item], { tags: ["kasumi"] }), []);
});

test("member set categories match exact defined groups", () => {
  const initial9 = ["ayumu", "kasumi", "shizuku", "karin", "ai", "kanata", "setsuna", "emma", "rina"];
  const anime10 = [...initial9, "yu"];
  const shioriko10 = [...initial9, "shioriko"];
  assert.deepEqual(automaticCombinationGroupIds(initial9), ["initial9"]);
  assert.deepEqual(automaticCombinationGroupIds(anime10), ["anime10"]);
  assert.deepEqual(automaticCombinationGroupIds(shioriko10), ["shioriko10"]);
  assert.ok(combinationGroupMatches(initial9, "initial9"));
  assert.ok(combinationGroupMatches(anime10, "anime10"));
  assert.ok(combinationGroupMatches(shioriko10, "shioriko10"));
  assert.ok(!combinationGroupMatches([...initial9, "mia"], "initial9"));
  assert.ok(!combinationGroupMatches([...anime10, "shioriko"], "anime10"));
  assert.ok(!combinationGroupMatches([...shioriko10, "yu"], "shioriko10"));
  assert.ok(!combinationGroupMatches(initial9.slice(0, -1), "initial9"));
  assert.deepEqual(automaticCombinationGroupIds([...initial9, "mia", "lanzhu"]), []);
  assert.deepEqual(
    COLLABO_COMBINATION_GROUPS.map((group) => group.label),
    ["初始9人", "动画一期10人", "栞子加入后10人"],
  );
});

test("legacy rejected flags no longer hide a cover or gallery image", () => {
  const item = normalizeItem({
    id: "one",
    cover_image_id: "a",
    images: [
      { id: "a", url: "/media/a.png", review_status: "rejected" },
      { id: "b", url: "/media/b.png", thumbnail_url: "/media/b-small.webp", review_status: "approved" },
    ],
  });
  assert.equal(coverUrl(item), "/media/a.png");
  assert.equal(item.images.length, 2);
  assert.equal(normalizeImage({ url: "javascript:alert(1)" }).url, "");
});

test("filter and pagination inputs are bounded and ordering is deterministic", () => {
  const items = [
    { id: "b", date: "2025-01-01", title: "Art", partners: ["Partner"], note: "" },
    { id: "a", date: "2025-01-01", title: "Art", partners: ["Partner"], note: "" },
    { id: "c", date: "2026-01-01", title: "Other", partners: [], note: "" },
  ];
  assert.deepEqual(
    filterItems(items, { q: "PARTNER", year: "2025" }).map((item) => item.id),
    ["a", "b"],
  );
  assert.deepEqual(
    filterItems(items, { year: "2025,2026" }).map((item) => item.id),
    ["c", "a", "b"],
  );
  assert.deepEqual(yearValues(["2025", "2025,2026", "invalid"]), ["2025", "2026"]);
  for (const value of ["-1", "0", "NaN", "1.5", "Infinity"]) assert.equal(pageNumber(value), 1);
  assert.equal(pageNumber("3"), 3);
});

test("swipe navigation requires a clear, short horizontal gesture", () => {
  const start = { x: 200, y: 200, time: 0 };
  assert.equal(swipeDirection(start, { x: 100, y: 205, time: 200 }), 1);
  assert.equal(swipeDirection(start, { x: 300, y: 205, time: 200 }), -1);
  assert.equal(swipeDirection(start, { x: 140, y: 205, time: 200 }), 0);
  assert.equal(swipeDirection(start, { x: 300, y: 280, time: 200 }), 0);
  assert.equal(swipeDirection(start, { x: 300, y: 200, time: 1000 }), 0);
  assert.equal(swipeDirection(null, start), 0);
});
