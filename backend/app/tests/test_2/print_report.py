import json
import glob
import os

report_dir = r"D:\ngxp_internship\nosql_agent\backend\app\tests\test_2\reports"
latest_report = max(glob.glob(os.path.join(report_dir, '*.json')), key=os.path.getctime)

with open(latest_report, 'r', encoding='utf-8') as f:
    report_data = json.load(f)

# Map case_id to its grading result from the report
report_map = {c["case_id"]: c for c in report_data.get("cases", [])}

# Load your test cases file (update filename if your test suite file has a different name)
# Looking in the test_2 directory or parent directories for test definitions
test_files = glob.glob(r"D:\ngxp_internship\nosql_agent\backend\app\tests\test_2\*.json")
test_file = [f for f in test_files if "report" not in f][0] # find the definition file

with open(test_file, 'r', encoding='utf-8') as f:
    test_definitions = json.load(f)

print(f"Full Comparison Report for: {os.path.basename(test_file)}\n" + "="*100)

for test in test_definitions:
    case_id = test.get("id")
    question = test.get("question")
    golden_sql = test.get("golden_sql")
    
    report_info = report_map.get(case_id, {})
    bucket = report_info.get("grading_bucket", "UNKNOWN")
    
    # Extract trace/error info if logged
    error_msg = report_info.get("error", "None")

    print(f"[{case_id}] Status: {str(bucket).upper()}")
    print(f"Q: {question}")
    print(f"  Golden SQL: {golden_sql}")
    if error_msg != "None":
        print(f"  Error/Log:  {error_msg}")
    print("-" * 100)