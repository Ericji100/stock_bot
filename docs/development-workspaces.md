# 開發工作區分流

本文件定義正式 `main` 與三條開發線的權責。`main` 只接受完成驗證、可部署的功能；研究中的程式、測試與文件留在各自分支，不能以整條長期分支直接覆蓋 `main`。

## 工作區

| 用途 | 分支 | 本機 worktree | 資料 profile |
|---|---|---|---|
| 正式 Bot | `main` | `D:\code\stock_ai_bot` | `.workdata/prod` |
| 股票監控策略 | `codex/stock-monitor-strategy` | `D:\code\_codex_worktrees\stock-monitor-strategy\stock_ai_bot` | `.workdata/dev/stock-monitor` |
| 台指期一分 K 監控 | `codex/tmf-1m-monitor` | `D:\code\_codex_worktrees\tmf-1m-monitor\stock_ai_bot` | `.workdata/dev/tmf-monitor` |
| 雙均線研究 | `codex/dual-ma-integration-v0.4.1` | `D:\code\_codex_worktrees\b974\stock_ai_bot` | `.workdata/dev/dual-ma` |

## Main 邊界

`main` 保留正式 Bot、正式股票功能、已上線的 Telegram bridge、目前正式台指期監控、必要測試、維運工具與文件。

股票課程回測、Enlightenment AI、formal AI replay、hybrid v2/v3/v4 與 v2 core 研究家族已由 `codex/stock-monitor-strategy` 保全，並從 `main` 移除。研究輸出與 checkpoint 留在 `stock-monitor` profile。

`trade_monitor_replay/`、對應測試與歷史規則目前仍是凍結相容資產。由於舊 replay schema 雜湊契約尚待修復，暫不從 `main` 搬動；所有新開發只能在 `codex/tmf-1m-monitor` 進行。契約修復完成後，再依版本索引封存舊規則及移除根目錄相容層。

## 同步規則

- 共用基礎修正以獨立 commit cherry-pick 到需要的開發分支。
- `main` 的研究檔刪除 commit 不合併到長期開發分支，避免刪掉已保全成果。
- 開發功能完成時，從最新 `main` 建立短期 release branch，只 cherry-pick 經驗證的正式 commit。
- 不把整條長期研究分支直接 merge 回 `main`。
- `.workdata/` 不進 Git，也不隨 worktree 同步；各 profile 保持寫入隔離。

## 完成功能回主線

1. 在開發 worktree 整理最小正式變更，排除報表、checkpoint、實驗設定及臨時腳本。
2. 執行該功能的 focused tests，記錄已知基線失敗。
3. 從最新 `main` 建立 release branch。
4. cherry-pick 正式 commit，處理與目前正式介面的差異。
5. 執行完整回歸與啟動驗證後才合併 `main`。
6. 合併後再通知其他開發線同步必要的共用 commit。
