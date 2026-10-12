from app.tests.golden_benchmark_catalog_2 import GOLDEN_BENCHMARK_CASES
from app.tests.golden_benchmark_catalog import GOLDEN_BENCHMARK_CASES as CATALOG_1


def _case(number):
    return next(case for case in GOLDEN_BENCHMARK_CASES if case["number"] == number)


def _catalog_1_case(number):
    return next(case for case in CATALOG_1 if case["number"] == number)


def test_state_paraphrase_goldens_use_case_and_space_insensitive_matching():
    for number in (32, 44):
        sql = _case(number)["golden_sql"].lower()
        assert "lower(replace(trim(state), ' ', ''))" in sql
        assert "'tamilnadu'" in sql


def test_plain_shop_count_golden_uses_active_shops_and_distinct_entities():
    case = _case(33)
    sql = case["golden_sql"].lower()

    assert "count(distinct sa.shop_id)" in sql
    assert "join shop_details sd" in sql
    assert "sd.status = 1" in sql
    assert case["golden_tables"] == ["shop_address", "shop_details"]


def test_posted_offer_count_golden_includes_all_offer_statuses():
    sql = _case(34)["golden_sql"].lower()

    assert "sd.status = 1" in sql
    assert "o.status" not in sql


def test_discount_percentage_golden_uses_project_contract_without_price_floor():
    sql = _case(35)["golden_sql"].lower()

    assert "offer_price_p" in sql
    assert "between 1 and 100" in sql
    assert "abs(" in sql
    assert "offer_price > 1" not in sql
    assert "coupon_price_p" not in sql


def test_general_offer_goldens_include_all_offer_statuses():
    for number in (16, 18, 21):
        sql = _catalog_1_case(number)["golden_sql"].lower()
        assert "offer.status" not in sql
        assert "o.status" not in sql


def test_rupee_discount_golden_uses_positive_fixed_price_savings():
    sql = _catalog_1_case(17)["golden_sql"].lower()

    assert "s.status = 1" in sql
    assert "o.status" not in sql
    assert "o.offer_price > 0" in sql
    assert "o.actual_price > o.offer_price" in sql
