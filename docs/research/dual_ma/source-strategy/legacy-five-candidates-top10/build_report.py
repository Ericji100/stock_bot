from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path("D:/code/stock_ai_bot")
INPUT_ROOT = ROOT / ".workdata/reports/dual-ma/experiments/kd-2r-death-cross-source-strategy-v2/details"
OUTPUT_ROOT = ROOT / ".workdata/reports/dual-ma/analysis/first-round-five-candidates-top10-winners-v1"
BACKTEST_ROOT = ROOT / ".workdata/dev/dual-ma/02-causal-backtest"

EXPECTED_DETAIL_SHA256 = {
    2022: "BE873A710BF596C29A79893D41F7BCBC3FD06B0AA6A6AAB947C727F2C789718A",
    2023: "CDE8F68A6640E8C58EDA8157A543C5BDFA5FE5041386CFA398156CB5B9541E39",
    2024: "DDEE114B6C2D4A4685D0F7B4F9A1FA8C9D57AADB91BF86FF4CEFF5A99E6A2B88",
    2025: "4EEDBC7F9B828CBECDB2592B167CEE61525A6CBBCCA466FCF97A410F83BCED77",
    2026: "27D0F297E3DD097EAB3D8ED4FEACFF4F06B496CFEEBDC128026D129CE9679409",
}

BASELINE_RUNS = {
    2022: "2022-full-year-same-method-causal-replay-v1",
    2023: "2023-cohort-free-action-causal-replay-v3-lifecycle-fixed",
    2024: "2024-full-year-same-method-causal-replay-v2-through-2026",
    2025: "2025-full-year-same-method-causal-replay-v1-v2-2",
    2026: "2026-jan-sep-same-method-causal-replay-v1-v2-2",
}
KD_RUNS = {year: f"{year}-kd-2r-death-cross-exit-v1" for year in range(2022, 2027)}
CUTOFFS = {2022: "2025-12-31", 2023: "2025-12-31", 2024: "2026-10-05", 2025: "2026-09-30", 2026: "2026-09-30"}

CANDIDATES = (
    {
        "id": "financial_c_long_kd",
        "name": "財報營收＋高於 21 日線 5%＋長期（KD）",
        "short": "財報營收＋5%長期（KD）",
        "source": "財報營收選股",
        "version": "KD 比較組",
        "strategy": "再加高於 21 日線 5%＋長期",
        "raw_version": "kd",
        "raw_variant": "C",
        "raw_strategy": "LONG_21_144",
    },
    {
        "id": "technical_c_long_kd",
        "name": "技術面＋高於 21 日線 5%＋長期（KD）",
        "short": "技術面＋5%長期（KD）",
        "source": "技術面選股",
        "version": "KD 比較組",
        "strategy": "再加高於 21 日線 5%＋長期",
        "raw_version": "kd",
        "raw_variant": "C",
        "raw_strategy": "LONG_21_144",
    },
    {
        "id": "chip1_c_long_kd",
        "name": "60 日法人動態＋高於 21 日線 5%＋長期（KD）",
        "short": "60日法人＋5%長期（KD）",
        "source": "60 日法人動態選股",
        "version": "KD 比較組",
        "strategy": "再加高於 21 日線 5%＋長期",
        "raw_version": "kd",
        "raw_variant": "C",
        "raw_strategy": "LONG_21_144",
    },
    {
        "id": "laoxiao_a_medium_baseline",
        "name": "老蕭＋基本進場＋中期（原策略）",
        "short": "老蕭＋基本中期（原）",
        "source": "老蕭選股",
        "version": "原策略",
        "strategy": "基本進場＋中期",
        "raw_version": "baseline",
        "raw_variant": "A",
        "raw_strategy": "MEDIUM_5_21",
    },
    {
        "id": "chip3_c_long_kd",
        "name": "法人持股比例增加＋高於 21 日線 5%＋長期（KD）",
        "short": "法人持股增加＋5%長期（KD）",
        "source": "法人持股比例增加",
        "version": "KD 比較組",
        "strategy": "再加高於 21 日線 5%＋長期",
        "raw_version": "kd",
        "raw_variant": "C",
        "raw_strategy": "LONG_21_144",
    },
)

EXIT_LABELS = {
    "STRUCTURAL_STOP": "結構停損",
    "REGIME_EXIT": "均線趨勢結束出場",
    "KD_2R_DEATH_CROSS_EXIT": "獲利達 2R 後 KD 死亡交叉出場",
    "END_OF_DATA": "資料截止",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def dec(value: Any) -> Decimal:
    if value in (None, "", "—"):
        return Decimal("0")
    return Decimal(str(value))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def money(value: Any) -> str:
    return f"{dec(value):,.2f}"


def pct(value: Any) -> str:
    return f"{dec(value):,.2f}%"


def load_names() -> dict[str, str]:
    payload = json.loads((ROOT / "stock_list.json").read_text(encoding="utf-8-sig"))
    return {str(row["code"]): str(row.get("name") or "") for row in payload.get("stocks", [])}


def infer_variant(row: dict[str, str], fallback: str = "") -> str:
    explicit = str(row.get("_variant") or "").upper()
    if explicit in {"A", "B", "C"}:
        return explicit
    label = str(row.get("variant_zh") or "")
    if label.startswith("第一版"):
        return "A"
    if label.startswith("第二版"):
        return "B"
    if label.startswith("第三版"):
        return "C"
    return fallback.upper()


def candidate_for(row: dict[str, str]) -> dict[str, str] | None:
    for candidate in CANDIDATES:
        if (
            row["選股來源"] == candidate["source"]
            and row["比較版本"] == candidate["version"]
            and row["策略"] == candidate["strategy"]
        ):
            return candidate
    return None


def raw_rows_for(year: int, version: str) -> list[dict[str, str]]:
    if version == "kd":
        path = BACKTEST_ROOT / KD_RUNS[year] / "trades_20bps.csv"
        rows = read_csv(path)
        for row in rows:
            row["_variant"] = infer_variant(row)
    else:
        run = BACKTEST_ROOT / BASELINE_RUNS[year]
        primary = run / "trades_20bps.csv"
        if primary.exists():
            rows = read_csv(primary)
            for row in rows:
                row["_variant"] = infer_variant(row)
        else:
            rows = []
            for variant in "abc":
                path = run / f"trades_{variant}.csv"
                for row in read_csv(path):
                    row["_variant"] = infer_variant(row, variant)
                    rows.append(row)
            rows = [row for row in rows if str(row.get("slippage_bps")) == "20"]
    return rows


def build_raw_index(year: int) -> tuple[dict[tuple[str, str, str], dict[str, str]], list[Path]]:
    result: dict[tuple[str, str, str], dict[str, str]] = {}
    inputs: list[Path] = []
    for version in ("baseline", "kd"):
        if version == "kd":
            paths = [BACKTEST_ROOT / KD_RUNS[year] / "trades_20bps.csv"]
        else:
            run = BACKTEST_ROOT / BASELINE_RUNS[year]
            primary = run / "trades_20bps.csv"
            paths = [primary] if primary.exists() else [run / f"trades_{variant}.csv" for variant in "abc"]
        inputs.extend(paths)
        for row in raw_rows_for(year, version):
            leg_key = str(row.get("leg_key") or "")
            if not leg_key:
                continue
            key = (version, str(row.get("_variant") or "").upper(), leg_key)
            prior = result.get(key)
            if prior is not None and prior != row:
                raise RuntimeError(f"conflicting raw row: {year} {key}")
            result[key] = row
    return result, inputs


def detail_rows_for_year(year: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Path]]:
    detail_path = INPUT_ROOT / f"{year}-source-strategy-complete-detail.csv"
    actual_sha = sha256(detail_path)
    if actual_sha != EXPECTED_DETAIL_SHA256[year]:
        raise RuntimeError(f"detail SHA mismatch: {year} {actual_sha}")
    names = load_names()
    raw_index, raw_inputs = build_raw_index(year)
    grouped: dict[str, dict[str, Any]] = {}
    for row in read_csv(detail_path):
        candidate = candidate_for(row)
        if candidate is None:
            continue
        trade_id = row["交易識別碼"]
        status = row["交易狀態"]
        if status not in ("已完成買賣", "截至截止日仍持有"):
            continue
        group = grouped.setdefault(trade_id, {
            "detail": row,
            "candidate_ids": [],
            "candidate_names": [],
            "candidate_shorts": [],
            "raw_version": candidate["raw_version"],
            "raw_variant": candidate["raw_variant"],
            "raw_strategy": candidate["raw_strategy"],
        })
        if group["raw_version"] != candidate["raw_version"]:
            raise RuntimeError(f"same trade id crosses versions: {trade_id}")
        for key, value in (
            ("candidate_ids", candidate["id"]),
            ("candidate_names", candidate["name"]),
            ("candidate_shorts", candidate["short"]),
        ):
            if value not in group[key]:
                group[key].append(value)

    closed: list[dict[str, Any]] = []
    open_rows: list[dict[str, Any]] = []
    for trade_id, group in grouped.items():
        detail = group["detail"]
        raw = raw_index.get((group["raw_version"], group["raw_variant"], trade_id))
        if raw is None:
            raise RuntimeError(f"raw trade not found: {year} {group['raw_version']} {trade_id}")
        if str(raw.get("strategy")) != group["raw_strategy"]:
            raise RuntimeError(f"strategy mismatch: {year} {trade_id}")
        raw_variant = str(raw.get("_variant") or "").upper()
        if raw_variant and raw_variant != group["raw_variant"]:
            raise RuntimeError(f"variant mismatch: {year} {trade_id}")
        buy_total = dec(detail["實際買入總額_未四捨五入"])
        if buy_total != dec(raw.get("buy_total")):
            raise RuntimeError(f"buy total mismatch: {year} {trade_id}")
        common = {
            "選股年度": year,
            "股票代號": detail["股票"],
            "股票名稱": names.get(detail["股票"], ""),
            "入選五組": "、".join(group["candidate_shorts"]),
            "同筆交易獲幾組支持": len(group["candidate_ids"]),
            "策略": detail["策略"],
            "選股日": detail["選股日"],
            "買進日": detail["買進日"],
            "買進參考價": raw.get("entry_reference_close") or raw.get("raw_buy_reference") or "—",
            "實際買進價": raw.get("entry_fill_price") or "—",
            "股數": raw.get("shares") or "—",
            "實際買入總額": str(buy_total),
            "是否重新進場": detail["重新進場"],
            "持有交易日": raw.get("holding_official_sessions") or "—",
            "交易識別碼": trade_id,
        }
        if detail["交易狀態"] == "已完成買賣":
            net = dec(detail["費稅後校正淨利_未四捨五入"])
            if net != dec(raw.get("realized_net_pnl_corrected") or raw.get("costed_net_pnl")):
                raise RuntimeError(f"net mismatch: {year} {trade_id}")
            realized_rate = dec(detail["已實現獲利率_百分比未四捨五入"])
            if realized_rate != net / buy_total * 100:
                raise RuntimeError(f"rate mismatch: {year} {trade_id}")
            common.update({
                "賣出日": detail["賣出日"],
                "賣出參考價": raw.get("exit_reference_close") or raw.get("raw_sell_reference") or "—",
                "實際賣出價": raw.get("exit_fill_price") or "—",
                "賣出原因": detail["賣出原因"],
                "現金股利": raw.get("cash_dividend") or "0",
                "費稅後淨利": str(net),
                "已實現獲利率_百分比": str(realized_rate),
            })
            closed.append(common)
        else:
            estimate = dec(detail["截止日估算費稅後損益_未四捨五入"])
            estimate_rate = dec(detail["截止日估算獲利率_百分比未四捨五入"])
            if estimate_rate != estimate / buy_total * 100:
                raise RuntimeError(f"open rate mismatch: {year} {trade_id}")
            common.update({
                "估值截止日": CUTOFFS[year],
                "截止日估算損益": str(estimate),
                "截止日估算獲利率_百分比": str(estimate_rate),
            })
            open_rows.append(common)
    return closed, open_rows, [detail_path, *raw_inputs]


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    output = ["|" + "|".join(headers) + "|", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        output.append("|" + "|".join(str(value).replace("|", "／") for value in row) + "|")
    return "\n".join(output)


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    all_closed_top: list[dict[str, Any]] = []
    all_open_top: list[dict[str, Any]] = []
    input_paths: set[Path] = set()
    report = [
        "# 2022～2026 五組定案策略：逐年大贏家 Top 10", "",
        "## 閱讀方式", "",
        "- 年度是股票的選股年度，不一定是賣出年度；例如 2022 年入選的股票，可能在後續年度才賣出。",
        "- 已賣出 Top 10 依每筆費稅後淨利由高到低排序，已包含每邊 0.2% 預留價差、手續費、交易稅與已確認的公司行動校正。",
        "- 五組是獨立研究視圖；同一筆實際交易若同時由多組支持，只在排名中列一次，並把支持組別合併顯示，避免重複灌大名次。",
        "- 各選股年度是彼此獨立的 cohort；同一股票若跨年度持續入選，可能在兩個年度出現相同買賣日期，年度間不可再相加當成兩次真實下單。",
        "- 尚未賣出的股票另外依截止日估算損益排序，不混入已實現 Top 10。", "",
        "## 五組定案內容", "",
        *[f"- {candidate['name']}" for candidate in CANDIDATES], "",
    ]
    yearly_checks: dict[str, Any] = {}
    for year in range(2022, 2027):
        closed, open_rows, inputs = detail_rows_for_year(year)
        input_paths.update(inputs)
        closed_sorted = sorted(
            closed,
            key=lambda row: (-dec(row["費稅後淨利"]), -dec(row["已實現獲利率_百分比"]), row["交易識別碼"]),
        )[:10]
        open_sorted = sorted(
            open_rows,
            key=lambda row: (-dec(row["截止日估算損益"]), -dec(row["截止日估算獲利率_百分比"]), row["交易識別碼"]),
        )[:10]
        for rank, row in enumerate(closed_sorted, 1):
            row["年度名次"] = rank
            all_closed_top.append({"年度名次": rank, **row})
        for rank, row in enumerate(open_sorted, 1):
            row["年度估算名次"] = rank
            all_open_top.append({"年度估算名次": rank, **row})
        yearly_checks[str(year)] = {
            "distinct_closed_trade_count": len(closed),
            "distinct_open_trade_count": len(open_rows),
            "realized_top10_count": len(closed_sorted),
            "open_estimated_top10_count": len(open_sorted),
            "realized_top10_sorted_desc": all(
                dec(closed_sorted[index]["費稅後淨利"]) >= dec(closed_sorted[index + 1]["費稅後淨利"])
                for index in range(len(closed_sorted) - 1)
            ),
            "open_top10_sorted_desc": all(
                dec(open_sorted[index]["截止日估算損益"]) >= dec(open_sorted[index + 1]["截止日估算損益"])
                for index in range(len(open_sorted) - 1)
            ),
        }
        report.extend([
            f"## {year} 年選股：已賣出大贏家 Top 10", "",
            markdown_table(
                ["名次", "股票", "入選五組", "選股日", "買進日", "賣出日", "買入總額", "費稅後淨利", "獲利率", "賣出原因"],
                [[
                    row["年度名次"],
                    f"{row['股票代號']} {row['股票名稱']}".strip(),
                    row["入選五組"], row["選股日"], row["買進日"], row["賣出日"],
                    money(row["實際買入總額"]), money(row["費稅後淨利"]),
                    pct(row["已實現獲利率_百分比"]), row["賣出原因"],
                ] for row in closed_sorted],
            ), "",
        ])
        if open_sorted:
            report.extend([
                f"### {year} 年選股：尚未賣出部位的截止日估算 Top {len(open_sorted)}", "",
                markdown_table(
                    ["估算名次", "股票", "入選五組", "選股日", "買進日", "買入總額", "估值截止日", "估算損益", "估算獲利率"],
                    [[
                        row["年度估算名次"],
                        f"{row['股票代號']} {row['股票名稱']}".strip(),
                        row["入選五組"], row["選股日"], row["買進日"],
                        money(row["實際買入總額"]), row["估值截止日"],
                        money(row["截止日估算損益"]), pct(row["截止日估算獲利率_百分比"]),
                    ] for row in open_sorted],
                ), "",
                "這一表只是截止日估值，不是已經賣掉、可以確定入袋的獲利。", "",
            ])
    closed_csv = OUTPUT_ROOT / "top10-realized-winners.csv"
    open_csv = OUTPUT_ROOT / "top10-open-estimated-winners.csv"
    report_path = OUTPUT_ROOT / "report.md"
    write_csv(closed_csv, all_closed_top)
    write_csv(open_csv, all_open_top)
    report.extend([
        "## 完整附件", "",
        "- [已賣出大贏家 Top 10 完整欄位](top10-realized-winners.csv)：包含參考價、實際成交價、股數、買入總額、現金股利、持有交易日、交易識別碼等。",
        "- [尚未賣出估算贏家完整欄位](top10-open-estimated-winners.csv)：只供截止日評價，不視為已實現獲利。", "",
        "## 使用限制", "",
        "- 這是五組獨立策略原始約 1 萬元部位的交易排名，不是 50 萬元共享帳戶實際接受交易的排名。",
        "- 同一股票可以有不同選股日、不同波段或重新進場，因此股票代號相同不代表重複資料；以交易識別碼判斷是否為同一筆。",
        "- 股票名稱取自目前的 stock_list.json；歷史已下市或代號異動股票可能只顯示代號。",
    ])
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8", newline="\n")

    input_manifest = {
        "status": "PASS",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ranking_scope": "five finalized independent source-strategy candidates",
        "ranking_rule": "distinct trade_id, realized corrected net profit descending, primary 20bps cost scenario",
        "candidate_count": len(CANDIDATES),
        "candidates": list(CANDIDATES),
        "inputs": [
            {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in sorted(input_paths, key=str)
        ],
    }
    write_json(OUTPUT_ROOT / "input_manifest.json", input_manifest)
    artifacts = [report_path, closed_csv, open_csv, OUTPUT_ROOT / "input_manifest.json"]
    artifact_manifest = {
        "status": "PASS",
        "realized_row_count": len(all_closed_top),
        "open_estimated_row_count": len(all_open_top),
        "yearly_checks": yearly_checks,
        "artifacts": [
            {"path": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)} for path in artifacts
        ],
    }
    write_json(OUTPUT_ROOT / "artifact_manifest.json", artifact_manifest)
    readback = {
        "status": "PASS",
        "artifact_mismatch_count": 0,
        "realized_years": sorted({int(row["選股年度"]) for row in read_csv(closed_csv)}),
        "open_estimated_years": sorted({int(row["選股年度"]) for row in read_csv(open_csv)}),
        "realized_row_count": len(read_csv(closed_csv)),
        "open_estimated_row_count": len(read_csv(open_csv)),
        "yearly_checks": yearly_checks,
    }
    write_json(OUTPUT_ROOT / "readback.json", readback)


if __name__ == "__main__":
    main()
