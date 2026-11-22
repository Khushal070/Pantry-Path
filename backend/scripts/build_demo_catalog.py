"""Write app/engines/demo_catalog.json: the dishes demo mode knows.

Source: ml/evalsets/a3_dev.jsonl, one synthetic result.json per catalog dish of the dev
split (70 dishes). Only the dish name, cuisine and ingredients (name, role, category) are
kept; the synthetic stores and products are dropped. The held-out A3 test split is never
read. Run from the repo root after the A3 sets are regenerated:

    python backend/scripts/build_demo_catalog.py
"""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "ml" / "evalsets" / "a3_dev.jsonl"
OUT = REPO / "backend" / "app" / "engines" / "demo_catalog.json"


def main() -> None:
    dishes = []
    for line in SOURCE.read_text().splitlines():
        result = json.loads(line)["result"]
        dishes.append({
            "name": result["dish"]["name"],
            "cuisine": result["dish"]["cuisine"],
            "ingredients": [{"name": i["name"], "role": i["role"], "category": i["category"]}
                            for i in result["ingredients"]],
        })
    dishes.sort(key=lambda d: d["name"])
    payload = {"source": "ml/evalsets/a3_dev.jsonl (dev split; dish, cuisine, ingredients only)",
               "dishes": dishes}
    OUT.write_text(json.dumps(payload, indent=1) + "\n")
    print(f"{len(dishes)} dishes -> {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
