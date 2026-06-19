from __future__ import annotations

import argparse
import json
from pathlib import Path

from export_service import build_stock_export_workbook, inspect_stock_export_workbook


def main() -> None:
    parser = argparse.ArgumentParser(description="本機驗證 /export 匯出結果")
    parser.add_argument("symbol", nargs="?", default="2330", help="股票代碼，例如 2330 或 8064")
    parser.add_argument("--save", dest="save_path", help="將產出的 Excel 另存到指定路徑")
    parser.add_argument("--preview-rows", type=int, default=3, help="每張工作表預覽列數")
    args = parser.parse_args()

    buffer, file_name, display_name = build_stock_export_workbook(args.symbol)
    summary = inspect_stock_export_workbook(
        buffer,
        file_name,
        display_name,
        sample_rows=args.preview_rows,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    if args.save_path:
        target_path = Path(args.save_path)
        target_path.write_bytes(buffer.getvalue())
        print(f"已儲存匯出檔案：{target_path}")


if __name__ == "__main__":
    main()
