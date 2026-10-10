import importlib.util
import json
from pathlib import Path


SCRIPT = Path("scripts/dual_ma_report_catalog.py")


def _module():
    spec = importlib.util.spec_from_file_location("dual_ma_report_catalog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path: Path):
    root = tmp_path / "reports"
    report = root / "final-v1"
    report.mkdir(parents=True)
    (report / "README.md").write_text("# 報告\n", encoding="utf-8")
    (report / "artifact.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (report / "checkpoint.json").write_text(
        json.dumps({"completed_at": "2026-10-10T10:00:00+08:00"}),
        encoding="utf-8",
    )
    for name in (
        "2022-report.md",
        "2023-report.md",
        "2024-report.md",
        "2025-report.md",
        "2026-report.md",
        "report-template-v1.md",
        "report-template-v2.md",
    ):
        (root / name).write_text("# 固定入口\n", encoding="utf-8")
    registry = {
        "sections": [
            {"id": "accepted", "title": "正式採納", "description": "正式報告。"}
        ],
        "entries": [
            {
                "id": "final-v1",
                "title": "定版報告",
                "directory": "final-v1",
                "entry": "README.md",
                "period": "2006～2026/09",
                "summary": "已驗收。",
                "status": "ACCEPT",
                "section": "accepted",
                "show_files": True,
            }
        ],
        "quick_entries": ["final-v1"],
        "ignored_directories": ["."],
    }
    return root, registry


def test_catalog_builds_clickable_complete_index(tmp_path: Path) -> None:
    catalog = _module()
    root, registry = _fixture(tmp_path)
    output = root / "README.md"
    text = catalog.build_readme(registry, root, output)
    output.write_text(text, encoding="utf-8")

    assert "[定版報告](final-v1/README.md)" in text
    assert "[artifact.csv](final-v1/artifact.csv)" in text
    assert "2026-10-10 10:00" in text
    assert catalog.validate_registry(registry, root) == []
    assert catalog.validate_readme_links(output) == []


def test_catalog_rejects_unregistered_report_directory(tmp_path: Path) -> None:
    catalog = _module()
    root, registry = _fixture(tmp_path)
    unknown = root / "unknown"
    unknown.mkdir()
    (unknown / "README.md").write_text("# 漏登錄\n", encoding="utf-8")

    assert "發現尚未登錄的報告目錄：unknown" in catalog.validate_registry(
        registry, root
    )
