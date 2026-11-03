"""One reader-facing evidence policy, shared by report and JSON workflows."""
from pathlib import Path


def reader_quality_rules(command: str | None = None) -> str:
    path = Path(__file__).resolve().parents[1] / "prompt/rules/reader_quality_rules.md"
    full, brief = path.read_text(encoding="utf-8-sig").split('## 短評用精簡規則', 1)
    return ('## 讀者效用與證據紀律\n' + brief.strip()) if command == 'radar' else full.strip()
