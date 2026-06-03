"""Tiny file-content cache.

Optional dependency for the file tools (ReadFile / WriteFile / EditFile). Kept
deliberately small: an in-memory map from absolute path to text, invalidated on
write. Lets ReadFile skip real IO on a cache hit and keeps tests fast.
"""

from __future__ import annotations


class FileCache:
    """In-memory path -> text cache."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, path: str) -> str | None:
        return self._store.get(path)

    def put(self, path: str, content: str) -> None:
        self._store[path] = content

    def invalidate(self, path: str) -> None:
        self._store.pop(path, None)

    def clear(self) -> None:
        self._store.clear()
