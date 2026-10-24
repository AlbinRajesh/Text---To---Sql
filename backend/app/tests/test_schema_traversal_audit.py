from app.tests.audit_schema_traversals import find_missing_traversals


def test_traversal_audit_finds_missing_metric_dimension_paths():
    tables = {
        "views": [],
        "shop_details": [],
        "categories": [],
        "subcategories": [],
        "users": [],
    }
    relationships = [
        {"table": "views", "referenced_table": "shop_details"},
        {"table": "shop_details", "referenced_table": "categories"},
        {"table": "shop_details", "referenced_table": "subcategories"},
    ]

    assert find_missing_traversals(
        tables,
        relationships,
        metric_tables=("views",),
        dimension_tables=("categories", "subcategories", "users"),
    ) == [("views", "users")]


def test_traversal_audit_accepts_multi_hop_dimensions():
    tables = {
        "views": [],
        "offer": [],
        "shop_details": [],
        "categories": [],
        "subcategories": [],
        "users": [],
    }
    relationships = [
        {"table": "views", "referenced_table": "offer"},
        {"table": "offer", "referenced_table": "shop_details"},
        {"table": "shop_details", "referenced_table": "categories"},
        {"table": "shop_details", "referenced_table": "subcategories"},
        {"table": "shop_details", "referenced_table": "users"},
    ]

    assert find_missing_traversals(
        tables,
        relationships,
        metric_tables=("views",),
        dimension_tables=("categories", "subcategories", "users"),
    ) == []
