"""Tile generation and serving.

Contains the image pipeline (decode → mipmap → crop/resample → progressive-JPEG
encode), the encoded-tile and decoded-page caches, and the tile HTTP route.
"""
