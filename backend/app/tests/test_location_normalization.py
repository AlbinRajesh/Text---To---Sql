import sqlite3

from app.agent.nodes.safety_check import _build_filter_expression


def test_state_equality_ignores_case_and_ordinary_spaces():
    schema = {
        "bookings": [
            {"column": "id", "data_type": "bigint"},
            {"column": "state", "data_type": "varchar"},
        ]
    }
    expression = {
        "table": "bookings",
        "column": "state",
        "operator": "=",
        "value": "Tamil Nadu",
    }
    sql, params = _build_filter_expression(
        expression, ["bookings"], schema
    )

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE bookings (state TEXT)")
    connection.executemany(
        "INSERT INTO bookings (state) VALUES (?)",
        [("Tamil Nadu",), ("tamilnadu",), (" TAMIL NADU ",), ("Kerala",)],
    )
    count = connection.execute(
        f"SELECT COUNT(*) FROM bookings WHERE {sql.replace('%s', '?')}",
        params,
    ).fetchone()[0]
    connection.close()

    assert count == 3
    assert params == ["tamilnadu"]
