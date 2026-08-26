"""Rights database and access policy.

Holds the SQLite rights store (editors, rights holders, book visibility,
per-page allow rules, accounts and grants) and, in later phases, the
region/date policy core that resolves each request to full or blurred access.
"""
