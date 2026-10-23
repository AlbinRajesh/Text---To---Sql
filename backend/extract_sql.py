import json, pathlib
p = sorted(pathlib.Path("app/tests/test_2/reports").glob("*.json"))[-1]
d = json.loads(p.read_text(encoding="utf-8-sig"))
for c in d["cases"]:
    if c["case_id"] in ["T021", "T023", "T025"]:
        tp = pathlib.Path(c["trace_path"])
        t = json.loads(tp.read_text(encoding="utf-8-sig")) if tp.exists() else {}
        print("=== CASE:", c["case_id"], "===")
        print("Real Generated SQL:", repr(t.get("generated_sql")))
        print("Real Golden SQL:", repr(t.get("golden_sql")))
        print("Evaluation / Checks metadata:", c.get("evaluation") or c.get("checks"))
        print("-" * 60)
