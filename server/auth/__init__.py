"""Authentication: owner and account logins, sessions, viewer identity.

Sessions are stateless HMAC-signed cookies; the owner is authenticated from env
credentials (constant-time compare) and accounts from pbkdf2 hashes in the
rights database. The :class:`~server.auth.service.Viewer` type is the identity
every later policy decision is based on.
"""
