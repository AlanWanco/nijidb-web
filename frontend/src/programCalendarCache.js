// Only public month-calendar snapshots are stored; admin data/cookies never enter this cache.
const databaseName = "nijidb-program-calendar-v1";
const storeName = "windows";
const maxAge = 24 * 60 * 60 * 1000;

function clone(value) {
  return typeof structuredClone === "function" ? structuredClone(value) : JSON.parse(JSON.stringify(value));
}

export function isCalendarPayload(value) {
  return Boolean(value && Array.isArray(value.events) && value.events.length <= 10000
    && Array.isArray(value.programs) && value.programs.length <= 5000
    && value.events.every(event => event && typeof event.id === "string" && typeof event.start === "string")
    && value.programs.every(program => program && typeof program.id === "string"));
}

function matchesWindow(payload, path) {
  try {
    const params = new URL(path, "https://calendar.invalid").searchParams;
    const start = params.get("start")?.split("T", 1)[0];
    const end = params.get("end")?.split("T", 1)[0];
    if (!start || !end) return true;
    const inclusiveEnd = new Date(`${end}T00:00:00Z`);
    inclusiveEnd.setUTCDate(inclusiveEnd.getUTCDate() - 1);
    return payload.start === start && payload.end === inclusiveEnd.toISOString().slice(0, 10);
  } catch { return false; }
}

function validEntry(value, now, ageLimit) {
  return Boolean(value && value.version === 1 && Number.isFinite(value.savedAt)
    && now >= value.savedAt && now - value.savedAt < ageLimit && isCalendarPayload(value.payload));
}

function safeEtag(value) {
  return /^(?:W\/)?"[A-Za-z0-9._:-]{1,128}"$/.test(String(value || "")) ? value : null;
}

export function createCalendarCache({ storage, now = Date.now, maxEntries = 6, ageLimit = maxAge, timeout = 120 } = {}) {
  const memory = new Map();
  let opening;
  function factory() {
    try { return storage === undefined ? globalThis.indexedDB : storage; } catch { return null; }
  }
  function open() {
    if (opening) return opening;
    opening = new Promise(resolve => {
      let settled = false;
      const done = value => {
        if (settled) { value?.close?.(); return; }
        settled = true;
        clearTimeout(timer);
        resolve(value);
      };
      const timer = setTimeout(() => done(null), timeout);
      try {
        const request = factory()?.open(databaseName, 1);
        if (!request) return done(null);
        request.onupgradeneeded = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains(storeName)) db.createObjectStore(storeName, { keyPath: "key" });
        };
        request.onerror = () => done(null);
        request.onblocked = () => done(null);
        request.onsuccess = () => {
          const db = request.result;
          db.onversionchange = () => { db.close(); opening = undefined; };
          done(db);
        };
      } catch { done(null); }
    });
    return opening;
  }
  async function operation(mode, action) {
    const db = await open();
    if (!db) return null;
    return new Promise(resolve => {
      let result = null;
      let settled = false;
      let transaction;
      const done = value => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      };
      const timer = setTimeout(() => {
        try { transaction?.abort(); } catch {}
        done(null);
      }, timeout);
      try {
        transaction = db.transaction(storeName, mode);
        transaction.oncomplete = () => done(result);
        transaction.onerror = transaction.onabort = () => done(null);
        action(transaction.objectStore(storeName), value => { result = value; });
      } catch { done(null); }
    });
  }
  function remember(key, entry) {
    memory.delete(key);
    memory.set(key, entry);
    while (memory.size > maxEntries) memory.delete(memory.keys().next().value);
  }
  return {
    async get(key) {
      let entry = memory.get(key);
      if (!validEntry(entry, now(), ageLimit)) {
        memory.delete(key);
        entry = await operation("readonly", (store, done) => {
          const request = store.get(key);
          request.onsuccess = () => done(request.result);
        });
      }
      if (!validEntry(entry, now(), ageLimit) || !matchesWindow(entry.payload, key)) return null;
      entry.etag = safeEtag(entry.etag);
      remember(key, entry);
      return clone(entry);
    },
    async put(key, entry) {
      if (!isCalendarPayload(entry.payload) || !matchesWindow(entry.payload, key)) return;
      const stored = { key, version: 1, savedAt: entry.savedAt ?? now(), etag: safeEtag(entry.etag), payload: clone(entry.payload) };
      remember(key, stored);
      await operation("readwrite", (store, done) => {
        store.put(stored);
        const request = store.getAll();
        request.onsuccess = () => {
          const rows = request.result.filter(row => validEntry(row, now(), ageLimit)).sort((a, b) => b.savedAt - a.savedAt || (a.key === key ? -1 : b.key === key ? 1 : 0));
          const retained = new Set(rows.slice(0, maxEntries).map(row => row.key));
          request.result.forEach(row => { if (!retained.has(row.key)) store.delete(row.key); });
          done(true);
        };
      });
    },
    async remove(key) {
      memory.delete(key);
      await operation("readwrite", (store, done) => { store.delete(key); done(true); });
    },
  };
}

export const calendarCache = createCalendarCache();

export async function requestCalendarSnapshot(path, { cached = null, signal, fetcher = globalThis.fetch, now = Date.now } = {}) {
  const headers = { Accept: "application/json" };
  const tag = safeEtag(cached?.etag);
  if (tag) headers["If-None-Match"] = tag;
  const response = await fetcher(path, { headers, signal, credentials: "same-origin", cache: "no-cache" });
  if (response.status === 304 && cached && isCalendarPayload(cached.payload) && matchesWindow(cached.payload, path)) {
    return { entry: { ...cached, etag: safeEtag(response.headers.get("etag")) || tag, savedAt: now() }, notModified: true, cacheable: true };
  }
  if (response.status === 304) throw new Error("日历缓存已失效，请重新加载");
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(payload?.detail || "日历加载失败");
    error.status = response.status;
    throw error;
  }
  if (!isCalendarPayload(payload) || !matchesWindow(payload, path)) throw new Error("日历数据格式无效");
  return {
    entry: { version: 1, payload, etag: safeEtag(response.headers.get("etag")), savedAt: now() },
    notModified: false,
    cacheable: !response.headers.get("cache-control")?.toLowerCase().includes("no-store"),
  };
}
