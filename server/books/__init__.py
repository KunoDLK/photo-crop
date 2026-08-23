"""Book discovery and listing.

Responsible for walking the archive root, parsing page filenames, and producing
the JSON listings the viewer consumes. Pure filesystem concerns; no imaging here
beyond header-only dimension reads.
"""
