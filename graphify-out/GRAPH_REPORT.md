# Graph Report - nosql_agent  (2026-11-03)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 3257 nodes · 9404 edges · 150 communities (122 shown, 28 thin omitted)
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 626 edges (avg confidence: 0.89)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `805b39fe`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- test_llm_service_retries.py
- run_safety_check
- api/chat.py
- asyncio
- generate_query.py
- mysql_client.py
- embedding_service.py
- resolve_business_request
- admin_business_rules.py
- test_enterprise_pipeline_hardening.py
- BusinessRulesStore
- mongo_client.py
- test_query_systemic_upgrades.py
- schema_grounding.py
- test_chat_interactions.py
- test_calculated_metrics.py
- test_pending_interaction_storage.py
- runner.py
- package.json
- audit_schema_traversals.py
- grade_sample.py
- graph.py
- cache_check.py
- grade_result
- return_save.py
- master_golden_benchmark.py
- ChatPage
- test_ranked_rating_query.py
- suggestion_store.py
- catalog_resolver.py
- SafetyViolationError
- regression_benchmark.py
- test_classifier_drift.py
- resolve_temporal_intents
- test_business_rule_markdown.py
- test_search_operation.py
- test_business_rule_revisions.py
- typing
- json
- pending_interactions.py
- run_query
- test_grade_sample_typed_refusal.py
- state.py
- pytest
- MessageBubble.jsx
- evaluate_case
- ChatPage.jsx
- validate_user_select
- test_did_you_mean.py
- QueryGenerationError
- main.py
- test_benchmark_scoring.py
- find_relevant_rules
- validate_metric
- _validate_llm_output
- safety_check.py
- test_contextual_clarification.py
- resolve_value_candidates
- agent/__init__.py
- generate_query
- _build_filter_expression
- _ast_verify
- conversation_context.py
- classify_and_rewrite
- test_suggestion_redemption.py
- test_frontend_suite.py
- chatApi.js
- clarification_gate.py
- repair_query_after_safety_rejection
- did_you_mean.py
- test_active_shop_filter.py
- _build_ranked_rating_query
- nodes/__init__.py
- test_shop_name_word_filter.py
- test_advanced_fanout_plans.py
- test_safety_check_hardening.py
- test_code_built_juice_bypass.py
- preflight_refusals
- _remove_unrequested_grouped_null_exclusions
- test_benchmark_catalog_2_contracts.py
- _enforce_default_address_filter
- evaluators.py
- test_compiler_consistency.py
- UnanswerableQuestionError
- test_general_query_catalog_path.py
- _run_case
- reporting.py
- AdminBusinessRulesPage.jsx
- test_query_grounding_policies.py
- relationship_overrides.py
- eval_engine.py
- test_pending_interactions.py
- cases_batch2.py
- validate_catalog_filter_plan
- trace_store.py
- collections
- test_admin_business_rules.py
- test_safety_rejection_logging.py
- redis
- langfuse_tracing.py
- timed_node
- test_advanced_benchmark_connects_replica_before_cases
- test_search_root_repair.py
- normalize_period
- golden_benchmark_catalog_4.py
- test_ambiguous_name_guard.py
- test_node6_rejection_logging.py
- _build_pipeline_diagnostics
- _validate_discount_source
- conftest.py
- _capture_response
- _iter_mappings
- _compare_results
- test_anti_join_distance.py
- test_clarification_regressions.py
- test_discount_rule_synonyms.py
- test_run_query_classifies_all_null_aggregate_with_matching_row_count
- test_order_by_hidden_child.py
- test_period_aggregation.py
- escalation.py
- test_booking_city_governance.py
- FakeLLM
- generate_query_prompt.py
- test_zero_result_contract.py
- _install_llm_recorder
- golden_benchmark_catalog_5.py
- test_http_429_retries_with_exponential_backoff
- test_measure_base_table_plan.py
- test_return_answer.py
- test_unique_users_plan.py
- test_unused_left_join_bridge.py
- test_missing_table_or_column_feedback_widens_to_known_table
- _apply_name_search_filters
- test_graph_passes_and_revalidates_typed_filters

## God Nodes (most connected - your core abstractions)
1. `_validate_llm_output()` - 212 edges
2. `run_safety_check()` - 160 edges
3. `QueryGenerationError` - 127 edges
4. `SafetyViolationError` - 88 edges
5. `resolve_business_request()` - 81 edges
6. `generate_query()` - 68 edges
7. `UnanswerableQuestionError` - 53 edges
8. `grade_schema_sufficiency()` - 52 edges
9. `find_relevant_rules()` - 52 edges
10. `BusinessRulesStore` - 51 edges

## Surprising Connections (you probably didn't know these)
- `classify_and_rewrite()` --uses--> `LLMRefusalError`  [INFERRED]
  backend/app/agent/nodes/classifier_rewrite.py → backend/app/services/llm_service.py
- `classify_and_rewrite()` --uses--> `LLMServiceError`  [INFERRED]
  backend/app/agent/nodes/classifier_rewrite.py → backend/app/services/llm_service.py
- `generate_query()` --uses--> `LLMServiceError`  [INFERRED]
  backend/app/agent/nodes/generate_query.py → backend/app/services/llm_service.py
- `repair_query_after_safety_rejection()` --uses--> `LLMServiceError`  [INFERRED]
  backend/app/agent/nodes/generate_query.py → backend/app/services/llm_service.py
- `create_draft()` --uses--> `BusinessRulesError`  [INFERRED]
  backend/app/api/admin_business_rules.py → backend/app/agent/nodes/business_rules.py

## Import Cycles
- None detected.

## Communities (150 total, 28 thin omitted)

### Community 0 - "test_llm_service_retries.py"
Cohesion: 0.05
Nodes (45): AgentConfig, _groq_json_failure(), LLMConfigError, LLMRefusalError, LLMService, _record(), LLMServiceError, _retry_after_seconds() (+37 more)

### Community 1 - "run_safety_check"
Cohesion: 0.05
Nodes (73): _enforce_at_least_one_joined_entity(), _normalize_grouped_dimension_count(), _table_name_forms(), run_safety_check(), test_safety_check_blocks_plan_and_ast_table_usage(), test_structured_plan_validation_enforces_typed_filter_level(), test_views_shop_relationship_is_schema_validated(), _category_count_query() (+65 more)

### Community 2 - "api/chat.py"
Cohesion: 0.06
Nodes (56): normalize_shop_name_term(), _build_error_response(), _build_response(), _catalog_option_records(), chat(), chat_stream(), emit(), event_stream() (+48 more)

### Community 3 - "asyncio"
Cohesion: 0.08
Nodes (25): get_cached_schema(), build_schema_index(), main(), _run(), main(), main(), _parse_case_numbers(), _run() (+17 more)

### Community 4 - "generate_query.py"
Cohesion: 0.06
Nodes (42): _and_terms(), _apply_resolved_temporal_constraints(), _apply_savings_policy(), _build_business_contract_query(), _business_semantic_contracts(), _coerce_filter_value(), _columns_for_table(), _strip_shop_status() (+34 more)

### Community 5 - "mysql_client.py"
Cohesion: 0.08
Nodes (26): connect_to_mysql(), connect_to_read_replica(), fetch_all_readonly(), fetch_readonly_result(), get_read_pool(), health_check(), _init_session(), MySQL (+18 more)

### Community 6 - "embedding_service.py"
Cohesion: 0.06
Nodes (26): embed_single_text(), _embed_sync(), _embed_texts(), EmbeddingServiceError, _get_model(), get_schema_index_metadata(), _guaranteed_table_names(), refresh_schema_index_if_stale() (+18 more)

### Community 7 - "resolve_business_request"
Cohesion: 0.08
Nodes (44): resolve_business_request(), test_and_between_same_level_business_types_requests_clarification(), test_and_request_with_unknown_item_never_returns_partial(), test_bakery_shop_name_search_option_is_preserved(), test_bare_listing_fallback_preserves_existing_resolver_behavior(), test_bare_listing_fallback_rejects_empty_long_and_unicode_input(), test_bare_listing_with_weak_or_ambiguous_match_bypasses_resolver(), test_bare_misspelled_business_listing_resolves_unique_catalog_match() (+36 more)

### Community 8 - "admin_business_rules.py"
Cohesion: 0.11
Nodes (25): _current_context_version(), get_business_rules_store(), get_cached_relationships(), create_draft(), draft_details(), _draft_preview(), export_current_rulebook(), _next_rulebook_version() (+17 more)

### Community 9 - "test_enterprise_pipeline_hardening.py"
Cohesion: 0.06
Nodes (32): _lint_structural_query_plan(), collect_requested_tables(), column_reference(), visit(), _sanitize_plan_values(), _expand_schema_for_relationships(), _edge(), test_column_reference_shapes_are_promoted_to_verified_joins() (+24 more)

### Community 10 - "BusinessRulesStore"
Cohesion: 0.10
Nodes (27): BusinessRulesError, BusinessRulesStore, _contains_rule_phrase(), _normalized_phrase_tokens(), _valid_result_policy(), _walk_values(), _condition_leaves(), test_active_plan_catalog_rule_requires_active_plan_status() (+19 more)

### Community 11 - "mongo_client.py"
Cohesion: 0.06
Nodes (19): QueryExecutionError, assert_read_only_operation(), close_mongo_connection(), connect_to_mongo(), get_database(), health_check(), MongoConnectionError, MongoDB (+11 more)

### Community 12 - "test_query_systemic_upgrades.py"
Cohesion: 0.11
Nodes (40): _normalize_booking_month_semantics(), remove_old_year_bounds(), _edge(), _join(), _plan(), test_booking_discount_rule_builds_bookings_only_average_metric_plan(), test_calculated_parent_metric_is_safe_from_transaction_base_grain(), test_column_comparison_rejects_incompatible_types() (+32 more)

### Community 13 - "schema_grounding.py"
Cohesion: 0.10
Nodes (17): fetch_ambiguous_names(), fetch_schema_from_information_schema(), _first_value(), refresh_schema_cache(), SchemaCache, SchemaGroundingError, _validated_relationships(), close_mysql_connection() (+9 more)

### Community 14 - "test_chat_interactions.py"
Cohesion: 0.12
Nodes (37): _handle_pending_or_sql(), _get_pending_interaction(), _store_clarification_if_needed(), _store_pending_interaction(), test_clarification_is_saved_as_a_structured_note(), test_clear_typo_selects_the_best_clarification_option(), test_close_typo_score_tie_is_forwarded_as_free_text(), test_exact_option_accepts_case_spacing_and_trailing_punctuation() (+29 more)

### Community 15 - "test_calculated_metrics.py"
Cohesion: 0.13
Nodes (38): _memory_database(), _q1_plan(), _q2_plan(), _q5_plan(), _reviews_scope_schema(), _run_sqlite(), _scoped_share_plan(), test_active_offer_policy_rejects_shop_status_or_date_filters() (+30 more)

### Community 16 - "test_pending_interaction_storage.py"
Cohesion: 0.09
Nodes (20): clear_all_pending_interactions(), close_pending_interaction_store(), initialize_pending_interaction_store(), memory_pending_store(), catalog(), memory_pending_store(), FakeRedis, select_redis_backend() (+12 more)

### Community 17 - "runner.py"
Cohesion: 0.10
Nodes (15): infrastructure_failure_code(), InfrastructureError, ChatStreamError, post_chat_stream(), _retry_after_seconds(), _execute_oracle(), _questions_for_case(), run() (+7 more)

### Community 18 - "package.json"
Cohesion: 0.08
Nodes (36): dependencies, react@^19.2.8, react-dom@^19.2.8, tailwindcss@^4.3.3, @tailwindcss/vite@^4.3.3, devDependencies, eslint@^10.10.0, @eslint/js@^10.0.1 (+28 more)

### Community 19 - "audit_schema_traversals.py"
Cohesion: 0.09
Nodes (19): blocked_table_names(), filter_blocked_relationships(), filter_blocked_schema(), find_blocked_tables(), validate_blocked_tables(), fetch_relationships_from_information_schema(), audit_live_schema(), find_missing_traversals() (+11 more)

### Community 20 - "grade_sample.py"
Cohesion: 0.09
Nodes (23): _add_configured_search_context(), _ask_cheap_model_if_sufficient(), _build_schema_grader_prompt(), _catalog_has_path(), _ensure_connected_schema_basket(), _flatten_schema_for_prompt(), grade_schema_sufficiency(), _is_irrelevant_active_offer_view_item() (+15 more)

### Community 21 - "graph.py"
Cohesion: 0.11
Nodes (25): build_graph(), get_graph(), node_business_rules(), node_cache_check(), node_classify(), node_escalation(), node_generate_query(), node_grade_schema() (+17 more)

### Community 22 - "cache_check.py"
Cohesion: 0.10
Nodes (23): _cache_key(), _catalog_filter_signature(), check_cache(), _normalize(), _numbers_in(), _prune(), store(), _run() (+15 more)

### Community 23 - "grade_result"
Cohesion: 0.08
Nodes (19): _format_results_for_prompt(), grade_result(), _normalize_ranked_metric(), _ranked_result_violation(), ResultGraderError, _verified_tied_leaders(), get_llm_service(), test_result_grader_rejects_wrong_order_but_accepts_lower_later_ranks() (+11 more)

### Community 24 - "return_save.py"
Cohesion: 0.09
Nodes (19): _attach_summary(), build_and_save_answer(), _build_result_answer(), _column_name(), _humanize(), _single_value(), generate_summary(), _single_value_label() (+11 more)

### Community 25 - "master_golden_benchmark.py"
Cohesion: 0.10
Nodes (20): _append(), _emit(), record_database_call(), record_llm_call(), start_request(), wrapper(), timing_scope(), evaluate_canonical_ast() (+12 more)

### Community 26 - "ChatPage"
Cohesion: 0.12
Nodes (7): run_batch3_case(), run_batch4_case(), ChatPage, _ask_and_click_search_option(), _click_option(), _run_case(), _send()

### Community 27 - "test_ranked_rating_query.py"
Cohesion: 0.09
Nodes (31): _expand(), _juice_plan(), _plan(), fake_cities(), fake_fetch(), test_best_rated_shop_in_nagercoil_uses_the_ranked_builder(), test_case_is_ignored(), test_city_extraction() (+23 more)

### Community 28 - "suggestion_store.py"
Cohesion: 0.10
Nodes (15): clear_suggestion_plans(), consume_suggestion_plan(), _json_default(), _redis_key(), _require_redis(), store_suggestion_plans(), SuggestionStoreUnavailableError, test_pending_interaction_storage_against_real_redis() (+7 more)

### Community 29 - "catalog_resolver.py"
Cohesion: 0.13
Nodes (30): _alias_values(), _catalog_phrases(), add(), _catalog_tokens(), CatalogValue, _clarification_options(), _connector_between(), _has_mixed_filter_connectors() (+22 more)

### Community 30 - "SafetyViolationError"
Cohesion: 0.18
Nodes (26): _build_empty_groups_sql(), _build_group_by_clause(), _build_order_by_clause(), _build_query_where_clause(), _build_search_expression(), _build_select_clause(), _build_single_best_with_ties_sql(), _build_sql() (+18 more)

### Community 31 - "regression_benchmark.py"
Cohesion: 0.11
Nodes (21): _json(), _retrieval_metrics(), _attempt(), _case(), _compare_partial_columns(), _compare_projected_distinct(), _hint(), _is_rate_limited() (+13 more)

### Community 32 - "test_classifier_drift.py"
Cohesion: 0.12
Nodes (26): _reply(), _run(), resolve_for_catalog(), resolve_value_candidates(), test_active_inactive_category_comparison_defaults_to_all_cities(), test_added_schema_table_word_falls_back_to_original(), test_ambiguous_catalog_values_are_not_guessed(), test_beverages_with_multiple_catalog_matches_get_grounded_options() (+18 more)

### Community 33 - "resolve_temporal_intents"
Cohesion: 0.12
Nodes (22): DateResolutionError, extract_relative_period_intents(), resolve_temporal_intents(), _shift_months(), _start_of_current_period(), _start_of_next_period(), _subtract_period(), _validate_intent() (+14 more)

### Community 34 - "test_business_rule_markdown.py"
Cohesion: 0.15
Nodes (22): BusinessRulesMarkdownError, parse_business_rules_markdown(), _reject_duplicate_keys(), render_business_rules_markdown(), validate_business_rule_schema_references(), has_path(), require_column(), require_table() (+14 more)

### Community 35 - "test_search_operation.py"
Cohesion: 0.11
Nodes (23): _name_search_conflict_plan(), _plan(), test_business_rule_enforces_narrow_search_scope(), test_business_rule_requires_typed_search_operation(), test_conflicting_business_rule_search_scopes_are_rejected(), test_generator_accepts_only_term_and_scope_for_search(), test_graded_search_schema_is_passed_to_query_generator(), test_model_written_subquery_is_rejected_before_compilation() (+15 more)

### Community 36 - "test_business_rule_revisions.py"
Cohesion: 0.11
Nodes (12): revisions(), get_status(), read_root(), _Connection, _Cursor, _new_rulebook(), _Pool, test_publish_commits_only_a_valid_draft_based_on_active_version() (+4 more)

### Community 37 - "typing"
Cohesion: 0.11
Nodes (10): case_parameters_batch3(), fetch_batch3_oracle_case(), case_parameters_batch4(), fetch_batch4_oracle_case(), case_parameters(), FailureReport, _db_kwargs(), fetch_oracle_case() (+2 more)

### Community 38 - "json"
Cohesion: 0.11
Nodes (12): bound_classifier_context(), build_classifier_messages(), load_cases(), test_case_main_has_expected_case_count_and_split(), test_case_main_has_valid_unique_case_contract(), test_catalog_validation_marks_invalid_sql_without_dropping_other_cases(), test_catalog_validation_rejects_unknown_ordering_contract(), test_catalog_validation_rejects_unknown_tie_policy() (+4 more)

### Community 39 - "pending_interactions.py"
Cohesion: 0.20
Nodes (22): clear_pending_interaction(), consume_pending_interaction(), consume_pending_interaction_if_matches(), _decode(), _encode(), get_pending_interaction(), _new_note(), _prune_expired() (+14 more)

### Community 40 - "run_query"
Cohesion: 0.10
Nodes (17): build_tie_expansion_query(), _pick_tie_column(), run_query(), _serialize_value(), redeem_suggestion(), _run(), fake_fetch(), test_alias_ordering_sets_tie_column() (+9 more)

### Community 41 - "test_grade_sample_typed_refusal.py"
Cohesion: 0.15
Nodes (21): GradeSampleError, MissingRelationshipError, SchemaGraderUnavailableError, test_master_benchmark_does_not_score_grader_outage_as_refusal(), unavailable(), _install(), fake_grade(), _rel() (+13 more)

### Community 42 - "state.py"
Cohesion: 0.10
Nodes (17): BusinessRule, BusinessRuleCondition, ClassifierResult, EscalationInfo, ExecutionResult, GeneratedQuery, GradeSampleResult, JoinRelationship (+9 more)

### Community 43 - "pytest"
Cohesion: 0.12
Nodes (18): require_admin(), _request(), test_admin_api_key_must_be_long_enough(), test_admin_auth_fails_closed_when_key_is_not_configured(), test_admin_auth_requires_https_in_production(), test_admin_auth_requires_matching_bearer_key(), _plan(), _run() (+10 more)

### Community 44 - "MessageBubble.jsx"
Cohesion: 0.21
Nodes (26): ActionButton(), answerText(), AssistantMessage(), MessageBubble(), time(), UserMessage(), displayCell(), QueryResultTable() (+18 more)

### Community 45 - "evaluate_case"
Cohesion: 0.19
Nodes (28): evaluate_case(), _outcome(), _case(), test_all_optimal_tie_policy_accepts_complete_tied_rows_in_any_order(), test_all_optimal_tie_policy_rejects_missing_or_nonoptimal_rows(), test_business_rule_evaluator_accepts_ontology_namespace_prefix(), test_business_rule_evaluator_reads_matcher_term_key(), test_configured_alias_differences_do_not_allow_extra_columns_or_rows() (+20 more)

### Community 46 - "ChatPage.jsx"
Cohesion: 0.17
Nodes (20): checkHealth(), App(), ChatWindow(), EXAMPLES, Icon(), PipelinePanel(), StepRow(), useNow() (+12 more)

### Community 47 - "validate_user_select"
Cohesion: 0.14
Nodes (14): _personal_column_use_allowed(), _column_owner(), execute_user_select(), _function_name(), _normalize_input(), UserSQLValidationError, validate_user_select(), test_allows_bounded_query_and_ordering_by_selected_alias() (+6 more)

### Community 48 - "test_did_you_mean.py"
Cohesion: 0.14
Nodes (17): _column_formats(), _identifier_column(), node_execute(), _filter(), _query(), test_execute_attaches_suggestions_only_for_nonaggregate_zero_rows(), fake_run_query(), test_execute_does_not_look_up_suggestions_for_aggregate_zero_rows() (+9 more)

### Community 49 - "QueryGenerationError"
Cohesion: 0.12
Nodes (18): _column_data_type(), QueryGenerationError, _reject_model_supplied_subquery(), _requires_user_active_subscription(), _retry_nudge_for(), _validate_active_offer_plan(), _validate_unanswerable_evidence(), _validate_user_active_subscription_plan() (+10 more)

### Community 50 - "main.py"
Cohesion: 0.13
Nodes (12): configured_admin_api_key(), validate_admin_configuration(), fetch_all(), check_health(), lifespan(), test_db(), close_suggestion_store(), initialize_suggestion_store() (+4 more)

### Community 51 - "test_benchmark_scoring.py"
Cohesion: 0.17
Nodes (20): _classify_outcome(), _score(), _case(), _evaluate_plan(), _anti_join_fixture(), _row(), test_anti_join_null_match_accepts_primary_key_to_verified_join_key(), test_anti_join_null_match_accepts_verified_join_key_to_primary_key() (+12 more)

### Community 52 - "find_relevant_rules"
Cohesion: 0.15
Nodes (20): find_relevant_rules(), _apply_entity_resolution_constraints(), add_verified_path(), _ontology_anchor_tables(), main(), test_active_offer_location_case_uses_canonical_verified_path(), test_active_offer_rule_ignores_view_relationship_requirements(), test_all_offers_rule_does_not_match_queries_that_only_mention_offers() (+12 more)

### Community 53 - "validate_metric"
Cohesion: 0.14
Nodes (13): _column_types(), compile_group_distribution_percentage(), compile_metric(), compile_share_of_rows(), MetricDefinition, _quote_column(), validate_group_distribution_dimension(), validate_group_distribution_plan() (+5 more)

### Community 54 - "_validate_llm_output"
Cohesion: 0.18
Nodes (19): _validate_llm_output(), ranking_policy_for(), _plan(), test_city_alias_filters_expand_to_parameterized_in(), test_default_address_join_scope_is_preserved_with_or_filter(), test_default_filter_alone_is_accepted(), test_default_filter_in_address_join_conditions_is_accepted(), test_default_filter_inside_and_group_is_accepted() (+11 more)

### Community 55 - "safety_check.py"
Cohesion: 0.11
Nodes (10): _catalog_relationship(), _find_verified_relationship(), _relationship_matches(), escape_like_term(), escape_shop_name_like_word(), search_term_variants(), SearchConfigurationError, shop_name_search_words() (+2 more)

### Community 56 - "test_contextual_clarification.py"
Cohesion: 0.16
Nodes (18): _inherit_verified_followup_filters(), _has_context_reference(), _is_relative_that(), test_context_reference_detection_handles_empty_long_unicode_and_punctuation(), test_followup_filter_inheritance_fails_closed_if_source_table_is_missing(), test_followup_plan_inherits_prior_filters_without_dropping_current_filter(), test_followup_that_refers_to_prior_result_still_requests_clarification(), test_genuine_followup_phrases_remain_context_references() (+10 more)

### Community 57 - "resolve_value_candidates"
Cohesion: 0.10
Nodes (14): _has_catalog_candidate(), derive_interpretation_notes(), leaves(), _normalized_tokens(), resolve_value_candidates(), shop_name_matches_term(), _tokens(), value_signature() (+6 more)

### Community 58 - "agent/__init__.py"
Cohesion: 0.10
Nodes (8): ClassificationError, _validate_input(), main(), test_non_filter_shop_questions_reach_classifier_without_catalog_lookup(), classify(), test_rule_defined_catalog_resolution_overrides_classifier_clarification(), test_run_pipeline_omits_callbacks_when_langfuse_is_not_configured(), test_run_pipeline_passes_request_scoped_langfuse_callback()

### Community 59 - "generate_query"
Cohesion: 0.10
Nodes (14): _build_booking_discount_percentage_query(), _build_highest_booking_value_query(), _build_max_savings_offer_query(), _build_perfect_rating_high_volume_query(), generate_query(), test_average_rating_contract_clarifies_when_threshold_is_missing(), test_node6_logs_aggregate_predicates_for_each_validation_attempt(), call_agent_json() (+6 more)

### Community 60 - "_build_filter_expression"
Cohesion: 0.15
Nodes (10): _normalize_filter_expression(), visit(), _build_filter_expression(), visit(), build_query_plan_documentation(), validate_current_month_boundary(), validate_dynamic_date_anchor(), validate_scalar_date_subquery() (+2 more)

### Community 61 - "_ast_verify"
Cohesion: 0.11
Nodes (16): _ast_verify(), contains_outer_aggregate(), has_cycle(), has_typed_date_filter(), outer_aggregate_nodes(), _canonical_expression(), _expected_clause_expression(), _flatten_and() (+8 more)

### Community 62 - "conversation_context.py"
Cohesion: 0.18
Nodes (13): node_return_save(), clear_all_conversation_contexts(), clear_conversation_context(), get_conversation_context(), _prune_expired(), _safe_scalar(), _safe_structure(), store_verified_turn() (+5 more)

### Community 63 - "classify_and_rewrite"
Cohesion: 0.13
Nodes (17): _ambiguous_metric_options(), _clarification_phrase(), classify_and_rewrite(), _find_rewrite_drift(), _format_context_filter(), _gate_classifier_clarification(), _normalize_llm_output(), _schema_terms() (+9 more)

### Community 64 - "test_suggestion_redemption.py"
Cohesion: 0.12
Nodes (11): SuggestionRedemptionRequest, SuggestionNotFoundError, _plan(), _request(), test_health_reports_suggestion_fast_path_status(), test_invalid_or_replayed_suggestion_returns_gone_without_query(), missing_plan(), test_redemption_executes_plan_without_running_pipeline() (+3 more)

### Community 65 - "test_frontend_suite.py"
Cohesion: 0.25
Nodes (17): CaseSpec, _assert_oracle_table(), _assert_status(), _canonical_key(), _canonical_value(), _column_mapping(), _compare_batch3_oracle_rows(), _compare_oracle_rows() (+9 more)

### Community 66 - "chatApi.js"
Cohesion: 0.19
Nodes (19): ApiError, BASE, fallbackChat(), mockResult(), mockStream(), postChat(), safeJson(), seen (+11 more)

### Community 67 - "clarification_gate.py"
Cohesion: 0.18
Nodes (13): _checked_clarification_phrase(), is_askable(), is_placeholder(), _language_words(), normalize_phrase(), _tokens(), test_business_phrase_is_askable(), test_drops_related_modifier_but_keeps_business_term() (+5 more)

### Community 68 - "repair_query_after_safety_rejection"
Cohesion: 0.18
Nodes (12): node_safety_check(), repair_query_after_safety_rejection(), test_blocked_table_safety_violation_does_not_retry(), FakeGenerator, _query(), _state(), test_blocking_sensitive_column_rejection_does_not_call_repair(), test_node_escalates_when_repair_response_is_unanswerable() (+4 more)

### Community 69 - "did_you_mean.py"
Cohesion: 0.20
Nodes (12): build_suggestion_execution_plan(), _candidate_filters(), _conjunctive_filters(), find_value_suggestions(), _lookup_suggestions(), _normalise_value(), _query_filters(), _remove_filter() (+4 more)

### Community 70 - "test_active_shop_filter.py"
Cohesion: 0.16
Nodes (17): _column_is_named_in_question(), _enforce_active_shop_filter(), _include_empty_text_values_for_missing_request(), visit(), _status_filter(), test_active_offer_status_policy_allows_requested_active_shop_filter(), test_active_status_is_added_outside_or_expressions(), test_conflicting_status_filter_is_rejected() (+9 more)

### Community 71 - "_build_ranked_rating_query"
Cohesion: 0.13
Nodes (13): _build_cheap_juice_query(), _build_good_restaurant_query(), _build_ranked_rating_query(), _expand_city_spellings(), _extract_city(), _extract_trade_subcategory(), is_ranked_rating_request(), _load_known_cities() (+5 more)

### Community 72 - "nodes/__init__.py"
Cohesion: 0.12
Nodes (9): test_schema_attempt_captures_retrieved_tables_and_missing_items(), test_schema_grader_raw_text_is_persisted(), test_follow_up_retrieval_uses_only_original_question(), test_schema_grader_keeps_real_missing_column_alongside_metric_name(), test_schema_grader_treats_matched_savings_metric_as_available(), grade(), test_schema_grader_uses_original_question_not_invented_field(), grade() (+1 more)

### Community 73 - "test_shop_name_word_filter.py"
Cohesion: 0.17
Nodes (16): _shop_name_word_filter(), _call(), test_empty_or_one_letter_values_fall_back_to_normal_handling(), test_exact_match_operator_is_also_split(), test_like_wildcards_inside_a_word_are_escaped(), test_like_with_wildcards_is_split_into_words(), test_literal_name_search_terms_are_not_sql_wildcards(), test_name_search_term_is_normalized_and_limited_by_search_config() (+8 more)

### Community 74 - "test_advanced_fanout_plans.py"
Cohesion: 0.24
Nodes (18): _edge(), _plan(), _relationship(), _review_average_with_address_plan(), _shop_rating_having_plan(), test_case_3_dual_child_aggregates_are_rejected(), test_case_8_address_child_without_flag_is_still_rejected(), test_case_8_review_average_with_flagged_address_child_is_accepted() (+10 more)

### Community 75 - "test_safety_check_hardening.py"
Cohesion: 0.17
Nodes (18): _plan(), test_correlated_exclusion_rejects_unverified_relationship(), test_correlated_exclusion_supports_cross_table_latest_event_day(), test_current_month_date_boundaries_compile_to_bounded_database_clock_range(), test_dynamic_date_anchor_rejects_unknown_or_unsafe_shapes(), test_dynamic_latest_recorded_date_compiles_to_max_date_subquery(), test_generator_rejects_invalid_time_function_filters(), test_generator_rejects_same_column_comparisons() (+10 more)

### Community 76 - "test_code_built_juice_bypass.py"
Cohesion: 0.13
Nodes (12): is_cheap_juice_request(), _code_built_context(), _code_built_juice_context(), _code_built_ranked_context(), test_business_rule_contract_blocks_bypass(), test_failed_rule_lookup_uses_normal_check(), test_is_cheap_juice_request_false(), test_is_cheap_juice_request_true() (+4 more)

### Community 77 - "preflight_refusals"
Cohesion: 0.19
Nodes (15): preflight_refusals(), test_preflight_keeps_relative_time_refusal_with_savings(), test_calendar_relative_phrases_are_no_longer_refused(), test_active_offers_use_the_configured_status_definition(), test_active_offers_reach_business_rule_generation(), test_discounted_offer_price_is_not_mistaken_for_discount_amount(), test_expired_validity_still_triggers_relative_status_refusal(), test_money_context_for_savings_reaches_typed_metric_generation() (+7 more)

### Community 78 - "_remove_unrequested_grouped_null_exclusions"
Cohesion: 0.20
Nodes (16): _remove_unrequested_grouped_null_exclusions(), test_assigned_plan_filter_is_kept(), test_at_least_one_protection_applies_only_to_the_matched_entity_table(), test_default_address_and_at_least_one_primary_key_filters_are_kept(), test_edge_case_wording_does_not_create_unrelated_followup_intent(), test_empty_string_exclusion_is_removed_and_empty_where_is_none(), test_explicit_value_requirements_preserve_grouped_null_exclusions(), test_mixed_and_or_removes_only_top_level_conjunctive_exclusions() (+8 more)

### Community 79 - "test_benchmark_catalog_2_contracts.py"
Cohesion: 0.15
Nodes (11): _case(), _validate_catalog(), _case(), _case(), _catalog_1_case(), test_discount_percentage_golden_uses_project_contract_without_price_floor(), test_general_offer_goldens_include_all_offer_statuses(), test_plain_shop_count_golden_uses_active_shops_and_distinct_entities() (+3 more)

### Community 80 - "_enforce_default_address_filter"
Cohesion: 0.17
Nodes (14): _enforce_default_address_filter(), strip_default(), _question_requests_alternate_address_scope(), test_default_request_without_is_default_column_is_rejected(), test_explicit_alternate_address_scope_does_not_force_default_filter(), test_explicit_default_request_without_filter_is_rejected(), test_location_scoped_shop_counts_require_default_address(), test_non_shop_grouping_is_unaffected_by_shop_location_rule() (+6 more)

### Community 81 - "evaluators.py"
Cohesion: 0.29
Nodes (15): _canonical_rule_name(), _check(), _condition_leaves(), _condition_matches(), _cutoff_tie_rows_match(), _has_missing_text_group(), _normalized_column(), _normalized_value() (+7 more)

### Community 82 - "test_compiler_consistency.py"
Cohesion: 0.15
Nodes (8): score_generated_plan(), has_declared_metric_predicate(), _normalized_plan_literal_keys(), test_child_operation_eval_scorer_catches_missing_behavior(), test_child_operation_eval_scorer_checks_expected_plan_features(), test_compiler_output_fields_are_registered_and_documented(), test_model_behavior_eval_runs_plan_validation_without_query_execution(), test_unseen_child_operation_evaluation_set_has_ten_diverse_cases()

### Community 83 - "UnanswerableQuestionError"
Cohesion: 0.24
Nodes (12): _iter_conditions(), UnanswerableQuestionError, _validate_discount_not_price_substitute(), test_master_benchmark_scores_typed_refusal_for_expected_block(), refused(), _plan(), test_actual_price_filter_without_discount_is_allowed(), test_case2_actual_price_filter_is_refused() (+4 more)

### Community 84 - "test_general_query_catalog_path.py"
Cohesion: 0.17
Nodes (8): build_grader_prompt(), build_ranked_result_note(), ranked_result_metadata(), test_ranked_note_is_built_from_validated_query_plan(), test_ranked_result_note_uses_only_validated_plan_shape(), test_schema_grader_uses_normal_retrieval_for_ranking_and_price_questions(), test_shop_rating_policy_is_catalogued_not_encoded_as_a_fixed_query(), test_grader_prompt_includes_note_only_when_given()

### Community 85 - "_run_case"
Cohesion: 0.16
Nodes (10): _actual_join_types(), _check_execution_expectation(), _execution_columns(), _explain_query(), _flatten(), _required_match_tables(), _run_case(), _safe_execution_preview() (+2 more)

### Community 86 - "reporting.py"
Cohesion: 0.31
Nodes (11): extract_trace(), _extract_intermediate_value(), extract_trace_payload(), _find_key(), _latest_retrieved_tables(), load_pipeline_diagnostics(), _mean_metric(), _sha256() (+3 more)

### Community 87 - "AdminBusinessRulesPage.jsx"
Cohesion: 0.32
Nodes (14): BASE, createBusinessRuleDraft(), exportBusinessRules(), getBusinessRuleDraft(), listBusinessRuleRevisions(), publishBusinessRuleDraft(), request(), responseError() (+6 more)

### Community 88 - "test_query_grounding_policies.py"
Cohesion: 0.19
Nodes (9): _reject_offer_heading_as_shop_type(), is_safe_per_shop_review_average(), has_default_address_filter(), _offer_heading_plan(), test_average_review_policy_allows_default_address_per_shop(), test_offer_filter_is_allowed_for_explicit_offer_request(), test_offer_heading_does_not_establish_shop_type(), test_validator_leaves_offer_heading_alone_when_not_enabled() (+1 more)

### Community 89 - "relationship_overrides.py"
Cohesion: 0.18
Nodes (7): _is_safe_child_having_aggregate(), child_aggregate_is_safe(), child_aggregate_is_safe_plan(), is_verified_many_to_one_parent(), join_is_anti_join_safe(), join_is_fanout_safe(), _top_level_and_terms()

### Community 90 - "eval_engine.py"
Cohesion: 0.18
Nodes (6): canonicalize_sql(), evaluate_execution_accuracy(), _iter_permutations(), walk(), normalize_value(), rows_to_matrix()

### Community 91 - "test_pending_interactions.py"
Cohesion: 0.30
Nodes (13): consume_pending_interaction(), consume_pending_interaction_if_matches(), get_pending_interaction(), store_pending_interaction(), test_clarification_note_can_only_be_consumed_for_its_current_version(), test_clarification_note_has_identity_version_and_short_expiry(), test_clarification_update_is_compare_and_swap(), test_concurrent_updates_cannot_both_claim_the_same_note_version() (+5 more)

### Community 92 - "cases_batch2.py"
Cohesion: 0.41
Nodes (12): _assert_no_table_rows(), case_parameters_batch2(), _count_value(), _decimal(), fetch_batch2_oracle_case(), _key(), _rows(), run_batch2_case() (+4 more)

### Community 93 - "validate_catalog_filter_plan"
Cohesion: 0.18
Nodes (11): _clarification_refusal(), validate_catalog_filter_plan(), branch_values(), contains_or_group(), or_branches(), visit(), test_grounded_catalog_constraint_accepts_only_exact_catalog_like_value(), test_validator_accepts_nested_or_for_compound_filters() (+3 more)

### Community 94 - "trace_store.py"
Cohesion: 0.22
Nodes (6): persist_pipeline_trace(), trace_directory(), test_backend_persists_diagnostics_by_request_id(), test_run_pipeline_persists_runtime_diagnostics(), test_trace_merges_persisted_pipeline_diagnostics(), test_trace_records_available_intermediate_outputs()

### Community 96 - "test_admin_business_rules.py"
Cohesion: 0.24
Nodes (8): _app(), test_upload_creates_validated_draft_preview(), active_info(), create_draft(), no_refresh(), test_upload_increments_active_rulebook_version_automatically(), test_upload_rejects_invalid_markdown_and_invalid_auth(), test_upload_requires_https_in_production()

### Community 97 - "test_safety_rejection_logging.py"
Cohesion: 0.20
Nodes (9): _bounded_log_atom(), _redact_filter_values(), _rejected_plan_log_json(), test_ast_rejection_is_logged_when_graph_handles_safety_exception(), reject_ast(), test_graph_repair_rejection_emits_a_record_for_each_safety_attempt(), test_rejected_join_condition_log_redacts_filter_value(), test_rejected_plan_log_stays_within_size_limit() (+1 more)

### Community 98 - "redis"
Cohesion: 0.17
Nodes (11): interval, retries, test, timeout, command, healthcheck, image, ports (+3 more)

### Community 99 - "langfuse_tracing.py"
Cohesion: 0.24
Nodes (4): create_callback_handler(), finish_llm_generation(), langfuse_configured(), start_llm_generation()

### Community 100 - "timed_node"
Cohesion: 0.27
Nodes (7): set_emitter(), timed_node(), test_build_response_does_not_include_unexecuted_sql(), test_build_response_includes_sql_for_executed_query(), test_safety_check_streams_validated_sql(), run_node(), safety_check()

### Community 101 - "test_advanced_benchmark_connects_replica_before_cases"
Cohesion: 0.20
Nodes (4): test_advanced_benchmark_connects_replica_before_cases(), fake_case(), fake_index(), test_master_main_writes_accuracy_and_latency_reports()

### Community 102 - "test_search_root_repair.py"
Cohesion: 0.31
Nodes (6): FakeGenerator, _search_plan(), _state(), test_second_wrong_search_root_escalates_cleanly(), test_wrong_search_root_has_repairable_rule_id(), test_wrong_search_root_is_repaired_once()

### Community 103 - "normalize_period"
Cohesion: 0.27
Nodes (4): normalize_period(), normalize_period_comparison(), validate_period_sum_column(), test_period_normalizer_rejects_unknown_or_non_date_columns()

### Community 105 - "test_ambiguous_name_guard.py"
Cohesion: 0.38
Nodes (7): _filter(), _plan(), repeated_names(), test_repeated_name_inside_in_list_is_refused(), test_repeated_name_is_refused(), test_repeated_name_is_refused_case_insensitively(), test_unique_name_is_accepted()

### Community 106 - "test_node6_rejection_logging.py"
Cohesion: 0.42
Nodes (7): _plan(), _run(), test_aggregate_rejection_logs_both_predicates(), test_count_not_distinct_rejection_logs_rule_id(), test_count_primary_key_rejection_logs_rule_id(), test_filter_values_never_appear_in_logs(), test_malformed_join_data_does_not_break_the_rejection()

### Community 107 - "_build_pipeline_diagnostics"
Cohesion: 0.31
Nodes (6): _build_pipeline_diagnostics(), _flatten_filter_expression(), test_pipeline_diagnostics_capture_execution_context(), test_pipeline_diagnostics_preserve_redacted_query_validation_attempts(), test_query_generation_failure_copies_validation_attempts_to_state(), test_refusal_diagnostics_include_full_schema_and_runtime_versions()

### Community 108 - "_validate_discount_source"
Cohesion: 0.44
Nodes (6): _validate_discount_source(), _query(), test_booking_discount_used_for_an_offer_style_question_is_refused(), test_plan_using_the_offer_table_is_allowed(), test_question_that_names_bookings_is_allowed(), test_question_without_discount_is_ignored()

### Community 109 - "conftest.py"
Cohesion: 0.33
Nodes (6): backend_health(), backend_url(), browser_factory(), frontend_url(), page(), pytest_addoption()

### Community 110 - "_capture_response"
Cohesion: 0.36
Nodes (5): _capture_response(), parse_stream_events(), RateLimitError, stream_result(), stream_rows()

### Community 111 - "_iter_mappings"
Cohesion: 0.22
Nodes (6): _iter_mappings(), walk(), _iter_partial_mappings(), walk(), catalog_index(), candidates()

### Community 112 - "_compare_results"
Cohesion: 0.43
Nodes (6): _compare_results(), test_empty_generated_fails(), test_fewer_columns_match_by_value(), test_missing_rows_still_fail(), test_unmatched_extra_column_is_ignored(), test_unrelated_values_still_fail()

### Community 113 - "test_anti_join_distance.py"
Cohesion: 0.39
Nodes (5): _join(), _plan(), test_adjacent_is_null_is_accepted(), test_distant_is_null_is_rejected(), test_having_count_zero_is_accepted()

### Community 114 - "test_clarification_regressions.py"
Cohesion: 0.25
Nodes (7): test_drinks_related_resolves_the_normalized_catalog_term(), test_food_related_resolves_matching_food_and_dining_category(), test_resolver_clarification_declares_its_selection_kind(), test_resumed_request_does_not_ask_about_the_same_item_again(), test_top_five_category_question_does_not_ask_about_numeric_filler(), test_unmatched_normalized_phrase_uses_plain_not_found_message(), test_unmatched_type_matching_shop_name_offers_name_search()

### Community 115 - "test_discount_rule_synonyms.py"
Cohesion: 0.46
Nodes (7): _terms(), test_booking_discount_rule_does_not_match_offer_discount_percentage_rule(), test_discounted_offer_price_is_not_a_discount_value_question(), test_maximum_savings_offer_rule_is_registered(), test_offer_discount_percentage_rule_still_matches_offer_questions(), test_shop_without_offers_and_city_consumable_rules_are_registered(), test_total_discount_still_matches_the_discount_value_rule()

### Community 116 - "test_run_query_classifies_all_null_aggregate_with_matching_row_count"
Cohesion: 0.25
Nodes (5): test_run_query_classifies_all_null_aggregate_with_matching_row_count(), test_run_query_does_not_mark_a_user_requested_top_n_as_truncated(), test_run_query_does_not_report_truncation_at_exact_limit(), test_run_query_trims_probe_row_and_reports_truncation(), fake_fetch_all_readonly()

### Community 117 - "test_order_by_hidden_child.py"
Cohesion: 0.50
Nodes (6): _plan(), _relationship(), _run(), test_ordering_by_selected_base_value_remains_valid(), test_ordering_by_selected_child_value_remains_valid(), test_unselected_raw_child_rating_with_limit_is_rejected()

### Community 118 - "test_period_aggregation.py"
Cohesion: 0.39
Nodes (6): _query(), test_conditional_count_returns_zero_for_no_matching_rows(), test_conditional_sum_uses_half_open_dates_and_select_parameter_order(), test_generator_validates_period_comparison_without_unbound_distinct(), test_invalid_period_plan_is_rejected(), test_period_comparison_compiles_later_minus_earlier_with_ordered_params()

### Community 120 - "test_booking_city_governance.py"
Cohesion: 0.38
Nodes (4): _plan(), test_generator_rejects_booking_city_group_and_filter(), test_live_schema_marks_booking_city_as_unvalidated_free_text(), test_safety_layer_independently_rejects_booking_city_usage()

### Community 121 - "FakeLLM"
Cohesion: 0.29
Nodes (3): test_retry_cannot_turn_a_validation_failure_into_an_unanswerable_verdict(), FakeLLM, test_booking_revenue_category_uses_shop_category_by_default()

### Community 122 - "generate_query_prompt.py"
Cohesion: 0.40
Nodes (3): build_generate_query_messages(), _precompiled_join_skeletons(), test_generation_prompt_receives_typed_filter_levels_and_combiner()

### Community 128 - "test_return_answer.py"
Cohesion: 0.50
Nodes (3): test_build_answer_preserves_empty_result_details(), fake_summary(), test_build_answer_returns_valid_results_and_summary()

### Community 132 - "_apply_name_search_filters"
Cohesion: 1.00
Nodes (3): _apply_name_search_filters(), matches_name_search(), strip_expression()

## Knowledge Gaps
- **28 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `QueryGenerationError` connect `QueryGenerationError` to `run_safety_check`, `asyncio`, `generate_query.py`, `_apply_name_search_filters`, `resolve_business_request`, `test_enterprise_pipeline_hardening.py`, `test_query_systemic_upgrades.py`, `schema_grounding.py`, `graph.py`, `master_golden_benchmark.py`, `regression_benchmark.py`, `resolve_temporal_intents`, `test_search_operation.py`, `pytest`, `find_relevant_rules`, `_validate_llm_output`, `test_contextual_clarification.py`, `generate_query`, `_build_filter_expression`, `repair_query_after_safety_rejection`, `test_active_shop_filter.py`, `_build_ranked_rating_query`, `test_advanced_fanout_plans.py`, `test_safety_check_hardening.py`, `_enforce_default_address_filter`, `_run_case`, `test_query_grounding_policies.py`, `test_node6_rejection_logging.py`, `test_anti_join_distance.py`?**
  _High betweenness centrality (0.048) - this node is a cross-community bridge._
- **Are the 55 inferred relationships involving `QueryGenerationError` (e.g. with `node_safety_check()` and `_run_case()`) actually correct?**
  _`QueryGenerationError` has 55 INFERRED edges - model-reasoned connections that need verification._
- **Should `test_llm_service_retries.py` be split into smaller, more focused modules?**
  _Cohesion score 0.05207835642618251 - nodes in this community are weakly interconnected._
- **Why does `run_safety_check()` connect `run_safety_check` to `test_unique_users_plan.py`, `asyncio`, `test_enterprise_pipeline_hardening.py`, `test_query_systemic_upgrades.py`, `test_calculated_metrics.py`, `audit_schema_traversals.py`, `graph.py`, `master_golden_benchmark.py`, `test_ranked_rating_query.py`, `SafetyViolationError`, `regression_benchmark.py`, `test_search_operation.py`, `run_query`, `pytest`, `find_relevant_rules`, `_validate_llm_output`, `safety_check.py`, `_ast_verify`, `repair_query_after_safety_rejection`, `did_you_mean.py`, `test_advanced_fanout_plans.py`, `test_safety_check_hardening.py`, `_run_case`, `test_safety_rejection_logging.py`, `test_search_root_repair.py`, `test_order_by_hidden_child.py`, `test_period_aggregation.py`, `test_booking_city_governance.py`?**
  _High betweenness centrality (0.034) - this node is a cross-community bridge._
- **Should `run_safety_check` be split into smaller, more focused modules?**
  _Cohesion score 0.04738721194417397 - nodes in this community are weakly interconnected._
- **Why does `_validate_llm_output()` connect `_validate_llm_output` to `run_safety_check`, `test_unique_users_plan.py`, `test_unused_left_join_bridge.py`, `generate_query.py`, `_apply_name_search_filters`, `resolve_business_request`, `test_enterprise_pipeline_hardening.py`, `test_query_systemic_upgrades.py`, `test_calculated_metrics.py`, `test_ranked_rating_query.py`, `test_search_operation.py`, `pytest`, `QueryGenerationError`, `validate_metric`, `safety_check.py`, `generate_query`, `_build_filter_expression`, `_ast_verify`, `repair_query_after_safety_rejection`, `test_active_shop_filter.py`, `test_advanced_fanout_plans.py`, `test_safety_check_hardening.py`, `_remove_unrequested_grouped_null_exclusions`, `_enforce_default_address_filter`, `UnanswerableQuestionError`, `test_query_grounding_policies.py`, `relationship_overrides.py`, `validate_catalog_filter_plan`, `normalize_period`, `test_ambiguous_name_guard.py`, `test_node6_rejection_logging.py`, `test_anti_join_distance.py`, `test_period_aggregation.py`, `test_booking_city_governance.py`, `test_measure_base_table_plan.py`?**
  _High betweenness centrality (0.029) - this node is a cross-community bridge._
- **Should `api/chat.py` be split into smaller, more focused modules?**
  _Cohesion score 0.05835010060362173 - nodes in this community are weakly interconnected._