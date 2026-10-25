import json

for cid in ['T031', 'T032']:
    path = f'backend/app/tests/test_2/traces/tuning_20261023T155024Z_f9421129/{cid}.json'
    with open(path, 'r', encoding='utf-8') as f:
        t = json.load(f)
    print(f"=== {cid} ===")
    print("Relationships supplied:")
    for r in t.get('schema_grader_output', [{}])[0].get('relationships', []):
        print(f"  {r.get('constraint_name')}: {r.get('table')} -> {r.get('referenced_table')} ({r.get('column_pairs')})")
    print("Validation attempts:")
    for a in t.get('validation_attempts', []):
        print(f"  Attempt {a.get('attempt')}: status={a.get('status')} error={a.get('error')}")
        plan = a.get('plan') or {}
        print(f"    table={plan.get('table')} joins={plan.get('joins')} exclude={plan.get('exclude_entities_with_rows_in')}")
        if not plan.get('answerable', True):
            print(f"    unanswerable_evidence={plan.get('unanswerable_evidence')}")
