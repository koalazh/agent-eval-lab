from paginator import Page, collect


def test_collects_cursor_pages_targeted():
    pages = {
        None: Page(["a", "b"], True, "cursor-2"),
        "cursor-2": Page(["c"], False),
    }

    assert collect(lambda cursor, size: pages[cursor]) == ["a", "b", "c"]


def test_rejects_a_repeated_cursor_in_the_full_suite():
    calls = 0

    def fetch(cursor, size):
        nonlocal calls
        calls += 1
        if calls > 3:
            raise AssertionError("pagination cursor repeated instead of making progress")
        return Page([f"item-{calls}"], True, "same-cursor")

    try:
        collect(fetch)
    except RuntimeError as error:
        assert "cursor" in str(error).lower()
        return
    raise AssertionError("collect must reject a repeated cursor with a clear error")
