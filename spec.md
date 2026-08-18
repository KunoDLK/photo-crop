# spec.md — Client-Side Tiled Photo Crop Tool

## 1. Overview

A portable, fully client-side web application for cropping photos out of large flatbed scans (e.g. Epson ET-7750 at 1200dpi). It renders the scan with a DeepZoom-style tile pyramid so panning/zooming stays smooth on very large images, lets the user draw crop boxes (with optional auto-detection), names/rotates each crop, and exports the crops at full resolution to a folder chosen via the OS save dialog.

No server, no build step, no dependencies. The entire app ships as a single `index.html` and runs by opening the file directly or serving it statically from any host.

## 2. Non-Goals

- No backend, no persistence, no auth, no multi-user.
- No server-side tile generation. Images up to the browser canvas limits (≈268MP in Chrome; 16384px per side in Firefox/Safari) are supported. A4/A5@1200dpi scans (≤ ~195MP) are fully supported.
- No editing of image content (only crop / rotate / name / export).

## 3. Project Layout

```
Documents/repos/photo-crop/
├── index.html   # entire application (HTML + CSS + JS inline)
└── spec.md      # this document
```

`index.html` is intentionally self-contained for portability.

## 4. UI Layout

Two panels, side by side:

- **Left panel — tile canvas.** The scan is displayed as a tiled map-style viewport. A toolbar row above the canvas holds:
  - Open image (file picker)
  - Auto-detect boxes
  - Clear boxes
  - Zoom level indicator / zoom-out view (optional)
- **Right panel — crop list & export.**
  - Scrollable list of crop boxes. Each entry:
    - Live preview thumbnail (rendered from the preview level, updates on resize/rotate/move)
    - Text input for output filename
    - Rotation readout
    - Delete button
    - "Include in export" checkbox
  - Export settings:
    - Format selector: PNG / JPEG
    - Quality slider (JPEG only, 0–100, default 90)
    - Output name prefix handling (names from list; duplicates auto-suffixed with `-2`, `-3`, …)
  - **Export** button (bottom-right, prominent).

## 4a. Visual Styling

- **Canvas background:** the area around/behind the image in the tile viewport is **middle grey `rgb(128, 128, 128)`** at all zoom levels, so scans (white beds, light photos) stand out clearly.
- **Light / dark mode:** the UI follows the **system colour-scheme preference** automatically via CSS `prefers-color-scheme` (with a live `matchMedia` listener so it updates instantly if the OS theme changes). No manual theme toggle. Dark mode uses dark surfaces (panels, toolbar, list) and light text; light mode uses light surfaces and dark text. Box handles/outlines and other accent colours are chosen to be visible in both themes.

## 5. Image Loading

- Drag-and-drop onto the window, or click "Open" to use a file picker.
- Supported: PNG, JPEG, TIFF, BMP, WebP.
- On drop the `File` is read as a Blob; the original is never uploaded anywhere.

## 6. Tile Pyramid Engine

Goal: smooth, map-style zoom/pan at any level, GPU-bound, on images up to browser limits.

- **Levels:** power-of-two scale pyramid. `maxLevel = ceil(log2(max(width, height) / 256))`. World size at level `L` is `ceil(image / 2^L)`.
- **Lazy, on-demand generation (no pre-built pyramid):** the pyramid is *not* computed up front on load. Tiles are computed only when actually needed, as the user navigates:
  - The full-resolution source is decoded once into an `ImageBitmap` (required as the ultimate sampling source).
  - Any downscaled level/tile is produced on demand the first time it is requested by the viewport (e.g. fit view requests low levels; zooming in requests progressively deeper levels).
  - Downscaling uses GPU-accelerated `createImageBitmap(source, {resizeWidth, resizeHeight})` / `drawImage` resampling.
- **Tile cache:** `Tile(x, y, level)` = 256×256 `OffscreenCanvas`. Generated lazily on first request by drawing the corresponding region of the nearest already-available source level. LRU eviction (default ~200 tiles). Cache keyed `L:x:y`. Once generated, tiles are reused for subsequent frames/pans/zooms until evicted.
- **Pending-tile scheduling:** during a pan/zoom the viewport requests only the tiles for the new visible range; tiles for out-of-view regions are skipped (not generated). Generation is debounced/prioritised so interaction stays smooth; a low-res placeholder (or empty background) is shown for tiles still being produced.
- **Progressive refinement on zoom-in:** when the user zooms in beyond the currently available level, the coarser **half-size level tiles are displayed scaled up as immediate placeholders** (kept visible underneath). Finer-level tiles are generated on demand, but each coarser tile stays visible until **all** of the finer tiles that cover it have finished generating — the whole block then swaps to full detail at once, so zoom-in appears as clean per-region sharpening rather than per-tile pop-in. If no coarser tile is cached for a region, it stays on the grey background until its block is ready.
- **Viewport renderer:** for each frame, computes the visible tile range from current zoom + pan, renders visible tiles to the display canvas (or composited elements). Only ~a handful of tiles are composited per frame.
- **Zoom:** mouse wheel, anchored at the cursor (zoom factor preserved at cursor point). Zoom-in is clamped at 1:1 original pixels; zoom-out is unbounded (tiles render at their coarsest level and stop enlarging).
- **Pan:** Ctrl+drag.
- All box coordinates are stored in **world/fractional** space (0–1 of full image) so they are resolution-independent across zoom levels.

## 7. Box Editing

- **Draw:** click-drag on empty canvas → new box (top-left to bottom-right).
- **Select:** click a box (also selectable from the right list).
- **Move:** drag inside the selected box.
- **Resize:** drag the 8 handles (4 corners + 4 edge midpoints).
- **Rotate:** free-angle at fractional-degree precision (0.01° granularity; dragging further from the box centre or zooming in gives finer control). The rotation changes the orientation of the rectangle on the source image; it does not rotate an axis-aligned crop on a white canvas. A rotation handle on the selected box (grab and drag around the box centre) plus nudge buttons (±0.1°, ±90°, 180°). Rotation value shown in list.
- **Delete:** `Del`/`Backspace` key or per-item button.
- **Minimum size guard** so boxes don't collapse to zero.
- Boxes drawn as vector overlays on the viewport (crisp at any zoom); the selected box gets handles + rotation handle.

## 8. Auto-Detect (optional)

Pure-JS port of the old `scan_crop.py` detection, run on a ~2000px overview (`getImageData`):

1. Downscale overview to `DETECT_MAX` (≈1000–2000px).
2. Luminance threshold (`THRESH = 235`), non-white mask.
3. Morphological open (1-px erode + dilate) to remove specks.
4. Connected components (flood fill / union-find); filter by min area & min side.
5. Merge nearby components (`MERGE_GAP_FRAC ≈ 0.035`).
6. Expand each box by `MARGIN_FRAC ≈ 0.012` to capture print borders.
7. Emit boxes in world coordinates with default names (`photo-1`, …).

## 9. Export

- On **Export**:
  1. Re-decode the original Blob into a full-resolution `ImageBitmap` (transient; `close()` after).
  2. For each included box: keep the output dimensions equal to the box's local width and height, inverse-rotate the source image around the box centre, and clip the result to that fixed rectangle. The box's short side remains the output's short side and its long side remains the output's long side at every angle. Call `convertToBlob` → `image/png` (lossless) or `image/jpeg` with chosen quality.
  3. Save all files to the user-chosen folder.
- **Save dialog:** `showDirectoryPicker()` (File System Access API) to pick a real destination folder and write files directly to disk. Requires a secure context (served over `localhost` or opened via `file://`); on browsers without the API (Firefox/Safari) fall back to standard download prompt(s).
- Output names come from the per-box name fields; empty names default to `photo-N`; invalid filename characters sanitized; duplicates auto-suffixed.

## 10. Performance Requirements

- No full-image decode to display; preview interaction GPU-bound on 256px tiles.
- Tiles are computed lazily on demand and cached — only the tiles the user actually views are ever generated (never a whole pre-built pyramid).
- Thumbnails render from the lowest cached level, not full-res crops.
- Full-res decode occurs exactly once at load (as the tile source) and once at export.
- Target: smooth 60fps pan/zoom; no blocking on the main thread during export (async, with progress indication).

## 11. Supported Browser Scope

- Chrome/Edge (full: tiling + directory save). Recommended.
- Firefox/Safari (tiling works; canvas dimension capped at 16384px; downloads fallback for save).
- Limits respected: single image ≤ ~268MP (Chrome), ≤ 16384px per side (Firefox/Safari).

## 12. Acceptance Criteria

1. Drop a 1200dpi scan → loads, auto-fits, zoom/pan via wheel + Ctrl+drag at 60fps.
2. Draw, select, move, resize, free-rotate, and delete boxes; list reflects each box with live preview, name, rotation.
3. Auto-detect proposes sensible boxes for photos on a white bed.
4. Export PNG and JPEG (quality slider honored) at full resolution; saved to a folder chosen via save dialog.
5. Duplicate names auto-suffixed; invalid characters sanitized.
6. Runs by double-clicking `index.html` and via `python -m http.server`.
