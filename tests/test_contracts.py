import json
from pathlib import Path


def test_contract_files_are_valid_json():
    for path in (Path(__file__).parents[1] / "api" / "contracts").glob("*.schema.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["$schema"].startswith("https://json-schema.org/")
        assert data["type"] == "object"
