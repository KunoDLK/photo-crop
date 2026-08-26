"""Server-rendered admin HTML pages.

Plain HTML + CSS with no build step and no required JavaScript: every mutation
is a standard POST form carrying a hidden per-session CSRF field. The pages are
intentionally spartan — this is a maintenance UI for the owner, not a product
surface.
"""
from __future__ import annotations

from html import escape

SITE = "Hyper.K Archive"

KIND_LABELS = {
    "unknown": "Unknown",
    "estate": "Estate",
    "publisher": "Publisher",
    "company": "Company",
}

COPYRIGHT_KINDS = [
    ("editor", "editor"),
    ("holder", "rights holder / publisher"),
    ("ad", "advertisement (28 years)"),
]


def esc(text) -> str:
    """Escape text for use in HTML (attributes and text nodes)."""
    return escape(str(text), quote=True)


_CSS = """
body { font-family: system-ui, sans-serif; margin: 0; background: #f4f4f5; color: #18181b; }
header { background: #18181b; color: #fff; padding: 10px 18px; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
header a { color: #a5b4fc; text-decoration: none; }
header a.active { color: #fff; text-decoration: underline; }
header button { background: none; border: none; color: #fca5a5; cursor: pointer; font-size: 14px; }
main { max-width: 980px; margin: 24px auto; padding: 0 16px; }
h1 { font-size: 20px; }
table { border-collapse: collapse; width: 100%; background: #fff; }
th, td { border: 1px solid #d4d4d8; padding: 6px 8px; text-align: left; font-size: 14px; vertical-align: top; }
th { background: #e4e4e7; }
input, select { padding: 3px 6px; }
button { padding: 3px 12px; cursor: pointer; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; }
.public { background: #dcfce7; color: #166534; }
.private { background: #fee2e2; color: #991b1b; }
.error { color: #b91c1c; font-weight: 600; }
.note { color: #52525b; font-size: 13px; }
h2 { font-size: 16px; margin-top: 26px; }
hr { border: 0; border-top: 1px solid #d4d4d8; margin: 18px 0; }
.form-row { margin: 6px 0; }
"""


# Preserve scroll position across the PRG redirects every admin form uses: the
# offset is stashed on submit and restored once the reloaded page settles, so
# saving a page's rights row doesn't throw the owner back to the top. Graceful
# degradation — with JS disabled the forms still work, just without the restore.
_SCROLL_JS = """
<script>
(function () {
  var KEY = "admin-scroll:" + location.pathname + location.search;
  try {
    var saved = sessionStorage.getItem(KEY);
    if (saved !== null) {
      sessionStorage.removeItem(KEY);
      if ("scrollRestoration" in history) history.scrollRestoration = "manual";
      var y = parseInt(saved, 10) || 0;
      addEventListener("load", function () { scrollTo(0, y); });
    }
    document.addEventListener("submit", function (e) {
      if (e.defaultPrevented) return;
      sessionStorage.setItem(KEY, String(window.scrollY));
    });
  } catch (err) {}
})();
</script>
"""


def _shell(title: str, body: str, active: str | None = None, csrf: str = "", nav: bool = True) -> str:
    """Wrap page body in the shared admin shell with the section nav."""
    nav_html = ""
    if nav:
        items = [
            ("books", "/admin/books", "Books"),
            ("editors", "/admin/editors", "Editors"),
            ("holders", "/admin/holders", "Rights holders"),
            ("pages", "/admin/pages", "Page rights"),
            ("users", "/admin/users", "Users"),
        ]
        links = "".join(
            f'<a href="{href}"{" class=\"active\"" if active == key else ""}>{label}</a>'
            for key, href, label in items
        )
        logout = (
            f'<form method="post" action="/admin/logout" style="margin-left:auto;display:inline">'
            f'<input type="hidden" name="_csrf" value="{esc(csrf)}">'
            f"<button>Log out</button></form>"
        )
        nav_html = (
            f"<header><strong>{esc(SITE)}</strong>{links}"
            f'<a href="/" style="margin-left:auto">Viewer</a>{logout}</header>'
        )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{esc(title)} · {esc(SITE)}</title><style>{_CSS}</style></head>"
        f"<body>{nav_html}<main>{body}</main>{_SCROLL_JS}</body></html>"
    )


def _options(items: list[tuple], selected=None, first: str | None = None) -> str:
    """A ``<option>`` string; ``first`` is an optional leading placeholder."""
    opts = [f'<option value="">{esc(first)}</option>'] if first is not None else []
    for value, label in items:
        sel = ' selected' if str(value) == str(selected) else ""
        opts.append(f'<option value="{esc(value)}"{sel}>{esc(label)}</option>')
    return "".join(opts)


def _editor_options(editors: list[dict], selected=None, first: str | None = None) -> str:
    items = [
        (e["id"], f"{e['name']}" + (f" (†{e['death_year']})" if e.get("death_year") else ""))
        for e in editors
    ]
    return _options(items, selected, first)


def _holder_options(holders: list[dict], selected=None, first: str | None = None) -> str:
    items = [(h["id"], h["name"]) for h in holders]
    return _options(items, selected, first)


def _visibility_badge(visibility: str) -> str:
    cls = "public" if visibility == "public" else "private"
    return f'<span class="badge {cls}">{esc(visibility)}</span>'


def _hidden_csrf(csrf: str) -> str:
    return f'<input type="hidden" name="_csrf" value="{esc(csrf)}">'


def login(error: str | None = None) -> str:
    """The owner login form (no session exists yet, so no CSRF field)."""
    error_html = f'<p class="error">{esc(error)}</p>' if error else ""
    body = (
        f"<h1>Owner login</h1>{error_html}"
        '<form method="post" action="/admin/login" style="max-width:340px">'
        '<div class="form-row"><label>Username<br><input name="username" autofocus style="width:100%"></label></div>'
        '<div class="form-row"><label>Password<br><input name="password" type="password" style="width:100%"></label></div>'
        '<div class="form-row"><button>Log in</button></div>'
        "</form>"
        '<p class="note">The archive admin is protected by Cloudflare Access and '
        "this owner login. Accounts with per-book grants cannot administer it.</p>"
    )
    return _shell("Owner login", body, nav=False)


def index(csrf: str, counts: dict) -> str:
    """Landing page: quick counts and links into each section."""
    rows = "".join(
        f'<tr><td><a href="/admin/{href}">{esc(label)}</a></td><td>{esc(count)}</td></tr>'
        for label, href, count in (
            ("Books on disk", "books", counts["books"]),
            ("Editors", "editors", counts["editors"]),
            ("Rights holders", "holders", counts["holders"]),
            ("Accounts", "users", counts["users"]),
        )
    )
    body = (
        "<h1>Archive admin</h1>"
        '<p class="note">Rights database: <code>rights.db</code> (books default to '
        "private, pages to blurred — make a collection public here, then assign "
        "editors to pages so their public-domain dates open them).</p>"
        f"<table><tr><th>Section</th><th>Count</th></tr>{rows}</table>"
    )
    return _shell("Archive admin", body, csrf=csrf)


def books(csrf: str, rows: list[dict], editors: list[dict], holders: list[dict]) -> str:
    """Book list with an inline edit form per book."""
    body_rows = []
    for r in rows:
        row = r["book"]
        rights_row = r.get("rights") or {}
        body_rows.append(
            "<tr>"
            f"<td><strong>{esc(row.name)}</strong><br>"
            f'<span class="note">{esc(rights_row.get("title") or row.name)}</span></td>'
            f"<td>{_visibility_badge(rights_row.get('visibility', 'private'))}</td>"
            "<td>"
            f'<form method="post" action="/admin/books/{esc(row.id)}">'
            f"{_hidden_csrf(csrf)}"
            f'<select name="visibility">'
            f'{_options([("public", "public"), ("private", "private")], rights_row.get("visibility", "private"))}'
            "</select>"
            f'<input name="publication_year" type="number" placeholder="year" '
            f'value="{esc(rights_row.get("publication_year") or "")}" style="width:70px">'
            f'<select name="editor_id">{_editor_options(editors, rights_row.get("editor_id"), "editor —")}</select>'
            f'<select name="rights_holder_id">{_holder_options(holders, rights_row.get("rights_holder_id"), "holder —")}</select>'
            "<button>Save</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    body = (
        "<h1>Books</h1>"
        '<p class="note">Flipping a book to <em>public</em> exposes its pages '
        "(blurred until each page's editor is assigned and its public-domain date "
        "has passed). The viewer and sitemap hide private books entirely.</p>"
        f"<table><tr><th>Book</th><th>Visibility</th><th>Rights</th></tr>{''.join(body_rows)}</table>"
    )
    return _shell("Books", body, active="books", csrf=csrf)


def editors_page(csrf: str, editors: list[dict]) -> str:
    """Editor CRUD: add form plus an inline edit form per editor."""
    add = (
        '<form method="post" action="/admin/editors">'
        f"{_hidden_csrf(csrf)}"
        '<input name="name" placeholder="Name" required>'
        '<input name="birth_year" type="number" placeholder="born" style="width:80px">'
        '<input name="death_year" type="number" placeholder="died" style="width:80px">'
        '<input name="notes" placeholder="Notes" style="width:220px">'
        "<button>Add editor</button>"
        "</form>"
    )
    rows = []
    for e in editors:
        rows.append(
            "<tr><td>"
            f'<form method="post" action="/admin/editors/{esc(e["id"])}">{_hidden_csrf(csrf)}'
            f'<input name="name" value="{esc(e["name"])}" required>'
            f'<input name="birth_year" type="number" value="{esc(e["birth_year"] or "")}" style="width:80px">'
            f'<input name="death_year" type="number" value="{esc(e["death_year"] or "")}" style="width:80px">'
            f'<input name="notes" value="{esc(e["notes"] or "")}" style="width:220px">'
            "<button>Save</button></form></td><td>"
            f'<form method="post" action="/admin/editors/{esc(e["id"])}/delete">{_hidden_csrf(csrf)}'
            '<button onclick="return confirm(\'Delete this editor?\')">Delete</button></form>'
            "</td></tr>"
        )
    body = (
        "<h1>Editors</h1>"
        '<p class="note">The death year is the access key: for UK/EU viewers a '
        "page opens when death year + 70 has passed; the US uses publication year.</p>"
        f"<h2>Add editor</h2>{add}"
        f"<h2>Editors</h2><table><tr><th>Editor</th><th></th></tr>{''.join(rows)}</table>"
    )
    return _shell("Editors", body, active="editors", csrf=csrf)


def holders_page(csrf: str, holders: list[dict]) -> str:
    """Rights-holder CRUD: add form plus an inline edit form per holder."""
    add = (
        '<form method="post" action="/admin/holders">'
        f"{_hidden_csrf(csrf)}"
        '<input name="name" placeholder="Name" required>'
        f'<select name="kind">{_options([(k, v) for k, v in KIND_LABELS.items()], "unknown")}</select>'
        '<input name="contact" placeholder="Contact" style="width:200px">'
        '<input name="notes" placeholder="Notes" style="width:200px">'
        "<button>Add holder</button>"
        "</form>"
    )
    rows = []
    for h in holders:
        rows.append(
            "<tr><td>"
            f'<form method="post" action="/admin/holders/{esc(h["id"])}">{_hidden_csrf(csrf)}'
            f'<input name="name" value="{esc(h["name"])}" required>'
            f'<select name="kind">{_options([(k, v) for k, v in KIND_LABELS.items()], h.get("kind", "unknown"))}</select>'
            f'<input name="contact" value="{esc(h["contact"] or "")}" style="width:200px">'
            f'<input name="notes" value="{esc(h["notes"] or "")}" style="width:200px">'
            "<button>Save</button></form></td><td>"
            f'<form method="post" action="/admin/holders/{esc(h["id"])}/delete">{_hidden_csrf(csrf)}'
            '<button onclick="return confirm(\'Delete this holder?\')">Delete</button></form>'
            "</td></tr>"
        )
    body = (
        "<h1>Rights holders</h1>"
        "<h2>Add rights holder</h2>"
        f"{add}"
        f"<h2>Rights holders</h2><table><tr><th>Holder</th><th></th></tr>{''.join(rows)}</table>"
    )
    return _shell("Rights holders", body, active="holders", csrf=csrf)


def page_chooser(csrf: str, books: list[dict]) -> str:
    """Pick a book to walk: every page with an editor override dropdown."""
    opts = _options([(b.id, b.name) for b in books], first="— choose a book —")
    body = (
        "<h1>Page rights</h1>"
        '<p class="note">Walk one book at a time: set a whole-book default editor, '
        "bulk-assign every page, then override individual pages. A page opens to "
        "full quality once its assigned editor's public-domain date has passed in "
        "the viewer's region.</p>"
        f'<form method="get" action="/admin/pages"><select name="book">{opts}</select>'
        "<button>Open</button></form>"
    )
    return _shell("Page rights", body, active="pages", csrf=csrf)


def page_rights(
    csrf: str, book_id: str, pages: list[dict], editors_by_page: dict[str, list[dict]],
    book_row: dict | None, editors: list[dict], kinds: dict[str, str],
    defaults: dict[str, str],
) -> str:
    """One book's page-rights screen: default, bulk, and per-page overrides.

    Per page the owner can pick which copyright the page falls under (the
    book's editor(s), the rights holder / publisher, or an advertisement's
    28-year protection) and attach one or more editors — in the UK/EU a page
    opens once the LAST editor's death year + 70 has passed.

    The per-page "Default access" selects mark images the owner made themselves
    as ``public`` (open to everyone, real tiles edge-cacheable) — unless an
    editor rule on the page governs it instead. ``block`` is the safe default.
    """
    default_editor = book_row.get("editor_id") if book_row else None

    def current_editor_name(page_id: str) -> str:
        page_editors = editors_by_page.get(page_id) or []
        if page_editors:
            names = ", ".join(e["name"] for e in page_editors)
            return f"<strong>{esc(names)}</strong> (page rule)"
        if default_editor is not None:
            match = next((e for e in editors if e["id"] == default_editor), None)
            return f"<em>{esc(match['name']) if match else default_editor}</em> (book default)"
        return '<span class="note">—</span>'

    # Clear sentinel: "" = clear the per-page editor set (fall back to the
    # book default), values = replace the set with exactly those editors.
    def editor_options(page_id: str, fid: str) -> str:
        selected = {e["id"] for e in (editors_by_page.get(page_id) or [])}
        opts = ['<option value="">— none (book default) —</option>']
        for e in editors:
            label = e["name"] + (f" (†{e['death_year']})" if e.get("death_year") else "")
            sel = " selected" if e["id"] in selected else ""
            opts.append(f'<option value="{e["id"]}"{sel}>{esc(label)}</option>')
        return (
            f'<select name="editors_{esc(page_id)}" form="{fid}" multiple size="3">'
            f"{''.join(opts)}</select>"
        )

    # Each row is its own form so one page can be saved without touching the
    # rest: the row's Save button owns a <form id="pr-N"> while the controls
    # in the other cells associate with it via the `form` attribute (valid
    # HTML5; the server handler applies only the fields that are present).
    page_rows = []
    for i, p in enumerate(pages):
        fid = f"pr-{i}"
        kind = kinds.get(p.page_id, "editor")
        default = defaults.get(p.page_id, "block")
        page_rows.append(
            "<tr>"
            f"<td><code>{esc(p.page_id)}</code></td>"
            f"<td>{current_editor_name(p.page_id)}</td>"
            f"<td>{editor_options(p.page_id, fid)}</td>"
            f'<td><select name="kind_{esc(p.page_id)}" form="{fid}">'
            f"{_options(COPYRIGHT_KINDS, kind)}"
            "</select></td>"
            f'<td><select name="default_{esc(p.page_id)}" form="{fid}">'
            f"{_options([('block', 'block'), ('public', 'public')], default)}"
            "</select></td>"
            "<td>"
            f'<form id="{fid}" method="post" action="/admin/pages/{esc(book_id)}">'
            f"{_hidden_csrf(csrf)}"
            '<button title="Save this page only">Save</button></form>'
            "</td>"
            "</tr>"
        )

    default_form = (
        '<form method="post" action="' + f"/admin/pages/{esc(book_id)}/default" + '">'
        f"{_hidden_csrf(csrf)}"
        f'<select name="editor_id">{_editor_options(editors, default_editor, "— none —")}</select>'
        "<button>Save default editor</button>"
        "</form>"
    )
    bulk_form = (
        '<form method="post" action="' + f"/admin/pages/{esc(book_id)}/bulk" + '">'
        f"{_hidden_csrf(csrf)}"
        f'<select name="editor_id">{_editor_options(editors, None, "— none (clear all) —")}</select>'
        f'<select name="kind">{_options(COPYRIGHT_KINDS, None, "— copyright kind: keep —")}</select>'
        f'<select name="default_access">{_options([("block", "block"), ("public", "public")], None, "— default access: keep —")}</select>'
        f"<button>Set for all {len(pages)} pages</button>"
        "</form>"
    )
    per_page_form = (
        "<table><tr><th>Page</th><th>Current rule</th><th>Editors</th>"
        f"<th>Copyright</th><th>Default access</th><th></th></tr>"
        f"{''.join(page_rows)}</table>"
    )
    body = (
        f"<h1>Page rights · {esc(book_id)}</h1>"
        '<p><a href="/admin/pages">← choose another book</a></p>'
        '<p class="note">Copyright per page: <code>editor</code> (named '
        "editor(s); UK/EU opens after the <em>last</em> editor's death + 70, "
        "US follows the publication-year rule), <code>rights holder / "
        "publisher</code> (fixed term from publication: +70 UK/EU, +95 US), or "
        "<code>advertisement</code> (28 years from publication in every zone — "
        "the book's copyright notice does not cover ads). Hold Ctrl to pick "
        "several editors in a box; select “none” to clear the per-page set and "
        "fall back to the book default. <code>Default access: public</code> "
        "marks images you own as open to everyone — unless a rule governs the "
        "page instead; <code>block</code> (the default) stays locked until a "
        "rule grants access.</p>"
        f"<h2>Whole-book default editor</h2>{default_form}"
        f"<h2>Bulk assign</h2>{bulk_form}"
        f"<h2>Per-page overrides</h2>{per_page_form}"
    )
    return _shell(f"Page rights · {book_id}", body, active="pages", csrf=csrf)


def users(csrf: str, users: list[dict], books: list[dict], grants: dict[int, set[str]]) -> str:
    """Account CRUD: create, reset password, set per-book grants, delete."""
    add = (
        '<form method="post" action="/admin/users">'
        f"{_hidden_csrf(csrf)}"
        '<input name="username" placeholder="Username" required>'
        '<input name="password" type="password" placeholder="Password" required>'
        "<button>Create account</button>"
        "</form>"
    )
    rows = []
    for u in users:
        granted = grants.get(u["id"], set())
        checks = "".join(
            f'<label style="white-space:nowrap"><input type="checkbox" name="book_{esc(b.id)}"'
            f'{" checked" if b.id in granted else ""}> {esc(b.name)}</label><br>'
            for b in books
        )
        rows.append(
            "<tr>"
            f"<td><strong>{esc(u['username'])}</strong><br><span class=\"note\">created {esc(u['created'])}</span></td>"
            f"<td>{esc(', '.join(sorted(granted))) or '<span class=\"note\">none</span>'}</td>"
            "<td>"
            f'<form method="post" action="/admin/users/{esc(u["id"])}/password">{_hidden_csrf(csrf)}'
            '<input name="password" type="password" placeholder="New password">'
            "<button>Reset password</button></form>"
            f'<form method="post" action="/admin/users/{esc(u["id"])}/grants">{_hidden_csrf(csrf)}'
            f"{checks}<button>Save grants</button></form>"
            f'<form method="post" action="/admin/users/{esc(u["id"])}/delete">{_hidden_csrf(csrf)}'
            '<button onclick="return confirm(\'Delete this account?\')">Delete</button></form>'
            "</td>"
            "</tr>"
        )
    body = (
        "<h1>Accounts</h1>"
        '<p class="note">Accounts see private books they are granted, in full, '
        "regardless of region; everything else follows the region/date rules.</p>"
        f"<h2>Create account</h2>{add}"
        f"<h2>Accounts</h2><table><tr><th>Account</th><th>Grants</th><th>Actions</th></tr>{''.join(rows)}</table>"
    )
    return _shell("Accounts", body, active="users", csrf=csrf)
