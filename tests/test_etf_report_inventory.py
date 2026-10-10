from pathlib import Path

from scripts.etf_report_inventory import inventory


def test_inventory_preserves_and_hashes_reports(tmp_path: Path) -> None:
    report = tmp_path / "final" / "v1"
    report.mkdir(parents=True)
    (report / "README.md").write_text("# report\n", encoding="utf-8")
    (report / "result.csv").write_text("value\n1\n", encoding="utf-8")
    result = inventory(tmp_path)
    assert result["policy"] == "PRESERVE_ALL_REPORTS_IN_PLACE"
    assert result["report_count"] == 1
    assert result["reports"][0]["relative_path"] == "final/v1"
    assert result["reports"][0]["file_count"] == 2
