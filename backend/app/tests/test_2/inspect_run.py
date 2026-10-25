import json, glob, os

with open('backend/app/tests/test_2/case_main.json', 'r', encoding='utf-8') as f:
    cases = {c['id']: c for c in json.load(f)}

trace_dir = 'backend/app/tests/test_2/traces/tuning_20261023T155024Z_f9421129'

for i in range(26, 36):
    cid = f'T0{i}'
    c = cases.get(cid, {})
    trace_path = os.path.join(trace_dir, f'{cid}.json')
    trace = {}
    if os.path.exists(trace_path):
        with open(trace_path, 'r', encoding='utf-8') as tf:
            trace = json.load(tf)
    
    print(f"================ {cid} ================")
    print(f"Question: {c.get('question')}")
    print(f"Golden SQL: {c.get('golden_sql')}")
    print(f"Expected Rows: {c.get('expected_rows') or trace.get('expected_rows')}")
    print(f"Actual Rows: {trace.get('actual_rows')}")
    print(f"Generated SQL: {trace.get('generated_sql')}")
    refusal = trace.get('refusal')
    if refusal:
        print(f"Refusal: {refusal}")
    val_attempts = trace.get('validation_attempts', [])
    if val_attempts:
        print(f"Validation attempts count: {len(val_attempts)}")
        for va in val_attempts:
            print(f"  Attempt {va.get('attempt')}: status={va.get('status')} error={va.get('error')}")
    sg_attempts = trace.get('schema_grader_output', [])
    if sg_attempts:
        for sa in sg_attempts:
            raw = sa.get('grader_raw_output')
            print(f"  Schema grader: {raw}")
    print()
