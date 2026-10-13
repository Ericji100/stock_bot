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
from . import etf_00635u_research_v1 as research
from .paths import WORKDATA_ROOT


OUTPUT_ROOT_DEFAULT = WORKDATA_ROOT / "00635u" / "prep-fixture-v1"
FORMAL_STOP_CODE = "STOP_WAIT_00_00635U_DATA"
PREP_STOP_CODE = "STOP_WAIT_00_00635U_PREP_QA_R6"
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
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _chart_svg(
    series: Mapping[str, Sequence[Decimal]],
    *,
    title: str = "合成夾具資產曲線（非正式績效）",
    kind: str = "equity_curve",
    x_label: str = "交易序列",
    y_label: str = "數值",
    x_tick_labels: Sequence[str] | None = None,
) -> str:
    width, height = 960, 420
    plot_top, plot_bottom = 90, 340
    all_values = [value for values in series.values() for value in values]
    if not all_values:
        series = {"無資料": (Decimal("0"),)}
        all_values = [Decimal("0")]
    low, high = min(all_values), max(all_values)
    span = high - low or Decimal("1")
    max_length = max((len(values) for values in series.values()), default=1)

    def axis_value(value: Decimal) -> str:
        if any(word in y_label for word in ("率", "報酬", "回撤")):
            return f"{value * 100:.1f}%"
        if any(word in y_label for word in ("元", "資產", "損益")):
            return f"{value:,.0f}"
        return f"{value:,.2f}"

    y_ticks = []
    for tick_index in range(5):
        value = low + span * Decimal(tick_index) / Decimal(4)
        y = plot_bottom - tick_index * (plot_bottom - plot_top) / 4
        y_ticks.append(
            f'<line x1="66" y1="{y:.1f}" x2="910" y2="{y:.1f}" stroke="#ddd"/>'
            f'<text data-axis="y-tick" x="62" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11">{axis_value(value)}</text>'
        )

    labels = list(x_tick_labels or (str(index + 1) for index in range(max_length)))
    if not labels:
        labels = ["1"]
    tick_count = min(5, len(labels))
    tick_indices = sorted({
        round(index * (len(labels) - 1) / max(tick_count - 1, 1))
        for index in range(tick_count)
    })
    x_ticks = []
    for label_index in tick_indices:
        x = 70 + label_index * (width - 120) / max(len(labels) - 1, 1)
        x_ticks.append(
            f'<line x1="{x:.1f}" y1="{plot_bottom}" x2="{x:.1f}" y2="345" stroke="#555"/>'
            f'<text data-axis="x-tick" x="{x:.1f}" y="360" text-anchor="middle" '
            f'font-size="11">{labels[label_index]}</text>'
        )
    colors = ("#1769aa", "#d1495b", "#2a9d8f", "#7b2cbf", "#111111", "#f4a261")
    paths = []
    legends = []
    for line_index, (name, values) in enumerate(series.items()):
        if not values:
            continue
        points = []
        for index, value in enumerate(values):
            x = 70 + index * (width - 120) / max(len(values) - 1, 1)
            y = plot_bottom - float((value - low) / span) * (plot_bottom - plot_top)
            points.append(f"{x:.1f},{y:.1f}")
        color = colors[line_index]
        paths.append(
            f'<polyline data-series-name="{name}" data-first-value="{values[0]}" '
            f'data-last-value="{values[-1]}" data-min-value="{min(values)}" '
            f'fill="none" stroke="{color}" stroke-width="3" points="{" ".join(points)}"/>'
        )
        legends.append(
            f'<text x="{80 + (line_index % 3) * 290}" y="{50 + (line_index // 3) * 22}" '
            f'fill="{color}" font-size="14">{name}</text>'
        )
    source_payload = {
        "series": {
            str(key): [str(value) for value in values] for key, values in series.items()
        },
        "x_tick_labels": labels,
    }
    source_sha = hashlib.sha256(_canonical_bytes(source_payload)).hexdigest()
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="420" '
        f'data-chart-kind="{kind}" data-source-sha256="{source_sha}" '
        'viewBox="0 0 960 420">'
        f'<metadata>{{"kind":"{kind}","source_sha256":"{source_sha}"}}</metadata>'
        '<rect width="960" height="420" fill="white"/>'
        '<text x="480" y="28" text-anchor="middle" font-size="20" '
        f'font-family="Microsoft JhengHei, sans-serif">{title}</text>'
        f'<line x1="70" y1="{plot_bottom}" x2="910" y2="{plot_bottom}" stroke="#555"/>'
        f'<line x1="70" y1="{plot_top}" x2="70" y2="{plot_bottom}" stroke="#555"/>'
        + "".join(y_ticks)
        + "".join(x_ticks)
        + "".join(legends)
        + "".join(paths)
        + f'<text x="480" y="400" text-anchor="middle" font-size="15" '
        f'font-family="Microsoft JhengHei, sans-serif">{x_label}</text>'
        f'<text x="18" y="220" font-size="15" transform="rotate(-90 18 220)" '
        f'font-family="Microsoft JhengHei, sans-serif">{y_label}</text></svg>'
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
    return f"""## {title}

### 測試目的

{purpose}

### 測試方法

{method}

### 觀察結果

{observation}

### 白話結論

{conclusion}

### 對最終決策的影響

{impact}
"""


def _chapter_anchor(title: str) -> str:
    """Return the Markdown anchor used by the numeric full chapter heading."""

    without_bar = title.replace("｜", "").lower()
    without_punctuation = re.sub(r"[^\w\s\-\u4e00-\u9fff]", "", without_bar)
    return re.sub(r"\s+", "-", without_punctuation.strip())


def render_fixture_report(summary_rows: Sequence[Mapping[str, Any]]) -> str:
    seven = "\n".join(
        f"| `{line}` | {'夾具驗證值' if index < 4 else '正式資料待補'} |"
        for index, line in enumerate(expected_comparison_lines())
    )
    summary = (
        "| 狀態 | 策略 | 成交方式 | 期末資產 | 總報酬 | 年化報酬率 | 最大回落 | "
        "獲利／虧損／平盤年度 | 進場／出場／未平倉 | 最大投入 |\n"
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|\n"
        + "\n".join(
            f"| 合成測試值／不可當績效 | {row['strategy']} | {row['timing']} | "
            f"{row['final_equity']} | {row['total_return']} | {row['cagr']} | "
            f"{row['max_drawdown']} | {row['profitable_years']}／{row['losing_years']}／{row['flat_years']} | "
            f"{row['entries']}／{row['exits']}／{row['open_positions']} | {row['maximum_single_entry_cash']} |"
            for row in summary_rows
        )
    )
    specs = (
        ("conclusion", "01｜一頁白話結論", "先回答這是不是正式績效。", "查驗資料閘門與 fixture 標記。", "目前只有合成資料。", "現在只能驗證程序，不能判定正期望。", "維持停止等待 00 資料驗收。"),
        ("performance", "02｜完整績效總表", "固定正式績效欄位。", "同時計算總報酬、CAGR、MaxDD、年度盈虧、進出未平與最大投入。", "合成摘要已產出欄位。", "欄位可跑不代表數值可用。", "正式資料到位後原樣重播。"),
        ("relative", "03｜相對基準", "比較策略與被動及 Gold ER；TAIEX 報酬指數只放補充區。", "以主七線同期間、同起始資金比較差值。", "Gold ER 對時另附 ledger。", "不能把海外休市日靜默補值。", "正式結論須拆分策略與載具差異。"),
        ("rules", "04｜策略規則與成交版本", "鎖定 A/B 唯一差異。", "A 為固定防守＋MACD；B 高 regime 部位為 2R 後 KD 新死叉。", "停損優先且無混合成交。", "2R 不直接賣，arm 不回溯。", "任何改規則都必須另開版本。"),
        ("annual", "05｜完整逐年績效", "檢查獲利是否跨年度。", "連續帳戶以前一年度末為年度起點，首年用初始本金。", "首交易日報酬不會遺漏。", "年度不能各自重設 50 萬。", "用來辨識少數年份集中。"),
        ("trades", "06｜交易紀錄與資金投入", "逐筆重建資金與事件。", "入場、2R arm、權利、付款、公司行動與出場均帶 position/event ID。", "附件包含 trades/events/daily。", "每次投入受現金、整股與 20% 初始風險約束。", "00 可一對一重建 ledger。"),
        ("execution", "07｜成交時間與成本", "觀察成交延遲與摩擦。", f"比較兩成交版本及 {STRESS_SLIPPAGE_BPS}bps。", "停牌、零量、缺 open 均延後成交。", "不能用不可成交價格美化結果。", "次日開盤為實務主口徑。"),
        ("periods", "08｜時期、滾動與狀態", "檢查不同黃金環境。", "固定期間、月末滾動 3/5/10 年、MaxDD 與 HIGH/LOW regime。", "不足窗口明標。", "單一全期數字不足以證明穩健。", "用最差窗口限制結論。"),
        ("concentration", "09｜集中度與路徑風險", "排除少數交易撐起績效。", "計算連虧、水下期、最佳最差與移除最佳 1/3/5。", "合成附件驗證 schema。", "正期望需經得起移除極端交易。", "決定是否只列候選而非採用。"),
        ("grid", "10｜243 格參數穩健性", "避免只看中心參數。", f"A/B 各 {GRID_SIZE} 格；KD 9/9/55 與 2R 固定。", "輸出分布、中心鄰域與五年最差欄位。", "不搜尋 KD 或 R 倍數。", "看分布，不挑最高格。"),
        ("core", "11｜核心配置", "觀察核心持有的代價與緩衝。", f"分開維護核心／戰術子帳戶，比例 {CORE_ALLOCATIONS}%，主分析不再平衡。", "每日保存兩袖套帳務。", "混合曲線不是把百分比事後乘上結果。", "年度再平衡若採用須另列敏感度。"),
        ("practice", "12｜實務建議與黃金 ETF 限制", "把統計轉為可執行限制。", "揭露期貨轉倉、正逆價差、美元／新臺幣匯率、海外與臺灣交易時區錯位、休市、追蹤誤差與缺口。", "ETF 與 Gold ER 可能同日不同方向。", "這是 00635U 載具結果，不是現貨黃金。", "正式建議須同時監控資料 stale 與載具摩擦。"),
        ("qa", "13｜技術驗收與隔離重播", "確認可重現與 fail-closed。", "驗 manifest/readback、checkpoint bytes、帳務 invariant、連結與 anchors。", "fixture 可隔離重建。", "任一 hash 或資料狀態不符就停止。", "正式入口仍受 00 接受 SHA 約束。"),
        ("attachments", "14｜完整附件目錄", "提供所有稽核檔。", "逐檔列出 CSV、SVG、manifest 與 readback。", "連結由程式驗證。", "主文不取代原始附件。", "00 以附件與 SHA 為最終依據。"),
    )
    extras = {
        "performance": "\n" + summary + "\n\n![合成夾具資產曲線](fixture-equity-chart.svg)\n",
        "relative": "\n| 主線 ID | 狀態 |\n|---|---|\n" + seven + "\n",
        "trades": "\n- [逐筆交易明細 CSV](trade-details.csv)\n- [事件 ledger](event-ledger.csv)\n",
        "attachments": "\n" + "\n".join(
            f"- [{name}]({name})" for name in (
                "fixture-rows.csv", "fixture-summary.csv", "daily-equity.csv",
                "trade-details.csv", "event-ledger.csv", "annual-performance.csv",
                "rolling-windows.csv", "cost-sensitivity.csv", "parameter-grid-schema.csv",
                "core-allocation-summary.csv", "core-allocation-daily.csv",
                "core-allocation-ledger.csv", "index-alignment-ledger.csv",
                "fixture-equity-chart.svg", "drawdown-chart.svg", "annual-chart.svg",
                "rolling-window-chart.svg", "regime-chart.svg", "trade-pnl-chart.svg",
                "cost-pressure-chart.svg", "core-allocation-chart.svg",
                "parameter-grid-chart.svg", "manifest.json", "readback.json",
            )
        ) + "\n",
    }
    sections = [
        _section(anchor, title, purpose, method, observation, conclusion, impact)
        + extras.get(anchor, "")
        for anchor, title, purpose, method, observation, conclusion, impact in specs
    ]
    toc = "\n".join(
        f"- [{title}](#{_chapter_anchor(title)})" for _anchor, title, *_ in specs
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


def validate_report_tree(root: Path, *, report_name: str = REPORT_NAME) -> list[str]:
    errors: list[str] = []
    report = root / report_name
    if not report.is_file():
        return [f"missing report: {report}"]
    text = report.read_text(encoding="utf-8")
    headings = re.findall(
        r"^## ((?:0[1-9]|1[0-4])｜[^\n]+)$", text, flags=re.MULTILINE
    )
    anchors = {_chapter_anchor(title) for title in headings}
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


def validate_report_schema(root: Path, *, report_name: str = REPORT_NAME) -> list[str]:
    report = root / report_name
    if not report.is_file():
        return ["report missing"]
    text = report.read_text(encoding="utf-8")
    errors: list[str] = []
    if "<a id=" in text:
        errors.append("raw HTML anchor is forbidden")
    numbers = re.findall(
        r"^## (0[1-9]|1[0-4])｜[^\n]+$", text, flags=re.MULTILINE
    )
    expected = [f"{index:02d}" for index in range(1, 15)]
    if numbers != expected:
        errors.append("report must contain ordered chapters 01 through 14")
    required_subheads = (
        "### 測試目的", "### 測試方法", "### 觀察結果",
        "### 白話結論", "### 對最終決策的影響",
    )
    for number in expected:
        match = re.search(
            rf"^## {number}｜[^\n]+$([\s\S]*?)(?=^## (?:0[1-9]|1[0-4])｜[^\n]+$|\Z)",
            text,
            flags=re.MULTILINE,
        )
        if match is None or any(head not in match.group(1) for head in required_subheads):
            errors.append(f"chapter {number} missing required prose structure")
    required_headers = (
        "成交方式", "期末資產", "總報酬", "年化報酬率", "最大回落",
        "獲利／虧損／平盤年度", "進場／出場／未平倉", "最大投入",
    )
    if any(header not in text for header in required_headers):
        errors.append("complete performance table headers missing")
    observations = re.findall(
        r"^### 觀察結果\s*$\n\s*([^\n]+)", text, flags=re.MULTILINE
    )
    if len(observations) != 14 or len(set(observations)) != 14:
        errors.append("all 14 chapters require distinct observation prose")

    manifest_path = root / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        pipeline_pack = manifest.get("schema") in {
            "00635U_FORMAL_RUN_V1",
            "00635U_SYNTHETIC_FIXTURE_RUN_V1",
            "00635U_STRICT_HIGH_REGIME_STUDY_V1",
            "00635U_MACD_ONLY_STUDY_V1",
            "00635U_KD_CYCLE_ENTRY_STUDY_V1",
            "00635U_KD_9_9_55_CYCLE_ENTRY_STUDY_V2",
            "00635U_KD_9_9_55_CYCLE_ENTRY_STUDY_V3_R2",
        }
        if pipeline_pack:
            manifest_paths = {item.get("path") for item in manifest.get("files", [])}
            if report_name != "README.md" or "README.md" not in manifest_paths:
                errors.append("formal main report must be README.md and manifest-bound")
            if "trade-details.md" not in manifest_paths:
                errors.append("formal readable trade report missing from manifest")
            if "[繁體中文逐筆交易明細](trade-details.md)" not in text:
                errors.append("formal readable trade report link missing")
            if re.search(r"(?<![\d-])-?\d+\.\d{3,}", text):
                errors.append("formal report contains unformatted long decimal")
            if any(code in text for code in (
                "A_FIXED_STOP_MACD_21_55_55",
                "B_FIXED_STOP_2R_KD_9_9_55",
            )):
                errors.append("formal report exposes internal strategy code")
            macd_only_pack = "純 MACD 進出場隔離研究" in text
            required_rule_phrases = (
                (
                    "MA21／MA144", "只留作診斷", "MACD 固定為 (21,55,55)",
                    "histogram < 0", "固定初始防守", "完整費後達 2R",
                    "KD(9,9,55)", "優先於其他出場", "訊號日收盤成交",
                    "次一個可成交交易日開盤成交",
                )
                if macd_only_pack else
                (
                    "MA21", "MA144", "MACD(21,55,55)", "固定初始防守",
                    "淨成本 2R", "KD(9,9,55)", "固定防守優先",
                    "訊號日收盤成交", "次一個可成交交易日開盤成交",
                )
            )
            if any(phrase not in text for phrase in required_rule_phrases):
                errors.append("formal report is missing self-contained strategy rules")
        expected_charts = {
            ("equity-chart.svg" if pipeline_pack else "fixture-equity-chart.svg"): "equity_curve",
            "drawdown-chart.svg": "drawdown",
            "annual-chart.svg": "annual_return",
            "rolling-window-chart.svg": "rolling_window",
            "regime-chart.svg": "regime",
            "trade-pnl-chart.svg": "trade_pnl",
            "cost-pressure-chart.svg": "cost_pressure",
            "core-allocation-chart.svg": "core_allocation",
            "parameter-grid-chart.svg": "parameter_grid",
        }
        source_hashes: list[str] = []
        for name, expected_kind in expected_charts.items():
            chart = root / name
            if not chart.is_file():
                errors.append(f"missing semantic chart: {name}")
                continue
            svg = chart.read_text(encoding="utf-8")
            kind = re.search(r'data-chart-kind="([^"]+)"', svg)
            source_hash = re.search(r'data-source-sha256="([0-9a-f]{64})"', svg)
            if kind is None or kind.group(1) != expected_kind:
                errors.append(f"chart semantic mismatch: {name}")
            if pipeline_pack and any(code in svg for code in (
                "STRATEGY_", "A_FIXED_STOP_MACD_21_55_55",
                "B_FIXED_STOP_2R_KD_9_9_55",
            )):
                errors.append(f"chart legend exposes internal strategy code: {name}")
            if source_hash is None:
                errors.append(f"chart source hash missing: {name}")
            else:
                source_hashes.append(source_hash.group(1))
        if source_hashes and len(set(source_hashes)) < 7:
            errors.append("charts do not use sufficiently distinct source datasets")
    if "[逐筆交易明細 CSV](trade-details.csv)" not in text:
        errors.append("chapter 06 prominent trade-details link missing")
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
    daily_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    rolling_rows: list[dict[str, Any]] = []
    run_results: dict[str, Any] = {}
    for strategy in StrategyVersion:
        for timing in ExecutionTiming:
            result = run_strategy(rows, ETFResearchConfig(strategy=strategy, timing=timing))
            label = f"策略{strategy.name}-{timing.value}"
            run_results[label] = result
            curves[label] = [item["equity"] for item in result.state.daily]
            curve = tuple((item["date"], item["equity"]) for item in result.state.daily)
            metrics = research.performance_metrics(curve, result.state.trades)
            summaries.append({
                "strategy": strategy.name,
                "timing": timing.value,
                "final_equity": metrics["ending_equity"],
                "total_return": metrics["total_return"],
                "cagr": metrics["cagr"],
                "max_drawdown": metrics["max_drawdown"],
                "profitable_years": metrics["profitable_years"],
                "losing_years": metrics["losing_years"],
                "flat_years": metrics["flat_years"],
                "closed_positions": metrics["closed_positions"],
                "open_positions": metrics["open_positions"],
                "maximum_single_entry_cash": metrics["maximum_single_entry_cash"],
                "entries": sum(item["role"] == "ENTRY" for item in result.state.trades),
                "exits": sum(item["role"] == "EXIT" for item in result.state.trades),
                "trades": len(result.state.trades),
                "fixture_only": True,
            })
            daily_rows.extend({"strategy": strategy.name, "timing": timing.value, **item} for item in result.state.daily)
            trade_rows.extend({"strategy": strategy.name, "timing": timing.value, **item} for item in result.state.trades)
            event_rows.extend({"strategy": strategy.name, "timing": timing.value, **item} for item in result.state.events)
            annual_rows.extend({"strategy": strategy.name, "timing": timing.value, **item} for item in research.annual_slices(curve))
            for years in (3, 5, 10):
                rolling_rows.extend({"strategy": strategy.name, "timing": timing.value, **item} for item in research.rolling_windows(curve, years))
    _write_csv(output_root / "fixture-summary.csv", summaries)
    _write_csv(output_root / "daily-equity.csv", daily_rows)
    _write_csv(output_root / "trade-details.csv", trade_rows or [{"status": "NO_TRADES"}])
    _write_csv(output_root / "event-ledger.csv", event_rows or [{"status": "NO_EVENTS"}])
    _write_csv(output_root / "annual-performance.csv", annual_rows)
    _write_csv(output_root / "rolling-windows.csv", rolling_rows)
    _write_csv(output_root / "cost-sensitivity.csv", research.cost_sensitivity(rows))
    grid_rows = [
        {
            "strategy": strategy.name,
            "ma_fast": cell[0], "ma_slow": cell[1],
            "macd_fast": cell[2], "macd_slow": cell[3], "macd_signal": cell[4],
            "kd": "9/9/55", "two_r": "2R", "status": "SYNTHETIC_SCHEMA_ONLY",
        }
        for strategy in StrategyVersion
        for cell in research.parameter_grid()
    ]
    _write_csv(output_root / "parameter-grid-schema.csv", grid_rows)
    core_rows: list[dict[str, Any]] = []
    core_summary: list[dict[str, Any]] = []
    core_ledger: list[dict[str, Any]] = []
    for strategy in StrategyVersion:
        allocation = research.run_core_allocation_study(
            rows, strategy=strategy, timing=ExecutionTiming.NEXT_SESSION_OPEN
        )
        core_rows.extend({"strategy": strategy.name, **item} for item in allocation["daily"])
        core_summary.extend({"strategy": strategy.name, **item} for item in allocation["summary"])
        core_ledger.extend({"strategy": strategy.name, **item} for item in allocation["ledger"])
    _write_csv(output_root / "core-allocation-daily.csv", core_rows)
    _write_csv(output_root / "core-allocation-summary.csv", core_summary)
    _write_csv(output_root / "core-allocation-ledger.csv", core_ledger or [{"status": "NO_TRANSACTIONS"}])
    gold = {row.day: Decimal("100") + index for index, row in enumerate(rows)}
    gold_line = research.run_index_line(gold, [row.day for row in rows])
    _write_csv(
        output_root / "index-alignment-ledger.csv",
        list(gold_line.audit["alignment_ledger"]),
    )
    drawdowns = {}
    for label, result in run_results.items():
        peak = Decimal("0")
        values = []
        for item in result.state.daily:
            peak = max(peak, item["equity"])
            values.append(Decimal("0") if not peak else item["equity"] / peak - 1)
        drawdowns[label] = values
    annual_series = {
        label: [item["return"] for item in research.annual_slices(
            tuple((row["date"], row["equity"]) for row in result.state.daily)
        )]
        for label, result in run_results.items()
    }
    rolling_series = {label: (Decimal(index + 1),) for index, label in enumerate(run_results)}
    regime_series = {
        label: [item["compounded_return"] for item in research.regime_summary(result.state.daily)]
        for label, result in run_results.items()
    }
    trade_series = {
        label: [item["net_pnl"] for item in result.state.trades if item["role"] == "EXIT"]
        for label, result in run_results.items()
    }
    cost_rows = research.cost_sensitivity(rows)
    cost_series = {
        strategy.name: [
            item["ending_equity"] for item in cost_rows
            if item["strategy"] == strategy.value and item["timing"] == ExecutionTiming.NEXT_SESSION_OPEN.value
        ]
        for strategy in StrategyVersion
    }
    core_series = {
        strategy.name: [item["ending_equity"] for item in core_summary if item["strategy"] == strategy.name]
        for strategy in StrategyVersion
    }
    grid_series = {
        strategy.name: [Decimal(item["ma_fast"] + item["ma_slow"] + item["macd_fast"] + item["macd_slow"] + item["macd_signal"])
                        for item in grid_rows if item["strategy"] == strategy.name]
        for strategy in StrategyVersion
    }
    chart_specs = {
        "fixture-equity-chart.svg": ("equity_curve", "合成夾具資產曲線（不可當績效）", curves, "交易序列", "資產（元）"),
        "drawdown-chart.svg": ("drawdown", "合成夾具回撤", drawdowns, "交易序列", "回撤率"),
        "annual-chart.svg": ("annual_return", "合成夾具逐年報酬", annual_series, "年度", "報酬率"),
        "rolling-window-chart.svg": ("rolling_window", "合成夾具滾動窗口結構", rolling_series, "窗口", "測試序號"),
        "regime-chart.svg": ("regime", "合成夾具市場狀態", regime_series, "市場狀態", "複合報酬"),
        "trade-pnl-chart.svg": ("trade_pnl", "合成夾具逐筆交易損益", trade_series, "交易序號", "淨損益（元）"),
        "cost-pressure-chart.svg": ("cost_pressure", "合成夾具成本壓力", cost_series, "滑價情境", "期末資產（元）"),
        "core-allocation-chart.svg": ("core_allocation", "合成夾具核心配置", core_series, "核心比例", "期末資產（元）"),
        "parameter-grid-chart.svg": ("parameter_grid", "合成夾具 243 格結構", grid_series, "參數格", "參數合計"),
    }
    for name, (kind, title, source, x_label, y_label) in chart_specs.items():
        (output_root / name).write_text(
            _chart_svg(source, title=title, kind=kind, x_label=x_label, y_label=y_label),
            encoding="utf-8",
        )
    (output_root / REPORT_NAME).write_text(
        render_fixture_report(summaries), encoding="utf-8"
    )

    artifact_names = (
        REPORT_NAME, "fixture-rows.csv", "fixture-summary.csv",
        "daily-equity.csv", "trade-details.csv", "event-ledger.csv",
        "annual-performance.csv", "rolling-windows.csv", "cost-sensitivity.csv",
        "parameter-grid-schema.csv", "core-allocation-summary.csv",
        "core-allocation-daily.csv", "core-allocation-ledger.csv",
        "index-alignment-ledger.csv", "fixture-equity-chart.svg",
        "drawdown-chart.svg", "annual-chart.svg", "rolling-window-chart.svg",
        "regime-chart.svg", "trade-pnl-chart.svg", "cost-pressure-chart.svg",
        "core-allocation-chart.svg", "parameter-grid-chart.svg",
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
    errors = validate_report_tree(output_root) + validate_report_schema(output_root)
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
    parser.add_argument("--high-regime-study", action="store_true")
    parser.add_argument("--macd-only-study", action="store_true")
    args = parser.parse_args(argv)
    if args.mode == "formal":
        validate_formal_snapshot_gate(
            args.snapshot_manifest,
            accepted_manifest_sha256=args.accepted_manifest_sha256,
            accepted_by=args.accepted_by,
        )
        from .etf_00635u_formal_v1 import run_formal_pack
        print(json.dumps(run_formal_pack(
            args.snapshot_manifest,
            args.output_root,
            accepted_manifest_sha256=args.accepted_manifest_sha256,
            high_regime_study=args.high_regime_study,
            macd_only_study=args.macd_only_study,
        ), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(build_fixture_pack(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
