from app.check_starvell_market_source import extract_items, parse_csv, unique_amounts


def test_extract_items_supports_top_level_list() -> None:
    assert extract_items([{"id": 1}, "skip"]) == [{"id": 1}]


def test_extract_items_supports_nested_data() -> None:
    assert extract_items({"data": {"items": [{"id": 1}]}}) == [{"id": 1}]


def test_unique_amounts_reads_subcategory_names() -> None:
    items = [
        {"subCategory": {"name": "500 робуксов"}},
        {"subCategory": {"name": "800 робуксов"}},
        {"subCategory": {"name": "500 робуксов"}},
    ]

    assert unique_amounts(items) == [500, 800]


def test_parse_csv_strips_empty_values_for_market_source() -> None:
    assert parse_csv("5, 10,, 20") == ["5", "10", "20"]
