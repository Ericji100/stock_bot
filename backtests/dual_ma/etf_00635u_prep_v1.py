"""00635U preparation pack, immutable snapshot gate, and report QA.

Only the deterministic synthetic fixture path is runnable in this preparation
version.  It exists to exercise code, accounting, report links, anchors,
manifests, and replay without making any claim about 00635U performance.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .etf_00635u_strategy_v1 import (
    CORE_ALLOCATIONS,
    GRID_SIZE,
    STRESS_SLIPPAGE_BPS,
    ETFResearchConfig,
    ExecutionTiming,
    IndicatorRow,
    StrategyVersion,
    expected_comparison_lines,
    run_strategy,
)
from .paths import WORKDATA_ROOT


OUTPUT_ROOT_DEFAULT = WORKDATA_ROOT / "00635u" / "prep-fixture-v1"
FORMAL_STOP_CODE = "STOP_WAIT_00_00635U_DATA"
PREP_STOP_CODE = "STOP_WAIT_00_00635U_PREP_QA"
MANIFEST_SCHEMA = "00635U_DATA_SNAPSHOT_V1"
REPORT_NAME = "00635u-prep-fixture-report.md"


class FormalSnapshotNotAccepted(RuntimeError):
    pass


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_formal_snapshot_gate(
    manifest_path: Path | None,
    *,
    accepted_manifest_sha256: str | None,
    accepted_by: str | None,
) -> dict[str, Any]:
    """Require an exact manifest and an explicit acceptance from coordinator 00."""

    if manifest_path is None or not manifest_path.is_file():
        raise FormalSnapshotNotAccepted(f"{FORMAL_STOP_CODE}: manifest missing")
    if accepted_by != "00":
        raise FormalSnapshotNotAccepted(f"{FORMAL_STOP_CODE}: accepted_by must be 00")
    if not re.fullmatch(r"[0-9a-f]{64}", accepted_manifest_sha256 or ""):
        raise FormalSnapshotNotAccepted(f"{FORMAL_STOP_CODE}: accepted SHA-256 missing")
    actual = sha256_file(manifest_path)
    if actual != accepted_manifest_sha256:
        raise FormalSnapshotNotAccepted(
            f"{FORMAL_STOP_CODE}: manifest SHA mismatch ({actual})"
        )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = {
        "schema": MANIFEST_SCHEMA,
        "symbol": "00635U",
        "listing_date": "2015-04-15",
        "immutable": True,
    }
    for key, expected in required.items():
        if payload.get(key) != expected:
            raise FormalSnapshotNotAccepted(
                f"{FORMAL_STOP_CODE}: manifest {key!r} must equal {expected!r}"
            )
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise FormalSnapshotNotAccepted(f"{FORMAL_STOP_CODE}: manifest files missing")
    root = manifest_path.parent.resolve()
    for record in files:
        candidate = (root / record["path"]).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise FormalSnapshotNotAccepted(f"{FORMAL_STOP_CODE}: unsafe/missing file")
        if sha256_file(candidate) != record.get("sha256"):
            raise FormalSnapshotNotAccepted(
                f"{FORMAL_STOP_CODE}: snapshot file hash mismatch: {record['path']}"
            )
    return payload


def fixture_rows() -> list[IndicatorRow]:
    start = date(2016, 1, 4)
    values = (
        (9, 9, 8, 10, 9, -1, 60, 50),
        (11, 11, 9, 10, 9, -0.5, 60, 50),
        (20, 25, 19, 10, 9, -0.2, 60, 50),
        (24, 24, 23, 10, 9, 0.1, 60, 50),
        (18, 18, 17, 10, 9, -0.1, 40, 50),
        (9, 9, 8, 10, 11, 0, 40, 50),
        (9.5, 9.5, 9, 10, 11, 0.2, 45, 50),
        (8, 8, 7.5, 9, 11, -0.2, 35, 50),
    )
    return [
        IndicatorRow(
            day=start + timedelta(days=index),
            raw_open=Decimal(str(open_)),
            raw_close=Decimal(str(close)),
            technical_close=Decimal(str(close)),
            technical_low=Decimal(str(low)),
            sma21=Decimal(str(sma21)),
            sma144=Decimal(str(sma144)),
            macd_histogram=Decimal(str(histogram)),
            k=Decimal(str(k)),
            d=Decimal(str(d)),
        )
        for index, (open_, close, low, sma21, sma144, histogram, k, d) in enumerate(values)
    ]


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        raise ValueError("cannot write empty CSV")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _chart_svg(series: Mapping[str, Sequence[Decimal]]) -> str:
    width, height = 960, 420
    all_values = [value for values in series.values() for value in values]
    low, high = min(all_values), max(all_values)
    span = high - low or Decimal("1")
    colors = ("#1769aa", "#d1495b", "#2a9d8f", "#7b2cbf")
    paths = []
    legends = []
    for line_index, (name, values) in enumerate(series.items()):
        points = []
        for index, value in enumerate(values):
            x = 70 + index * (width - 120) / max(len(values) - 1, 1)
            y = 350 - float((value - low) / span) * 270
            points.append(f"{x:.1f},{y:.1f}")
        color = colors[line_index]
        paths.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{" ".join(points)}"/>'
        )
        legends.append(
            f'<text x="90" y="{45 + line_index * 24}" fill="{color}" font-size="16">{name}</text>'
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="420" '
        'viewBox="0 0 960 420">'
        '<rect width="960" height="420" fill="white"/>'
        '<text x="480" y="28" text-anchor="middle" font-size="20" '
        'font-family="Microsoft JhengHei, sans-serif">合成夾具資產曲線（非正式績效）</text>'
        '<line x1="70" y1="350" x2="910" y2="350" stroke="#555"/>'
        '<line x1="70" y1="80" x2="70" y2="350" stroke="#555"/>'
        + "".join(legends)
        + "".join(paths)
        + '<text x="480" y="400" text-anchor="middle" font-size="15" '
        'font-family="Microsoft JhengHei, sans-serif">夾具交易日</text></svg>'
    )


def _section(
    anchor: str,
    title: str,
    purpose: str,
    method: str,
    observation: str,
    conclusion: str,
    impact: str,
) -> str:
    return f"""<a id="{anchor}"></a>
## {title}

- **目的**：{purpose}
- **方法**：{method}
- **觀察**：{observation}
- **白話結論**：{conclusion}
- **決策影響**：{impact}
"""


def render_fixture_report(summary_rows: Sequence[Mapping[str, Any]]) -> str:
    seven = "\n".join(
        f"| `{line}` | {'夾具驗證值' if index < 4 else '正式資料待補'} |"
        for index, line in enumerate(expected_comparison_lines())
    )
    summary = "\n".join(
        f"| {row['strategy']} | {row['timing']} | {row['final_equity']} | {row['trades']} |"
        for row in summary_rows
    )
    sections = [
        _section(
            "scope", "01｜研究範圍與禁止事項",
            "確認準備版只驗證程式與文件，不產生正式績效。",
            "使用 2016 年起的人工合成小型資料，並在正式入口加上 00 驗收 SHA 閘門。",
            "目前沒有任何已驗收的 00635U 正式資料快照。",
            "這份數字只能證明管線會運作，不能拿來判斷策略是否賺錢。",
            "維持正式回測停止；等待 00 驗收 01 的 manifest。",
        ),
        _section(
            "rules", "02｜策略 A／B 凍結規則",
            "把唯一允許的策略差異寫成可稽核規則。",
            "A 沿用 MA21／MA144 分流固定防守＋MACD；B 只改高 regime 進場部位為淨成本 2R 後永久啟用 KD(9,9,55) 新死叉。",
            "固定防守優先；2R 不直接賣；arm 前與非新死叉均不賣。",
            "B 不是門檻搜尋，也不改低 regime 部位。",
            "正式比較只能使用這兩版與兩種完整成交時點。",
        ),
        _section(
            "fixture", "03｜合成夾具執行結果",
            "檢查四條策略／成交時點管線可以產出一致帳務。",
            "初始資金 50 萬元、整股、不借款、連續帳戶、20bps 與既定費稅。",
            "下表與圖只來自人工資料。",
            "夾具能抓規則錯誤，但完全不代表 00635U 歷史報酬。",
            "通過後才可進入資料綁定 QA。",
        )
        + "\n" + summary
        + "\n\n![合成夾具資產曲線](fixture-equity-chart.svg)\n",
        _section(
            "comparison", "04｜正式七線比較版面",
            "固定正式報告的主比較集合，避免事後換基準。",
            "四條策略成交線、兩條 00635U 買進持有與官方追蹤 Gold ER 指數。",
            "TAIEX 報酬指數只放補充區，不列入主七線。",
            "主表目前保留欄位，不填正式績效。",
            "正式資料驗收後依相同順序填入。",
        )
        + "\n| 主線 ID | 狀態 |\n|---|---|\n"
        + seven
        + "\n",
        _section(
            "analysis", "05｜待執行分析矩陣",
            "預先鎖定完整性與穩健性檢查。",
            f"全期／年度、固定期間、滾動 3／5／10 年（不足明標）、regime、交易穩定與集中度、{STRESS_SLIPPAGE_BPS}bps、A/B 各 {GRID_SIZE} 格、核心持有 {CORE_ALLOCATIONS}%。",
            "準備版只檢查欄位與章節存在。",
            "沒有正式資料就不應產生任何矩陣排名。",
            "避免把樣本不足或參數搜尋誤當正期望證據。",
        ),
        _section(
            "limitations", "06｜黃金期貨 ETF 特有限制",
            "防止把 ETF 價格直接等同現貨黃金。",
            "正式報告逐項揭露期貨轉倉、正逆價差、美元／新臺幣匯率、海外與臺灣交易時區錯位、休市差異、追蹤誤差與追蹤缺口。",
            "這些來源可使 ETF、Gold ER 指數與臺股帳戶在同日出現不同方向。",
            "策略結果必須被解讀為 00635U 可交易載具的結果。",
            "正式結論需區分訊號效果、載具摩擦與資料對時。",
        ),
        _section(
            "attachments", "07｜附件、manifest 與重播",
            "讓 00 可逐檔核對輸入、輸出與雜湊。",
            "manifest 記錄附件 SHA-256；readback 重算後保存檢查結果。",
            "本包可由同一夾具重建。",
            "任一附件或 SHA 不一致即視為失敗。",
            "正式快照也必須套用同一不可變驗證介面。",
        )
        + "\n- [夾具輸入](fixture-rows.csv)\n"
        + "- [四版本摘要](fixture-summary.csv)\n"
        + "- [夾具圖](fixture-equity-chart.svg)\n"
        + "- [manifest](manifest.json)\n"
        + "- [readback](readback.json)\n",
    ]
    toc = "\n".join(
        f"- [{label}](#{anchor})"
        for anchor, label in (
            ("scope", "01｜研究範圍與禁止事項"),
            ("rules", "02｜策略 A／B 凍結規則"),
            ("fixture", "03｜合成夾具執行結果"),
            ("comparison", "04｜正式七線比較版面"),
            ("analysis", "05｜待執行分析矩陣"),
            ("limitations", "06｜黃金期貨 ETF 特有限制"),
            ("attachments", "07｜附件、manifest 與重播"),
        )
    )
    return (
        "# 00635U 黃金期貨 ETF 因果回測｜準備版驗證報告\n\n"
        "> **狀態：SYNTHETIC FIXTURE ONLY／非正式績效。** 00 尚未驗收正式資料 manifest，"
        "不得引用本文數字判斷策略期望值。\n\n"
        "## 目錄\n\n"
        + toc
        + "\n\n"
        + "\n\n".join(sections)
        + f"\n\n`{PREP_STOP_CODE}`\n"
    )


def validate_report_tree(root: Path) -> list[str]:
    errors: list[str] = []
    report = root / REPORT_NAME
    if not report.is_file():
        return [f"missing report: {report}"]
    text = report.read_text(encoding="utf-8")
    anchors = set(re.findall(r'<a id="([^"]+)"', text))
    for target in re.findall(r"\]\(#([^)]+)\)", text):
        if target not in anchors:
            errors.append(f"missing anchor: {target}")
    for target in re.findall(r"\]\((?!#|https?://)([^)]+)\)", text):
        if not (root / target).is_file():
            errors.append(f"missing local link: {target}")
    manifest_path = root / "manifest.json"
    readback_path = root / "readback.json"
    if not manifest_path.is_file() or not readback_path.is_file():
        errors.append("manifest/readback missing")
        return errors
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest.get("files", []):
        candidate = root / item["path"]
        if not candidate.is_file():
            errors.append(f"manifest file missing: {item['path']}")
        elif sha256_file(candidate) != item["sha256"]:
            errors.append(f"manifest hash mismatch: {item['path']}")
    readback = json.loads(readback_path.read_text(encoding="utf-8"))
    if readback.get("manifest_sha256") != sha256_file(manifest_path):
        errors.append("readback manifest SHA mismatch")
    if readback.get("status") != "PASS":
        errors.append("readback status is not PASS")
    return errors


def build_fixture_pack(output_root: Path = OUTPUT_ROOT_DEFAULT) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    rows = fixture_rows()
    fixture_csv = [
        {
            "date": row.day.isoformat(),
            "raw_open": row.raw_open,
            "raw_close": row.raw_close,
            "technical_close": row.technical_close,
            "technical_low": row.technical_low,
            "sma21": row.sma21,
            "sma144": row.sma144,
            "macd_histogram": row.macd_histogram,
            "k": row.k,
            "d": row.d,
        }
        for row in rows
    ]
    _write_csv(output_root / "fixture-rows.csv", fixture_csv)

    summaries: list[dict[str, Any]] = []
    curves: dict[str, list[Decimal]] = {}
    for strategy in StrategyVersion:
        for timing in ExecutionTiming:
            result = run_strategy(rows, ETFResearchConfig(strategy=strategy, timing=timing))
            label = f"策略{strategy.name}-{timing.value}"
            curves[label] = [item["equity"] for item in result.state.daily]
            summaries.append({
                "strategy": strategy.name,
                "timing": timing.value,
                "final_equity": result.final_equity,
                "trades": len(result.state.trades),
                "fixture_only": True,
            })
    _write_csv(output_root / "fixture-summary.csv", summaries)
    (output_root / "fixture-equity-chart.svg").write_text(
        _chart_svg(curves), encoding="utf-8"
    )
    (output_root / REPORT_NAME).write_text(
        render_fixture_report(summaries), encoding="utf-8"
    )

    artifact_names = (
        REPORT_NAME,
        "fixture-rows.csv",
        "fixture-summary.csv",
        "fixture-equity-chart.svg",
    )
    manifest = {
        "schema": "00635U_PREP_FIXTURE_V1",
        "symbol": "00635U",
        "fixture_only": True,
        "formal_results": False,
        "files": [
            {
                "path": name,
                "bytes": (output_root / name).stat().st_size,
                "sha256": sha256_file(output_root / name),
            }
            for name in artifact_names
        ],
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_bytes(_canonical_bytes(manifest) + b"\n")
    readback = {
        "status": "PASS",
        "fixture_only": True,
        "manifest_sha256": sha256_file(manifest_path),
        "files_verified": len(artifact_names),
        "stop_code": PREP_STOP_CODE,
    }
    (output_root / "readback.json").write_bytes(_canonical_bytes(readback) + b"\n")
    errors = validate_report_tree(output_root)
    if errors:
        raise RuntimeError("; ".join(errors))
    return {
        "output_root": str(output_root),
        "report": str(output_root / REPORT_NAME),
        "manifest_sha256": readback["manifest_sha256"],
        "status": PREP_STOP_CODE,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("fixture", "formal"), default="fixture")
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT_DEFAULT)
    parser.add_argument("--snapshot-manifest", type=Path)
    parser.add_argument("--accepted-manifest-sha256")
    parser.add_argument("--accepted-by")
    args = parser.parse_args(argv)
    if args.mode == "formal":
        validate_formal_snapshot_gate(
            args.snapshot_manifest,
            accepted_manifest_sha256=args.accepted_manifest_sha256,
            accepted_by=args.accepted_by,
        )
        raise RuntimeError(
            "formal snapshot gate passed, but formal execution is intentionally absent from prep v1"
        )
    print(json.dumps(build_fixture_pack(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
