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

/** Read a CSS custom property value from the document root. */
export function getCss(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#000";
}

/** Read a query parameter (defaults to ""). */
export function queryParam(name) {
  return new URLSearchParams(location.search).get(name) || "";
}
