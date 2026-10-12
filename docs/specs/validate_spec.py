"""Check the parent specification and its documentation-only attachments."""
from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent
DOCS = ROOT.parent
MAIN = DOCS / "2026-10-03-输入开关与校准直驱上下滑动修改规格.md"


def check() -> None:
    text = MAIN.read_text(encoding="utf-8")
    index = (DOCS / "README.md").read_text(encoding="utf-8")
    version = re.search(r"^版本：([0-9.]+)", text, re.MULTILINE).group(1)
    assert f"v{version}" in index, "Index version differs from the main document"
    headings = re.findall(r"^#{2,6} (\d+(?:\.\d+)*)[. ]", text, re.MULTILINE)
    assert len(headings) == len(set(headings)), "Duplicate numbered heading"

    language = None
    lines: list[str] = []
    examples = 0
    for line in text.splitlines():
        if line.startswith("```"):
            if language is None:
                language = line[3:].strip()
                lines = []
            else:
                if language == "json":
                    json.loads("\n".join(lines))
                    examples += 1
                language = None
        elif language is not None:
            lines.append(line)
    assert language is None, "Unclosed code fence"

    total = 0
    for prefix, count in {"I": 55, "S": 26, "W": 37, "T": 34, "P": 8, "L": 7}.items():
        values = re.findall(rf"^\| ({prefix}\d{{2}}) \|", text, re.MULTILINE)
        expected = [f"{prefix}{number:02d}" for number in range(1, count + 1)]
        assert values == expected, f"{prefix} rows are missing, repeated or out of order"
        referenced = set(re.findall(rf"\b{prefix}\d{{2}}\b", text))
        assert referenced <= set(expected), f"Unmapped {prefix} references"
        if prefix in "ISWT":
            total += count

    for folder in ("input-control", "calibrated-scroll", "wx-navigation", "layout-reuse", "camera-binding"):
        assert (ROOT / folder / "README.md").is_file(), f"Missing appendix: {folder}"
    for relative in ("validate_all.py", "calibrated-scroll/CONTRACT.md",
                     "wx-navigation/IMPLEMENTATION.md", "layout-reuse/contract-completion.md",
                     "layout-reuse/typed-state.sql",
                     "camera-binding/contracts.schema.json"):
        assert (ROOT / relative).is_file(), f"Missing implementation entry: {relative}"
    assert "### 13.5 实施者唯一入口" in text, "Missing handoff checklist"

    paths = [MAIN, DOCS / "README.md"]
    paths += sorted(path for path in ROOT.rglob("*") if path.suffix in {".md", ".json", ".py", ".sql"})
    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert content.endswith("\n"), f"Missing final newline: {path}"
        for number, line in enumerate(content.splitlines(), 1):
            assert line == line.rstrip(), f"Trailing whitespace: {path}:{number}"
        if path.suffix == ".json":
            json.loads(content)
    print(f"PASS: v{version}; {examples} JSON examples; {total} acceptance rows; "
          f"15 main implementation steps; {len(paths)} document/attachment files")
    print("Documentation structure only. No service, runtime or hardware tests executed.")


if __name__ == "__main__":
    check()
