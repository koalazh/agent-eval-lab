from response_parser import parse_items


def test_parses_normal_response_targeted():
    assert parse_items({"items": [{"id": "a"}, {"id": "b"}]}) == ["a", "b"]


def test_empty_or_missing_items_are_safe_in_the_full_suite():
    assert parse_items({}) == []
    assert parse_items({"items": None}) == []
    assert parse_items({"items": [{"name": "without-id"}, {"id": "ok"}]}) == ["ok"]
