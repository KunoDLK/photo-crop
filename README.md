# Photo Crop

A portable, fully client-side web tool for cropping photos out of large flatbed scans
(Epson ET-7750 @ 1200dpi and similar). Renders scans with a DeepZoom-style tile pyramid so
pan/zoom stays smooth on very large images, then exports crops at full resolution.

No server, no build step, no dependencies — the entire app is a single `index.html`.

## Run

Either:

- **Double-click `index.html`** (works via `file://`), or
- Serve the folder and open in a browser:

  ```bash
  python -m http.server 8000
  # open http://localhost:8000
  ```

Chrome/Edge are recommended (full tiling + folder save dialog). Firefox/Safari work for
viewing/editing (canvas capped at 16384px per side); export falls back to standard downloads.

## Usage

1. **Open** — drag & drop a scan onto the window, or click *Open image…*.
2. **Zoom / pan** — mouse wheel zooms (anchored at the cursor); Ctrl+drag pans.
3. **Boxes** — drag empty canvas to draw a box; click to select; drag inside to move; drag
   the 8 handles to resize; drag the rotation handle (or use the list buttons) to rotate;
   `Del`/`Backspace` deletes the selected box. *Auto-detect* proposes boxes for photos on a
   white scanner bed.
4. **Name & preview** — each box appears in the right-hand list with a live thumbnail and a
   filename field; uncheck *Include* to skip a crop.
5. **Export** — pick PNG or JPEG (+ quality slider), press **Export**, choose a destination
   folder (Chrome) or accept the downloads. Files are saved at full scan resolution; duplicate
   names are auto-suffixed.

## Files

- `index.html` — the whole application
- `spec.md` — design specification
