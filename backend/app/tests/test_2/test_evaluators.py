from app.tests.test_2.evaluators import evaluate_case


def _case():
    return {
        "expected_outcome_type": "answer",
        "expected_tables": ["shop_details"],
        "expected_checkpoints": {
            "route": "data_question",
            "tables_found": ["shop_details"],
        },
        "compare_rules": {
            "order_matters": False,
            "extra_columns_allowed": False,
            "rounding_allowed": False,
        },
        "golden_sql": "SELECT COUNT(*) FROM shop_details",
    }


def test_extra_schema_tables_reduce_precision_but_do_not_fail_recall_or_result():
    result = evaluate_case(
        _case(),
        [
            {
                "type": "trace",
                "trace": {
                    "route": "data_question",
                    "schema": {"selected_tables": ["shop_details", "reviews"]},
                },
            }
        ],
        {"status": "ok", "results": [{"count": 3}]},
        [{"count": 3}],
        12.0,
    )

    assert result["checks"]["schema_retrieval"]["status"] == "pass"
    assert result["checks"]["schema_efficiency"]["precision"] == 0.5
    assert result["checks"]["schema_efficiency"]["recall"] == 1.0
    assert result["checks"]["result_comparison"]["status"] == "pass"


def test_missing_required_schema_table_is_a_severe_failure():
    result = evaluate_case(
        _case(),
        [
            {
                "type": "trace",
                "trace": {
                    "route": "data_question",
                    "schema": {"selected_tables": ["reviews"]},
                },
            }
        ],
        {"status": "ok", "results": [{"count": 3}]},
        [{"count": 3}],
        12.0,
    )

    assert result["checks"]["schema_retrieval"]["status"] == "fail"
    assert result["checks"]["schema_efficiency"]["recall"] == 0.0


def test_infrastructure_failures_are_excluded_from_accuracy_scoring():
    result = evaluate_case(
        _case(),
        [],
        {"status": "escalated"},
        None,
        12.0,
        pipeline_diagnostics={
            "infrastructure_errors": [{
                "code": "LLM_RATE_LIMIT_429",
                "stage": "generate_query",
            }]
        },
    )

    assert result["grading_bucket"] == "Infrastructure Error"
    assert result["checks"]["infrastructure"]["status"] == "not_evaluated"
    assert result["trace_payload_available"] is True


def test_unavailable_pending_storage_is_excluded_from_accuracy_scoring():
    result = evaluate_case(
        _case(),
        [],
        {
            "status": "storage_unavailable",
            "message": "Pending interaction storage is unavailable.",
        },
        None,
        12.0,
    )

    assert result["grading_bucket"] == "Infrastructure Error"
    assert result["checks"]["infrastructure"]["status"] == "not_evaluated"
    assert result["checks"]["infrastructure"]["observed"]["code"] == (
        "PENDING_INTERACTION_STORAGE_UNAVAILABLE"
    )
    assert result["trace_payload_available"] is True


def test_result_comparison_uses_values_and_allows_extra_columns():
    case = _case()
    case["compare_rules"]["extra_columns_allowed"] = False
    result = evaluate_case(
        case,
        [],
        {
            "status": "ok",
            "results": [
                {"user_id": 1, "display_name": "Ada", "phone": "555-0100"},
                {"user_id": 2, "display_name": "Bea", "phone": "555-0101"},
            ],
        },
        [
            {"id": 1, "name": "Ada"},
            {"id": 2, "name": "Bea"},
        ],
        12.0,
    )

    assert result["checks"]["result_comparison"]["status"] == "different_columns"
    assert result["grading_bucket"] == "Correct with different columns"


def test_configured_alias_differences_pass_when_rows_and_values_match():
    case = _case()
    case["compare_rules"]["allow_column_alias_differences"] = True
    result = evaluate_case(
        case,
        [],
        {
            "status": "ok",
            "results": [
                {"booking_city": "Colachel", "average_discount": 4.25},
                {"booking_city": "Nagercoil", "average_discount": 3.75},
            ],
        },
        [
            {"city": "Colachel", "avg_discount": 4.25},
            {"city": "Nagercoil", "avg_discount": 3.75},
        ],
        12.0,
    )

    assert result["checks"]["result_comparison"]["status"] == "pass"
    assert "aliases are allowed to differ" in (
        result["checks"]["result_comparison"]["detail"]
    )


def test_configured_alias_differences_do_not_allow_extra_columns_or_rows():
    case = _case()
    case["compare_rules"]["allow_column_alias_differences"] = True

    extra_column = evaluate_case(
        case,
        [],
        {
            "status": "ok",
            "results": [{
                "booking_city": "Colachel",
                "average_discount": 4.25,
                "shop_id": 1,
            }],
        },
        [{"city": "Colachel", "avg_discount": 4.25}],
        12.0,
    )
    different_row_count = evaluate_case(
        case,
        [],
        {
            "status": "ok",
            "results": [{"booking_city": "Colachel", "average_discount": 4.25}],
        },
        [
            {"city": "Colachel", "avg_discount": 4.25},
            {"city": "Nagercoil", "avg_discount": 3.75},
        ],
        12.0,
    )

    assert extra_column["checks"]["result_comparison"]["status"] == "different_columns"
    assert different_row_count["checks"]["result_comparison"]["status"] == "fail"


def test_display_order_is_ignored_even_when_legacy_flag_is_true():
    case = _case()
    case["compare_rules"]["order_matters"] = True
    case["compare_rules"]["ordering_contract"] = "none"
    result = evaluate_case(
        case,
        [],
        {
            "status": "ok",
            "results": [{"name": "Bea"}, {"name": "Ada"}],
        },
        [{"name": "Ada"}, {"name": "Bea"}],
        12.0,
    )

    assert result["checks"]["result_comparison"]["status"] == "pass"


def test_ranking_contract_requires_expected_row_order():
    case = _case()
    case["compare_rules"]["order_matters"] = False
    case["compare_rules"]["ordering_contract"] = "ranking"
    result = evaluate_case(
        case,
        [],
        {
            "status": "ok",
            "results": [{"name": "Bea"}, {"name": "Ada"}],
        },
        [{"name": "Ada"}, {"name": "Bea"}],
        12.0,
    )

    assert result["checks"]["result_comparison"]["status"] == "fail"


def test_all_optimal_tie_policy_accepts_complete_tied_rows_in_any_order():
    case = _case()
    case["compare_rules"].update({
        "ordering_contract": "ranking",
        "tie_policy": "all_optimal_rows",
    })
    result = evaluate_case(
        case,
        [],
        {
            "status": "ok",
            "results": [
                {"offer_heading": "Offer B", "offer_price": 50, "shop": "Shop B"},
                {"offer_heading": "Offer A", "offer_price": 50, "shop": "Shop A"},
            ],
        },
        [
            {"offer_heading": "Offer A", "offer_price": 50, "shop": "Shop A"},
            {"offer_heading": "Offer B", "offer_price": 50, "shop": "Shop B"},
        ],
        12.0,
    )

    assert result["checks"]["result_comparison"]["status"] == "pass"
    assert "complete set of optimal tied rows" in (
        result["checks"]["result_comparison"]["detail"]
    )


def test_all_optimal_tie_policy_rejects_missing_or_nonoptimal_rows():
    case = _case()
    case["compare_rules"].update({
        "ordering_contract": "ranking",
        "tie_policy": "all_optimal_rows",
    })
    result = evaluate_case(
        case,
        [],
        {
            "status": "ok",
            "results": [
                {"offer_heading": "Offer A", "offer_price": 50, "shop": "Shop A"},
            ],
        },
        [
            {"offer_heading": "Offer A", "offer_price": 50, "shop": "Shop A"},
            {"offer_heading": "Offer B", "offer_price": 50, "shop": "Shop B"},
        ],
        12.0,
    )

    assert result["checks"]["result_comparison"]["status"] == "fail"


def test_top_n_cutoff_tie_accepts_different_entity_with_matching_rank_score():
    case = _case()
    case["compare_rules"].update({
        "ordering_contract": "top_n",
        "tie_policy": "cutoff_value_equivalence",
        "tie_metric_column": "avg_rating",
        "rounding_allowed": True,
    })
    result = evaluate_case(
        case,
        [],
        {
            "status": "ok",
            "results": [
                {"shop_name": "Shop A", "average_rating": 5.0},
                {"shop_name": "Shop B", "average_rating": 4.9},
                {"shop_name": "Shop E", "average_rating": 4.8},
            ],
        },
        [
            {"shop_name": "Shop A", "avg_rating": 5.0},
            {"shop_name": "Shop B", "avg_rating": 4.9},
            {"shop_name": "Shop C", "avg_rating": 4.8},
        ],
        12.0,
    )

    comparison = result["checks"]["result_comparison"]
    assert comparison["status"] == "pass"
    assert "score parity" in comparison["detail"]


def test_top_n_cutoff_tie_rejects_different_rank_scores_or_prefix_rows():
    case = _case()
    case["compare_rules"].update({
        "ordering_contract": "top_n",
        "tie_policy": "cutoff_value_equivalence",
        "tie_metric_column": "avg_rating",
        "rounding_allowed": True,
    })
    expected = [
        {"shop_name": "Shop A", "avg_rating": 5.0},
        {"shop_name": "Shop B", "avg_rating": 4.9},
        {"shop_name": "Shop C", "avg_rating": 4.8},
    ]
    wrong_score = evaluate_case(
        case,
        [],
        {
            "status": "ok",
            "results": [
                {"shop_name": "Shop A", "avg_rating": 5.0},
                {"shop_name": "Shop B", "avg_rating": 4.9},
                {"shop_name": "Shop E", "avg_rating": 4.7},
            ],
        },
        expected,
        12.0,
    )
    wrong_prefix = evaluate_case(
        case,
        [],
        {
            "status": "ok",
            "results": [
                {"shop_name": "Wrong shop", "avg_rating": 5.0},
                {"shop_name": "Shop B", "avg_rating": 4.9},
                {"shop_name": "Shop E", "avg_rating": 4.8},
            ],
        },
        expected,
        12.0,
    )

    assert wrong_score["checks"]["result_comparison"]["status"] == "fail"
    assert wrong_prefix["checks"]["result_comparison"]["status"] == "fail"
