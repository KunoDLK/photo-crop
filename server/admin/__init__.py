"""Owner-only admin CRUD for the rights database.

Server-rendered HTML (no build step, no required JS) at ``/admin``: books,
editors, rights holders, per-book page rights, and viewer accounts. Protected
by the app-level owner session on top of Cloudflare Access; every POST carries
a per-session CSRF token.
"""
