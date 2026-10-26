import sys
import os
import json
import glob
import importlib

sys.path.insert(0, os.path.abspath('backend'))

from app.tests.e2e_frontend import cases, cases_batch2, cases_batch3, cases_batch4

all_questions = []

# 1. test_2/case_main.json
with open('backend/app/tests/test_2/case_main.json', 'r', encoding='utf-8') as f:
    cm = json.load(f)
for c in cm:
    all_questions.append({
        'source': 'case_main.json',
        'id': c.get('id'),
        'question': c.get('question'),
        'golden_sql': c.get('golden_sql'),
        'expected_outcome': c.get('expected_outcome_type', 'answer'),
        'category': c.get('category', 'general'),
    })

# 2. golden_benchmark_catalog 1..5
for g in sorted(glob.glob('backend/app/tests/golden_benchmark_catalog*.py')):
    mod_name = 'app.tests.' + os.path.splitext(os.path.basename(g))[0]
    m = importlib.import_module(mod_name)
    val = getattr(m, 'GOLDEN_BENCHMARK_CASES', [])
    for idx, c in enumerate(val):
        cid = c.get('id', f'G_{idx}')
        all_questions.append({
            'source': os.path.basename(g),
            'id': f'{os.path.splitext(os.path.basename(g))[0]}_{cid}',
            'question': c.get('question'),
            'golden_sql': c.get('golden_sql') or c.get('expected_sql'),
            'expected_outcome': c.get('expected_outcome', 'answer'),
            'category': c.get('category', 'catalog'),
        })

# 3. e2e cases
for mod in [cases, cases_batch2, cases_batch3, cases_batch4]:
    for attr in dir(mod):
        v = getattr(mod, attr)
        if isinstance(v, (list, tuple)) and len(v) > 0 and hasattr(v[0], 'question'):
            for idx, c in enumerate(v):
                cid = getattr(c, 'case_id', f'E2E_{idx}')
                all_questions.append({
                    'source': mod.__name__.split('.')[-1],
                    'id': f'{mod.__name__.split(".")[-1]}_{cid}',
                    'question': c.question,
                    'golden_sql': getattr(c, 'oracle_sql', None) or getattr(c, 'golden_sql', None),
                    'expected_outcome': 'answer',
                    'category': 'e2e',
                })

out_path = 'backend/app/tests/patch_audit/merged_questions.json'
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(all_questions, f, indent=2)

print(f'Successfully merged {len(all_questions)} questions into {out_path}')
