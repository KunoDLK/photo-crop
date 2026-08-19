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

## Open from the command line

A browser can't read arbitrary file paths, so `open.py` serves the scan over localhost and
opens it in the app (requires Python 3):

```bash
python3 open.py /path/to/scan.png
```

`index.html` also accepts a `?file=` query parameter (a path relative to the page), so any
static server works too:

```bash
python -m http.server 8000
# open http://localhost:8000/?file=scan.png
```

For a scan-then-crop workflow, e.g.:

```bash
scanimage --format png --resolution 1200 > scan.png && python3 open.py scan.png
```

A ready-made `scan-crop` command (install: `ln -s "$PWD/scan-crop" ~/.local/bin/scan-crop`)
scans the Epson ET-7750 at 1200dpi color into `~/Docker-Server/copyparty/data/private/Photos/Scans/Ingest/`
(override with `SCAN_CROP_INGEST`) and opens the result in Chrome. `open.py` exits by itself
once the page has loaded, so the terminal returns.

## Usage

1. **Open** — drag & drop a scan onto the window, or click *Open image…*.
2. **Zoom / pan** — mouse wheel zooms (anchored at the cursor); Ctrl+drag pans.
3. **Boxes** — drag empty canvas to draw a box; click to select; drag inside to move; drag
   the 8 handles to resize; drag the rotation handle (or use the list buttons) to rotate the
   rectangle over the source image; the crop's short/long dimensions stay unchanged while
   the image underneath is re-oriented to the rectangle before export;
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
