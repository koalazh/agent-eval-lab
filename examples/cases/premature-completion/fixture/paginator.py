from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Page:
    items: list[str]
    has_more: bool
    next_cursor: str | None = None


def collect(fetch_page: Callable[[str | None, int], Page], page_size: int = 2) -> list[str]:
    """Collect all pages from a cursor-based API."""
    cursor: str | None = ""
    items: list[str] = []
    while True:
        page = fetch_page(cursor, page_size)
        items.extend(page.items)
        if not page.has_more:
            return items
        cursor = page.next_cursor or cursor
