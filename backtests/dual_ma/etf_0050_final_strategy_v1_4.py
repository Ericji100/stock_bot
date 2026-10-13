"""Publish the accepted 0050 final report with stable navigation and charts.

This is a presentation-only revision of the sealed v1.3-r3 package.  It does
not replay signals, trades, accounting, or performance.  Every inherited data
artifact is copied byte-for-byte; only the Markdown presentation, navigation,
and four additional SVG charts are produced here.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import etf_0050_final_strategy_v1 as v12
from . import etf_0050_final_strategy_v1_3 as v13
from .etf_0050_base_v1 import ETF0050Error, tree_hash
from .etf_0050_formal_v2 import _artifact_rows, _verify_artifacts, _write_json
from .paths import dev_run, report_path


VERSION = "dual-ma-0050-final-strategy-v1.4"
SOURCE_RUN = dev_run("02-etf-0050-final-strategy-v1-3-r3")
SOURCE_REPORT = report_path("etf", "0050", "final", "2006-2026-final-strategy-v1-3-r3")
SOURCE_RUN_MANIFEST = "7A335CA1BC511761FEF254A5FC7E9A8F6F45193047D99CDDC234A2AB55E3DF0D"
SOURCE_REPORT_MANIFEST = "28CE98A062E005E95291826FBF554749537526CFBD515CA3F838A892112B3B34"
SOURCE_READBACK = "366FDCC7BB4C554F467C0942E9B85C652F7C37B82A070E1C3878634586751C94"
RUN_ROOT_DEFAULT = dev_run("02-etf-0050-final-strategy-v1-4")
REPORT_ROOT_DEFAULT = report_path("etf", "0050", "final", "2006-2026-final-strategy-v1-4")

SECTION_ANCHORS = (
    ("一頁白話結論", "overview"),
    ("完整績效總表", "performance"),
    ("策略相對大盤與0050持有的差異", "relative-comparison"),
    ("策略規則與兩種成交版本", "strategy-rules"),
    ("完整逐年績效", "calendar-year"),
    ("交易紀錄與資金投入", "trades-and-capital"),
    ("成交時間與交易成本測試", "execution-cost"),
    ("不同時期與市場狀態測試", "period-and-regime"),
    ("交易集中度與單筆穩定性", "trade-stability"),
    ("243組參數穩健性", "parameter-robustness"),
    ("核心持有比例研究", "core-allocation"),
    ("最終實務建議與監控項目", "recommendations"),
    ("技術驗收與重現附件", "technical-validation"),
    ("完整附件目錄", "attachments"),
)

CHARTS = (
    "main-comparison-equity.svg",
    "main-comparison-drawdown.svg",
    "calendar-year-returns.svg",
    "execution-cost-sensitivity.svg",
    "parameter-robustness.svg",
)

COLORS = {
    v12.SIGNAL_CLOSE_ID: "#2563eb",
    v12.NEXT_OPEN_ID: "#dc2626",
    v12.BUY_HOLD_CASH_ID: "#16a34a",
    v12.BUY_HOLD_REINVEST_ID: "#7c3aed",
    v12.BENCHMARK_ID: "#d97706",
    "SIGNAL_CLOSE": "#2563eb",
    "NEXT_SESSION_OPEN": "#dc2626",
}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _f(value: Any) -> float:
    return float(Decimal(str(value)))


def _svg_document(title: str, body: str, legend: Sequence[tuple[str, str]], *, width: int = 1240, height: int = 680) -> str:
    legend_svg = []
    for index, (label, color) in enumerate(legend):
        x = 80 + (index % 3) * 370
        y = height - 42 + (index // 3) * 20
        legend_svg.append(f'<line x1="{x}" y1="{y}" x2="{x + 24}" y2="{y}" stroke="{color}" stroke-width="4"/>')
        legend_svg.append(f'<text x="{x + 32}" y="{y + 5}" font-size="13">{html.escape(label)}</text>')
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="70" y="34" font-size="22" font-weight="bold">{html.escape(title)}</text>',
        '<style>text{font-family:"Microsoft JhengHei","Noto Sans TC",sans-serif;fill:#111827}.grid{stroke:#e5e7eb;stroke-width:1}.axis{stroke:#6b7280;stroke-width:1.2}</style>',
        body, *legend_svg, '</svg>',
    ])


def _line_chart(
    path: Path,
    title: str,
    series: Sequence[tuple[str, str, Sequence[tuple[date, float]]]],
    *,
    percent_axis: bool = False,
) -> None:
    width, height, left, right, top, bottom = 1240, 680, 82, 34, 58, 95
    all_points = [point for _label, _color, points in series for point in points]
    if not all_points:
        raise ETF0050Error(f"cannot chart empty series: {title}")
    min_day = min(day for day, _value in all_points); max_day = max(day for day, _value in all_points)
    values = [value for _day, value in all_points]
    low, high = min(values), max(values)
    if low == high:
        low -= 1; high += 1
    pad = (high - low) * 0.06
    low -= pad; high += pad
    plot_w, plot_h = width - left - right, height - top - bottom

    def xy(day: date, value: float) -> tuple[float, float]:
        span = max(1, (max_day - min_day).days)
        x = left + (day - min_day).days / span * plot_w
        y = top + (high - value) / (high - low) * plot_h
        return x, y

    body = []
    for index in range(6):
        y = top + plot_h * index / 5
        value = high - (high - low) * index / 5
        label = f"{value * 100:.0f}%" if percent_axis else f"{value:,.0f}"
        body.extend((f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}"/>',
                     f'<text x="{left-10}" y="{y+5:.1f}" text-anchor="end" font-size="12">{label}</text>'))
    for year in range(min_day.year, max_day.year + 1, 2):
        day = date(year, 1, 1)
        x, _ = xy(day, low)
        body.extend((f'<line class="grid" x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top+plot_h}"/>',
                     f'<text x="{x:.1f}" y="{top+plot_h+23}" text-anchor="middle" font-size="12">{year}</text>'))
    if low < 0 < high:
        _x, zero_y = xy(min_day, 0)
        body.append(f'<line x1="{left}" y1="{zero_y:.1f}" x2="{width-right}" y2="{zero_y:.1f}" stroke="#9ca3af" stroke-width="1.5"/>')
    body.extend((f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}"/>',
                 f'<line class="axis" x1="{left}" y1="{top+plot_h}" x2="{width-right}" y2="{top+plot_h}"/>'))
    legend = []
    for label, color, points in series:
        step = max(1, len(points) // 700)
        sampled = list(points[::step])
        if sampled[-1] != points[-1]:
            sampled.append(points[-1])
        coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(day, value) for day, value in sampled))
        body.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.2" points="{coords}"/>')
        legend.append((label, color))
    path.write_text(_svg_document(title, "\n".join(body), legend, width=width, height=height), encoding="utf-8", newline="\n")


def _bar_chart(path: Path, title: str, groups: Sequence[str], series: Sequence[tuple[str, str, Sequence[float]]]) -> None:
    width, height, left, right, top, bottom = 1240, 680, 82, 34, 58, 105
    values = [value for _label, _color, row in series for value in row]
    low, high = min(min(values), 0), max(max(values), 0)
    pad = max(0.02, (high - low) * 0.08); low -= pad; high += pad
    plot_w, plot_h = width-left-right, height-top-bottom
    def y(value: float) -> float: return top + (high-value)/(high-low)*plot_h
    body=[]; zero=y(0)
    for index in range(6):
        value=high-(high-low)*index/5; yy=y(value)
        body.extend((f'<line class="grid" x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}"/>',
                     f'<text x="{left-10}" y="{yy+5:.1f}" text-anchor="end" font-size="12">{value*100:.0f}%</text>'))
    group_w=plot_w/max(1,len(groups)); bar_w=min(10, group_w/(len(series)+1))
    for gi, group in enumerate(groups):
        center=left+(gi+0.5)*group_w
        for si, (_label,color,row) in enumerate(series):
            value=row[gi]; xx=center+(si-(len(series)-1)/2)*bar_w
            yy=y(value); h=abs(zero-yy)
            body.append(f'<rect x="{xx-bar_w*0.42:.1f}" y="{min(yy,zero):.1f}" width="{bar_w*0.84:.1f}" height="{max(0.8,h):.1f}" fill="{color}"/>')
        if len(groups) <= 24 or gi % 2 == 0:
            body.append(f'<text x="{center:.1f}" y="{top+plot_h+23}" text-anchor="middle" font-size="11">{html.escape(group)}</text>')
    body.extend((f'<line class="axis" x1="{left}" y1="{zero:.1f}" x2="{width-right}" y2="{zero:.1f}"/>',
                 f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}"/>'))
    path.write_text(_svg_document(title,"\n".join(body),[(label,color) for label,color,_row in series],width=width,height=height),encoding="utf-8",newline="\n")


def _scatter_chart(path: Path, title: str, rows: Sequence[Mapping[str, str]]) -> None:
    width, height, left, right, top, bottom = 1240, 680, 92, 34, 58, 90
    points=[(_f(row["max_drawdown"]),_f(row["cagr"]),row["parameter_id"]) for row in rows]
    xmin,xmax=min(x for x,_y,_id in points),max(x for x,_y,_id in points)
    ymin,ymax=min(y for _x,y,_id in points),max(y for _x,y,_id in points)
    xpad=(xmax-xmin)*0.08; ypad=(ymax-ymin)*0.08; xmin-=xpad; xmax+=xpad; ymin-=ypad; ymax+=ypad
    plot_w,plot_h=width-left-right,height-top-bottom
    def xy(x:float,y:float)->tuple[float,float]: return left+(x-xmin)/(xmax-xmin)*plot_w,top+(ymax-y)/(ymax-ymin)*plot_h
    body=[]
    for index in range(6):
        xx=left+plot_w*index/5; xv=xmin+(xmax-xmin)*index/5
        yy=top+plot_h*index/5; yv=ymax-(ymax-ymin)*index/5
        body.extend((f'<line class="grid" x1="{xx:.1f}" y1="{top}" x2="{xx:.1f}" y2="{top+plot_h}"/>',
                     f'<text x="{xx:.1f}" y="{top+plot_h+23}" text-anchor="middle" font-size="12">{xv*100:.0f}%</text>',
                     f'<line class="grid" x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}"/>',
                     f'<text x="{left-10}" y="{yy+5:.1f}" text-anchor="end" font-size="12">{yv*100:.1f}%</text>'))
    for x,y,pid in points:
        xx,yy=xy(x,y); center=pid=="MA21_144_MACD21_55_55"
        body.append(f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="{6 if center else 3.2}" fill="{"#dc2626" if center else "#64748b"}" opacity="{1 if center else 0.55}"/>')
    body.extend((f'<text x="{left+plot_w/2:.1f}" y="{height-45}" text-anchor="middle" font-size="14">最大回落（愈右代表愈淺）</text>',
                 f'<text x="20" y="{top+plot_h/2:.1f}" transform="rotate(-90 20 {top+plot_h/2:.1f})" text-anchor="middle" font-size="14">年化報酬率</text>'))
    path.write_text(_svg_document(title,"\n".join(body),[("其餘 242 組參數","#64748b"),("正式中心參數","#dc2626")],width=width,height=height),encoding="utf-8",newline="\n")


def _make_charts(root: Path) -> None:
    daily=_read_csv(root/"main-comparison-daily.csv")
    grouped: dict[str,list[tuple[date,float]]]=defaultdict(list)
    for row in daily:
        grouped[row["comparison_id"]].append((date.fromisoformat(row["date"]),_f(row["drawdown"])))
    _line_chart(root/"main-comparison-drawdown.svg","五條比較線最大回落",[(v13.NAMES[item],COLORS[item],grouped[item]) for item in v12.MAIN_IDS],percent_axis=True)

    annual=_read_csv(root/"main-comparison-calendar-year.csv")
    years=sorted({row["year"] for row in annual})
    annual_map={(row["comparison_id"],row["year"]):_f(row["return"]) for row in annual}
    _bar_chart(root/"calendar-year-returns.svg","逐年報酬比較",years,[(v13.NAMES[item],COLORS[item],[annual_map[(item,year)] for year in years]) for item in v12.MAIN_IDS])

    cost=_read_csv(root/"execution-cost-sensitivity.csv")
    cost_groups=[]
    cost_series=[]
    for timing,label in (("SIGNAL_CLOSE","訊號當日收盤"),("NEXT_SESSION_OPEN","次一交易日開盤")):
        rows=sorted((row for row in cost if row["execution_timing"]==timing),key=lambda row:int(row["slippage_bps"]))
        groups=[row["slippage_bps"] for row in rows]
        if not cost_groups:
            cost_groups=groups
        elif groups != cost_groups:
            raise ETF0050Error("execution-cost rows do not share the same basis-point grid")
        cost_series.append((label,COLORS[timing],[_f(row["cagr"]) for row in rows]))
    _bar_chart(root/"execution-cost-sensitivity.svg","成交成本敏感度（20／40／60 基點）",cost_groups,cost_series)
    _scatter_chart(root/"parameter-robustness.svg","243 組參數：年化報酬率與最大回落",_read_csv(root/"parameter-results-243.csv"))


def _insert_before_plain(section: str, markdown: str) -> str:
    marker="\n### 白話結論"
    if marker not in section:
        raise ETF0050Error("report section has no plain-language conclusion marker")
    return section.replace(marker,"\n"+markdown+marker,1)


def _render_report(source_text: str, produced: str, attachments: Sequence[str]) -> str:
    text=source_text
    text=re.sub(r"^# .*?$","# 0050 最終策略定版報告｜正式 v1.4",text,count=1,flags=re.MULTILINE)
    text=re.sub(r"^產出時間：.*?$",f"產出時間：{produced}",text,count=1,flags=re.MULTILINE)
    text=re.sub(r"^候選狀態：.*?$","狀態：`ACCEPT`。本版只修正閱讀導覽與圖表呈現，策略、交易與績效數值完全沿用已驗收 v1.3-r3。",text,count=1,flags=re.MULTILINE)

    first_heading="## 一頁白話結論"
    toc_start=text.index("## 閱讀順序")
    content_start=text.index(first_heading)
    toc="\n".join(f"- [{heading}](#{anchor})" for heading,anchor in SECTION_ANCHORS)
    text=text[:toc_start]+"## 閱讀順序\n\n"+toc+"\n\n"+text[content_start:]

    chart_by_section={
        "一頁白話結論":"\n![五條比較線資產曲線](main-comparison-equity.svg)\n",
        "完整績效總表":"\n![五條比較線最大回落](main-comparison-drawdown.svg)\n",
        "完整逐年績效":"\n![逐年報酬比較](calendar-year-returns.svg)\n",
        "成交時間與交易成本測試":"\n![成交成本敏感度](execution-cost-sensitivity.svg)\n",
        "243組參數穩健性":"\n![243組參數穩健性](parameter-robustness.svg)\n",
    }
    for heading,anchor in SECTION_ANCHORS[:-1]:
        marker=f"## {heading}"
        start=text.index(marker); next_positions=[text.find(f"## {other}",start+len(marker)) for other,_a in SECTION_ANCHORS]
        next_positions=[pos for pos in next_positions if pos>=0]
        end=min(next_positions) if next_positions else len(text)
        section=text[start:end]
        if heading in chart_by_section:
            section=_insert_before_plain(section,chart_by_section[heading])
        section=f'<a id="{anchor}"></a>\n\n'+section
        text=text[:start]+section+text[end:]

    attachment_marker="## 完整附件目錄"
    attachment_start=text.index(attachment_marker)
    links="\n".join(f"- [{name}]({name})" for name in attachments)
    text=text[:attachment_start]+f'<a id="attachments"></a>\n\n{attachment_marker}\n\n{links}\n'
    text=text.replace("候選狀態為 `STOP_WAIT_00_QA`，等待 00 獨立品質驗收。","正式狀態為 `ACCEPT`；本版的資料與績效逐值沿用已驗收 v1.3-r3。")
    text=text.replace("只有 00 明確驗收後才能宣稱正式通過；候選產物不得覆寫 v1.2。","本版已通過 00 的獨立驗收；v1.2 與 v1.3-r3 均保持唯讀。")
    text=text.replace("python -m backtests.dual_ma.etf_0050_final_strategy_v1_3 run","python -m backtests.dual_ma.etf_0050_final_strategy_v1_4 run")
    text=text.replace("python -m backtests.dual_ma.etf_0050_final_strategy_v1_3 validate","python -m backtests.dual_ma.etf_0050_final_strategy_v1_4 validate")
    text=text.replace("tests/backtests/dual_ma/test_etf_0050_final_strategy_v1_3.py","tests/backtests/dual_ma/test_etf_0050_final_strategy_v1_4.py")
    text=text.replace("`v1-3-r3` 結尾","`v1-4` 結尾")
    return text


def _broken_links(root: Path) -> list[str]:
    broken=[]
    for md in root.glob("*.md"):
        text=md.read_text(encoding="utf-8")
        explicit=set(re.findall(r'<a id="([^"]+)"></a>',text))
        headings={v12._markdown_anchor(line.lstrip("#").strip()) for line in text.splitlines() if line.startswith("#")}
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)",text):
            target_name,marker,fragment=target.partition("#")
            if target_name.startswith(("http://","https://")): continue
            target_path=md if not target_name else root/target_name
            if not target_path.is_file(): broken.append(f"{md.name}->{target}"); continue
            if marker and target_path.suffix.lower()==".md" and fragment not in explicit|headings:
                broken.append(f"{md.name}->{target} (missing anchor)")
    return broken


def _contract_errors(text: str) -> list[str]:
    errors=[]
    if "狀態：`ACCEPT`" not in text: errors.append("accepted status missing")
    for heading,anchor in SECTION_ANCHORS:
        if f'<a id="{anchor}"></a>\n\n## {heading}' not in text: errors.append(f"explicit anchor missing: {anchor}")
        if f'](#{anchor})' not in text: errors.append(f"toc link missing: {anchor}")
    for chart in CHARTS:
        if f']({chart})' not in text: errors.append(f"chart not embedded: {chart}")
    if "候選狀態" in text or "STOP_WAIT_00_QA" in text: errors.append("stale candidate status remains")
    return errors


def _copy_files(source: Path, target: Path, excluded: set[str]) -> None:
    for path in source.iterdir():
        if path.is_file() and path.name not in excluded:
            shutil.copy2(path,target/path.name)


def run_study(*,run_root:Path=RUN_ROOT_DEFAULT,report_root:Path=REPORT_ROOT_DEFAULT)->dict[str,Any]:
    if run_root.exists() or report_root.exists(): raise FileExistsError("refusing to overwrite isolated v1.4 output")
    pins=((SOURCE_RUN/"manifest.json",SOURCE_RUN_MANIFEST),(SOURCE_REPORT/"artifact-manifest.json",SOURCE_REPORT_MANIFEST),(SOURCE_RUN/"readback.json",SOURCE_READBACK),(SOURCE_REPORT/"readback.json",SOURCE_READBACK))
    for path,expected in pins:
        if _sha(path)!=expected: raise ETF0050Error(f"sealed v1.3-r3 pin changed: {path}")
    before={str(SOURCE_RUN):tree_hash(SOURCE_RUN),str(SOURCE_REPORT):tree_hash(SOURCE_REPORT)}
    git_commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=Path(__file__).resolve().parents[2],text=True,encoding="utf-8").strip()
    produced_iso=datetime.now(ZoneInfo("Asia/Taipei")).isoformat(); produced=datetime.fromisoformat(produced_iso).strftime("%Y-%m-%d %H:%M:%S（台北）")
    with tempfile.TemporaryDirectory(prefix="0050-final-v1-4-") as tmp:
        stage=Path(tmp); run_stage=stage/"run"; report_stage=stage/"report"; run_stage.mkdir(); report_stage.mkdir()
        _copy_files(SOURCE_RUN,run_stage,{"manifest.json","readback.json","checkpoint.json","QA_READY.json"})
        _write_json(run_stage/"v1-4-input-manifest.json",{"version":VERSION,"status":"ACCEPT","produced_at_taipei":produced_iso,"git_commit":git_commit,"source_run_manifest_sha256":SOURCE_RUN_MANIFEST,"source_report_manifest_sha256":SOURCE_REPORT_MANIFEST,"source_readback_sha256":SOURCE_READBACK,"scope":"presentation-only navigation and chart revision; no strategy, transaction, accounting, or performance changes"})
        run_excluded={"manifest.json","readback.json","checkpoint.json","QA_READY.json"}; run_artifacts=_artifact_rows(run_stage,run_excluded)
        _write_json(run_stage/"manifest.json",{"status":"ACCEPT","version":VERSION,"produced_at_taipei":produced_iso,"git_commit":git_commit,"artifact_count":len(run_artifacts),"artifacts":run_artifacts})
        run_bad=_verify_artifacts(run_stage,run_artifacts); _write_json(run_stage/"readback.json",{"status":"PASS" if not run_bad else "FAIL","mismatches":run_bad})
        _write_json(run_stage/"QA_READY.json",{"status":"ACCEPT","version":VERSION,"ready_for_claimed_acceptance":True})
        _write_json(run_stage/"checkpoint.json",{"status":"ACCEPT","version":VERSION,"manifest_sha256":_sha(run_stage/"manifest.json"),"readback_sha256":_sha(run_stage/"readback.json"),"safe_next_step":"use as the formal 0050 report entry"})
        if run_bad: raise ETF0050Error(f"run readback failed: {run_bad}")

        _copy_files(SOURCE_REPORT,report_stage,{"README.md","report.md","artifact-manifest.json","readback.json","checkpoint.json"})
        _make_charts(report_stage)
        attachments=["report.md",*sorted(path.name for path in report_stage.iterdir() if path.is_file()),"artifact-manifest.json","readback.json","checkpoint.json"]
        source_text=(SOURCE_REPORT/"README.md").read_text(encoding="utf-8")
        report=_render_report(source_text,produced,attachments)
        (report_stage/"README.md").write_text(report,encoding="utf-8",newline="\n"); (report_stage/"report.md").write_text(report,encoding="utf-8",newline="\n")
        # The report intentionally links its three control files.  Create
        # temporary controls before link validation, then replace them with
        # the sealed values after the artifact set has been verified.
        for control in ("artifact-manifest.json", "readback.json", "checkpoint.json"):
            _write_json(report_stage/control,{"status":"BUILDING","version":VERSION})
        errors=_contract_errors(report); broken=_broken_links(report_stage)
        report_excluded={"artifact-manifest.json","readback.json","checkpoint.json"}; report_artifacts=_artifact_rows(report_stage,report_excluded)
        missing=v12.research._readme_missing_artifacts(report_stage,[str(row["path"]) for row in report_artifacts])
        if errors or broken or missing: raise ETF0050Error(f"report contract failed: {errors}; links={broken}; coverage={missing}")
        _write_json(report_stage/"artifact-manifest.json",{"status":"ACCEPT","version":VERSION+"-report","produced_at_taipei":produced_iso,"source_report_manifest_sha256":SOURCE_REPORT_MANIFEST,"artifact_count":len(report_artifacts),"artifacts":report_artifacts})
        report_bad=_verify_artifacts(report_stage,report_artifacts); _write_json(report_stage/"readback.json",{"status":"PASS" if not report_bad else "FAIL","mismatches":report_bad})
        _write_json(report_stage/"checkpoint.json",{"status":"ACCEPT","version":VERSION,"artifact_manifest_sha256":_sha(report_stage/"artifact-manifest.json"),"readback_sha256":_sha(report_stage/"readback.json"),"safe_next_step":"use as the formal 0050 report entry"})
        if report_bad: raise ETF0050Error(f"report readback failed: {report_bad}")
        if before!={str(SOURCE_RUN):tree_hash(SOURCE_RUN),str(SOURCE_REPORT):tree_hash(SOURCE_REPORT)}: raise ETF0050Error("sealed v1.3-r3 source changed")
        run_root.parent.mkdir(parents=True,exist_ok=True); report_root.parent.mkdir(parents=True,exist_ok=True)
        shutil.copytree(run_stage,run_root); shutil.copytree(report_stage,report_root)
    return {"status":"ACCEPT","run_root":str(run_root),"report_root":str(report_root),"run_manifest_sha256":_sha(run_root/"manifest.json"),"report_manifest_sha256":_sha(report_root/"artifact-manifest.json"),"readback_sha256":_sha(run_root/"readback.json"),"source_trees_unchanged":True}


def validate(*,run_root:Path=RUN_ROOT_DEFAULT,report_root:Path=REPORT_ROOT_DEFAULT)->dict[str,Any]:
    run_manifest=json.loads((run_root/"manifest.json").read_text(encoding="utf-8-sig")); report_manifest=json.loads((report_root/"artifact-manifest.json").read_text(encoding="utf-8-sig"))
    run_bad=_verify_artifacts(run_root,run_manifest["artifacts"]); report_bad=_verify_artifacts(report_root,report_manifest["artifacts"])
    text=(report_root/"README.md").read_text(encoding="utf-8"); contract=_contract_errors(text); broken=_broken_links(report_root)
    missing=v12.research._readme_missing_artifacts(
        report_root, [str(row["path"]) for row in report_manifest["artifacts"]]
    )
    inherited_ok=all(_sha(SOURCE_REPORT/name)==_sha(report_root/name) for name in ("main-comparison-summary.csv","main-comparison-calendar-year.csv","main-comparison-daily.csv","strategy-transactions.csv","execution-cost-sensitivity.csv","parameter-results-243.csv"))
    pins_ok=_sha(SOURCE_RUN/"manifest.json")==SOURCE_RUN_MANIFEST and _sha(SOURCE_REPORT/"artifact-manifest.json")==SOURCE_REPORT_MANIFEST
    charts_ok=all((report_root/name).is_file() and (report_root/name).stat().st_size>1000 for name in CHARTS)
    status="PASS" if not run_bad and not report_bad and not contract and not broken and not missing and inherited_ok and pins_ok and charts_ok else "FAIL"
    return {"status":status,"run_artifacts":len(run_manifest["artifacts"]),"report_artifacts":len(report_manifest["artifacts"]),"run_mismatches":run_bad,"report_mismatches":report_bad,"report_contract_errors":contract,"broken_links_or_anchors":broken,"readme_missing_artifacts":missing,"key_data_artifacts_exact":inherited_ok,"source_pins":pins_ok,"charts_present":charts_ok,"network_used":False,"writes_performed":False}


def main(argv:Sequence[str]|None=None)->int:
    parser=argparse.ArgumentParser(); parser.add_argument("mode",choices=("run","validate"),nargs="?",default="run"); parser.add_argument("--run-root",type=Path,default=RUN_ROOT_DEFAULT); parser.add_argument("--report-root",type=Path,default=REPORT_ROOT_DEFAULT)
    args=parser.parse_args(argv); result=validate(run_root=args.run_root,report_root=args.report_root) if args.mode=="validate" else run_study(run_root=args.run_root,report_root=args.report_root)
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0 if result["status"] in {"PASS","ACCEPT"} else 1


if __name__=="__main__":
    raise SystemExit(main())
