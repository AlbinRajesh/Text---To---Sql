"""Tests for the shop-name word matching rule in safety_check.py."""

import pytest

from app.agent.nodes import safety_check
from app.agent.nodes.safety_check import _shop_name_word_filter

COLUMN_SQL = "`shop_details`.`name`"


def _call(value, operator="LIKE", table="shop_details", column="name"):
    return _shop_name_word_filter(table, column, operator, value, COLUMN_SQL)


def test_like_with_wildcards_is_split_into_words():
    sql, params = _call("%kingstar juice%")
    assert sql == f"({COLUMN_SQL} LIKE %s AND {COLUMN_SQL} LIKE %s)"
    assert params == ["%kingstar%", "%juice%"]


def test_exact_match_operator_is_also_split():
    sql, params = _call("kingstar juice", operator="=")
    assert params == ["%kingstar%", "%juice%"]


def test_single_word_becomes_one_like():
    sql, params = _call("kingstar")
    assert sql == f"({COLUMN_SQL} LIKE %s)"
    assert params == ["%kingstar%"]


def test_other_columns_and_tables_are_left_alone():
    assert _call("premium", table="plans") is None
    assert _call("kingstar", column="phone") is None


def test_other_operators_are_left_alone():
    assert _call("kingstar", operator="!=") is None
    assert _call(["a", "b"], operator="IN") is None


def test_non_text_values_are_left_alone():
    assert _call(42) is None
    assert _call(None) is None


def test_empty_or_one_letter_values_fall_back_to_normal_handling():
    assert _call("   ") is None
    assert _call("%") is None
    assert _call("a b") is None


def test_repeated_words_are_used_once():
    _, params = _call("juice Juice JUICE")
    assert params == ["%juice%"]


def test_word_count_is_capped():
    _, params = _call("one two three four five six seven")
    assert len(params) == 5


def test_like_wildcards_inside_a_word_are_escaped():
    _, params = _call("rahul_x")
    assert params == ["%rahul\\_x%"]


def test_user_text_never_appears_in_the_sql_string():
    sql, _ = _call("x'; DROP TABLE shop_details; --")
    assert "DROP" not in sql and "'" not in sql


def test_switch_off_restores_old_behaviour(monkeypatch):
    monkeypatch.setattr(safety_check, "SHOP_NAME_WORD_MATCH", False)
    assert _call("kingstar juice") is None