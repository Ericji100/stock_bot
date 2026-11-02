"""Explicit, paid official-API smoke tests. Captures messages without sending Telegram."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_center.config import load_research_config
from research_center.orchestrator import ResearchCenter
from stock_ai_bot.monitoring.radar_service import run_radar, format_radar_report

COMMANDS = {
    "research": "/research 2330 --deep",
    "value_scan": "/value_scan 2330 --deep",
    "macro": "/macro 台股",
    "theme": "/theme AI電源",
    "theme_flow": "/theme_flow AI電源",
    "theme_radar": "/theme_radar",
    "sector_strength": "/sector_strength",
    "radar": "/radar --ai-top 3",
    "news": "/news refresh",
    "topic_maintain": "/topic_maintain --deep",
}


def main(argv=None):
    parser = argparse.ArgumentParser(description="官方 DeepSeek 真實測試；會消耗搜尋與模型額度，不發 Telegram、不套用題材變更。")
    parser.add_argument("--case", choices=list(COMMANDS), default="research")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--check-config", action="store_true", help="只檢查設定，不連線、不付費。")
    args = parser.parse_args(argv)
    config = load_research_config()
    if not config.deepseek_only or not config.deepseek_api_key:
        print("尚未設定官方 DeepSeek 金鑰或未啟用遷移模式；未開始任何付費請求。")
        return 2
    if args.check_config:
        print(json.dumps({"provider": "deepseek", "model": config.deepseek_model, "key_configured": True}, ensure_ascii=False))
        return 0
    # No Bot/Telegram client is created. Ordinary command paths perform real collection and analysis.
    center = ResearchCenter(config)
    records = []
    run_dir = ROOT / "logs" / "deepseek_live_validation" / (datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        center.deepseek.ensure_ready()
        for name in COMMANDS if args.all else [args.case]:
            command = COMMANDS[name]
            case_dir = run_dir / name
            case_dir.mkdir()
            messages = []
            def progress(message):
                messages.append(str(message))
                print(message, flush=True)
            started = time.monotonic()
            record = {"command": command, "manual_quality_review": "pending", "telegram_sent": False}
            try:
                if name == "radar":
                    result = run_radar(command.split()[1:], progress=progress)
                    text = format_radar_report(result, limit=50)
                    selected = [c for c in result.candidates if c.code in result.ai_enriched_codes]
                    formal = bool(selected) and all(c.ai_comment.get("status") == "ok" for c in selected)
                    record.update({"formal_ai_success": formal, "status": "success" if formal else "partial_success"})
                    (case_dir / "result.json").write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                else:
                    result = center.run_text_command(command, progress=progress)
                    text = result.markdown or result.summary
                    meta = result.report_json.get("metadata") or {}
                    formal = result.status == "success" and result.ai_used and not result.fallback_reason and meta.get("ai_status", "ai_success") == "ai_success"
                    record.update({"status": result.status, "formal_ai_success": formal,
                        "fallback_reason": result.fallback_reason, "model": result.ai_model,
                        "artifact_paths": asdict(result.artifacts), "source_count": len(result.sources)})
                    (case_dir / "result.json").write_text(json.dumps(result.report_json, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                (case_dir / "output.md").write_text(text, encoding="utf-8")
            except Exception as exc:
                record.update({"status": "failed", "formal_ai_success": False, "error_type": type(exc).__name__})
            finally:
                record["elapsed_seconds"] = round(time.monotonic() - started, 2)
                (case_dir / "progress.log").write_text("\n".join(messages), encoding="utf-8")
                records.append(record)
                (run_dir / "manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            if not record.get("formal_ai_success"):
                break
    finally:
        center.database.close()
    print(f"驗證資料：{run_dir}；正式報告品質仍需人工檢閱，不以產檔成功代替品質驗收。")
    expected = 10 if args.all else 1
    return 0 if len(records) == expected and all(r.get("formal_ai_success") for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
