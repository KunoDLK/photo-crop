# RightsUpdate — spec: regional access control, rights DB, dark-blur gating, admin, accounts

Status: proposal (awaiting implementation).

## Goals

1. Detect the client's region server-side (Cloudflare `CF-IPCountry` header; `DEFAULT_REGION` fallback for local dev).
2. Maintain a rights database (SQLite) that organises editors (death years), rights holders, book visibility, per-page allow rules, and viewer accounts with per-book grants.
3. Serve dark-blurred images (blurry colour masses, no detail) for any page the requester is not allowed to view in full, with correctly sized "Unavailable in your region until <date>" text rendered over the blur.
4. A separate `/admin` page (server-rendered CRUD) to manage all of the above; protected by Cloudflare Access at the network layer **and** the app-level owner login.
5. An app-level login so the owner (and granted accounts) can view the collection, including private content.
6. Keep SEO/OG working for public books: `full` and `blurred` pages are indexable; OCR text and image previews are never exposed to requests without full access.

## Access model

Two orthogonal axes. All enforcement is server-side; the client only renders UX. Fail-closed defaults.

### Book visibility (per book / collection)

| Value | Meaning |
|---|---|
| `public` | Everyone can at least see the blurred version of its pages. Indexed by search engines. |
| `private` | **Does not exist** to anyone not logged in: no tiles, no OCR, no OG image, no sitemap entry, no SEO content, `X-Robots-Tag: noindex, nofollow`, share URLs render an empty shell. Default. |

### Page access (per image, whitelist only)

Resolved per `(viewer, region, date)` → `full` or `blurred`. **Default: blurred.**

A page is `full` only when an allow rule exists whose PD date has passed in the viewer's zone:

```
image X -> Editor X {death 1955}   =>   PD date = 1955 + 70 = 1 Jan 2025
                                        -> full for UK/EU viewers from 2025 on
```

- No rule ⇒ blurred.
- The decision is recomputed from `date.today()` on every request, so pages flip to `full` automatically on their PD date (no manual step).
- The old "regional hidden/block" concept is dropped: a viewer in a region with no allow rule simply gets the dark blur, like everyone else.

### Viewers

| Role | Access |
|---|---|
| `owner` | Env password (`ARCHIVE_PASSWORD` / `ARCHIVE_USERNAME`). Full access to everything, including private books. |
| `account` | Full access to books it is granted (`user_grants`), including private ones. Region/date rules apply to everything else. |
| `anonymous` | Region/date rules on public books only. Private books are invisible. |

### Defaults

- New/unknown book → `private`.
- Page with no rights data → `blurred`.
- Making a collection public and pages fully accessible is an explicit admin action.

## Rights database (`server/rights/store.py`)

SQLite at `cache_dir/rights.db` (stdlib `sqlite3`; survives restarts like `locations.json`; keep out of git; back up with the tile-cache volume).

| Table | Columns | Purpose |
|---|---|---|
| `editors` | id, name, birth_year, death_year, notes | Death year is the access key. |
| `rights_holders` | id, name, kind (estate/publisher/company/unknown), contact, notes | Attached at book level. |
| `books` | id (= book dir name), title, visibility (public/private), editor_id, rights_holder_id, publication_year, notes | Book-level metadata and visibility. |
| `page_rights` | book_id, page_name (NULL = whole book), editor_id, notes | **The allow rule.** Row present = whitelist grant; absent = blurred. |
| `users` | id, username, password_hash (pbkdf2, stdlib), created | Viewer accounts. |
| `user_grants` | user_id, book_id | Per-book account overrides. |

Resolution order for a page's rule: exact `page_rights` row → book-level editor → default `blurred`.

## Policy core (`server/rights/geo.py`, `rules.py`, `policy.py`)

- `geo.region_of(request) -> ISO 3166-1 alpha-2`: `CF-IPCountry` request header (present because production traffic goes through the Cloudflare tunnel); `DEFAULT_REGION` env fallback; per-IP in-memory TTL cache; unknown → `unknown` zone.
- Zone map (country → policy zone): `US → us`, `GB/IE → uk`, EU member states → `eu`, anything else → `unknown`.
- `rules.pd_year(editor, zone) -> int | None`:
  - `uk` / `eu`: death year + 70 (CDPA 1988 / life + 70).
  - `us`: publication year ≤ 1929 → public domain (95-year rule), else never from an editor alone.
  - `unknown`: never (fail closed).
- `policy.resolve(viewer, book, page, zone, today) -> {status, until?}`:
  1. viewer is `owner` → `full`.
  2. book `private`: viewer granted (`user_grants` or owner) → `full`; else → `nonexistent`.
  3. book `public`: allow rule present and `pd_year <= today.year` for the zone → `full`; else → `blurred` (`until` = 1 Jan of `pd_year` when one exists).

## Dark-blur pipeline + tile-cache flag (`server/tiles/`)

- `TileService.get_blur_tile(...)`: identical geometry path to a real tile (mipmap → crop → resample), then heavy Gaussian blur + dark multiply overlay (`blur_strength`, `blur_darkness` settings) → progressive JPEG. Blurred pages keep the viewer's pan/zoom; no detail survives, only colour masses.
- **Cache flag**: `TileCache.key()` gains a `blur: bool` argument — keys become `t/{book}/{page}/{version}/{level}/{tx}/{ty}` (real) vs `x/{book}/...` (dark-blur tile). The flag lives in the key because diskcache values are opaque bytes. The router refuses:
  - a real tile to a `blurred` request, and
  - a blur tile to a `full` request,
  so a real tile cached from an owner's visit can never leak to an anonymous viewer, and a blur tile is never served as content.
- **`/tiles/...` router**: policy → `full`: `get_tile`; `blurred`: `get_blur_tile`; `nonexistent`: 404 (indistinguishable from a missing page).
- **Text overlay** — "Unavailable in your region until 1 Jan 2026" (no date when the zone grants nothing):
  - Client-side in the viewer: DOM labels sized to each blurred image's on-screen box, reusing the OCR-overlay transform machinery (per-tile text would repeat and misalign).
  - Server-side baked into the OG/SEO preview image (see below), sized to fit using measured font metrics.

## Image info + listings (`server/models.py`, `server/books/router.py`)

- `PageInfo` and `ImageInfo` gain `access: {status, zone, until?}`.
- `BookSummary` gains `visibility`.
- `/api/books` filters private books for anonymous viewers; logged-in viewers see them with a badge.
- `/api/books/{book}/pages` → 404 for anonymous viewers on private books.
- `/api/books/{book}/pages/{page}/info` carries the access field (the client renders blur labels/badges with no extra calls).

## Gating everywhere content leaks

- **OCR** (`server/ocr/router.py`): served only for `full`/owner; `blurred` and `nonexistent` → 404. The text of a page is itself a copy of the work.
- **Search** (`server/ocr/router.py`): results filtered to pages the requester can see fully.
- **OG previews** (`server/social.py`): `full` → real preview; `blurred` → blurred preview with baked "Unavailable…" text; `nonexistent` → 404. Preview cache keys include the resolved status so each variant stays content-addressed and immutable.
- **SEO fragments** (`server/pages.py`): public books keep full SEO; blurred pages get the blurred preview and **no OCR text** in the HTML fragment; private locations render an empty fragment.
- **Sitemap** (`server/social.py`): includes the root, all public books, and all their pages (blurred or full); excludes every private book/page.
- **Owner/account bypass**: a `current_viewer` dependency (parses the session cookie) is checked before any policy call; owner always wins.

## Admin app (`/admin`, new `server/admin/`)

Server-rendered CRUD (plain HTML + POST forms, no build step, no JS required). Reachable at `/admin`, protected by **both** Cloudflare Access (network layer) and the app-level owner session, with a CSRF token on every POST.

Sections:

- **Books**: list; flip public/private; link rights holder; set default editor; publication year.
- **Editors**: CRUD (name, birth year, death year).
- **Rights holders**: CRUD (name, kind, contact).
- **Page rights**: one screen per book — every page with an editor dropdown, a whole-book default rule, and a "set editor for all pages" bulk action. This is where a collection is walked and editors assigned to images.
- **Users**: create account, reset password, grant/revoke books.

## Auth (`server/auth/`)

- Owner: `ARCHIVE_PASSWORD` / `ARCHIVE_USERNAME` env vars; constant-time compare (`hmac.compare_digest`); login rate limit.
- Accounts: `users` table, pbkdf2-hashed passwords (stdlib only, no new dependencies).
- Session cookie: HMAC-signed, `httpOnly`, `Secure`, `SameSite=Lax`, ~30-day expiry; signing secret auto-generated and persisted to `cache_dir/secret` so sessions survive restarts.
- Endpoints:
  - `POST /api/login` `{username, password}` → session cookie
  - `POST /api/logout`
  - `GET /api/me` → `{authenticated, username, is_owner, grants}`
- Client: `login.js` (modal + lock button in the toolbar), `access.js` (region + `/api/me` fetch, banner, blur labels, private badges), new state events in `state.js`; after login the client refetches listings so private books appear.

## Files

New:

- `server/rights/store.py`, `server/rights/geo.py`, `server/rights/rules.py`, `server/rights/policy.py`, `server/rights/router.py`
- `server/auth/service.py`, `server/auth/router.py`
- `server/admin/router.py`, `server/admin/pages.py`
- `server/static/js/access.js`, `server/static/js/login.js`
- `server/scripts/import_rights.py` (CSV seed for bulk data entry)

Changed:

- `server/config.py` (new Settings fields)
- `server/app.py` (state + routers)
- `server/models.py` (access/visibility fields)
- `server/books/router.py` (private filtering, access in listings/info)
- `server/tiles/cache.py`, `server/tiles/manager.py`, `server/tiles/router.py` (blur flag + blur tiles)
- `server/ocr/router.py` (gating)
- `server/social.py`, `server/pages.py` (OG/SEO/sitemap gating)
- `server/static/index.html`, `server/static/js/main.js`, `server/static/js/state.js`, `server/static/js/layout.js`, `server/static/css/viewer.css`

## Config additions (`server/config.py`)

`rights_db_path`, `archive_username`, `archive_password`, `session_secret` (or auto-persist), `default_region`, `blur_strength`, `blur_darkness`, `login_rate_limit`, zone map. All env-driven like the existing settings.

## Phases

1. Rights DB + auth (users, grants, owner, `/api/me`)
2. Policy core + access on listings/info
3. Blur tile pipeline + cache flag + text overlays + gating (tiles/og/ocr/search/sitemap/html)
4. `/admin` CRUD
5. Client UX (access.js, login.js, labels, private badges)

Verification per phase: `bun build server/static/js/main.js --outdir /tmp/check --target browser` (JS smoke test), `python -m server.main` + curl checks with a dev-only test-region header, `sqlite3` inspection of `rights.db`.

## Legal notes (not legal advice)

- Serving a blurred image is still a derivative of the work in most jurisdictions; the blur must reveal no detail (spec: colour masses only). Confirm with the rights holder / counsel before relying on it.
- OCR text is a full copy of the work and is therefore gated identically to the image (never served for `blurred` or `private`).
- The whitelist model means an unconfigured archive is invisible (private books) and unconfigured pages are blurred — safe by default.
- `unknown` regions fail closed (blurred), never open.
