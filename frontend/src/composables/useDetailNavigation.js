import { onBeforeUnmount, onMounted } from "vue";

const interactive =
  "a,button,input,textarea,select,[contenteditable]:not([contenteditable='false']),[data-no-detail-swipe]";

export function swipeDirection(start, end) {
  if (!start || !end) return 0;
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  if (Math.abs(dx) < 64 || Math.abs(dx) < Math.abs(dy) * 1.8 || end.time - start.time > 900) return 0;
  return dx < 0 ? 1 : -1;
}

// Shared by release and collaboration pages. Never capture pinch or browser-edge navigation.
export function useDetailNavigation({ previous, following, enabled = () => true }) {
  let start = null;
  function keydown(event) {
    if (
      !enabled() ||
      event.defaultPrevented ||
      event.repeat ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey ||
      event.target?.closest?.(interactive) ||
      window.getSelection()?.toString()
    )
      return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      (event.key === "ArrowLeft" ? previous : following)();
    }
  }
  function onTouchStart(event) {
    start = null;
    if (!enabled() || event.touches.length !== 1 || event.target?.closest?.(interactive)) return;
    const touch = event.touches[0];
    if (touch.clientX < 28 || touch.clientX > window.innerWidth - 28) return;
    start = { x: touch.clientX, y: touch.clientY, time: performance.now(), id: touch.identifier };
  }
  function onTouchMove(event) {
    if (!start) return;
    const touch = event.touches[0];
    if (event.touches.length !== 1 || Math.abs(touch.clientY - start.y) > 32) start = null;
  }
  function onTouchCancel() {
    start = null;
  }
  function onTouchEnd(event) {
    const origin = start;
    start = null;
    if (!origin || !enabled() || event.touches.length || window.getSelection()?.toString()) return;
    const touch = [...event.changedTouches].find((item) => item.identifier === origin.id);
    if (!touch) return;
    const direction = swipeDirection(origin, { x: touch.clientX, y: touch.clientY, time: performance.now() });
    if (direction) (direction === 1 ? following : previous)();
  }
  onMounted(() => window.addEventListener("keydown", keydown));
  onBeforeUnmount(() => window.removeEventListener("keydown", keydown));
  return { onTouchStart, onTouchMove, onTouchEnd, onTouchCancel };
}
