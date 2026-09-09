import json
from pathlib import Path
import bot

BASE = Path(__file__).resolve().parent
ROOT = BASE / "dataset" / "generated"

def load_dir(folder, key):
    out = {}
    for p in sorted((ROOT / folder).glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out[str(d.get(key) or d.get("id"))] = d
    return out

categories = load_dir("categories", "slug")
merchants = load_dir("merchants", "merchant_id")
customers = load_dir("customers", "customer_id")
triggers = load_dir("triggers", "trigger_id")
pairs = json.loads((ROOT / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]

records = []
for pair in pairs:
    merchant = merchants[pair["merchant_id"]]
    category = categories[merchant["category_slug"]]
    trigger = triggers[pair["trigger_id"]]
    customer = customers.get(pair.get("customer_id")) if pair.get("customer_id") else None
    result = bot.compose(category, merchant, trigger, customer)
    result = bot.validate_output(result, [])
    records.append({"test_id": pair["test_id"], **result})

out = BASE / "submission.jsonl"
out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
print(f"Wrote {len(records)} records to {out}")
