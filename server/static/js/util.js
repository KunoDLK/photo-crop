/**
 * Pure, dependency-free helpers used across modules.
 */

/** Clamp a value into [a, b]. */
export function clamp(v, a, b) {
  return v < a ? a : v > b ? b : v;
}

/** Human-readable pixel count (kilo/mega/giga). */
export function formatPixels(px) {
  if (px >= 1e9) return (px / 1e9).toFixed(2) + " gigapixels";
  if (px >= 1e6) return (px / 1e6).toFixed(2) + " megapixels";
  if (px >= 1e3) return (px / 1e3).toFixed(2) + " kilopixels";
  return px + " pixels";
}

/** Human-readable byte count (B/KB/MB/GB). */
export function formatBytes(n) {
  if (n >= 1 << 30) return (n / (1 << 30)).toFixed(2) + " GB";
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  if (n >= 1 << 10) return (n / (1 << 10)).toFixed(0) + " KB";
  return n + " B";
}

/** Human-readable duration: "123ms" below a second, "1.2s" below ten, else "12s". */
export function formatDuration(ms) {
  if (ms < 1000) return Math.round(ms) + "ms";
  if (ms < 10000) return (ms / 1000).toFixed(1) + "s";
  return Math.round(ms / 1000) + "s";
}

/** Read a CSS custom property value from the document root. */
export function getCss(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#000";
}

/** Read a query parameter (defaults to ""). */
export function queryParam(name) {
  return new URLSearchParams(location.search).get(name) || "";
}
