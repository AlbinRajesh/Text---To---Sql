import json

path = 'backend/app/tests/test_2/traces/tuning_20261023T155024Z_f9421129/T034.json'
with open(path, 'r', encoding='utf-8') as f:
    t = json.load(f)
print('Question:', t.get('question'))
print('Golden SQL:', t.get('golden_sql'))
print('Refusal:', t.get('refusal'))
for a in t.get('validation_attempts', []):
    print(f"Attempt {a.get('attempt')}: error={a.get('error')}")
    print('Plan:', json.dumps(a.get('plan'), indent=2))
