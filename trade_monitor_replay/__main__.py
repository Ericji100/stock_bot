from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, time
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, load_replay_config
from .runner import ReplayRunError, ReplayRunner


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必須是YYYY-MM-DD。") from exc


def _time(value: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("時間必須是HH:MM。") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TMF歷史逐K監控回放（確定性程式、MiniMax或Codex）")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--date", type=_date, default=None, help="日盤交易日，例如2026-08-27")
    parser.add_argument("--instrument", default="TMF", choices=["TMF"])
    parser.add_argument("--mode", choices=["auto", "step"], default="auto")
    parser.add_argument("--from", dest="start_time", type=_time, default=None)
    parser.add_argument("--to", dest="end_time", type=_time, default=None)
    parser.add_argument(
        "--at",
        dest="analysis_times",
        type=_time,
        action="append",
        default=None,
        metavar="HH:MM",
        help="只在指定的已收盤K時間呼叫AI；可重複使用並依時間順序回放",
    )
    parser.add_argument("--max-bars", type=int, default=None)
    parser.add_argument("--analysis-every", type=int, default=None, metavar="N", help="每N根已收盤1分K呼叫一次AI，預設2")
    parser.add_argument(
        "--event-driven",
        action="store_true",
        help="回放專用：每根K由程式更新，只在結構事件或節流後的象限變化呼叫AI",
    )
    parser.add_argument("--interval", type=float, default=0)
    parser.add_argument("--preopen-only", action="store_true")
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument(
        "--review-mode",
        choices=["supervised", "unattended"],
        default="unattended",
        help="supervised先保存已驗證訊息、逐則審核後再用--deliver-at補送；unattended可直接依通知政策傳送",
    )
    parser.add_argument(
        "--program-only",
        action="store_true",
        help="只使用確定性課程與交易引擎；不呼叫AI，適合逐K重現性驗證",
    )
    parser.add_argument("--dry-run", action="store_true", help="只驗證資料與顯示呼叫數，不呼叫AI或Telegram")
    parser.add_argument("--resume", default=None, help="接續既有run_id")
    parser.add_argument("--continue-day", action="store_true", help="讓已完成的盤前run接著跑日盤")
    parser.add_argument("--rerun-last", action="store_true", help="還原前一個checkpoint後重跑最後一根日盤K")
    parser.add_argument(
        "--rewind-to",
        type=_time,
        default=None,
        metavar="HH:MM",
        help="只回退到指定日盤分析點之前的checkpoint，不呼叫AI；可用於守門器版本遷移後局部重跑",
    )
    parser.add_argument("--batch-size", type=int, default=None, help="本次最多新增處理幾個分析點，僅供resume分批執行")
    parser.add_argument(
        "--revalidate-latest-raw",
        action="store_true",
        help="本地守門器修正後重驗目前分析點已保存的M3原始輸出，不重新呼叫AI",
    )
    parser.add_argument(
        "--reuse-saved-raw",
        action="store_true",
        help="明確回退後，依序重驗各待處理點既有的因果原始輸出；可搭配--batch-size，不重新呼叫AI",
    )
    parser.add_argument("--deliver-latest", default=None, metavar="RUN_ID", help="補送run中最新的已驗證訊息，不重跑AI")
    parser.add_argument("--deliver-at", default=None, metavar="RUN_ID", help="補送run中指定時間的已驗證訊息，不重跑AI")
    parser.add_argument("--delivery-time", type=_time, default=None, metavar="HH:MM", help="搭配--deliver-at指定日盤訊息時間")
    parser.add_argument("--telegram-test", action="store_true", help="向模擬群組發送一次可去重的純連線測試")
    return parser


async def _run(args: argparse.Namespace) -> dict[str, object]:
    config = load_replay_config(args.config)
    if args.instrument != config.instrument:
        raise ReplayRunError("instrument_mismatch", "命令列商品與回放設定不一致。")
    runner = ReplayRunner(config)
    if args.telegram_test:
        if args.deliver_latest or args.deliver_at or args.resume or args.dry_run or args.date is not None:
            raise ReplayRunError("telegram_test_arguments_invalid", "--telegram-test不能與其他執行模式並用。")
        return await runner.test_telegram()
    if args.deliver_at:
        if args.deliver_latest or args.resume or args.dry_run or args.date is not None or args.delivery_time is None:
            raise ReplayRunError("delivery_arguments_invalid", "--deliver-at必須單獨搭配--delivery-time使用。")
        return await runner.deliver_at(args.deliver_at, at_time=args.delivery_time)
    if args.deliver_latest:
        if args.resume or args.dry_run or args.date is not None or args.delivery_time is not None:
            raise ReplayRunError("delivery_arguments_invalid", "--deliver-latest不能與--resume、--dry-run或--date並用。")
        return await runner.deliver_latest(args.deliver_latest)
    if args.delivery_time is not None:
        raise ReplayRunError("delivery_time_requires_deliver_at", "--delivery-time必須搭配--deliver-at。")
    if args.resume:
        if args.dry_run:
            raise ReplayRunError("resume_dry_run_invalid", "resume不能與dry-run並用。")
        if args.analysis_times:
            raise ReplayRunError("resume_analysis_times_invalid", "既有run不能變更--at時間清單。")
        if args.start_time is not None:
            raise ReplayRunError("resume_start_time_invalid", "既有run不能變更--from；延長終點請使用--to。")
        if args.event_driven:
            raise ReplayRunError("resume_event_driven_invalid", "既有run是否為事件驅動已由manifest鎖定。")
        if args.program_only:
            raise ReplayRunError("resume_program_only_locked", "既有run的分析模式已由manifest鎖定；resume時不要再指定--program-only。")
        return await runner.resume(
            args.resume,
            continue_day=args.continue_day,
            rerun_last=args.rerun_last,
            rewind_to=args.rewind_to,
            max_bars=args.max_bars,
            analysis_every_bars=args.analysis_every,
            end_time=args.end_time,
            batch_size=args.batch_size,
            telegram=True if args.telegram else None,
            revalidate_latest_raw=args.revalidate_latest_raw,
            reuse_saved_raw=args.reuse_saved_raw,
        )
    if args.continue_day or args.rerun_last or args.rewind_to is not None or args.revalidate_latest_raw or args.reuse_saved_raw or args.batch_size is not None:
        raise ReplayRunError(
            "resume_option_requires_resume",
            "--continue-day、--rerun-last、--rewind-to、--batch-size、--revalidate-latest-raw及--reuse-saved-raw必須搭配--resume。",
        )
    if args.date is None:
        raise ReplayRunError("date_missing", "請提供--date YYYY-MM-DD。")
    if args.analysis_times and any(
        value is not None
        for value in (args.start_time, args.end_time, args.max_bars, args.analysis_every)
    ):
        raise ReplayRunError(
            "analysis_times_conflict",
            "--at不能與--from、--to、--max-bars或--analysis-every並用。",
        )
    if args.event_driven and (args.analysis_times or args.analysis_every is not None):
        raise ReplayRunError(
            "event_driven_conflict",
            "--event-driven不能與--at或--analysis-every並用。",
        )
    if args.dry_run:
        return runner.data_plan(
            target_date=args.date,
            event_driven=args.event_driven,
            start_time=args.start_time,
            end_time=args.end_time,
            analysis_times=args.analysis_times,
            max_bars=args.max_bars,
            analysis_every_bars=args.analysis_every,
            program_only=args.program_only,
        )
    return await runner.run(
        target_date=args.date,
        mode=args.mode,
        start_time=args.start_time,
        end_time=args.end_time,
        analysis_times=args.analysis_times,
        max_bars=args.max_bars,
        analysis_every_bars=args.analysis_every,
        event_driven=args.event_driven,
        preopen_only=args.preopen_only,
        telegram=args.telegram,
        interval_seconds=args.interval,
        program_only=args.program_only,
        review_mode=args.review_mode,
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        payload = {
            "ok": False,
            "status": "failed",
            "error_code": getattr(exc, "code", "replay_failed"),
            "error": " ".join(str(exc).split())[:800],
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
