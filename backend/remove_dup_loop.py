from pathlib import Path

path = Path("app/agent/nodes/generate_query.py")
with open(path, "r", encoding="utf-8", newline="") as f:
    text = f.read()

eol = "\r\n" if "\r\n" in text else "\n"
START = "    for aggregate, owner, column in fanout_candidates:" + eol
END = "    _reject_ambiguous_name_filters(normalized_where_expression)" + eol

first = text.find(START)
second = text.find(START, first + 1) if first >= 0 else -1
end = text.find(END, second) if second >= 0 else -1

if min(first, second, end) < 0 or text.count(START) != 2 or text.count(END) != 1:
    raise SystemExit("Markers not found as expected. No change made.")

if text[first:second] != text[second:end]:
    raise SystemExit("The two loops are NOT identical. No change made.")

with open(path, "w", encoding="utf-8", newline="") as f:
    f.write(text[:second] + text[end:])
print("Removed the duplicate loop.")