"""Fetchers package — one module per data source category.

Each fetcher exposes:
- async def fetch() -> List[dict]   (or dict)
- CATEGORY constant for tagging rows in SQLite

Fetchers return normalized dicts that the API layer re-shapes for the frontend.
"""
