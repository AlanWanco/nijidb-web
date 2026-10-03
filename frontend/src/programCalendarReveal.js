// Reveal existing calendar nodes, rather than repeatedly inserting events and
// resizing the whole month. Off-screen nodes retain their layout space.
export function createCalendarReveal(view = globalThis) {
  const seen = new Set();
  const nodes = new Map();
  const motion = view.matchMedia?.("(prefers-reduced-motion: reduce)");
  let destroyed = false;

  function clean(element, entry) {
    element.classList.remove("program-event-reveal-pending", "program-event-enter");
    element.style.removeProperty("--program-event-enter-delay");
    if (entry?.end) element.removeEventListener("animationend", entry.end);
    if (entry?.focus) element.removeEventListener("focusin", entry.focus);
  }
  function show(element, animate = false, index = 0) {
    const entry = nodes.get(element);
    if (!entry) return;
    observer?.unobserve(element);
    clean(element, entry);
    seen.add(entry.key);
    if (animate && !motion?.matches) {
      element.style.setProperty("--program-event-enter-delay", `${Math.min(index, 5) * 18}ms`);
      element.classList.add("program-event-enter");
      entry.end = () => clean(element, entry);
      element.addEventListener("animationend", entry.end, { once: true });
    }
    entry.pending = false;
  }
  const observer = typeof view.IntersectionObserver === "function" ? new view.IntersectionObserver(entries => {
    const visible = entries.filter(entry => entry.isIntersecting && nodes.get(entry.target)?.pending)
      .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top || a.boundingClientRect.left - b.boundingClientRect.left);
    visible.forEach((entry, index) => show(entry.target, true, index));
  }, { threshold: 0, rootMargin: "0px" }) : null;

  function motionChanged() {
    if (motion?.matches) nodes.forEach((entry, element) => show(element));
  }
  if (motion?.addEventListener) motion.addEventListener("change", motionChanged);
  else motion?.addListener?.(motionChanged);

  return {
    register(element, key) {
      if (destroyed) return;
      const previous = nodes.get(element);
      if (previous) clean(element, previous);
      const entry = { key, pending: false, end: null, focus: null };
      nodes.set(element, entry);
      if (!observer || motion?.matches || seen.has(key)) return show(element);
      entry.pending = true;
      entry.focus = () => show(element, true);
      element.addEventListener("focusin", entry.focus);
      element.classList.add("program-event-reveal-pending");
      observer.observe(element);
    },
    unregister(element) {
      observer?.unobserve(element);
      clean(element, nodes.get(element));
      nodes.delete(element);
    },
    reset() {
      observer?.disconnect();
      nodes.forEach((entry, element) => clean(element, entry));
      nodes.clear();
      seen.clear();
    },
    destroy() {
      this.reset();
      destroyed = true;
      if (motion?.removeEventListener) motion.removeEventListener("change", motionChanged);
      else motion?.removeListener?.(motionChanged);
    },
  };
}
