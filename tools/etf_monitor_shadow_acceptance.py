"""Read-only CLI for the finite 0050 shadow observation acceptance."""
import argparse
import json
from datetime import date

from stock_ai_bot.monitoring.etf_monitor.acceptance import inspect_day


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    result = inspect_day(args.day)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
